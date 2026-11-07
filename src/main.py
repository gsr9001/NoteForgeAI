"""NoteForge AI - interactive command-line interface (Phase 7).

Run from the project root::

    python src/main.py

This module only orchestrates. Every real step is done by an earlier phase:

    extract (article_extractor) -> process (content_processor) -> notes (notes_generator,
    with a backend chosen through backends.resolve_backend) -> verify -> Word (word_generator)

Safety and behaviour, by design:
* The default backend is the local, free, offline one. Only "Local" and "Ollama" are offered.
  OpenAI is not reachable from this program, and ``OPENAI_*`` environment variables are never read.
  Only the documented ``OLLAMA_*`` variables are consulted, and only when Ollama is used.
* A paywall is never bypassed; the user is told how to supply content they may legitimately access.
* Existing documents are never overwritten without an explicit yes; otherwise a unique name is used.
* The chosen detail level is never reduced behind the user's back.
* Everything printed by this module is plain ASCII so it is safe in a legacy Windows console.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

if __package__ in (None, ""):  # started as "python src/main.py": make "import src..." work from anywhere
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import __version__
from src.article_extractor import (
    Article, ArticleInaccessibleError, Completeness, ExtractionError, FetchError, InvalidSourceError,
    NoContentError, PaywallError, SubstackExtractor, is_post_url, validate_url,
)
from src.backends import BackendSelection, OllamaSettings, resolve_backend
from src.backends.base import BackendError, QuestionSpec
from src.content_processor import ProcessedContent, ProcessingError, process_article
from src.notes_generator import NotesConfig, NotesGenerationError, NotesGenerator, validate_notes, verify_provenance
from src.notes_model import DetailLevel, Notes
from src.word_generator import DEFAULT_OUTPUT_DIR, SaveResult, WordGenerationError, WordGenerator, safe_filename

logger = logging.getLogger(__name__)

APP_NAME = "NOTEFORGE AI"
DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"  # the model the Ollama integration was validated with
MAX_INPUT_FILE_BYTES = 10 * 1024 * 1024
BACK_WORDS = {"b", "back", "cancel", "q"}
#: The only environment variables the CLI ever reads (and only for the Ollama backend). Never OPENAI_*.
OLLAMA_ENV_KEYS = ("OLLAMA_BASE_URL", "OLLAMA_HOST", "OLLAMA_MODEL", "OLLAMA_NUM_CTX", "OLLAMA_TEMPERATURE",
                   "OLLAMA_THINK", "OLLAMA_KEEP_ALIVE", "OLLAMA_MAX_OUTPUT_TOKENS", "OLLAMA_TIMEOUT")

MENU = (
    "Generate notes from Substack URL",
    "Generate notes from HTML file",
    "Generate notes from text file",
    "Select AI backend",
    "View available models",
    "View generated documents",
    "Exit",
)


# =========================================================================== small helpers
class _Back(Exception):
    """The user asked to go back / cancel the current action."""


class SourceFileError(Exception):
    """A local input file could not be used (message is user-facing)."""


def format_duration(seconds: float) -> str:
    """'45s', '3m 05s' or '1h 02m 10s'."""
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s" if m else f"{s}s"


def read_source_file(path_text: str) -> tuple[str, str]:
    """Read a user-supplied HTML/text file. Returns ``(text, encoding)`` or raises :class:`SourceFileError`.

    Accepts quoted paths ("Copy as path" in Explorer), ``~``, and UTF-8/UTF-16/Windows-1252 files.
    Refuses directories, missing files, empty files, binary data and files over 10 MB.
    """
    raw = (path_text or "").strip().strip('"').strip("'").strip()
    if not raw:
        raise SourceFileError("No file path was entered.")
    path = Path(os.path.expanduser(raw))
    try:
        if not path.exists():
            raise SourceFileError(f"File not found: {path}")
        if path.is_dir():
            raise SourceFileError(f"That is a folder, not a file: {path}")
        size = path.stat().st_size
        if size == 0:
            raise SourceFileError(f"The file is empty: {path.name}")
        if size > MAX_INPUT_FILE_BYTES:
            raise SourceFileError(f"The file is too large ({size // (1024 * 1024)} MB; the limit is "
                                  f"{MAX_INPUT_FILE_BYTES // (1024 * 1024)} MB): {path.name}")
        data = path.read_bytes()
    except PermissionError as exc:
        raise SourceFileError(f"Permission denied reading {raw}.") from exc
    except OSError as exc:
        raise SourceFileError(f"Cannot read {raw}: {exc.strerror or exc}") from exc
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    if b"\x00" in data[:4096]:
        raise SourceFileError(f"{path.name} looks like a binary file, not HTML or text.")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8 (some characters replaced)"


def describe_error(exc: BaseException) -> str:
    """A plain-language, actionable message for any error the pipeline can raise."""
    if isinstance(exc, InvalidSourceError):
        return f"That input is not usable: {exc}"
    if isinstance(exc, PaywallError):
        return ("This article is behind a paywall and no public text could be read. NoteForge AI never bypasses "
                "paywalls. If you have legitimate access, save the page as an HTML file (menu option 2) or copy the "
                "text into a .txt file (menu option 3).")
    if isinstance(exc, ArticleInaccessibleError):
        return (f"The article is not publicly accessible: {exc} If you can read it in your browser, save the page "
                "as HTML (option 2) or copy the text into a .txt file (option 3).")
    if isinstance(exc, FetchError):
        return f"Could not download the article: {exc} Check your internet connection and the URL, then try again."
    if isinstance(exc, NoContentError):
        return f"No article content could be found: {exc}"
    if isinstance(exc, ExtractionError):
        return f"The article could not be extracted: {exc}"
    if isinstance(exc, ProcessingError):
        return f"The article content could not be processed: {exc}"
    if isinstance(exc, WordGenerationError):
        return f"The Word document could not be created: {exc}"
    if isinstance(exc, (NotesGenerationError, BackendError)):
        return f"Notes generation failed: {exc}"
    return f"Unexpected error ({type(exc).__name__}): {exc}"


def estimate_text(backend_kind: str, detail: DetailLevel, include_questions: bool) -> str:
    """Honest time guidance. Ollama on a CPU-only PC generated about 5 tokens per second in testing."""
    if backend_kind != "ollama":
        return "The local backend is fast; this should take a few seconds."
    ranges = {DetailLevel.BASIC: (8, 20), DetailLevel.INTERMEDIATE: (10, 30), DetailLevel.ADVANCED: (15, 40)}
    lo, hi = ranges[detail]
    if not include_questions:
        lo, hi = max(3, lo // 3), max(8, hi // 2)
    return (f"Ollama writes about 5 words per second on a CPU-only PC, so {detail.value} notes"
            f"{'' if include_questions else ' without questions'} may take roughly {lo}-{hi}+ minutes. "
            "Requests run one at a time. Press Ctrl+C to cancel.")


class _Heartbeat:
    """Prints 'still working' lines from a helper thread during long operations (not for fast ones)."""

    def __init__(self, out: Callable[[str], None], interval: Optional[float], label: str,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.out, self.interval, self.label, self.clock = out, interval, label, clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "_Heartbeat":
        if self.interval and self.interval > 0:
            started = self.clock()

            def beat() -> None:
                while not self._stop.wait(self.interval):  # type: ignore[arg-type]
                    self.out(f"      ... still {self.label} ({format_duration(self.clock() - started)} elapsed, "
                             "Ctrl+C to cancel)")

            self._thread = threading.Thread(target=beat, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


# A UTF-8 byte-order mark is U+FEFF when decoded as UTF-8, or three odd Latin-1 characters when a legacy code
# page decoded it. Windows PowerShell 5.1 puts one at the very start of text piped to a program.
_BOM_PREFIXES = ("\ufeff", "\xef\xbb\xbf")


def strip_bom(text: str) -> str:
    """Remove leading byte-order marks from one line of typed or piped input; anything else is returned unchanged."""
    changed = True
    while changed and text:
        changed = False
        for prefix in _BOM_PREFIXES:
            if text.startswith(prefix):
                text, changed = text[len(prefix):], True
    return text


@dataclass
class Settings:
    backend: str = "local"  # "local" (default) or "ollama"
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    detail: DetailLevel = DetailLevel.INTERMEDIATE
    include_questions: bool = True


@dataclass
class RunResult:
    """What the last successful run produced (used by tests and for the summary)."""

    article: Optional[Article] = None
    content: Optional[ProcessedContent] = None
    notes: Optional[Notes] = None
    save: Optional[SaveResult] = None
    selection: Optional[BackendSelection] = None
    timings: dict[str, float] = field(default_factory=dict)


# =========================================================================== the application
class App:
    """The interactive program. ``input_fn`` / ``output_fn`` and the service factories are injectable."""

    def __init__(
        self,
        *,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
        output_dir: Optional[Path] = None,
        resolver: Callable[..., BackendSelection] = resolve_backend,
        extractor_factory: Callable[[], SubstackExtractor] = SubstackExtractor,
        env_source: Optional[Mapping[str, str]] = None,
        models_fn: Optional[Callable[[str], list[dict[str, Any]]]] = None,
        opener: Optional[Callable[[Path], None]] = None,
        heartbeat_seconds: Optional[float] = 60.0,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.input_fn: Callable[[str], str] = lambda prompt: strip_bom(input_fn(prompt))
        self.out = output_fn
        self.output_dir = Path(output_dir) if output_dir is not None else DEFAULT_OUTPUT_DIR
        self.resolver, self.extractor_factory = resolver, extractor_factory
        self.env_source = env_source if env_source is not None else os.environ
        self.models_fn = models_fn or self._fetch_ollama_models
        self.opener = opener or self._open_file
        self.heartbeat_seconds, self.clock = heartbeat_seconds, clock
        self.settings = Settings()
        env_model = self._ollama_env().get("OLLAMA_MODEL", "").strip()
        if env_model:  # D-2: the documented variable sets the starting model; the menu can still change it
            self.settings.ollama_model = env_model
        self.last = RunResult()

    # ------------------------------------------------------------------ prompts
    def ask(self, prompt: str, default: Optional[str] = None, *, allow_back: bool = True) -> str:
        suffix = f" [{default}]" if default else ""
        raw = self.input_fn(f"{prompt}{suffix}: ").strip()
        if allow_back and raw.lower() in BACK_WORDS:
            raise _Back()
        return raw if raw else (default or "")

    def confirm(self, prompt: str, default: bool) -> bool:
        hint = "Y/n" if default else "y/N"
        for _ in range(5):
            raw = self.input_fn(f"{prompt} [{hint}]: ").strip().lower()
            if raw in ("", "y", "yes", "n", "no"):
                return default if raw == "" else raw.startswith("y")
            self.out("      Please answer y or n.")
        return default

    def say(self, text: str = "") -> None:
        self.out(text)

    # ------------------------------------------------------------------ screens
    def banner(self) -> None:
        self.say("")
        self.say("=" * 60)
        self.say(f"  {APP_NAME}   v{__version__}")
        self.say("  Turn articles into professional Word study notes")
        self.say("=" * 60)

    def status_line(self) -> str:
        s = self.settings
        backend = "Local (free, offline)" if s.backend == "local" else f"Ollama ({s.ollama_model})"
        return f"Backend: {backend} | Detail: {s.detail.value} | Output: {self.output_dir}"

    def show_menu(self) -> None:
        self.say("")
        self.say(self.status_line())
        self.say("")
        for number, label in enumerate(MENU, start=1):
            self.say(f"  {number}. {label}.")
        self.say("")

    # ------------------------------------------------------------------ main loop
    def run(self) -> int:
        handler = self._attach_log_file()
        try:
            self.banner()
            while True:
                self.show_menu()
                try:
                    choice = self.input_fn("Choose an option (1-7): ").strip().lower()
                except KeyboardInterrupt:
                    self.say("\nInterrupted. Goodbye.")
                    return 130
                except EOFError:
                    self.say("\nNo more input. Goodbye.")
                    return 0
                if choice in ("7", "exit", "quit"):
                    self.say("Goodbye.")
                    return 0
                if not choice:
                    continue
                action = {"1": self.action_url, "2": self.action_html, "3": self.action_text, "4": self.action_backend,
                          "5": self.action_models, "6": self.action_documents}.get(choice)
                if action is None:
                    self.say(f"'{choice}' is not a menu option. Please enter a number from 1 to 7.")
                    continue
                try:
                    action()
                except _Back:
                    self.say("Cancelled. Back to the main menu.")
                except KeyboardInterrupt:
                    self.say("\nCancelled by Ctrl+C. No document was created. Back to the main menu.")
                except EOFError:
                    self.say("\nNo more input. Goodbye.")
                    return 0
                except Exception as exc:  # last line of defence: a bug must not kill the whole session
                    logger.exception("Unexpected error in menu action %s", choice)
                    self.say(f"[!] {describe_error(exc)}")
                    self.say("    Details were written to the log file. Back to the main menu.")
        finally:
            self._detach_log_file(handler)

    def _attach_log_file(self) -> Optional[logging.Handler]:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(self.output_dir / "noteforge.log", encoding="utf-8")
        except OSError:
            return None
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        if root.level in (logging.NOTSET, logging.WARNING):
            root.setLevel(logging.INFO)
        return handler

    @staticmethod
    def _detach_log_file(handler: Optional[logging.Handler]) -> None:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()

    # ------------------------------------------------------------------ options 1-3: sources
    def action_url(self) -> None:
        self.say("\nGenerate notes from a Substack URL  (type 'b' at any prompt to go back)")
        url = ""
        for attempt in range(3):
            candidate = self.ask("Article URL (for example https://name.substack.com/p/article-title)", allow_back=True)
            try:
                url = validate_url(candidate)
                if not is_post_url(url):
                    raise InvalidSourceError("it does not look like a Substack post (expected a path like /p/<article-title>)")
                break
            except InvalidSourceError as exc:
                self.say(f"[!] {describe_error(exc)}")
                if attempt == 2:
                    self.say("    Too many invalid attempts. Back to the main menu.")
                    return
        self._pipeline(lambda ex: ex.extract_from_url(url), f"URL {url}")

    def action_html(self) -> None:
        self.say("\nGenerate notes from a saved HTML file  (type 'b' to go back)")
        text = self._read_file_prompt("Path to the HTML file (tip: 'Save page as...' in your browser)")
        url = self._optional_url()
        self._pipeline(lambda ex: ex.extract_from_html(text, url=url), "HTML file")

    def action_text(self) -> None:
        self.say("\nGenerate notes from a plain-text file  (type 'b' to go back)")
        text = self._read_file_prompt("Path to the text file")
        title = self.ask("Title (optional, Enter to detect it)", default="") or None
        author = self.ask("Author (optional)", default="") or None
        published: Optional[date] = None
        while True:
            raw = self.ask("Publication date YYYY-MM-DD (optional)", default="")
            if not raw:
                break
            try:
                published = date.fromisoformat(raw)
                break
            except ValueError:
                self.say("[!] Please use the format YYYY-MM-DD (for example 2025-03-14), or press Enter to skip.")
        url = self._optional_url()
        self._pipeline(lambda ex: ex.extract_from_text(text, title=title, author=author, published=published, url=url),
                       "text file")

    def _read_file_prompt(self, prompt: str) -> str:
        for attempt in range(3):
            path = self.ask(prompt)
            try:
                text, encoding = read_source_file(path)
                self.say(f"      Read {len(text):,} characters ({encoding}).")
                return text
            except SourceFileError as exc:
                self.say(f"[!] {exc}")
        self.say("    Too many attempts.")
        raise _Back()

    def _optional_url(self) -> Optional[str]:
        while True:
            raw = self.ask("Original article URL (optional, shown in the notes as the reference)", default="")
            if not raw:
                return None
            try:
                return validate_url(raw)
            except InvalidSourceError as exc:
                self.say(f"[!] {describe_error(exc)} Enter a valid URL, or press Enter to skip.")

    # ------------------------------------------------------------------ option 4: backend
    def _ollama_env(self) -> dict[str, str]:
        """Only the documented OLLAMA_* variables, read one by one. Nothing else in the environment is looked at."""
        env: dict[str, str] = {}
        for key in OLLAMA_ENV_KEYS:
            value = self.env_source.get(key)
            if value:
                env[key] = value
        return env

    def action_backend(self) -> None:
        s = self.settings
        self.say("\nSelect the notes backend  (Enter keeps the current choice, 'b' goes back)")
        self.say("")
        self.say(f"  1. Local   - free, offline, no AI model. Quotes and reorganises the article.  {'<- current' if s.backend == 'local' else ''}".rstrip())
        self.say(f"  2. Ollama  - a language model running on this PC (model: {s.ollama_model}). Adds explanations,")
        self.say(f"               analogies and AI-written questions, always labelled. Slower.  {'<- current' if s.backend == 'ollama' else ''}".rstrip())
        self.say("")
        self.say("  (Paid cloud services are not available from this menu.)")
        choice = self.ask("Backend (1-2)", default="").strip().lower()
        if not choice:
            return
        if choice in ("1", "local"):
            s.backend = "local"
            self.say("Local backend selected (this is the default).")
        elif choice in ("2", "ollama"):
            s.backend = "ollama"
            self.say(f"Ollama selected, model {s.ollama_model}.")
            self._report_ollama_status()
        else:
            self.say(f"'{choice}' is not a valid choice; the backend was not changed.")

    def _report_ollama_status(self) -> None:
        try:
            selection = self.resolver("ollama", env=self._ollama_env(), ollama_model=self.settings.ollama_model)
        except Exception as exc:  # noqa: BLE001
            self.say(f"[!] Could not check Ollama: {exc}")
            return
        if selection.fell_back:
            self.say(f"[!] Ollama is not usable right now: {selection.fallback_reason}")
            self.say("    You can still keep it selected; you will be asked before anything falls back to Local.")
        else:
            self.say("      Ollama is running and the model is installed.")

    # ------------------------------------------------------------------ option 5: models
    def _fetch_ollama_models(self, base_url: str) -> list[dict[str, Any]]:
        import requests

        try:
            resp = requests.get(f"{base_url}/api/tags", timeout=4)
            resp.raise_for_status()
            data = resp.json()
        except requests.ConnectionError as exc:
            raise BackendError(f"Ollama is not running at {base_url}. Start the Ollama app (or run `ollama serve`).") from exc
        except (requests.RequestException, ValueError) as exc:
            raise BackendError(f"Ollama did not answer properly at {base_url} ({type(exc).__name__}).") from exc
        if not isinstance(data, dict) or not isinstance(data.get("models", []), list):
            raise BackendError(f"Something else answered at {base_url}; it does not look like Ollama.")
        return [m for m in data.get("models", []) if isinstance(m, dict)]

    def action_models(self) -> None:
        self.say("\nAvailable models")
        self.say("  Local backend : no model needed (rule-based, always available).")
        try:
            base_url = OllamaSettings.from_env(self._ollama_env()).base_url
            models = self.models_fn(base_url)
        except BackendError as exc:
            self.say(f"  Ollama        : [!] {exc}")
            self.say(f"                  (After starting it, install the recommended model with: ollama pull {DEFAULT_OLLAMA_MODEL})")
            return
        if not models:
            self.say("  Ollama        : running, but no models are installed.")
            self.say(f"                  Install the recommended one with: ollama pull {DEFAULT_OLLAMA_MODEL}")
            return
        self.say(f"  Ollama        : {len(models)} model(s) installed at {base_url}")
        names = []
        for i, m in enumerate(models, start=1):
            name = str(m.get("name", "?"))
            names.append(name)
            details = m.get("details") or {}
            size = f"{(m.get('size') or 0) / 1e9:.1f} GB"
            extra = ", ".join(str(x) for x in (details.get("parameter_size"), details.get("quantization_level")) if x)
            mark = "  <- selected" if name == self.settings.ollama_model else ""
            self.say(f"     {i}. {name:<26} {size:>8}  {extra}{mark}")
        if DEFAULT_OLLAMA_MODEL not in names:
            self.say(f"  Note: the validated model {DEFAULT_OLLAMA_MODEL} is not installed (ollama pull {DEFAULT_OLLAMA_MODEL}).")
        pick = self.ask("Enter a number to use that model for Ollama, or press Enter to go back", default="")
        if not pick:
            return
        if pick.isdigit() and 1 <= int(pick) <= len(names):
            self.settings.ollama_model = names[int(pick) - 1]
            self.say(f"Ollama model set to {self.settings.ollama_model}. (Select the Ollama backend with option 4 to use it.)")
        else:
            self.say(f"'{pick}' is not in the list; the model was not changed.")

    # ------------------------------------------------------------------ option 6: documents
    def list_documents(self) -> list[Path]:
        if not self.output_dir.is_dir():
            return []
        return sorted((p for p in self.output_dir.glob("*.docx") if p.is_file() and not p.name.startswith("~$")),
                      key=lambda p: p.stat().st_mtime, reverse=True)

    def action_documents(self) -> None:
        docs = self.list_documents()
        self.say(f"\nGenerated documents in {self.output_dir}")
        if not docs:
            self.say("  (none yet)")
            return
        for i, p in enumerate(docs, start=1):
            st = p.stat()
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            self.say(f"  {i:>2}. {p.name}   {st.st_size / 1024:.0f} KB   {stamp}")
        pick = self.ask("Enter a number to open that document, or press Enter to go back", default="")
        if not pick:
            return
        if pick.isdigit() and 1 <= int(pick) <= len(docs):
            target = docs[int(pick) - 1]
            self.say(f"Opening {target} ...")
            try:
                self.opener(target)
            except Exception as exc:  # noqa: BLE001
                self.say(f"[!] Could not open it automatically ({exc}). Full path: {target}")
        else:
            self.say(f"'{pick}' is not in the list.")

    @staticmethod
    def _open_file(path: Path) -> None:
        if hasattr(os, "startfile"):
            os.startfile(str(path))  # type: ignore[attr-defined]  # Windows: default application
        else:
            raise OSError("automatic opening is only supported on Windows")

    # ------------------------------------------------------------------ the end-to-end pipeline
    def _choose_detail(self) -> DetailLevel:
        levels = list(DetailLevel)
        self.say("\nNotes detail level:")
        for i, lv in enumerate(levels, start=1):
            self.say(f"  {i}. {lv.value}{'  <- current' if lv is self.settings.detail else ''}")
        while True:
            raw = self.ask("Detail (1-3 or name)", default=self.settings.detail.value)
            try:
                chosen = levels[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= 3 else DetailLevel.parse(raw)
                self.settings.detail = chosen
                return chosen
            except ValueError:
                self.say("[!] Please enter 1, 2, 3, Basic, Intermediate or Advanced.")

    def _select_backend(self) -> BackendSelection:
        """Resolve the chosen backend, and ask before falling back from Ollama to Local."""
        s = self.settings
        if s.backend == "local":
            return self.resolver("local", env={})
        selection = self.resolver("ollama", env=self._ollama_env(), ollama_model=s.ollama_model)
        if selection.fell_back:
            self.say(f"[!] Ollama cannot be used: {selection.fallback_reason}")
            if not self.confirm("    Continue with the Local backend instead?", default=True):
                raise _Back()
        return selection

    def _pipeline(self, load: Callable[[SubstackExtractor], Article], source_label: str) -> None:
        s = self.settings
        detail = self._choose_detail()
        s.include_questions = self.confirm("Include questions (10 short-answer, 5 descriptive, 10 multiple-choice)?",
                                           default=s.include_questions)
        timings: dict[str, float] = {}
        selection = self._select_backend()
        info = selection.backend.info
        kind = info.kind.value
        self.say("")
        self.say(f"Backend : {info.name}{f' (model {info.model})' if info.model else ''} - "
                 f"{'free, runs on this machine' if info.is_local and not info.is_paid else 'EXTERNAL / PAID'}")
        self.say(f"Detail  : {detail.value}   Questions: {'yes' if s.include_questions else 'no'}   Source: {source_label}")
        self.say(estimate_text(kind, detail, s.include_questions))
        if kind == "ollama" and not self.confirm("Start generating now?", default=True):
            raise _Back()

        t0 = self.clock()
        self.say("\n[1/6] Reading and extracting the article ...")
        try:
            article = load(self.extractor_factory())
        except ExtractionError as exc:
            self.say(f"[!] {describe_error(exc)}")
            return
        timings["extract"] = self.clock() - t0
        self._show_extraction(article)
        if article.completeness is Completeness.PARTIAL and not self.confirm(
                "    Only part of the article is available. Continue with the accessible part?", default=True):
            raise _Back()

        self.say("[2/6] Processing the content ...")
        try:
            content = process_article(article)
        except ProcessingError as exc:
            self.say(f"[!] {describe_error(exc)}")
            return
        self.say(f"      {sum(1 for _ in content.iter_sections())} sections, {len(content.definitions)} definitions, "
                 f"{len(content.keywords)} key terms found.")

        self.say(f"[3/6] Writing the notes with {info.name} ...")
        config = NotesConfig(detail=detail, include_questions=s.include_questions)
        t1 = self.clock()
        try:
            with _Heartbeat(self.say, self.heartbeat_seconds if kind == "ollama" else None, "generating", self.clock):
                notes = NotesGenerator(selection, config).generate(content)
        except (NotesGenerationError, BackendError) as exc:
            self.say(f"[!] {describe_error(exc)}")
            return
        timings["notes"] = self.clock() - t1
        self._show_notes_summary(notes, timings["notes"])

        self.say("[4/6] Checking the notes (structure and provenance) ...")
        spec = (QuestionSpec(config.short_questions, config.descriptive_questions, config.mcq_questions)
                if s.include_questions else None)
        problems = validate_notes(notes, spec) + verify_provenance(notes, content)
        if problems:
            self.say(f"[!] {len(problems)} problem(s) found in the generated notes:")
            for p in problems[:5]:
                self.say(f"      - {p}")
            if not self.confirm("    Create the Word document anyway?", default=False):
                self.say("    Not created.")
                return
        else:
            self.say("      OK: every quoted passage is from the article and the structure is valid.")

        self.say("[5/6] Creating the Word document ...")
        t2 = self.clock()
        try:
            result = WordGenerator().save(notes, self.output_dir, confirm_overwrite=self._confirm_overwrite)
        except WordGenerationError as exc:
            self.say(f"[!] {describe_error(exc)}")
            self._offer_json_backup(notes)
            return
        timings["word"] = self.clock() - t2

        self.say("[6/6] Done.\n")
        self._show_result(notes, result, timings, self.clock() - t0)
        self.last = RunResult(article, content, notes, result, selection, timings)

    def _confirm_overwrite(self, path: Path) -> bool:
        self.say(f"      A document named {path.name} already exists.")
        return self.confirm("      Overwrite it? (No saves under a new name instead)", default=False)

    # ------------------------------------------------------------------ result displays
    def _show_extraction(self, article: Article) -> None:
        labels = {Completeness.COMPLETE: "COMPLETE", Completeness.PARTIAL: "PARTIAL", Completeness.UNVERIFIED: "NOT VERIFIED"}
        self.say(f"      Title     : {article.title}")
        self.say(f"      Author    : {article.author or 'unknown'}   Published: {article.published or 'unknown'}")
        self.say(f"      Extracted : {labels[article.completeness]} - {article.word_count:,} words in {len(article.blocks)} blocks "
                 f"({article.extractor})")
        if article.completeness is Completeness.PARTIAL:
            self.say("      [!] PARTIAL CONTENT: the notes will cover only the accessible part, and say so on every page.")
            for reason in article.completeness_reasons[:3]:
                self.say(f"          - {reason}")
        elif article.completeness is Completeness.UNVERIFIED:
            self.say("      Note: completeness of supplied text cannot be verified.")
        for warning in article.warnings[:4]:
            self.say(f"      Note: {warning}")

    def _show_notes_summary(self, notes: Notes, seconds: float) -> None:
        questions = sum(len(c.questions) for c in notes.chapters)
        words = sum(c.word_count for c in notes.chapters)
        self.say(f"      Notes written in {format_duration(seconds)}: {words:,} words, {questions} questions"
                 f"{', includes AI-generated text (labelled)' if notes.used_ai else ''}.")
        if notes.fallback_reason:
            self.say(f"      [!] The requested backend was not fully used: {notes.fallback_reason}")
        for w in notes.generation_warnings[:6]:
            self.say(f"      Note: {w}")

    def _show_result(self, notes: Notes, result: SaveResult, timings: dict[str, float], total: float) -> None:
        self.say("-" * 60)
        self.say("Your document is ready:")
        self.say(f"  {result.path.resolve()}")
        if result.renamed:
            self.say(f"  (saved under a new name; {result.requested_path.name} already existed and was not touched)")
        if result.overwritten:
            self.say("  (replaced the existing file, as you confirmed)")
        self.say(f"  File name : {result.path.name}   Size: {result.path.stat().st_size / 1024:.0f} KB")
        self.say(f"  Made with : {notes.backend.name}{f' ({notes.backend.model})' if notes.backend.model else ''}"
                 f" | Source status: {notes.completeness.value.upper()}")
        self.say(f"  Time      : extract {format_duration(timings.get('extract', 0))}, notes {format_duration(timings.get('notes', 0))}, "
                 f"Word {format_duration(timings.get('word', 0))} - total {format_duration(total)}")
        if result.problems:
            self.say(f"  [!] {len(result.problems)} completeness problem(s) found when re-reading the file:")
            for p in result.problems[:3]:
                self.say(f"      - {p}")
        if result.sanitized_chars:
            self.say(f"  Note: {result.sanitized_chars} invalid control character(s) were removed from the text.")
        if notes.is_partial:
            self.say("  [!] Reminder: these notes are based on PARTIAL article content.")
        self.say("-" * 60)

    def _offer_json_backup(self, notes: Notes) -> None:
        if not self.confirm("    Save the generated notes as a JSON file so the work is not lost?", default=True):
            return
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            base = safe_filename(notes.title, ext=".json")
            stem = base[: -len(".json")]
            for counter in range(1, 1000):
                path = self.output_dir / (base if counter == 1 else f"{stem}_{counter}.json")
                try:
                    with open(path, "x", encoding="utf-8") as handle:
                        json.dump(notes.to_dict(), handle, ensure_ascii=False, indent=1)
                    self.say(f"      Notes saved to {path.resolve()}")
                    return
                except FileExistsError:
                    continue
            self.say("[!] Could not find a free file name for the backup.")
        except OSError as exc:
            self.say(f"[!] The backup could not be saved either: {exc}")


# =========================================================================== entry point
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python src/main.py", description="NoteForge AI: turn articles into Word study notes (interactive).")
    p.add_argument("--version", action="version", version=f"NoteForge AI {__version__}")
    p.add_argument("--output-dir", type=Path, default=None, help="folder for generated documents (default: the project's output/ folder)")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr, sys.stdin):  # a legacy console must never crash on a non-ASCII title
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    try:
        return App(output_dir=args.output_dir).run()
    except KeyboardInterrupt:
        print("\nInterrupted. Goodbye.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
