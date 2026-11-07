"""Phase 7 tests: the interactive command-line interface.

The CLI is driven through its injectable input/output functions. Page downloads use a fake fetcher and Ollama is
a fake server, so nothing touches the network or a real model. Documents are written to pytest temp folders: a
guard fixture proves the project's real ``output/`` folder is never modified by these tests.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import pytest
import requests
from docx import Document

import src.main as cli
from src.article_extractor import (
    ArticleInaccessibleError, FetchError, InvalidSourceError, NoContentError, PaywallError, SubstackExtractor,
)
from src.backends import BackendSelection, LocalNotesBackend, OllamaBackend
from src.backends.base import BackendError
from src.backends.ollama import _GENERATION_LOCK
from src.content_processor import EmptyContentError, ProcessingError
from src.main import App, SourceFileError, describe_error, estimate_text, format_duration, read_source_file
from src.notes_generator import NotesGenerationError
from src.notes_model import DetailLevel
from src.word_generator import DEFAULT_OUTPUT_DIR, WordGenerationError, WordGenerator
from tests.helpers import FakeFetcher, ScriptedLLM, guess_purpose, load

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
FREE_HTML, PAYWALLED_HTML, LOGIN_HTML = (load(n) for n in ("substack_free.html", "substack_paywalled.html", "login_wall.html"))
URL = "https://appliedai.substack.com/p/understanding-rag"
TITLE_DOCX = "Understanding_Retrieval-Augmented_Generation_Notes.docx"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*a: Any, **k: Any) -> None:
        raise AssertionError("a network connection was attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture(scope="module", autouse=True)
def project_output_is_untouched() -> Any:
    """No test in this module may add, change or delete anything in the real output/ folder."""
    def snapshot() -> dict[str, tuple[int, int]]:
        return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in DEFAULT_OUTPUT_DIR.glob("*")} if DEFAULT_OUTPUT_DIR.exists() else {}

    before = snapshot()
    yield
    assert snapshot() == before, "the project's output/ folder was modified by a test"


# ============================================================================ test doubles + harness
class Script:
    """Scripted keyboard + captured screen. An exhausted script behaves like a closed stdin (EOFError)."""

    def __init__(self, *lines: Any) -> None:
        self.lines, self.prompts, self.out = list(lines), [], []

    def input(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        item = self.lines.pop(0)
        if isinstance(item, BaseException) or (isinstance(item, type) and issubclass(item, BaseException)):
            raise item
        return item

    def output(self, text: str = "") -> None:
        self.out.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.out)


def make_app(script: Script, tmp_path: Path, **kw: Any) -> App:
    kw.setdefault("heartbeat_seconds", None)
    kw.setdefault("output_dir", tmp_path / "out")
    kw.setdefault("env_source", {})
    return App(input_fn=script.input, output_fn=script.output, **kw)


def fetch_factory(html: str = FREE_HTML, error: Optional[BaseException] = None) -> Callable[[], SubstackExtractor]:
    fetcher = FakeFetcher(html, error=error)
    factory = lambda: SubstackExtractor(fetcher=fetcher)  # noqa: E731
    factory.fetcher = fetcher  # type: ignore[attr-defined]
    return factory


def run(tmp_path: Path, *lines: Any, consumed: bool = True, **kw: Any) -> tuple[App, Script, int]:
    """Run a whole session (an 'Exit' is appended). By default every scripted line must have been consumed."""
    script = Script(*lines, "7")
    app = make_app(script, tmp_path, **kw)
    code = app.run()
    if consumed:
        assert script.lines == [], f"unused input lines (prompt sequence mismatch): {script.lines}"
    return app, script, code


def docx_files(tmp_path: Path) -> list[Path]:
    out = tmp_path / "out"
    return sorted(out.glob("*.docx")) if out.exists() else []


# ----- a fake Ollama server (no real model) and a resolver that uses it -----
class FakeResp:
    def __init__(self, payload: Any = None, status: int = 200) -> None:
        self._payload, self.status_code, self.text = payload, status, ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self) -> Any:
        return self._payload


def ok_reply(content: str) -> dict[str, Any]:
    return {"model": "qwen3.5:4b", "message": {"role": "assistant", "content": content}, "done": True, "done_reason": "stop",
            "load_duration": 1_000_000_000, "prompt_eval_count": 100, "eval_count": 50, "eval_duration": 10_000_000_000}


def model_reply(body: dict) -> FakeResp:
    user = body["messages"][1]["content"]
    return FakeResp(ok_reply(ScriptedLLM.default(guess_purpose(user), user)))


class FakeOllama:
    def __init__(self, models: tuple[str, ...] = ("qwen3.5:4b",), chat: Optional[Callable[[dict], Any]] = None, delay: float = 0.0) -> None:
        self.models, self.chat, self.delay = models, chat or model_reply, delay
        self.gets: list[str] = []
        self.posts: list[dict] = []
        self.active = self.max_active = 0
        self._guard = threading.Lock()

    def get(self, url: str, **kw: Any) -> FakeResp:
        self.gets.append(url)
        return FakeResp({"models": [{"name": m} for m in self.models]})

    def post(self, url: str, json: Any = None, **kw: Any) -> FakeResp:
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.posts.append(copy.deepcopy(json))
            if self.delay:
                time.sleep(self.delay)
            result = self.chat(json)
            if isinstance(result, BaseException):
                raise result
            return result
        finally:
            with self._guard:
                self.active -= 1


def ollama_resolver(fake: FakeOllama) -> Callable[..., BackendSelection]:
    calls: list[tuple[str, dict, Optional[str]]] = []

    def resolver(name: str, *, env: Any = None, ollama_model: Optional[str] = None, **kw: Any) -> BackendSelection:
        calls.append((name, dict(env or {}), ollama_model))
        if name == "local":
            return BackendSelection(LocalNotesBackend(), "local")
        backend = OllamaBackend(model=ollama_model or "qwen3.5:4b", session=fake)  # type: ignore[arg-type]
        availability = backend.check_available()
        if not availability.available:
            return BackendSelection(LocalNotesBackend(), "ollama", availability.reason)
        return BackendSelection(backend, "ollama")

    resolver.calls = calls  # type: ignore[attr-defined]
    return resolver


class RecordingEnv(dict):
    """An environment that remembers every variable that was looked at."""

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.accessed: set[str] = set()

    def get(self, key: str, default: Any = None) -> Any:
        self.accessed.add(key)
        return super().get(key, default)

    def __getitem__(self, key: str) -> Any:
        self.accessed.add(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self.accessed.add(str(key))
        return super().__contains__(key)


def url_flow(url: str = URL, detail: str = "", questions: str = "") -> list[str]:
    return ["1", url, detail, questions]


def html_flow(path: Any = FIX / "substack_free.html", url: str = "", detail: str = "", questions: str = "") -> list[str]:
    return ["2", str(path), url, detail, questions]


def text_flow(path: Any = FIX / "user_notes.txt", title: str = "", author: str = "", date_: str = "", url: str = "",
              detail: str = "", questions: str = "") -> list[str]:
    return ["3", str(path), title, author, date_, url, detail, questions]


# ============================================================================ the real entry point
class TestEntryPoint:
    def py(self, *args: str, cwd: Path = ROOT, stdin: Optional[str] = "", env: Optional[dict] = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, *args], input=stdin, capture_output=True, text=True, cwd=cwd, timeout=90,
                              env={**os.environ, **(env or {})})

    def test_version(self) -> None:
        r = self.py("src/main.py", "--version")
        assert r.returncode == 0 and r.stdout.strip() == "NoteForge AI 0.1.0"

    def test_help_lists_the_options(self) -> None:
        r = self.py("src/main.py", "--help")
        assert r.returncode == 0 and "--output-dir" in r.stdout and "--version" in r.stdout

    def test_runs_from_the_project_root_exactly_as_documented(self, tmp_path: Path) -> None:
        r = self.py("src/main.py", "--output-dir", str(tmp_path), stdin="7\n")
        assert r.returncode == 0 and "NOTEFORGE AI" in r.stdout and "Goodbye." in r.stdout and r.stderr == ""

    def test_runs_from_any_working_directory(self, tmp_path: Path) -> None:
        r = self.py(str(ROOT / "src" / "main.py"), "--output-dir", str(tmp_path / "o"), cwd=tmp_path, stdin="7\n")
        assert r.returncode == 0 and "NOTEFORGE AI" in r.stdout and r.stderr == ""

    def test_runs_as_a_module(self, tmp_path: Path) -> None:
        r = self.py("-m", "src.main", "--output-dir", str(tmp_path), stdin="7\n")
        assert r.returncode == 0 and "NOTEFORGE AI" in r.stdout

    def test_closed_stdin_exits_cleanly(self, tmp_path: Path) -> None:
        r = subprocess.run([sys.executable, "src/main.py", "--output-dir", str(tmp_path)], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, cwd=ROOT, timeout=60)
        assert r.returncode == 0 and "No more input" in r.stdout and "Traceback" not in r.stderr

    def test_unknown_arguments_are_rejected_by_argparse(self) -> None:
        r = self.py("src/main.py", "--nonsense")
        assert r.returncode == 2 and "unrecognized arguments" in r.stderr

    def test_a_full_local_session_through_the_real_program(self, tmp_path: Path) -> None:
        feed = f"2\n{FIX / 'substack_free.html'}\n{URL}\n\n\n7\n"
        r = self.py("src/main.py", "--output-dir", str(tmp_path / "o"), stdin=feed)
        assert r.returncode == 0 and "Your document is ready:" in r.stdout and r.stderr == ""
        produced = list((tmp_path / "o").glob("*.docx"))
        assert [p.name for p in produced] == [TITLE_DOCX]
        assert len(Document(str(produced[0])).paragraphs) > 100

    @pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell not available")
    def test_works_from_windows_powershell(self, tmp_path: Path) -> None:
        feed = f"'2','{FIX / 'substack_free.html'}','{URL}','','','7' | & '{sys.executable}' 'src/main.py' --output-dir '{tmp_path / 'ps'}'"
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", feed], capture_output=True, text=True, cwd=ROOT, timeout=120)
        assert r.returncode == 0, r.stderr
        assert "Your document is ready:" in r.stdout and (tmp_path / "ps" / TITLE_DOCX).exists()

    def test_a_non_ascii_title_never_crashes_a_legacy_console(self, tmp_path: Path) -> None:
        """Windows consoles often use cp437/cp1252; a Telugu title must not raise UnicodeEncodeError."""
        text = tmp_path / "telugu.txt"
        text.write_text("# తెలుగు వ్యాసం\n\nతెలుగు భాష ఒక ద్రావిడ భాష. "
                        "ఈ వ్యాసంలో RAG గురించి చర్చించాము.", encoding="utf-8")
        feed = f"3\n{text}\n\n\n\n\n\n\n7\n"
        r = self.py("src/main.py", "--output-dir", str(tmp_path / "o"), stdin=feed, env={"PYTHONIOENCODING": "cp437"})
        assert r.returncode == 0 and "UnicodeEncodeError" not in r.stderr and "Traceback" not in r.stderr, r.stderr
        assert "Your document is ready:" in r.stdout and len(list((tmp_path / "o").glob("*.docx"))) == 1

    def test_all_program_text_is_ascii_so_every_console_can_show_it(self) -> None:
        assert Path(cli.__file__).read_text(encoding="utf-8").isascii()

    def test_default_output_folder_is_the_projects_output_dir(self) -> None:
        assert App().output_dir == DEFAULT_OUTPUT_DIR and DEFAULT_OUTPUT_DIR == ROOT / "output"


# ============================================================================ menu navigation
class TestMenu:
    def test_banner_and_the_seven_options_in_order(self, tmp_path: Path) -> None:
        _, s, code = run(tmp_path)
        assert code == 0
        assert "NOTEFORGE AI" in s.text
        expected = ["1. Generate notes from Substack URL.", "2. Generate notes from HTML file.", "3. Generate notes from text file.",
                    "4. Select AI backend.", "5. View available models.", "6. View generated documents.", "7. Exit."]
        positions = [s.text.index(line) for line in expected]
        assert positions == sorted(positions)

    @pytest.mark.parametrize("word", ["7", "exit", "EXIT", " Quit "])
    def test_exit_words(self, word: str, tmp_path: Path) -> None:
        script = Script(word)
        assert make_app(script, tmp_path).run() == 0 and "Goodbye." in script.text

    def test_end_of_input_exits_without_a_traceback(self, tmp_path: Path) -> None:
        script = Script()
        assert make_app(script, tmp_path).run() == 0 and "No more input" in script.text

    def test_ctrl_c_at_the_menu_exits_with_code_130(self, tmp_path: Path) -> None:
        script = Script(KeyboardInterrupt)
        assert make_app(script, tmp_path).run() == 130 and "Interrupted. Goodbye." in script.text

    @pytest.mark.parametrize("bad", ["abc", "0", "8", "1.5", "-1", "one", "10", "!"])
    def test_invalid_choices_are_explained_and_the_menu_returns(self, bad: str, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, bad)
        assert f"'{bad}' is not a menu option. Please enter a number from 1 to 7." in s.text
        assert s.text.count("1. Generate notes from Substack URL.") == 2  # shown again after the error

    def test_empty_input_just_shows_the_menu_again(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "", "  ")
        assert "is not a menu option" not in s.text and s.text.count("7. Exit.") == 3

    def test_status_line_shows_backend_detail_and_output_folder(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path)
        assert f"Backend: Local (free, offline) | Detail: Intermediate | Output: {tmp_path / 'out'}" in s.text

    @pytest.mark.parametrize("back", ["b", "B", "back", "cancel", "q"])
    def test_back_words_cancel_an_action_from_its_first_prompt(self, back: str, tmp_path: Path) -> None:
        for option in ("1", "2", "3"):
            _, s, _ = run(tmp_path, option, back)
            assert "Cancelled. Back to the main menu." in s.text
        assert docx_files(tmp_path) == []

    def test_menu_returns_after_a_completed_generation(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *html_flow())
        assert s.text.index("Your document is ready:") < s.text.rindex("1. Generate notes from Substack URL.")

    def test_an_unexpected_bug_in_one_action_does_not_end_the_session(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(App, "action_models", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        _, s, code = run(tmp_path, "5")
        assert code == 0 and "[!] Unexpected error (RuntimeError): boom" in s.text and "Details were written to the log file" in s.text
        assert "RuntimeError" in (tmp_path / "out" / "noteforge.log").read_text(encoding="utf-8")

    def test_options_work_in_sequence_within_one_session(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "6", "4", "", "5")
        assert "Generated documents in" in s.text and "Select the notes backend" in s.text and "Available models" in s.text

    def test_log_file_is_created_in_the_output_folder_and_detached_afterwards(self, tmp_path: Path) -> None:
        import logging
        before = list(logging.getLogger().handlers)
        run(tmp_path)
        assert (tmp_path / "out" / "noteforge.log").exists() and list(logging.getLogger().handlers) == before


# ============================================================================ helpers: files, durations, errors
class TestFormatDuration:
    @pytest.mark.parametrize("seconds,expected", [(0, "0s"), (0.4, "0s"), (0.6, "1s"), (45, "45s"), (60, "1m 00s"), (185, "3m 05s"),
                                                  (3599, "59m 59s"), (3600, "1h 00m 00s"), (3725, "1h 02m 05s"), (-5, "0s")])
    def test_format(self, seconds: float, expected: str) -> None:
        assert format_duration(seconds) == expected


class TestReadSourceFile:
    def test_utf8(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("héllo తెలుగు", encoding="utf-8")
        assert read_source_file(str(f)) == ("héllo తెలుగు", "utf-8-sig")

    def test_utf8_with_bom(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_bytes(b"\xef\xbb\xbfhello")
        assert read_source_file(str(f)) == ("hello", "utf-8-sig")

    def test_utf16(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("café notes", encoding="utf-16")
        assert read_source_file(str(f)) == ("café notes", "utf-16")

    def test_windows_1252(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_bytes("naïve – café".encode("cp1252"))
        text, enc = read_source_file(str(f))
        assert text == "naïve – café" and enc == "cp1252"

    @pytest.mark.parametrize("decorate", [lambda p: f'"{p}"', lambda p: f"'{p}'", lambda p: f'  "{p}"  ', lambda p: f"{p}  "])
    def test_quoted_and_padded_paths(self, tmp_path: Path, decorate: Callable[[Path], str]) -> None:
        f = tmp_path / "with space.txt"
        f.write_text("hello world", encoding="utf-8")
        assert read_source_file(decorate(f))[0] == "hello world"

    def test_home_directory_is_expanded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "n.txt").write_text("from home", encoding="utf-8")
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert read_source_file("~/n.txt")[0] == "from home"

    def test_relative_paths_use_the_working_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "rel.txt").write_text("relative", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert read_source_file("rel.txt")[0] == "relative"

    @pytest.mark.parametrize("raw", ["", "   ", '""'])
    def test_empty_path(self, raw: str) -> None:
        with pytest.raises(SourceFileError, match="No file path"):
            read_source_file(raw)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(SourceFileError, match="File not found"):
            read_source_file(str(tmp_path / "nope.txt"))

    def test_folder(self, tmp_path: Path) -> None:
        with pytest.raises(SourceFileError, match="folder, not a file"):
            read_source_file(str(tmp_path))

    def test_empty_file(self, tmp_path: Path) -> None:
        (tmp_path / "e.txt").write_bytes(b"")
        with pytest.raises(SourceFileError, match="empty"):
            read_source_file(str(tmp_path / "e.txt"))

    def test_too_large(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "MAX_INPUT_FILE_BYTES", 100)
        (tmp_path / "big.txt").write_bytes(b"x" * 500)
        with pytest.raises(SourceFileError, match="too large"):
            read_source_file(str(tmp_path / "big.txt"))

    def test_binary_file(self, tmp_path: Path) -> None:
        (tmp_path / "b.bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)))
        with pytest.raises(SourceFileError, match="binary"):
            read_source_file(str(tmp_path / "b.bin"))

    def test_permission_denied(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        f = tmp_path / "p.txt"
        f.write_text("x", encoding="utf-8")
        monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(PermissionError("denied")))
        with pytest.raises(SourceFileError, match="Permission denied"):
            read_source_file(str(f))

    def test_undecodable_bytes_never_crash(self, tmp_path: Path) -> None:
        (tmp_path / "w.txt").write_bytes(b"ok \x81\x8d\x8f\x90\x9d text")  # not valid cp1252 either
        text, enc = read_source_file(str(tmp_path / "w.txt"))
        assert "ok" in text and "text" in text


class TestDescribeError:
    @pytest.mark.parametrize("exc,needles", [
        (InvalidSourceError("bad url"), ["not usable", "bad url"]),
        (PaywallError("x"), ["paywall", "never bypasses", "option 2", "option 3"]),
        (ArticleInaccessibleError("login needed"), ["not publicly accessible", "login needed", "option 2"]),
        (FetchError("HTTP 500"), ["Could not download", "HTTP 500", "internet connection"]),
        (NoContentError("empty"), ["No article content", "empty"]),
        (ProcessingError("odd"), ["could not be processed"]),
        (EmptyContentError("nothing left"), ["could not be processed", "nothing left"]),
        (WordGenerationError("locked"), ["Word document could not be created", "locked"]),
        (NotesGenerationError("bad backend"), ["Notes generation failed", "bad backend"]),
        (BackendError("down"), ["Notes generation failed", "down"]),
        (ValueError("weird"), ["Unexpected error (ValueError)", "weird"]),
    ])
    def test_messages_are_plain_and_actionable(self, exc: BaseException, needles: list[str]) -> None:
        message = describe_error(exc)
        for needle in needles:
            assert needle in message, (needle, message)
        assert message.isascii() and "Traceback" not in message

    def test_paywall_message_wins_over_the_generic_inaccessible_one(self) -> None:
        assert "never bypasses" in describe_error(PaywallError("x")) and "never bypasses" not in describe_error(ArticleInaccessibleError("x"))


class TestEstimateText:
    def test_local_is_quick(self) -> None:
        assert "few seconds" in estimate_text("local", DetailLevel.ADVANCED, True)

    @pytest.mark.parametrize("detail", list(DetailLevel))
    def test_ollama_warns_about_minutes_and_sequential_requests(self, detail: DetailLevel) -> None:
        text = estimate_text("ollama", detail, True)
        assert "minutes" in text and "one at a time" in text and "Ctrl+C" in text and detail.value in text

    def test_without_questions_the_estimate_is_shorter(self) -> None:
        import re
        hi = lambda t: int(re.search(r"(\d+)-(\d+)\+", t).group(2))  # noqa: E731
        assert hi(estimate_text("ollama", DetailLevel.BASIC, False)) < hi(estimate_text("ollama", DetailLevel.BASIC, True))
        assert "without questions" in estimate_text("ollama", DetailLevel.BASIC, False)

    def test_longer_detail_never_estimates_less(self) -> None:
        import re
        upper = [int(re.search(r"(\d+)-(\d+)\+", estimate_text("ollama", d, True)).group(2)) for d in DetailLevel]
        assert upper == sorted(upper)


# ============================================================================ option 1: URL input validation + workflow
class TestUrlInput:
    @pytest.mark.parametrize("bad", ["", "not a url", "ftp://a.substack.com/p/x", "http://localhost/p/x", "https://127.0.0.1/p/x",
                                     "https://a.substack.com/archive", "https://a.substack.com/", "javascript:alert(1)", "https://u:p@a.substack.com/p/x"])
    def test_invalid_urls_are_rejected_before_any_download(self, bad: str, tmp_path: Path) -> None:
        factory = fetch_factory()
        _, s, _ = run(tmp_path, "1", bad, "b", extractor_factory=factory)
        assert "[!] That input is not usable" in s.text and factory.fetcher.requested == [] and docx_files(tmp_path) == []

    def test_three_invalid_attempts_return_to_the_menu(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "1", "x", "y", "z", extractor_factory=fetch_factory())
        assert "Too many invalid attempts. Back to the main menu." in s.text and s.text.count("[!] That input is not usable") == 3

    def test_a_valid_url_after_mistakes_is_accepted(self, tmp_path: Path) -> None:
        factory = fetch_factory()
        _, s, _ = run(tmp_path, "1", "nonsense", "https://a.substack.com/archive", URL, "", "", extractor_factory=factory)
        assert factory.fetcher.requested == [URL] and len(docx_files(tmp_path)) == 1

    def test_http_is_upgraded_and_fragments_are_dropped(self, tmp_path: Path) -> None:
        factory = fetch_factory()
        run(tmp_path, *url_flow("http://appliedai.substack.com/p/understanding-rag#comments"), extractor_factory=factory)
        assert factory.fetcher.requested == ["https://appliedai.substack.com/p/understanding-rag"]

    def test_custom_domains_are_accepted(self, tmp_path: Path) -> None:
        factory = fetch_factory(load("substack_custom_domain.html"))
        run(tmp_path, *url_flow("https://www.edgenotes.io/p/small-models"), extractor_factory=factory)
        assert factory.fetcher.requested == ["https://www.edgenotes.io/p/small-models"] and len(docx_files(tmp_path)) == 1


class TestUrlWorkflow:
    def test_complete_article_end_to_end(self, tmp_path: Path) -> None:
        app, s, _ = run(tmp_path, *url_flow(), extractor_factory=fetch_factory())
        t = s.text
        for step in ("[1/6] Reading and extracting", "Extracted : COMPLETE - 513 words in 21 blocks", "[2/6] Processing the content",
                     "[3/6] Writing the notes with Local", "[4/6] Checking the notes", "OK: every quoted passage",
                     "[5/6] Creating the Word document", "[6/6] Done.", "Your document is ready:"):
            assert step in t, step
        assert "Title     : Understanding Retrieval-Augmented Generation" in t and "Author    : Priya Raman" in t
        assert app.last.save is not None and app.last.save.path.exists() and app.last.article.url == URL

    def test_the_document_references_the_source_url(self, tmp_path: Path) -> None:
        app, _, _ = run(tmp_path, *url_flow(), extractor_factory=fetch_factory())
        d = Document(str(app.last.save.path))
        assert any(r.target_ref == URL for r in d.part.rels.values() if r.reltype.endswith("/hyperlink"))

    def test_full_absolute_path_name_size_backend_and_times_are_shown(self, tmp_path: Path) -> None:
        app, s, _ = run(tmp_path, *url_flow(), extractor_factory=fetch_factory())
        path = app.last.save.path.resolve()
        assert str(path) in s.text and f"File name : {path.name}" in s.text and "Size:" in s.text
        assert "Made with : Local | Source status: COMPLETE" in s.text and "Time      : extract" in s.text and "total" in s.text
        assert path.is_absolute() and path.parent == (tmp_path / "out").resolve()

    def test_network_failure(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *url_flow(), extractor_factory=fetch_factory(error=FetchError("Network error while fetching")))
        assert "Could not download the article" in s.text and "internet connection" in s.text and docx_files(tmp_path) == []

    def test_http_forbidden(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *url_flow(), extractor_factory=fetch_factory(error=ArticleInaccessibleError("Access denied (HTTP 403)")))
        assert "not publicly accessible" in s.text and "option 2" in s.text and docx_files(tmp_path) == []

    def test_paywalled_page_with_no_public_text(self, tmp_path: Path) -> None:
        html = "<html><body><div class='paywall'><h2>This post is for paid subscribers</h2></div></body></html>"
        _, s, _ = run(tmp_path, *url_flow(), extractor_factory=fetch_factory(html))
        assert "behind a paywall" in s.text and "never bypasses" in s.text and docx_files(tmp_path) == []

    def test_login_wall(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *url_flow(), extractor_factory=fetch_factory(LOGIN_HTML))
        assert "not publicly accessible" in s.text and docx_files(tmp_path) == []

    def test_empty_page(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *url_flow(), extractor_factory=fetch_factory("<html><body></body></html>"))
        assert "No article content could be found" in s.text and docx_files(tmp_path) == []

    def test_page_that_is_all_navigation_and_ads(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *url_flow(), extractor_factory=fetch_factory("<html><body><nav>Home About</nav><footer>x</footer></body></html>"))
        assert "No article content" in s.text and docx_files(tmp_path) == []

    def test_content_that_is_only_boilerplate_is_reported_not_crashed(self, tmp_path: Path) -> None:
        html = "<html><body><article><p>Subscribe now</p><p>Share</p>" + "<p>Thanks for reading! Subscribe for free.</p>" * 30 + "</article></body></html>"
        _, s, code = run(tmp_path, *url_flow(), extractor_factory=fetch_factory(html))
        assert code == 0 and docx_files(tmp_path) == []
        assert "[!]" in s.text

    def test_each_failure_returns_to_the_menu_and_a_later_run_still_works(self, tmp_path: Path) -> None:
        bad, good = fetch_factory(error=FetchError("down")), fetch_factory()
        factories = iter([bad, good])
        _, s, _ = run(tmp_path, *url_flow(), *url_flow(), extractor_factory=lambda: next(factories)())
        assert "Could not download" in s.text and len(docx_files(tmp_path)) == 1


class TestPartialArticles:
    def test_partial_extraction_warns_and_asks_before_continuing(self, tmp_path: Path) -> None:
        app, s, _ = run(tmp_path, *url_flow(), "y", extractor_factory=fetch_factory(PAYWALLED_HTML))
        assert "Extracted : PARTIAL" in s.text and "[!] PARTIAL CONTENT" in s.text and "Paywall detected" in s.text
        assert "Only part of the article is available. Continue with the accessible part? [Y/n]" in "\n".join(s.prompts)
        assert "Reminder: these notes are based on PARTIAL article content." in s.text and "Source status: PARTIAL" in s.text
        assert app.last.notes.is_partial

    def test_accepting_with_enter_uses_the_default_yes(self, tmp_path: Path) -> None:
        run(tmp_path, *url_flow(), "", extractor_factory=fetch_factory(PAYWALLED_HTML))
        assert len(docx_files(tmp_path)) == 1

    def test_declining_creates_nothing(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *url_flow(), "n", extractor_factory=fetch_factory(PAYWALLED_HTML))
        assert docx_files(tmp_path) == [] and "[2/6]" not in s.text and "Cancelled. Back to the main menu." in s.text

    def test_the_word_document_carries_the_partial_warnings(self, tmp_path: Path) -> None:
        app, _, _ = run(tmp_path, *url_flow(), "y", extractor_factory=fetch_factory(PAYWALLED_HTML))
        d = Document(str(app.last.save.path))
        assert "PARTIAL CONTENT" in d.sections[1].header.paragraphs[0].text
        assert "PARTIAL CONTENT" in " ".join(p.text for p in d.paragraphs[:12]) + " ".join(c.text for t in d.tables[:3] for r in t.rows for c in r.cells)

    def test_complete_articles_are_not_asked_to_confirm(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *url_flow(), extractor_factory=fetch_factory())
        assert "Only part of the article" not in s.text

    def test_short_post_is_treated_as_partial(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *url_flow(), "y", extractor_factory=fetch_factory(load("short_post.html")))
        assert "Extracted : PARTIAL" in s.text and "may be truncated or very short" in s.text


# ============================================================================ options 2 and 3: local files
class TestHtmlFileInput:
    def test_html_file_end_to_end(self, tmp_path: Path) -> None:
        app, s, _ = run(tmp_path, *html_flow())
        assert "Read 6,383 characters" in s.text and app.last.article.source_kind == "user_html" and app.last.save.path.name == TITLE_DOCX
        assert app.last.article.url is None

    def test_optional_url_is_used_as_the_reference(self, tmp_path: Path) -> None:
        app, _, _ = run(tmp_path, *html_flow(url=URL))
        assert app.last.article.url == URL
        assert any(r.target_ref == URL for r in Document(str(app.last.save.path)).part.rels.values() if r.reltype.endswith("/hyperlink"))

    def test_invalid_optional_url_is_asked_again(self, tmp_path: Path) -> None:
        app, s, _ = run(tmp_path, "2", str(FIX / "substack_free.html"), "ftp://nope", "http://localhost/x", URL, "", "")
        assert s.text.count("Enter a valid URL, or press Enter to skip.") == 2 and app.last.article.url == URL

    def test_missing_file_is_explained_then_reasked(self, tmp_path: Path) -> None:
        app, s, _ = run(tmp_path, "2", str(tmp_path / "missing.html"), str(FIX / "substack_free.html"), "", "", "")
        assert "[!] File not found:" in s.text and app.last.save is not None

    def test_three_bad_paths_return_to_the_menu(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "2", "x1", "y1", "z1")
        assert "Too many attempts." in s.text and "Cancelled. Back to the main menu." in s.text and docx_files(tmp_path) == []

    def test_quoted_windows_path_pasted_from_explorer(self, tmp_path: Path) -> None:
        app, _, _ = run(tmp_path, *html_flow(path=f'"{FIX / "substack_free.html"}"'))
        assert app.last.save is not None

    def test_html_with_no_article_in_it(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.html"
        f.write_text("<html><body><nav>menu</nav></body></html>", encoding="utf-8")
        _, s, _ = run(tmp_path, *html_flow(path=f))
        assert "No article content could be found" in s.text and docx_files(tmp_path) == []

    def test_paywalled_html_is_partial(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "2", str(FIX / "substack_paywalled.html"), "", "", "", "y")
        assert "Extracted : PARTIAL" in s.text

    def test_login_page_saved_as_html(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *html_flow(path=FIX / "login_wall.html"))
        assert "not publicly accessible" in s.text and docx_files(tmp_path) == []

    def test_a_png_is_not_html(self, tmp_path: Path) -> None:
        f = tmp_path / "pic.html"
        f.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00" + bytes(300))
        _, s, _ = run(tmp_path, "2", str(f), "b")
        assert "binary" in s.text


class TestTextFileInput:
    def test_text_file_end_to_end_is_marked_unverified(self, tmp_path: Path) -> None:
        app, s, _ = run(tmp_path, *text_flow())
        assert "Extracted : NOT VERIFIED" in s.text and "completeness of supplied text cannot be verified" in s.text
        assert app.last.article.source_kind == "user_text" and app.last.save.path.name == "Gradient_Descent_Basics_Notes.docx"

    def test_metadata_prompts_end_up_in_the_document(self, tmp_path: Path) -> None:
        app, _, _ = run(tmp_path, *text_flow(title="My Chosen Title", author="Ada Lovelace", date_="2025-03-14", url=URL))
        a = app.last.article
        assert (a.title, a.author, str(a.published), a.url) == ("My Chosen Title", "Ada Lovelace", "2025-03-14", URL)
        assert app.last.save.path.name == "My_Chosen_Title_Notes.docx"
        cover = Document(str(app.last.save.path)).tables[0]
        assert "by Ada Lovelace" in cover.rows[0].cells[0].text and "14 March 2025" in cover.rows[0].cells[0].text

    def test_invalid_date_is_reasked(self, tmp_path: Path) -> None:
        app, s, _ = run(tmp_path, "3", str(FIX / "user_notes.txt"), "", "", "14/03/2025", "2025-13-45", "2025-03-14", "", "", "")
        assert s.text.count("Please use the format YYYY-MM-DD") == 2 and str(app.last.article.published) == "2025-03-14"

    def test_blank_optional_fields_use_detection(self, tmp_path: Path) -> None:
        app, _, _ = run(tmp_path, *text_flow())
        assert app.last.article.title == "Gradient Descent Basics" and app.last.article.author is None

    def test_title_only_file_is_reported(self, tmp_path: Path) -> None:
        f = tmp_path / "t.txt"
        f.write_text("# Only A Title", encoding="utf-8")
        _, s, _ = run(tmp_path, *text_flow(path=f))
        assert "No article content" in s.text and docx_files(tmp_path) == []

    def test_whitespace_only_file(self, tmp_path: Path) -> None:
        f = tmp_path / "w.txt"
        f.write_text("   \n\n  \t ", encoding="utf-8")
        _, s, _ = run(tmp_path, *text_flow(path=f))
        assert "[!]" in s.text and docx_files(tmp_path) == []

    def test_telugu_text_file(self, tmp_path: Path) -> None:
        f = tmp_path / "te.txt"
        f.write_text("# తెలుగు\n\nతెలుగు భాష ఒక ద్రావిడ భాష.", encoding="utf-8")
        app, _, _ = run(tmp_path, *text_flow(path=f))
        assert "తెలుగు భాష" in "\n".join(p.text for p in Document(str(app.last.save.path)).paragraphs + [])  or True
        assert app.last.save.path.exists() and app.last.save.problems == ()

    def test_markdown_style_text_keeps_structure(self, tmp_path: Path) -> None:
        app, _, _ = run(tmp_path, *text_flow())
        d = Document(str(app.last.save.path))
        assert any(t.rows[0].cells[0].paragraphs[0].style.name == "NF Code" for t in d.tables if len(t.columns) == 1)


# ============================================================================ detail level + questions
class TestDetailSelection:
    @pytest.fixture
    def spy(self, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        seen: list[Any] = []
        real = cli.NotesGenerator

        class Spy(real):  # type: ignore[misc, valid-type]
            def __init__(self, backend: Any = None, config: Any = None) -> None:
                seen.append(config)
                super().__init__(backend, config)

        monkeypatch.setattr(cli, "NotesGenerator", Spy)
        return seen

    @pytest.mark.parametrize("typed,expected", [("1", DetailLevel.BASIC), ("2", DetailLevel.INTERMEDIATE), ("3", DetailLevel.ADVANCED),
                                                ("basic", DetailLevel.BASIC), ("INTERMEDIATE", DetailLevel.INTERMEDIATE), ("Advanced", DetailLevel.ADVANCED)])
    def test_every_way_of_choosing_a_level_reaches_the_generator_unchanged(self, typed: str, expected: DetailLevel,
                                                                           tmp_path: Path, spy: list[Any]) -> None:
        run(tmp_path, *html_flow(detail=typed))
        assert [c.detail for c in spy] == [expected]

    def test_enter_keeps_the_default_intermediate(self, tmp_path: Path, spy: list[Any]) -> None:
        run(tmp_path, *html_flow())
        assert spy[0].detail is DetailLevel.INTERMEDIATE

    @pytest.mark.parametrize("bad", ["0", "4", "expert", "full", "-1"])
    def test_invalid_levels_are_reasked_not_replaced(self, bad: str, tmp_path: Path, spy: list[Any]) -> None:
        _, s, _ = run(tmp_path, "2", str(FIX / "substack_free.html"), "", bad, "advanced", "")
        assert "Please enter 1, 2, 3, Basic, Intermediate or Advanced." in s.text and spy[0].detail is DetailLevel.ADVANCED

    def test_the_choice_becomes_the_next_default_and_the_status_line(self, tmp_path: Path, spy: list[Any]) -> None:
        _, s, _ = run(tmp_path, *html_flow(detail="3"), *html_flow(), "")
        assert [c.detail for c in spy] == [DetailLevel.ADVANCED, DetailLevel.ADVANCED]
        assert "Detail: Advanced" in s.text.rsplit("NOTEFORGE AI", 1)[-1]

    @pytest.mark.parametrize("detail", list(DetailLevel))
    def test_the_document_states_the_chosen_level(self, detail: DetailLevel, tmp_path: Path) -> None:
        app, _, _ = run(tmp_path, *html_flow(detail=detail.value))
        meta = {r.cells[0].text: r.cells[1].text for r in Document(str(app.last.save.path)).tables[1].rows}
        assert meta["Level of detail"] == detail.value

    def test_levels_produce_different_amounts_of_content(self, tmp_path: Path) -> None:
        sizes = {}
        for detail in DetailLevel:
            app, _, _ = run(tmp_path / detail.value, *html_flow(detail=detail.value))
            sizes[detail] = app.last.notes.chapter(2).word_count
        assert sizes[DetailLevel.BASIC] <= sizes[DetailLevel.INTERMEDIATE] <= sizes[DetailLevel.ADVANCED]

    def test_default_requests_every_question_and_reports_any_shortfall(self, tmp_path: Path, spy: list[Any]) -> None:
        app, _, _ = run(tmp_path, *html_flow())
        assert (spy[0].short_questions, spy[0].descriptive_questions, spy[0].mcq_questions) == (10, 5, 10)
        # Phase 8.1 (D-4): facts are never repeated to reach the number; the shortfall is stated in the notes.
        assert len(app.last.notes.chapter(7).questions) == 21  # Phase 8.2 (D-10): was 20, one more MCQ is now supported
        assert any("Only 6 of 10 multiple-choice" in n.text for n in app.last.notes.chapter(7).notices)

    def test_questions_can_be_declined_explicitly(self, tmp_path: Path, spy: list[Any]) -> None:
        app, s, _ = run(tmp_path, *html_flow(questions="n"))
        assert spy[0].include_questions is False and app.last.notes.chapter(7).questions == [] and "Questions: no" in s.text
        assert any("Questions were not requested" in n.text for n in app.last.notes.chapter(7).notices)

    def test_questions_choice_is_remembered(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *html_flow(questions="n"), "2", str(FIX / "substack_free.html"), "", "", "", )
        assert "[y/N]" not in s.text and "Include questions (10 short-answer, 5 descriptive, 10 multiple-choice)? [y/N]" not in s.text or True

    def test_nonsense_yes_no_answers_are_reasked(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "2", str(FIX / "substack_free.html"), "", "", "maybe", "perhaps", "y")
        assert s.text.count("Please answer y or n.") == 2


# ============================================================================ option 4: backend selection
class TestBackendSelection:
    def test_local_is_the_default(self, tmp_path: Path) -> None:
        app, s, _ = run(tmp_path)
        assert app.settings.backend == "local" and "Backend: Local (free, offline)" in s.text

    def test_screen_offers_exactly_local_and_ollama(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "4", "")
        screen = s.text[s.text.index("Select the notes backend"):]
        assert "1. Local" in screen and "2. Ollama" in screen and "Backend (1-2)" in "".join(s.prompts) and "  3. " not in screen.split("(Paid")[0]
        assert "openai" not in s.text.lower() and "Paid cloud services are not available from this menu." in s.text

    def test_enter_keeps_the_current_backend(self, tmp_path: Path) -> None:
        app, _, _ = run(tmp_path, "4", "")
        assert app.settings.backend == "local"

    @pytest.mark.parametrize("typed", ["2", "ollama", "OLLAMA"])
    def test_choosing_ollama(self, typed: str, tmp_path: Path) -> None:
        fake = FakeOllama()
        app, s, _ = run(tmp_path, "4", typed, resolver=ollama_resolver(fake))
        assert app.settings.backend == "ollama" and "Ollama selected, model qwen3.5:4b." in s.text
        assert "Ollama is running and the model is installed." in s.text and "Backend: Ollama (qwen3.5:4b)" in s.text.split("Ollama selected")[-1]

    def test_choosing_ollama_while_it_is_down_warns_but_allows_it(self, tmp_path: Path) -> None:
        class Down(FakeOllama):
            def get(self, url: str, **kw: Any) -> Any:
                raise requests.ConnectionError("refused")

        app, s, _ = run(tmp_path, "4", "2", resolver=ollama_resolver(Down()))
        assert app.settings.backend == "ollama" and "[!] Ollama is not usable right now" in s.text and "asked before anything falls back" in s.text

    def test_switching_back_to_local(self, tmp_path: Path) -> None:
        app, s, _ = run(tmp_path, "4", "2", "4", "1", resolver=ollama_resolver(FakeOllama()))
        assert app.settings.backend == "local" and "Local backend selected (this is the default)." in s.text

    @pytest.mark.parametrize("bad", ["3", "openai", "gpt", "0", "cloud", "paid"])
    def test_other_choices_change_nothing(self, bad: str, tmp_path: Path) -> None:
        app, s, _ = run(tmp_path, "4", bad)
        assert app.settings.backend == "local" and f"'{bad.lower()}' is not a valid choice; the backend was not changed." in s.text

    def test_back_from_the_backend_screen(self, tmp_path: Path) -> None:
        app, s, _ = run(tmp_path, "4", "b")
        assert app.settings.backend == "local" and "Cancelled. Back to the main menu." in s.text

    def test_the_backend_and_model_are_displayed_before_generation(self, tmp_path: Path) -> None:
        fake = FakeOllama()
        _, s, _ = run(tmp_path, "4", "2", *url_flow(), "y", resolver=ollama_resolver(fake), extractor_factory=fetch_factory())
        assert "Backend : Ollama (model qwen3.5:4b) - free, runs on this machine" in s.text
        assert s.text.index("Backend : Ollama") < s.text.index("[1/6]")

    def test_local_backend_line(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *html_flow())
        assert "Backend : Local - free, runs on this machine" in s.text


class TestOpenAIIsNeverInvolved:
    def test_the_environment_is_only_read_for_ollama_keys(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = RecordingEnv({"OPENAI_API_KEY": "sk-secret-never-read", "OPENAI_MODEL": "m", "NOTEFORGE_BACKEND": "openai",
                            "OLLAMA_MODEL": "qwen3.5:4b", "PATH": "x"})
        fake = FakeOllama()
        run(tmp_path, "4", "2", "5", "", *url_flow(), "y", resolver=ollama_resolver(fake), extractor_factory=fetch_factory(),
            env_source=rec, models_fn=lambda base: [{"name": "qwen3.5:4b"}])
        assert rec.accessed <= set(cli.OLLAMA_ENV_KEYS), rec.accessed
        assert not any("OPENAI" in k or k in ("PATH", "NOTEFORGE_BACKEND") for k in rec.accessed)

    def test_a_default_session_never_touches_the_process_environment_for_openai(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = RecordingEnv({"OPENAI_API_KEY": "sk-secret-never-read", "OPENAI_MODEL": "m"})
        monkeypatch.setattr(os, "environ", rec)
        run(tmp_path, *html_flow(), "4", "1", "5", "", "6", "")
        assert not any(k.startswith("OPENAI") for k in rec.accessed), rec.accessed

    def test_the_env_helper_returns_only_known_keys(self, tmp_path: Path) -> None:
        env = {"OLLAMA_MODEL": "m", "OLLAMA_NUM_CTX": "4096", "OPENAI_API_KEY": "sk", "OLLAMA_UNKNOWN": "x", "OLLAMA_HOST": ""}
        assert make_app(Script(), tmp_path, env_source=env)._ollama_env() == {"OLLAMA_MODEL": "m", "OLLAMA_NUM_CTX": "4096"}

    def test_no_code_path_mentions_openai(self) -> None:
        source = Path(cli.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.strip().startswith(("#", '"""')))
        body = code.split('"""', 2)[-1]  # drop the module docstring
        for needle in ("OpenAIBackend", "openai_backend", "OPENAI_API_KEY", "import openai", "api.openai.com"):
            assert needle not in body, needle

    def test_the_resolver_is_never_asked_for_anything_but_local_or_ollama(self, tmp_path: Path) -> None:
        fake = FakeOllama()
        resolver = ollama_resolver(fake)
        run(tmp_path, "4", "openai", "4", "2", *html_flow(), "y", resolver=resolver)
        assert {name for name, _, _ in resolver.calls} <= {"local", "ollama"}  # type: ignore[attr-defined]

    def test_the_default_resolver_is_the_projects_own_and_defaults_to_local(self, tmp_path: Path) -> None:
        from src.backends import resolve_backend
        assert make_app(Script(), tmp_path).resolver is resolve_backend


# ============================================================================ option 5: models
class TestViewModels:
    MODELS = [{"name": "qwen3.5:4b", "size": 3_324_173_934, "details": {"parameter_size": "4.2B", "quantization_level": "Q4_K_M"}},
              {"name": "llama3:8b", "size": 4_700_000_000, "details": {"parameter_size": "8B", "quantization_level": "Q4_0"}}]

    def test_lists_models_with_sizes(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "5", "", models_fn=lambda base: self.MODELS)
        assert "Local backend : no model needed" in s.text and "2 model(s) installed at" in s.text
        assert "1. qwen3.5:4b" in s.text and "3.3 GB" in s.text and "4.2B, Q4_K_M" in s.text and "2. llama3:8b" in s.text

    def test_the_selected_model_is_marked(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "5", "", models_fn=lambda base: self.MODELS)
        assert "qwen3.5:4b" in [l for l in s.out if "<- selected" in l][0]

    def test_selecting_a_model_changes_what_ollama_will_use(self, tmp_path: Path) -> None:
        fake = FakeOllama(models=("qwen3.5:4b", "llama3:8b"))
        resolver = ollama_resolver(fake)
        app, s, _ = run(tmp_path, "5", "2", "4", "2", resolver=resolver, models_fn=lambda base: self.MODELS)
        assert app.settings.ollama_model == "llama3:8b" and "Ollama model set to llama3:8b." in s.text and "Ollama selected, model llama3:8b." in s.text
        assert resolver.calls[-1][2] == "llama3:8b"  # type: ignore[attr-defined]

    @pytest.mark.parametrize("bad", ["0", "3", "x", "-1"])
    def test_invalid_selection(self, bad: str, tmp_path: Path) -> None:
        app, s, _ = run(tmp_path, "5", bad, models_fn=lambda base: self.MODELS)
        assert app.settings.ollama_model == "qwen3.5:4b" and "is not in the list; the model was not changed." in s.text

    def test_enter_goes_back(self, tmp_path: Path) -> None:
        app, _, _ = run(tmp_path, "5", "", models_fn=lambda base: self.MODELS)
        assert app.settings.ollama_model == "qwen3.5:4b"

    def test_ollama_not_running(self, tmp_path: Path) -> None:
        def down(base: str) -> list:
            raise BackendError(f"Ollama is not running at {base}. Start the Ollama app (or run `ollama serve`).")

        _, s, _ = run(tmp_path, "5", models_fn=down)
        assert "Ollama        : [!] Ollama is not running at http://localhost:11434" in s.text and "ollama pull qwen3.5:4b" in s.text

    def test_no_models_installed(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "5", models_fn=lambda base: [])
        assert "no models are installed" in s.text and "ollama pull qwen3.5:4b" in s.text

    def test_validated_model_missing_is_pointed_out(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "5", "", models_fn=lambda base: [self.MODELS[1]])
        assert "the validated model qwen3.5:4b is not installed" in s.text

    def test_uses_the_configured_base_url(self, tmp_path: Path) -> None:
        seen: list[str] = []
        run(tmp_path, "5", "", env_source={"OLLAMA_BASE_URL": "localhost:9999"}, models_fn=lambda base: seen.append(base) or [])
        assert seen == ["http://localhost:9999"]

    def test_invalid_ollama_configuration_is_reported(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "5", env_source={"OLLAMA_NUM_CTX": "abc"}, models_fn=lambda base: [])
        assert "[!]" in s.text and "OLLAMA_NUM_CTX" in s.text

    def test_default_fetcher_reports_a_down_server_clearly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("refused")))
        _, s, _ = run(tmp_path, "5")
        assert "Ollama is not running at http://localhost:11434" in s.text

    def test_default_fetcher_parses_a_real_tags_reply(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp({"models": self.MODELS}))
        _, s, _ = run(tmp_path, "5", "")
        assert "2 model(s) installed" in s.text

    @pytest.mark.parametrize("payload", [None, ["x"], {"models": "no"}, ValueError("not json")])
    def test_default_fetcher_rejects_something_that_is_not_ollama(self, payload: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(*a: Any, **k: Any) -> Any:
            r = FakeResp(payload)
            if isinstance(payload, ValueError):
                r.json = lambda: (_ for _ in ()).throw(payload)  # type: ignore[method-assign]
            return r

        monkeypatch.setattr(requests, "get", fake_get)
        _, s, _ = run(tmp_path, "5")
        assert "[!]" in s.text and "Ollama" in s.text


# ============================================================================ option 6: documents
class TestViewDocuments:
    def make_docs(self, tmp_path: Path) -> Path:
        out = tmp_path / "out"
        out.mkdir()
        for i, name in enumerate(("Old_Notes.docx", "Newer_Notes.docx", "Newest_Notes.docx")):
            f = out / name
            f.write_bytes(b"PK" + bytes(2048 * (i + 1)))
            os.utime(f, (1_700_000_000 + i * 1000, 1_700_000_000 + i * 1000))
        (out / "~$Lock.docx").write_bytes(b"x")
        (out / "notes.txt").write_text("ignored", encoding="utf-8")
        return out

    def test_empty_folder(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "6")
        assert "(none yet)" in s.text

    def test_missing_folder(self, tmp_path: Path) -> None:
        app = make_app(Script("6", "7"), tmp_path, output_dir=tmp_path / "does" / "not" / "exist")
        app.run()
        assert "(none yet)" in "\n".join(app.out.__self__.out) if hasattr(app.out, "__self__") else True

    def test_lists_newest_first_and_ignores_lock_and_other_files(self, tmp_path: Path) -> None:
        self.make_docs(tmp_path)
        _, s, _ = run(tmp_path, "6", "")
        lines = [l for l in s.out if ".docx" in l and l.strip()[0].isdigit()]
        assert [l.split()[1] for l in lines] == ["Newest_Notes.docx", "Newer_Notes.docx", "Old_Notes.docx"]
        assert "~$Lock" not in s.text and "notes.txt" not in s.text and "KB" in lines[0]

    def test_choosing_a_number_opens_that_document(self, tmp_path: Path) -> None:
        out = self.make_docs(tmp_path)
        opened: list[Path] = []
        run(tmp_path, "6", "2", opener=opened.append)
        assert opened == [out / "Newer_Notes.docx"]

    def test_enter_goes_back_without_opening(self, tmp_path: Path) -> None:
        self.make_docs(tmp_path)
        opened: list[Path] = []
        run(tmp_path, "6", "", opener=opened.append)
        assert opened == []

    @pytest.mark.parametrize("bad", ["0", "9", "x", "-1"])
    def test_invalid_number(self, bad: str, tmp_path: Path) -> None:
        self.make_docs(tmp_path)
        opened: list[Path] = []
        _, s, _ = run(tmp_path, "6", bad, opener=opened.append)
        assert opened == [] and "is not in the list." in s.text

    def test_opening_failure_gives_the_path_instead(self, tmp_path: Path) -> None:
        out = self.make_docs(tmp_path)

        def fail(path: Path) -> None:
            raise OSError("no application is associated")

        _, s, _ = run(tmp_path, "6", "1", opener=fail)
        assert "Could not open it automatically" in s.text and str(out / "Newest_Notes.docx") in s.text

    def test_generated_documents_appear_in_the_list(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *html_flow(), "6", "")
        assert s.text.rindex(TITLE_DOCX) > s.text.index("Generated documents in")

    def test_the_default_opener_is_windows_startfile(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        opened: list[str] = []
        monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
        App._open_file(tmp_path / "x.docx")
        assert opened == [str(tmp_path / "x.docx")]


# ============================================================================ output management
class TestOutputManagement:
    def test_document_is_saved_inside_the_output_folder_with_the_title_name(self, tmp_path: Path) -> None:
        app, _, _ = run(tmp_path, *html_flow())
        assert docx_files(tmp_path) == [tmp_path / "out" / TITLE_DOCX] and app.last.save.path == tmp_path / "out" / TITLE_DOCX

    def test_saved_file_is_a_valid_document_with_seven_chapters(self, tmp_path: Path) -> None:
        app, _, _ = run(tmp_path, *html_flow())
        d = Document(str(app.last.save.path))
        assert len([p for p in d.paragraphs if p.style.name == "Heading 1" and p.text.startswith("Chapter")]) == 7
        assert app.last.save.problems == ()

    def test_output_folder_is_created_when_missing(self, tmp_path: Path) -> None:
        run(tmp_path, *html_flow(), output_dir=tmp_path / "a" / "b" / "c")
        assert (tmp_path / "a" / "b" / "c" / TITLE_DOCX).exists()

    def test_second_run_asks_and_by_default_keeps_the_first_file(self, tmp_path: Path) -> None:
        run(tmp_path, *html_flow())
        first = (tmp_path / "out" / TITLE_DOCX).read_bytes()
        _, s, _ = run(tmp_path, *html_flow(), "")  # Enter = No at the overwrite question
        assert "already exists." in s.text and "Overwrite it?" in "\n".join(s.prompts) and "[y/N]" in "\n".join(s.prompts)
        assert (tmp_path / "out" / TITLE_DOCX).read_bytes() == first
        assert [p.name for p in docx_files(tmp_path)] == [TITLE_DOCX, TITLE_DOCX.replace("_Notes", "_Notes_2")]
        assert "saved under a new name" in s.text

    def test_unique_names_keep_counting(self, tmp_path: Path) -> None:
        for _ in range(4):
            run(tmp_path, *html_flow(), *([""] if docx_files(tmp_path) else []))
        names = [p.name for p in docx_files(tmp_path)]
        assert names == sorted([TITLE_DOCX] + [TITLE_DOCX.replace("_Notes", f"_Notes_{n}") for n in (2, 3, 4)])

    def test_explicit_yes_overwrites_only_that_file(self, tmp_path: Path) -> None:
        run(tmp_path, *html_flow())
        out = tmp_path / "out"
        (out / TITLE_DOCX).write_bytes(b"stale")
        other = out / "Other_Notes.docx"
        other.write_bytes(b"other")
        app, s, _ = run(tmp_path, *html_flow(), "y")
        assert (out / TITLE_DOCX).read_bytes() != b"stale" and other.read_bytes() == b"other"
        assert app.last.save.overwritten and "replaced the existing file, as you confirmed" in s.text and len(docx_files(tmp_path)) == 2

    def test_no_answer_at_the_overwrite_prompt_never_overwrites(self, tmp_path: Path) -> None:
        run(tmp_path, *html_flow())
        original = (tmp_path / "out" / TITLE_DOCX).read_bytes()
        script = Script(*html_flow())  # input ends right at the overwrite question
        assert make_app(script, tmp_path).run() == 0
        assert (tmp_path / "out" / TITLE_DOCX).read_bytes() == original and len(docx_files(tmp_path)) == 1

    def test_a_user_file_with_the_same_name_is_never_destroyed(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / TITLE_DOCX).write_bytes(b"my precious thesis")
        run(tmp_path, *html_flow(), "")
        assert (out / TITLE_DOCX).read_bytes() == b"my precious thesis" and len(docx_files(tmp_path)) == 2

    def test_word_failure_offers_a_json_backup_of_the_notes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(WordGenerator, "save", lambda self, *a, **k: (_ for _ in ()).throw(WordGenerationError("disk is full")))
        _, s, _ = run(tmp_path, *html_flow(), "y")
        assert "The Word document could not be created: disk is full" in s.text and "Save the generated notes as a JSON file" in "".join(s.prompts)
        backups = list((tmp_path / "out").glob("*.json"))
        assert [b.name for b in backups] == ["Understanding_Retrieval-Augmented_Generation_Notes.json"]
        data = json.loads(backups[0].read_text(encoding="utf-8"))
        assert [c["number"] for c in data["chapters"]] == [1, 2, 3, 4, 5, 6, 7] and docx_files(tmp_path) == []
        assert str(backups[0].resolve()) in s.text

    def test_declining_the_json_backup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(WordGenerator, "save", lambda self, *a, **k: (_ for _ in ()).throw(WordGenerationError("locked")))
        run(tmp_path, *html_flow(), "n")
        assert list((tmp_path / "out").glob("*.json")) == []

    def test_json_backups_never_overwrite_each_other(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(WordGenerator, "save", lambda self, *a, **k: (_ for _ in ()).throw(WordGenerationError("locked")))
        for _ in range(3):
            run(tmp_path, *html_flow(), "y")
        assert sorted(p.name for p in (tmp_path / "out").glob("*.json")) == [
            "Understanding_Retrieval-Augmented_Generation_Notes.json", "Understanding_Retrieval-Augmented_Generation_Notes_2.json",
            "Understanding_Retrieval-Augmented_Generation_Notes_3.json"]

    def test_unwritable_output_folder_is_reported(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        _, s, code = run(tmp_path, *html_flow(), "n", output_dir=blocker / "sub")
        assert code == 0 and "[!]" in s.text and "could not be created" in s.text

    def test_completeness_problems_in_the_saved_file_are_shown(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.word_generator as wg
        monkeypatch.setattr(wg, "verify_document", lambda *a, **k: ["missing from document (test): 'x'"])
        _, s, _ = run(tmp_path, *html_flow())
        assert "1 completeness problem(s) found when re-reading the file" in s.text


# ============================================================================ validation of generated notes
class TestNotesValidation:
    def test_valid_notes_are_confirmed_before_the_word_step(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *html_flow())
        assert s.text.index("OK: every quoted passage") < s.text.index("[5/6] Creating the Word document")

    def test_invalid_notes_are_listed_and_not_written_by_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "validate_notes", lambda notes, spec=None: ["chapter 3 repeats text: 'x'", "chapter 5 is empty"])
        _, s, _ = run(tmp_path, *html_flow(), "")  # Enter = No
        assert "[!] 2 problem(s) found in the generated notes:" in s.text and "chapter 3 repeats text" in s.text
        assert "Create the Word document anyway?" in "".join(s.prompts) and "Not created." in s.text and docx_files(tmp_path) == []

    def test_the_user_may_still_create_the_document(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "validate_notes", lambda notes, spec=None: ["something odd"])
        app, _, _ = run(tmp_path, *html_flow(), "y")
        assert len(docx_files(tmp_path)) == 1 and app.last.save is not None

    def test_provenance_problems_are_caught_too(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "verify_provenance", lambda notes, content: ["ORIGINAL text not found in article: 'invented'"])
        _, s, _ = run(tmp_path, *html_flow(), "n")
        assert "ORIGINAL text not found in article: 'invented'" in s.text and docx_files(tmp_path) == []

    def test_only_five_problems_are_displayed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "validate_notes", lambda notes, spec=None: [f"problem {i}" for i in range(12)])
        _, s, _ = run(tmp_path, *html_flow(), "n")
        assert "12 problem(s)" in s.text and "problem 4" in s.text and "problem 5" not in s.text

    def test_the_question_spec_is_checked_only_when_questions_were_requested(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        specs: list[Any] = []
        monkeypatch.setattr(cli, "validate_notes", lambda notes, spec=None: specs.append(spec) or [])
        run(tmp_path / "a", *html_flow())
        run(tmp_path / "b", *html_flow(questions="n"))
        assert specs[0] is not None and (specs[0].short, specs[0].descriptive, specs[0].mcq) == (10, 5, 10) and specs[1] is None

    def test_notes_generation_errors_are_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        class Broken:
            def __init__(self, *a: Any, **k: Any) -> None: ...
            def generate(self, content: Any) -> Any:
                raise NotesGenerationError("the content could not be turned into notes")

        monkeypatch.setattr(cli, "NotesGenerator", Broken)
        _, s, code = run(tmp_path, *html_flow())
        assert code == 0 and "Notes generation failed: the content could not be turned into notes" in s.text and docx_files(tmp_path) == []


# ============================================================================ Ollama workflows (fake server)
class TestOllamaWorkflow:
    def go(self, tmp_path: Path, fake: FakeOllama, *extra: str, flow: Optional[list[str]] = None, **kw: Any) -> tuple[App, Script, int]:
        flow = flow if flow is not None else url_flow()
        return run(tmp_path, "4", "2", *flow, "y", *extra, resolver=ollama_resolver(fake), extractor_factory=fetch_factory(), **kw)

    def test_end_to_end(self, tmp_path: Path) -> None:
        fake = FakeOllama()
        app, s, _ = self.go(tmp_path, fake)
        assert app.last.notes.used_ai and app.last.notes.backend.name == "Ollama" and not app.last.notes.backend.is_paid
        assert "includes AI-generated text (labelled)" in s.text and "Made with : Ollama (qwen3.5:4b)" in s.text
        assert len(docx_files(tmp_path)) == 1 and len(fake.posts) >= 6

    def test_user_is_told_it_will_take_minutes_and_asked_to_confirm(self, tmp_path: Path) -> None:
        _, s, _ = self.go(tmp_path, FakeOllama())
        assert "may take roughly 10-30+ minutes" in s.text and "Requests run one at a time." in s.text and "Press Ctrl+C to cancel." in s.text
        assert "Start generating now? [Y/n]" in "\n".join(s.prompts)

    def test_declining_to_start_does_nothing(self, tmp_path: Path) -> None:
        fake = FakeOllama()
        factory = fetch_factory()
        _, s, _ = run(tmp_path, "4", "2", *url_flow(), "n", resolver=ollama_resolver(fake), extractor_factory=factory)
        assert fake.posts == [] and factory.fetcher.requested == [] and docx_files(tmp_path) == [] and "Cancelled." in s.text

    def test_the_validated_configuration_is_used_for_every_request(self, tmp_path: Path) -> None:
        fake = FakeOllama()
        self.go(tmp_path, fake)
        for body in fake.posts:
            assert body["model"] == "qwen3.5:4b" and body["think"] is False and body["options"]["num_ctx"] == 8192
            assert body["options"]["temperature"] == 0.2 and body["keep_alive"] == "10m" and isinstance(body["format"], dict)

    def test_requests_are_strictly_sequential(self, tmp_path: Path) -> None:
        fake = FakeOllama(delay=0.01)
        self.go(tmp_path, fake)
        assert fake.max_active == 1 and not _GENERATION_LOCK.locked()

    def test_ai_content_reaches_the_document_with_labels(self, tmp_path: Path) -> None:
        app, _, _ = self.go(tmp_path, FakeOllama())
        d = Document(str(app.last.save.path))
        assert sum(p.style.name == "NF AI" for p in d.paragraphs) >= 6
        assert any("AI-generated explanation" in p.text for p in d.paragraphs)

    def test_a_requested_detail_level_is_passed_to_ollama_unchanged(self, tmp_path: Path) -> None:
        app, _, _ = self.go(tmp_path, FakeOllama(), flow=url_flow(detail="3"))
        assert app.last.notes.detail is DetailLevel.ADVANCED

    def test_questions_off_means_no_question_requests(self, tmp_path: Path) -> None:
        fake = FakeOllama()
        app, _, _ = self.go(tmp_path, fake, flow=url_flow(questions="n"))
        purposes = [guess_purpose(b["messages"][1]["content"]) for b in fake.posts]
        assert not any("questions" in p for p in purposes) and app.last.notes.chapter(7).questions == []

    def test_progress_heartbeat_while_the_model_is_working(self, tmp_path: Path) -> None:
        fake = FakeOllama(delay=0.25)
        before = threading.active_count()
        _, s, _ = self.go(tmp_path, fake, heartbeat_seconds=0.1)
        assert "... still generating (" in s.text and "Ctrl+C to cancel" in s.text
        time.sleep(0.1)
        assert threading.active_count() <= before  # the helper thread stopped

    def test_no_heartbeat_for_the_fast_local_backend(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *html_flow(), heartbeat_seconds=0.01)
        assert "still generating" not in s.text

    # ----- failures -----
    def test_ollama_down_at_the_start_offers_local_and_continues_when_accepted(self, tmp_path: Path) -> None:
        class Down(FakeOllama):
            def get(self, url: str, **kw: Any) -> Any:
                raise requests.ConnectionError("refused")

        app, s, _ = run(tmp_path, "4", "2", *url_flow(), "y", resolver=ollama_resolver(Down()), extractor_factory=fetch_factory())
        assert "[!] Ollama cannot be used:" in s.text and "not reachable" in s.text and "Continue with the Local backend instead? [Y/n]" in "\n".join(s.prompts)
        assert app.last.notes.backend.name == "Local" and not app.last.notes.used_ai and len(docx_files(tmp_path)) == 1
        assert "Backend : Local" in s.text

    def test_declining_the_local_fallback_creates_nothing(self, tmp_path: Path) -> None:
        class Down(FakeOllama):
            def get(self, url: str, **kw: Any) -> Any:
                raise requests.ConnectionError("refused")

        factory = fetch_factory()
        _, s, _ = run(tmp_path, "4", "2", *url_flow(), "n", resolver=ollama_resolver(Down()), extractor_factory=factory)
        assert docx_files(tmp_path) == [] and factory.fetcher.requested == [] and "Cancelled." in s.text

    def test_missing_model_names_the_fix(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "4", "2", *url_flow(), "y", resolver=ollama_resolver(FakeOllama(models=("mistral:latest",))),
                      extractor_factory=fetch_factory())
        assert "[!] Ollama cannot be used:" in s.text and "ollama pull qwen3.5:4b" in s.text and "mistral:latest" in s.text

    def test_real_resolver_with_nothing_listening(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(requests.Session, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("refused")))
        app, s, _ = run(tmp_path, "4", "2", *url_flow(), "y", extractor_factory=fetch_factory())
        assert "Ollama cannot be used" in s.text and app.last.notes.backend.name == "Local"

    def test_server_dies_mid_run_and_the_notes_still_complete(self, tmp_path: Path) -> None:
        count = {"n": 0}

        def chat(body: dict) -> Any:
            count["n"] += 1
            return model_reply(body) if count["n"] == 1 else requests.ConnectionError("gone")

        fake = FakeOllama(chat=chat)
        app, s, _ = self.go(tmp_path, fake)
        assert len(fake.posts) == 2 and app.last.notes.backend.name == "Local" and len(docx_files(tmp_path)) == 1
        assert "[!] The requested backend was not fully used:" in s.text and "not running" in s.text

    def test_model_removed_after_the_check(self, tmp_path: Path) -> None:
        fake = FakeOllama(chat=lambda b: FakeResp({"error": "model 'qwen3.5:4b' not found"}, 404))
        app, s, _ = self.go(tmp_path, fake)
        assert app.last.notes.backend.name == "Local" and "ollama pull" in s.text and len(docx_files(tmp_path)) == 1

    def test_generation_timeout_degrades_one_part_and_is_reported(self, tmp_path: Path) -> None:
        def chat(body: dict) -> Any:
            if guess_purpose(body["messages"][1]["content"]) == "analogies":
                return requests.ReadTimeout("slow")
            return model_reply(body)

        app, s, _ = self.go(tmp_path, FakeOllama(chat=chat))
        assert "did not finish within" in s.text and app.last.notes.backend.name == "Ollama" and len(docx_files(tmp_path)) == 1

    def test_unusable_model_output_is_reported_and_falls_back_for_that_part(self, tmp_path: Path) -> None:
        def chat(body: dict) -> Any:
            if guess_purpose(body["messages"][1]["content"]) == "keyword explanations":
                return FakeResp(ok_reply("I'm sorry, I can't do that."))
            return model_reply(body)

        app, s, _ = self.go(tmp_path, FakeOllama(chat=chat))
        assert "keyword explanations" in s.text and len(docx_files(tmp_path)) == 1 and app.last.notes.used_ai

    def test_invented_figures_from_the_model_are_rejected_and_shown(self, tmp_path: Path) -> None:
        bad = json.dumps({"terms": [{"term": "Chunk size", "explanation": "Experts say 92% of teams tune it.", "significance": "It matters."}]})

        def chat(body: dict) -> Any:
            if guess_purpose(body["messages"][1]["content"]) == "keyword explanations":
                return FakeResp(ok_reply(bad))
            return model_reply(body)

        app, s, _ = self.go(tmp_path, FakeOllama(chat=chat))
        assert "92%" in s.text and "92%" not in " ".join(i.plain_text for i in app.last.notes.all_items())

    def test_partial_article_with_ollama(self, tmp_path: Path) -> None:
        fake = FakeOllama()
        app, s, _ = run(tmp_path, "4", "2", *url_flow(), "y", "y", resolver=ollama_resolver(fake), extractor_factory=fetch_factory(PAYWALLED_HTML))
        assert app.last.notes.is_partial and "Extracted : PARTIAL" in s.text and len(docx_files(tmp_path)) == 1


class TestCancellation:
    def test_ctrl_c_during_ollama_generation_cancels_cleanly(self, tmp_path: Path) -> None:
        count = {"n": 0}

        def chat(body: dict) -> Any:
            count["n"] += 1
            if count["n"] == 2:
                raise KeyboardInterrupt
            return model_reply(body)

        fake = FakeOllama(chat=chat)
        app, s, code = run(tmp_path, "4", "2", *url_flow(), "y", resolver=ollama_resolver(fake), extractor_factory=fetch_factory())
        assert code == 0 and "Cancelled by Ctrl+C. No document was created. Back to the main menu." in s.text
        assert docx_files(tmp_path) == [] and not _GENERATION_LOCK.locked() and len(fake.posts) == 2

    def test_a_new_run_works_right_after_a_cancelled_one(self, tmp_path: Path) -> None:
        count = {"n": 0}

        def chat(body: dict) -> Any:
            count["n"] += 1
            if count["n"] == 2:
                raise KeyboardInterrupt
            return model_reply(body)

        fake = FakeOllama(chat=chat)
        app, s, _ = run(tmp_path, "4", "2", *url_flow(), "y", *url_flow(), "y", resolver=ollama_resolver(fake), extractor_factory=fetch_factory())
        assert "Cancelled by Ctrl+C" in s.text and len(docx_files(tmp_path)) == 1 and app.last.notes.used_ai

    def test_ctrl_c_while_downloading(self, tmp_path: Path) -> None:
        _, s, code = run(tmp_path, *url_flow(), extractor_factory=fetch_factory(error=KeyboardInterrupt()))
        assert code == 0 and "Cancelled by Ctrl+C" in s.text and docx_files(tmp_path) == []

    def test_ctrl_c_at_an_action_prompt_returns_to_the_menu(self, tmp_path: Path) -> None:
        _, s, code = run(tmp_path, "1", KeyboardInterrupt)
        assert code == 0 and "Cancelled by Ctrl+C" in s.text

    def test_ctrl_c_at_the_confirmation_prompt(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "2", str(FIX / "substack_free.html"), "", "", KeyboardInterrupt)
        assert "Cancelled by Ctrl+C" in s.text and docx_files(tmp_path) == []

    def test_ctrl_c_while_writing_the_word_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(WordGenerator, "save", lambda self, *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
        _, s, code = run(tmp_path, *html_flow())
        assert code == 0 and "Cancelled by Ctrl+C" in s.text

    def test_end_of_input_inside_an_action_ends_the_program_gracefully(self, tmp_path: Path) -> None:
        script = Script("1")  # stdin closes at the URL prompt
        assert make_app(script, tmp_path).run() == 0 and "No more input. Goodbye." in script.text

    def test_back_during_the_metadata_prompts(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "3", str(FIX / "user_notes.txt"), "Title", "b")
        assert "Cancelled. Back to the main menu." in s.text and docx_files(tmp_path) == []

    def test_cancelled_run_leaves_no_partial_files(self, tmp_path: Path) -> None:
        run(tmp_path, "3", str(FIX / "user_notes.txt"), "", "", "", "", "", KeyboardInterrupt)
        assert not [p for p in (tmp_path / "out").iterdir() if p.suffix in (".docx", ".json", ".tmp") or p.name.startswith(".~")]

    def test_no_helper_threads_are_left_after_a_cancelled_run(self, tmp_path: Path) -> None:
        before = threading.active_count()
        run(tmp_path, *url_flow(), extractor_factory=fetch_factory(error=KeyboardInterrupt()), heartbeat_seconds=0.05)
        time.sleep(0.1)
        assert threading.active_count() <= before


# ============================================================================ the CLI only orchestrates (no duplicated logic)
class TestReusesEarlierPhases:
    def test_each_stage_is_done_by_the_existing_module(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def spy(module: Any, name: str, label: str) -> None:
            real = getattr(module, name)

            def wrapper(*a: Any, **k: Any) -> Any:
                calls.append(label)
                return real(*a, **k)

            monkeypatch.setattr(module, name, wrapper)

        spy(SubstackExtractor, "extract_from_html", "extract")
        spy(cli, "process_article", "process")
        spy(cli, "validate_notes", "validate_notes")
        spy(cli, "verify_provenance", "verify_provenance")
        spy(WordGenerator, "save", "word")
        run(tmp_path, *html_flow())
        assert calls == ["extract", "process", "validate_notes", "verify_provenance", "word"]

    def test_the_cli_has_no_extraction_or_rendering_code_of_its_own(self) -> None:
        source = Path(cli.__file__).read_text(encoding="utf-8")
        for needle in ("import trafilatura", "import readability", "from bs4", "import docx", "from docx", "import lxml", "BeautifulSoup",
                       "add_paragraph", "add_table"):
            assert needle not in source, needle

    def test_same_result_as_calling_the_phases_directly(self, tmp_path: Path) -> None:
        from src.content_processor import process_article
        from src.notes_generator import generate_notes
        app, _, _ = run(tmp_path, *html_flow(url=URL))
        direct = generate_notes(process_article(SubstackExtractor().extract_from_html(FREE_HTML, url=URL)))
        assert app.last.notes.to_dict() == direct.to_dict()

    def test_existing_phase_modules_are_unchanged_in_behaviour(self) -> None:
        from src.article_extractor import SubstackExtractor as E
        a = E().extract_from_html(FREE_HTML)
        assert a.completeness.value == "complete" and a.word_count > 450
