"""Ollama integration tests (all mocked): settings, requests, errors, concurrency, generator flow.

The real-model checks are in ``scripts``-style runs reported separately; nothing here touches the
network. A fake Ollama server stands in for ``requests.Session`` and records exactly what NoteForge
would send, so the request format is verified field by field.
"""
from __future__ import annotations

import copy
import logging
import socket
import threading
import time
from typing import Any, Callable, Optional

import pytest
import requests

import src.backends.ollama as ollama_mod
from src.backends import LocalNotesBackend, OllamaBackend, OllamaSettings, resolve_backend
from src.backends.base import (
    BackendError, BackendResponseError, BackendUnavailableError, GenerationParams, QuestionSpec, TermPacket,
)
from src.backends.llm import SCHEMAS, LLMBackend
from src.backends.ollama import DEFAULT_BASE_URL, estimate_tokens, normalize_base_url
from src.notes_generator import NotesConfig, NotesGenerator, validate_notes, verify_provenance
from tests.helpers import ScriptedLLM, fixture_content, guess_purpose

PARAMS = GenerationParams()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*a: Any, **k: Any) -> None:
        raise AssertionError("a real network connection was attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture(scope="module")
def free():
    return fixture_content("substack_free.html")


# ============================================================================ a fake Ollama server
class FakeResp:
    def __init__(self, payload: Any = None, status: int = 200, text: str = "") -> None:
        self._payload, self.status_code, self.text = payload, status, text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def ok_reply(content: str, **extra: Any) -> dict[str, Any]:
    base = {"model": "qwen3.5:4b", "message": {"role": "assistant", "content": content}, "done": True,
            "done_reason": "stop", "total_duration": 9_000_000_000, "load_duration": 1_000_000_000,
            "prompt_eval_count": 200, "prompt_eval_duration": 2_000_000_000, "eval_count": 60,
            "eval_duration": 6_000_000_000}
    base.update(extra)
    return base


def model_reply(body: dict[str, Any]) -> FakeResp:
    """Answer like a model would, based on which task the prompt belongs to."""
    user = body["messages"][1]["content"]
    return FakeResp(ok_reply(ScriptedLLM.default(guess_purpose(user), user)))


class FakeOllama:
    """Stands in for requests.Session. Records every call and how many ran at the same time."""

    def __init__(self, models: tuple[str, ...] = ("qwen3.5:4b",), chat: Optional[Callable[[dict], Any]] = None,
                 delay: float = 0.0, get_error: Optional[Exception] = None) -> None:
        self.models, self.chat, self.delay, self.get_error = models, chat or model_reply, delay, get_error
        self.gets: list[str] = []
        self.posts: list[dict[str, Any]] = []
        self.active = self.max_active = 0
        self._guard = threading.Lock()

    def get(self, url: str, **kw: Any) -> FakeResp:
        self.gets.append(url)
        if self.get_error:
            raise self.get_error
        return FakeResp({"models": [{"name": m} for m in self.models]})

    def post(self, url: str, json: Any = None, **kw: Any) -> FakeResp:
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.posts.append({"url": url, "body": copy.deepcopy(json), "kw": kw})  # a snapshot, as a server would see it
            if self.delay:
                time.sleep(self.delay)
            result = self.chat(json)
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            with self._guard:
                self.active -= 1


def backend(fake: FakeOllama, **kw: Any) -> OllamaBackend:
    kw.setdefault("model", "qwen3.5:4b")
    return OllamaBackend(session=fake, **kw)  # type: ignore[arg-type]


# ============================================================================ settings
class TestSettingsDefaults:
    def test_defaults_match_the_requested_configuration(self) -> None:
        s = OllamaSettings()
        assert s.base_url == "http://localhost:11434" == DEFAULT_BASE_URL
        assert s.num_ctx == 8192 and s.think is False and s.keep_alive == "10m"
        assert 0 <= s.temperature <= 0.3 and s.temperature == 0.2
        assert s.model is None and s.allow_remote is False and s.max_output_tokens == 1500

    def test_settings_are_immutable(self) -> None:
        with pytest.raises(Exception):
            OllamaSettings().num_ctx = 1  # type: ignore[misc]


class TestSettingsValidation:
    @pytest.mark.parametrize("kwargs", [
        {"num_ctx": 1024}, {"num_ctx": 100000}, {"num_ctx": 8192.5}, {"num_ctx": True}, {"num_ctx": "8192"},
        {"temperature": -0.1}, {"temperature": 0.31}, {"temperature": 1.0}, {"temperature": "0.2"}, {"temperature": True},
        {"max_output_tokens": 10}, {"max_output_tokens": 9999}, {"max_output_tokens": 5000, "num_ctx": 8192},
        {"max_output_tokens": 4096, "num_ctx": 8192},
        {"keep_alive": "ten minutes"}, {"keep_alive": ""}, {"keep_alive": 10}, {"keep_alive": "10x"},
        {"timeout": 0}, {"timeout": -5}, {"timeout": "soon"},
        {"think": "false"}, {"think": 0},
    ])
    def test_rejected(self, kwargs: dict) -> None:
        with pytest.raises(BackendError):
            OllamaSettings(**kwargs)

    @pytest.mark.parametrize("kwargs", [
        {"num_ctx": 2048, "max_output_tokens": 512}, {"num_ctx": 32768}, {"temperature": 0}, {"temperature": 0.3},
        {"keep_alive": "30s"}, {"keep_alive": "1h"}, {"keep_alive": "0"}, {"keep_alive": "-1"}, {"keep_alive": "500ms"},
        {"think": None}, {"think": True}, {"timeout": 1}, {"max_output_tokens": 64},
    ])
    def test_accepted(self, kwargs: dict) -> None:
        OllamaSettings(**kwargs)

    def test_temperature_error_explains_why(self) -> None:
        with pytest.raises(BackendError, match="invented details"):
            OllamaSettings(temperature=0.8)


class TestNormalizeBaseUrl:
    @pytest.mark.parametrize("raw,expected", [
        (None, "http://localhost:11434"), ("", "http://localhost:11434"), ("   ", "http://localhost:11434"),
        ("http://localhost:11434", "http://localhost:11434"), ("http://localhost:11434/", "http://localhost:11434"),
        ("localhost:11434", "http://localhost:11434"), ("localhost", "http://localhost:11434"),
        ("127.0.0.1", "http://127.0.0.1:11434"), ("127.0.0.1:9999", "http://127.0.0.1:9999"),
        ("0.0.0.0:11434", "http://127.0.0.1:11434"), ("0.0.0.0", "http://127.0.0.1:11434"),
        ("http://localhost:11434/api", "http://localhost:11434"), ("http://localhost:11434/api/", "http://localhost:11434"),
        ("https://example.com:8443", "https://example.com:8443"), ("[::1]:11434", "http://[::1]:11434"),
        ("  http://localhost:1234  ", "http://localhost:1234"),
    ])
    def test_valid(self, raw: Optional[str], expected: str) -> None:
        assert normalize_base_url(raw) == expected

    @pytest.mark.parametrize("raw", ["ftp://localhost", "http://", "http://localhost:notaport", "file:///x", "://x"])
    def test_invalid(self, raw: str) -> None:
        with pytest.raises(BackendError):
            normalize_base_url(raw)


class TestBaseUrlVersusHostMismatch:
    """The configuration mismatch: NoteForge's docs say OLLAMA_BASE_URL, Ollama itself uses OLLAMA_HOST."""

    def test_base_url_is_used(self) -> None:
        assert OllamaSettings.from_env({"OLLAMA_BASE_URL": "http://localhost:5555"}).base_url == "http://localhost:5555"

    def test_ollama_host_still_works_as_a_fallback(self) -> None:
        assert OllamaSettings.from_env({"OLLAMA_HOST": "http://localhost:6666"}).base_url == "http://localhost:6666"

    def test_ollamas_own_host_format_is_understood(self) -> None:
        assert OllamaSettings.from_env({"OLLAMA_HOST": "0.0.0.0:11434"}).base_url == "http://127.0.0.1:11434"
        assert OllamaSettings.from_env({"OLLAMA_HOST": "127.0.0.1"}).base_url == "http://127.0.0.1:11434"

    def test_base_url_wins_when_both_are_set_and_differ(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING)
        s = OllamaSettings.from_env({"OLLAMA_BASE_URL": "http://localhost:1111", "OLLAMA_HOST": "http://localhost:2222"})
        assert s.base_url == "http://localhost:1111" and "OLLAMA_BASE_URL" in caplog.text

    def test_no_warning_when_both_agree(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING)
        OllamaSettings.from_env({"OLLAMA_BASE_URL": "http://localhost:11434", "OLLAMA_HOST": "localhost:11434"})
        assert caplog.text == ""

    def test_default_when_neither_is_set(self) -> None:
        assert OllamaSettings.from_env({}).base_url == DEFAULT_BASE_URL

    def test_blank_values_are_ignored(self) -> None:
        s = OllamaSettings.from_env({"OLLAMA_BASE_URL": "  ", "OLLAMA_HOST": "", "OLLAMA_MODEL": " "})
        assert s.base_url == DEFAULT_BASE_URL and s.model is None


class TestSettingsFromEnv:
    def test_every_variable_is_read(self) -> None:
        s = OllamaSettings.from_env({
            "OLLAMA_MODEL": "qwen3.5:4b", "OLLAMA_NUM_CTX": "4096", "OLLAMA_TEMPERATURE": "0.1", "OLLAMA_THINK": "false",
            "OLLAMA_KEEP_ALIVE": "30m", "OLLAMA_MAX_OUTPUT_TOKENS": "800", "OLLAMA_TIMEOUT": "120"})
        assert (s.model, s.num_ctx, s.temperature, s.think, s.keep_alive, s.max_output_tokens, s.timeout) == \
               ("qwen3.5:4b", 4096, 0.1, False, "30m", 800, 120.0)

    @pytest.mark.parametrize("value,expected", [("true", True), ("TRUE", True), ("1", True), ("on", True),
                                                ("false", False), ("0", False), ("no", False), ("off", False),
                                                ("default", None), ("auto", None), ("model", None)])
    def test_think_values(self, value: str, expected: Optional[bool]) -> None:
        assert OllamaSettings.from_env({"OLLAMA_THINK": value}).think is expected

    def test_invalid_think(self) -> None:
        with pytest.raises(BackendError, match="OLLAMA_THINK"):
            OllamaSettings.from_env({"OLLAMA_THINK": "maybe"})

    @pytest.mark.parametrize("var,value", [("OLLAMA_NUM_CTX", "abc"), ("OLLAMA_NUM_CTX", "8192.5"), ("OLLAMA_TEMPERATURE", "warm"),
                                           ("OLLAMA_MAX_OUTPUT_TOKENS", "lots"), ("OLLAMA_TIMEOUT", "never")])
    def test_non_numeric_values_name_the_variable(self, var: str, value: str) -> None:
        with pytest.raises(BackendError, match=var):
            OllamaSettings.from_env({var: value})

    def test_out_of_range_values_are_rejected(self) -> None:
        with pytest.raises(BackendError):
            OllamaSettings.from_env({"OLLAMA_TEMPERATURE": "0.9"})
        with pytest.raises(BackendError):
            OllamaSettings.from_env({"OLLAMA_NUM_CTX": "512"})

    def test_explicit_overrides_beat_the_environment(self) -> None:
        s = OllamaSettings.from_env({"OLLAMA_MODEL": "a", "OLLAMA_NUM_CTX": "4096"}, model="b", num_ctx=16384)
        assert s.model == "b" and s.num_ctx == 16384

    def test_none_overrides_are_ignored(self) -> None:
        assert OllamaSettings.from_env({"OLLAMA_MODEL": "a"}, model=None).model == "a"

    def test_empty_environment_gives_the_defaults(self) -> None:
        assert OllamaSettings.from_env({}) == OllamaSettings()


# ============================================================================ the request NoteForge sends
class TestRequestFormat:
    def test_exact_request_for_a_structured_task(self, free) -> None:
        fake = FakeOllama()
        backend(fake).explain_keywords(free, [TermPacket("chunk", None)], PARAMS)
        post = fake.posts[0]
        assert post["url"] == "http://localhost:11434/api/chat"
        body = post["body"]
        assert set(body) == {"model", "stream", "keep_alive", "format", "options", "messages", "think"}
        assert body["model"] == "qwen3.5:4b" and body["stream"] is False and body["keep_alive"] == "10m"
        assert body["think"] is False
        assert body["options"] == {"num_ctx": 8192, "temperature": 0.2, "num_predict": 1500}
        assert body["format"] == SCHEMAS["keyword explanations"]
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert post["kw"]["timeout"] == 900.0

    def test_thinking_is_disabled_at_the_top_level_not_in_options(self, free) -> None:
        fake = FakeOllama()
        backend(fake).explain_keywords(free, [TermPacket("chunk", None)], PARAMS)
        body = fake.posts[0]["body"]
        assert body["think"] is False and "think" not in body["options"]

    def test_think_can_be_left_to_the_model(self, free) -> None:
        fake = FakeOllama()
        backend(fake, think=None).explain_keywords(free, [TermPacket("chunk", None)], PARAMS)
        assert "think" not in fake.posts[0]["body"]

    def test_think_can_be_enabled_explicitly(self, free) -> None:
        fake = FakeOllama()
        backend(fake, think=True).explain_keywords(free, [TermPacket("chunk", None)], PARAMS)
        assert fake.posts[0]["body"]["think"] is True

    def test_plain_json_format_when_there_is_no_schema(self) -> None:
        fake = FakeOllama(chat=lambda b: FakeResp(ok_reply("{}")))
        backend(fake)._complete("sys", "user", "anything")
        assert fake.posts[0]["body"]["format"] == "json"

    def test_each_task_gets_its_own_schema(self, free) -> None:
        fake = FakeOllama()
        b = backend(fake)
        b.explain_keywords(free, [TermPacket("chunk", None)], PARAMS)
        b.create_questions(free, QuestionSpec(1, 1, 1), PARAMS)
        formats = [p["body"]["format"] for p in fake.posts]
        assert formats == [SCHEMAS["keyword explanations"], SCHEMAS["short questions"],
                           SCHEMAS["descriptive questions"], SCHEMAS["mcq questions"]]

    def test_custom_settings_reach_the_request(self, free) -> None:
        fake = FakeOllama()
        b = backend(fake, num_ctx=16384, temperature=0.05, keep_alive="1h", max_output_tokens=900, timeout=60)
        b.explain_keywords(free, [TermPacket("chunk", None)], PARAMS)
        body = fake.posts[0]["body"]
        assert body["options"] == {"num_ctx": 16384, "temperature": 0.05, "num_predict": 900}
        assert body["keep_alive"] == "1h" and fake.posts[0]["kw"]["timeout"] == 60

    def test_settings_object_is_accepted(self, free) -> None:
        fake = FakeOllama()
        OllamaBackend(settings=OllamaSettings(model="qwen3.5:4b", num_ctx=4096, max_output_tokens=500),
                      session=fake).explain_keywords(free, [TermPacket("chunk", None)], PARAMS)  # type: ignore[arg-type]
        assert fake.posts[0]["body"]["options"]["num_ctx"] == 4096

    def test_keyword_overrides_beat_the_settings_object(self) -> None:
        b = OllamaBackend(settings=OllamaSettings(num_ctx=4096), num_ctx=8192, model="m")
        assert b.settings.num_ctx == 8192 and b.model == "m"

    def test_legacy_host_argument_still_works(self) -> None:
        assert OllamaBackend(host="http://localhost:7777").host == "http://localhost:7777"
        assert OllamaBackend(base_url="localhost:8888").host == "http://localhost:8888"

    def test_no_secrets_or_auth_headers_are_sent(self, free) -> None:
        fake = FakeOllama()
        backend(fake).explain_keywords(free, [TermPacket("chunk", None)], PARAMS)
        assert set(fake.posts[0]["kw"]) == {"timeout"}  # no headers/auth at all
        assert "key" not in str(fake.posts[0]["body"]).lower().replace("keyword", "")

    def test_reply_content_is_returned_and_parsed(self, free) -> None:
        out = backend(FakeOllama()).explain_keywords(free, [TermPacket("chunk", None)], PARAMS)
        assert out["chunk"].explanation[0].text.startswith("In this article chunk is an idea")


# ============================================================================ think rejected by a model
class TestThinkNotSupported:
    def test_retried_once_without_think_then_remembered(self, free) -> None:
        calls = {"n": 0}

        def chat(body: dict) -> FakeResp:
            calls["n"] += 1
            if "think" in body:
                return FakeResp({"error": "\"llama3\" does not support thinking"}, status=400)
            return model_reply(body)

        fake = FakeOllama(chat=chat)
        b = backend(fake)
        b.explain_keywords(free, [TermPacket("chunk", None)], PARAMS)
        assert [("think" in p["body"]) for p in fake.posts] == [True, False]
        assert any("does not support the 'think' option" in w for w in b.drain_warnings())
        b.explain_keywords(free, [TermPacket("chunk", None)], PARAMS)
        assert "think" not in fake.posts[-1]["body"] and len(fake.posts) == 3  # remembered: no second retry

    def test_unrelated_400_errors_are_not_retried(self) -> None:
        fake = FakeOllama(chat=lambda b: FakeResp({"error": "invalid options"}, status=400))
        with pytest.raises(BackendError, match="HTTP 400: invalid options"):
            backend(fake)._complete("s", "u", "t")
        assert len(fake.posts) == 1


# ============================================================================ errors
class TestErrors:
    def call(self, fake: FakeOllama, **kw: Any) -> str:
        return backend(fake, **kw)._complete("sys", "user", "task")

    def test_ollama_not_running(self) -> None:
        fake = FakeOllama(chat=lambda b: requests.ConnectionError("refused"))
        with pytest.raises(BackendUnavailableError) as exc:
            self.call(fake)
        assert "not running" in str(exc.value) and "ollama serve" in str(exc.value)

    def test_model_not_found_says_how_to_install_it(self) -> None:
        fake = FakeOllama(chat=lambda b: FakeResp({"error": "model 'qwen3.5:4b' not found"}, status=404))
        with pytest.raises(BackendUnavailableError) as exc:
            self.call(fake)
        assert "not installed" in str(exc.value) and "ollama pull qwen3.5:4b" in str(exc.value)

    def test_a_404_for_something_else_is_not_misreported_as_a_missing_model(self) -> None:
        fake = FakeOllama(chat=lambda b: FakeResp(None, status=404, text="404 page not found"))
        with pytest.raises(BackendError) as exc:
            self.call(fake)
        assert not isinstance(exc.value, BackendUnavailableError) and "404" in str(exc.value)

    def test_timeout_explains_cpu_speed_and_the_remedy(self) -> None:
        fake = FakeOllama(chat=lambda b: requests.ReadTimeout("slow"))
        with pytest.raises(BackendError) as exc:
            self.call(fake, timeout=30)
        assert "did not finish within 30s" in str(exc.value) and "OLLAMA_MAX_OUTPUT_TOKENS" in str(exc.value)
        assert not isinstance(exc.value, BackendUnavailableError)

    def test_server_errors_include_the_servers_own_message(self) -> None:
        fake = FakeOllama(chat=lambda b: FakeResp({"error": "llama runner process has terminated"}, status=500))
        with pytest.raises(BackendError, match="HTTP 500: llama runner process has terminated"):
            self.call(fake)

    def test_error_without_a_body(self) -> None:
        fake = FakeOllama(chat=lambda b: FakeResp(ValueError("no json"), status=502, text=""))
        with pytest.raises(BackendError, match="HTTP 502"):
            self.call(fake)

    @pytest.mark.parametrize("reply", [FakeResp(ValueError("x")), FakeResp(["not", "a", "dict"]), FakeResp({"done": True}),
                                       FakeResp({"message": "str"}), FakeResp({"message": {"role": "assistant"}})])
    def test_malformed_replies(self, reply: FakeResp) -> None:
        with pytest.raises(BackendError):
            self.call(FakeOllama(chat=lambda b: reply))

    def test_other_transport_errors(self) -> None:
        fake = FakeOllama(chat=lambda b: requests.exceptions.ChunkedEncodingError("cut"))
        with pytest.raises(BackendError, match="ChunkedEncodingError"):
            self.call(fake)

    def test_answer_cut_off_by_the_token_limit(self) -> None:
        fake = FakeOllama(chat=lambda b: FakeResp(ok_reply('{"terms": [{"term": "x"', done_reason="length", eval_count=1500)))
        with pytest.raises(BackendResponseError, match="cut off after 1500 tokens"):
            self.call(fake)

    def test_empty_answer(self) -> None:
        with pytest.raises(BackendResponseError, match="empty answer"):
            self.call(FakeOllama(chat=lambda b: FakeResp(ok_reply(""))))

    def test_empty_answer_caused_by_thinking_gets_a_hint(self) -> None:
        reply = ok_reply("")
        reply["message"]["thinking"] = "Let me think about this..."
        with pytest.raises(BackendResponseError, match="OLLAMA_THINK=false"):
            self.call(FakeOllama(chat=lambda b: FakeResp(reply)))

    def test_no_model_selected(self) -> None:
        with pytest.raises(BackendUnavailableError, match="OLLAMA_MODEL"):
            OllamaBackend(session=FakeOllama())._complete("s", "u", "t")  # type: ignore[arg-type]

    def test_garbage_json_from_a_working_server(self, free) -> None:
        fake = FakeOllama(chat=lambda b: FakeResp(ok_reply("Sure! Here are your notes.")))
        with pytest.raises(BackendResponseError):
            backend(fake).explain_keywords(free, [TermPacket("chunk", None)], PARAMS)


# ============================================================================ availability
class TestAvailabilityMessages:
    def test_not_running(self) -> None:
        a = backend(FakeOllama(get_error=requests.ConnectionError("refused"))).check_available()
        assert not a.available and "not reachable" in a.reason and "does not appear to be running" in a.reason
        assert "ollama serve" in a.reason and "11434" in a.reason

    def test_timeout(self) -> None:
        a = backend(FakeOllama(get_error=requests.Timeout("slow"))).check_available()
        assert not a.available and "3 seconds" in a.reason

    def test_something_else_on_the_port(self) -> None:
        a = backend(FakeOllama(get_error=ValueError("<html>"))).check_available()
        assert not a.available and "something else" in a.reason

    def test_model_missing_names_the_fix_and_the_installed_models(self) -> None:
        a = backend(FakeOllama(models=("mistral:latest",)), model="qwen3.5:4b").check_available()
        assert not a.available and "ollama pull qwen3.5:4b" in a.reason and "mistral:latest" in a.reason

    def test_no_models_installed(self) -> None:
        a = backend(FakeOllama(models=()), model="x").check_available()
        assert not a.available and "ollama pull qwen3.5:4b" in a.reason

    def test_model_present(self) -> None:
        assert backend(FakeOllama()).check_available().available

    def test_unspecified_model_uses_the_first_installed_and_logs_it(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO)
        b = OllamaBackend(session=FakeOllama(models=("qwen3.5:4b", "other")))  # type: ignore[arg-type]
        assert b.check_available().available and b.model == "qwen3.5:4b" and "first installed model" in caplog.text

    def test_success_is_cached_but_failure_is_not(self) -> None:
        fake = FakeOllama(get_error=requests.ConnectionError("down"))
        b = backend(fake)
        assert not b.check_available().available and not b.check_available().available
        assert len(fake.gets) == 2  # a failure is re-checked: the user may start Ollama in between
        fake.get_error = None
        assert b.check_available().available and b.check_available().available
        assert len(fake.gets) == 3  # a success is remembered

    def test_availability_check_never_generates(self) -> None:
        fake = FakeOllama()
        backend(fake).check_available()
        assert fake.posts == []


# ============================================================================ context safety
class TestContextWindowProtection:
    def test_oversized_prompt_is_refused_before_anything_is_sent(self) -> None:
        fake = FakeOllama()
        b = backend(fake, num_ctx=2048, max_output_tokens=256)
        with pytest.raises(BackendResponseError, match="too large for the model's context window"):
            b._complete("sys", "word " * 4000, "t")
        assert fake.posts == []  # Ollama would have silently truncated it; we never send it

    def test_a_prompt_that_fits_is_sent(self) -> None:
        fake = FakeOllama(chat=lambda b: FakeResp(ok_reply("{}")))
        backend(fake, num_ctx=2048, max_output_tokens=256)._complete("sys", "word " * 400, "t")
        assert len(fake.posts) == 1

    def test_non_latin_text_is_counted_pessimistically(self) -> None:
        assert estimate_tokens("abc" * 30) == 30
        assert estimate_tokens("తెలుగు" * 10) == 60  # one token per character
        fake = FakeOllama()
        with pytest.raises(BackendResponseError):
            backend(fake, num_ctx=2048, max_output_tokens=256)._complete("s", "త" * 2000, "t")
        assert estimate_tokens("") == 0

    def test_prompt_budget_follows_the_context_window(self) -> None:
        assert backend(FakeOllama()).max_prompt_chars == 12000
        assert backend(FakeOllama(), num_ctx=4096).max_prompt_chars == (4096 - 1500 - 700) * 3
        assert backend(FakeOllama(), num_ctx=2048, max_output_tokens=256).max_prompt_chars == (2048 - 256 - 700) * 3
        assert backend(FakeOllama(), num_ctx=32768).max_prompt_chars == 12000  # never above the global cap

    def test_the_budgeted_prompt_always_fits(self, free) -> None:
        for ctx, out in ((2048, 256), (4096, 1500), (8192, 1500), (16384, 2000)):
            b = backend(FakeOllama(), num_ctx=ctx, max_output_tokens=out)
            worst = "x" * b.max_prompt_chars
            assert estimate_tokens(worst) + 700 + out <= ctx

    def test_long_articles_are_batched_to_the_budget(self) -> None:
        from tests.helpers import F, H, P, content_of
        from src.backends.base import SectionPacket
        big = content_of(*[b for i in range(30) for b in (H(f"S{i}"), P(F(str(i)) * 5))])
        fake = FakeOllama()
        packets = [SectionPacket(f"s{i}", f"S{i}", (f"S{i}",), F(str(i)) * 12, (i,)) for i in range(30)]
        backend(fake, num_ctx=4096).explain_sections(big, packets, PARAMS)
        assert len(fake.posts) > 1
        for post in fake.posts:
            prompt = post["body"]["messages"][1]["content"]
            assert estimate_tokens(prompt) + 700 + 1500 <= 4096


# ============================================================================ one request at a time
class TestOneRequestAtATime:
    def test_concurrent_callers_are_serialised(self) -> None:
        fake = FakeOllama(chat=lambda b: FakeResp(ok_reply("{}")), delay=0.05)
        b = backend(fake)
        threads = [threading.Thread(target=lambda: b._complete("s", "u", "t")) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert len(fake.posts) == 6 and fake.max_active == 1

    def test_the_limit_holds_across_backend_instances(self) -> None:
        fake = FakeOllama(chat=lambda b: FakeResp(ok_reply("{}")), delay=0.05)
        backends = [backend(fake), backend(fake), backend(fake)]
        threads = [threading.Thread(target=lambda b=b: b._complete("s", "u", "t")) for b in backends for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert len(fake.posts) == 6 and fake.max_active == 1

    def test_a_failed_request_releases_the_lock(self) -> None:
        results: list[str] = []

        def chat(body: dict) -> Any:
            return requests.ConnectionError("down") if not results else FakeResp(ok_reply("{}"))

        fake = FakeOllama(chat=chat)
        b = backend(fake)
        with pytest.raises(BackendUnavailableError):
            b._complete("s", "u", "t")
        results.append("recovered")
        done = threading.Event()

        def second() -> None:
            b._complete("s", "u", "t")
            done.set()

        t = threading.Thread(target=second)
        t.start()
        assert done.wait(timeout=5), "the lock was never released after the failure (deadlock)"

    def test_waiting_is_measured(self) -> None:
        fake = FakeOllama(chat=lambda b: FakeResp(ok_reply("{}")), delay=0.3)
        b = backend(fake)
        t = threading.Thread(target=lambda: b._complete("s", "u", "first"))
        t.start()
        time.sleep(0.05)
        b._complete("s", "u", "second")
        t.join(timeout=5)
        second = next(s for s in b.stats if s.purpose == "second")
        assert second.wait_s > 0.1


class TestStatistics:
    def test_timings_are_recorded(self) -> None:
        reply = ok_reply("{}", eval_count=100, eval_duration=20_000_000_000, load_duration=4_000_000_000,
                         prompt_eval_count=50)
        b = backend(FakeOllama(chat=lambda body: FakeResp(reply)))
        b._complete("s", "u", "demo")
        s = b.stats[0]
        assert (s.purpose, s.prompt_tokens, s.output_tokens, s.load_s, s.done_reason) == ("demo", 50, 100, 4.0, "stop")
        assert s.tokens_per_s == pytest.approx(5.0) and s.wall_s >= 0

    def test_missing_timing_fields_do_not_crash(self) -> None:
        b = backend(FakeOllama(chat=lambda body: FakeResp({"message": {"content": "{}"}})))
        b._complete("s", "u", "t")
        assert b.stats[0].tokens_per_s == 0.0 and b.stats[0].output_tokens == 0


# ============================================================================ selection + safety
class RecordingEnv(dict):
    """An environment that remembers which variables were looked at."""

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.accessed: set[str] = set()

    def get(self, key: str, default: Any = None) -> Any:
        self.accessed.add(key)
        return super().get(key, default)

    def __getitem__(self, key: str) -> Any:
        self.accessed.add(key)
        return super().__getitem__(key)


@pytest.fixture
def server_up(monkeypatch: pytest.MonkeyPatch) -> FakeOllama:
    fake = FakeOllama()
    monkeypatch.setattr(requests.Session, "get", lambda self, url, **kw: fake.get(url, **kw))
    monkeypatch.setattr(requests.Session, "post", lambda self, url, json=None, **kw: fake.post(url, json=json, **kw))
    return fake


class TestSelectionAndSafety:
    def test_local_remains_the_default_even_with_ollama_fully_configured(self) -> None:
        env = {"NOTEFORGE_BACKEND": "ollama", "OLLAMA_BASE_URL": DEFAULT_BASE_URL, "OLLAMA_MODEL": "qwen3.5:4b"}
        for sel in (resolve_backend(env=env), resolve_backend("local", env=env), resolve_backend("", env=env)):
            assert isinstance(sel.backend, LocalNotesBackend) and sel.requested == "local" and not sel.fell_back

    def test_choosing_ollama_builds_it_from_the_environment(self, server_up: FakeOllama) -> None:
        sel = resolve_backend("ollama", env={"OLLAMA_BASE_URL": "localhost:11434", "OLLAMA_MODEL": "qwen3.5:4b",
                                              "OLLAMA_NUM_CTX": "8192", "OLLAMA_THINK": "false"})
        assert isinstance(sel.backend, OllamaBackend) and not sel.fell_back
        s = sel.backend.settings
        assert (s.base_url, s.model, s.num_ctx, s.think) == ("http://localhost:11434", "qwen3.5:4b", 8192, False)
        assert "Ollama, model qwen3.5:4b [free; runs on this machine]" in sel.describe()

    def test_ollama_host_environment_variable_still_selects_the_server(self, server_up: FakeOllama) -> None:
        sel = resolve_backend("ollama", env={"OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_MODEL": "qwen3.5:4b"})
        assert sel.backend.host == "http://127.0.0.1:11434"
        assert server_up.gets == ["http://127.0.0.1:11434/api/tags"]

    def test_legacy_keyword_still_accepted(self, server_up: FakeOllama) -> None:
        sel = resolve_backend("ollama", env={}, ollama_host="http://localhost:11434", ollama_model="qwen3.5:4b")
        assert isinstance(sel.backend, OllamaBackend)

    def test_model_missing_falls_back_with_the_pull_command(self, server_up: FakeOllama) -> None:
        sel = resolve_backend("ollama", env={"OLLAMA_MODEL": "llama9:70b"})
        assert isinstance(sel.backend, LocalNotesBackend) and "ollama pull llama9:70b" in sel.fallback_reason

    @pytest.mark.parametrize("env,needle", [
        ({"OLLAMA_NUM_CTX": "abc"}, "OLLAMA_NUM_CTX"), ({"OLLAMA_TEMPERATURE": "0.9"}, "temperature"),
        ({"OLLAMA_THINK": "maybe"}, "OLLAMA_THINK"), ({"OLLAMA_KEEP_ALIVE": "soon"}, "keep_alive"),
        ({"OLLAMA_BASE_URL": "ftp://x"}, "Invalid Ollama URL"), ({"OLLAMA_BASE_URL": "http://203.0.113.9:11434"}, "not this machine"),
    ])
    def test_invalid_configuration_falls_back_to_local_with_a_clear_reason(self, env: dict, needle: str) -> None:
        sel = resolve_backend("ollama", env=env)
        assert isinstance(sel.backend, LocalNotesBackend)
        assert sel.fallback_reason.startswith("Invalid Ollama configuration") and needle in sel.fallback_reason

    def test_ollama_not_running_falls_back_with_the_start_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(requests.Session, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("x")))
        sel = resolve_backend("ollama", env={"OLLAMA_MODEL": "qwen3.5:4b"})
        assert isinstance(sel.backend, LocalNotesBackend) and "ollama serve" in sel.fallback_reason

    def test_the_openai_key_is_never_looked_at_when_ollama_is_chosen(self, server_up: FakeOllama) -> None:
        env = RecordingEnv({"OPENAI_API_KEY": "sk-secret-never-read", "OPENAI_MODEL": "m", "OLLAMA_MODEL": "qwen3.5:4b"})
        resolve_backend("ollama", env=env)
        resolve_backend("local", env=env)
        OllamaSettings.from_env(env)
        assert not any(k.startswith("OPENAI") for k in env.accessed), env.accessed

    def test_ollama_never_leads_to_openai(self, server_up: FakeOllama) -> None:
        sel = resolve_backend("ollama", env={"OPENAI_API_KEY": "sk-x", "OLLAMA_MODEL": "gone"}, confirm=lambda r: True,
                              openai_model="m")
        assert isinstance(sel.backend, LocalNotesBackend) and not sel.backend.info.is_paid  # fell back to local, not OpenAI

    def test_the_backend_is_free_and_local(self) -> None:
        info = backend(FakeOllama()).info
        assert not info.is_paid and info.is_local and info.model == "qwen3.5:4b"

    @pytest.mark.parametrize("url", ["http://192.168.1.9:11434", "https://ollama.example.com", "http://203.0.113.9:11434"])
    def test_remote_servers_are_still_refused(self, url: str) -> None:
        with pytest.raises(BackendError, match="not this machine"):
            OllamaBackend(base_url=url)
        assert OllamaBackend(base_url=url, allow_remote=True).info.is_local is False

    def test_module_exposes_no_network_calls_at_import(self) -> None:
        assert ollama_mod._GENERATION_LOCK.locked() is False


# ============================================================================ schemas
class TestSchemas:
    TASKS = ["keyword explanations", "section explanations", "analogies", "short questions",
             "descriptive questions", "mcq questions"]

    def test_every_task_has_a_schema(self) -> None:
        assert sorted(SCHEMAS) == sorted(self.TASKS)

    @pytest.mark.parametrize("task", TASKS)
    def test_schema_is_a_json_object_schema(self, task: str) -> None:
        import json
        schema = SCHEMAS[task]
        assert json.loads(json.dumps(schema)) == schema and schema["type"] == "object" and schema["required"]

    def test_mcq_schema_forces_four_options_and_a_valid_index(self) -> None:
        item = SCHEMAS["mcq questions"]["properties"]["questions"]["items"]
        assert item["properties"]["options"]["minItems"] == item["properties"]["options"]["maxItems"] == 4
        assert item["properties"]["correct_index"]["minimum"] == 0 and item["properties"]["correct_index"]["maximum"] == 3
        assert set(item["required"]) == {"question", "options", "correct_index", "explanation"}

    def test_analogy_kind_is_restricted(self) -> None:
        kind = SCHEMAS["analogies"]["properties"]["examples"]["items"]["properties"]["kind"]
        assert kind["enum"] == ["analogy", "application"]

    @pytest.mark.parametrize("task", TASKS)
    def test_well_formed_replies_match_the_declared_required_keys(self, task: str) -> None:
        import json
        reply = json.loads(ScriptedLLM.default(task, "x\nARTICLE MATERIAL:\n" + json.dumps(
            [{"term": "t", "id": "s1", "heading": "h"}])))
        schema = SCHEMAS[task]
        assert set(schema["required"]) <= set(reply)
        top = schema["required"][0]
        item_schema = schema["properties"][top]["items"]
        for entry in reply[top]:
            assert set(item_schema["required"]) <= set(entry)

    def test_backends_without_schema_support_still_work(self, free) -> None:
        class Plain(LLMBackend):
            info = ScriptedLLM().info  # type: ignore[assignment]
            seen: list[Any] = []

            def _complete(self, system: str, user: str, purpose: str) -> str:
                self.seen.append(purpose)
                return ScriptedLLM.default(purpose, user)

        p = Plain()
        assert p.explain_keywords(free, [TermPacket("chunk", None)], PARAMS)["chunk"].explanation
        assert p.seen == ["keyword explanations"]


# ============================================================================ the whole generator over the Ollama backend
class TestGeneratorWithOllama:
    def generate(self, free, fake: FakeOllama, **cfg: Any):
        return NotesGenerator(backend(fake), NotesConfig(**cfg)).generate(free), fake

    def test_full_notes_validate_and_label_ai_output(self, free) -> None:
        notes, fake = self.generate(free, FakeOllama())
        assert notes.used_ai and notes.backend.name == "Ollama" and notes.backend.model == "qwen3.5:4b"
        assert "Ollama, model qwen3.5:4b [free; runs on this machine]" in notes.banners[0].text
        assert validate_notes(notes, QuestionSpec()) == [] and verify_provenance(notes, free) == []
        ai = [i for i in notes.all_items() if i.is_ai_generated]
        assert ai and all(i.label for i in ai)
        assert len(notes.chapter(7).questions) == 25

    def test_every_request_uses_the_tuned_settings(self, free) -> None:
        _, fake = self.generate(free, FakeOllama())
        assert len(fake.posts) >= 6
        for post in fake.posts:
            body = post["body"]
            assert body["think"] is False and body["options"]["num_ctx"] == 8192 and body["keep_alive"] == "10m"
            assert isinstance(body["format"], dict) and body["options"]["temperature"] == 0.2

    def test_requests_never_overlap(self, free) -> None:
        _, fake = self.generate(free, FakeOllama(delay=0.01))
        assert fake.max_active == 1

    def test_original_text_is_untouched_by_the_model(self, free) -> None:
        notes, _ = self.generate(free, FakeOllama())
        local = NotesGenerator(LocalNotesBackend(), NotesConfig()).generate(free)
        original = lambda n: [i.plain_text for c in (n.chapter(1), n.chapter(2), n.chapter(5)) for i in c.items() if i.is_original]  # noqa: E731
        assert original(notes) == original(local)

    def test_server_dies_mid_run_so_the_rest_falls_back_without_retrying(self, free) -> None:
        count = {"n": 0}

        def chat(body: dict) -> Any:
            count["n"] += 1
            return model_reply(body) if count["n"] == 1 else requests.ConnectionError("gone")

        notes, fake = self.generate(free, FakeOllama(chat=chat))
        assert len(fake.posts) == 2  # one success, one failure, then it stopped asking
        assert notes.backend.name == "Local" and "not running" in notes.fallback_reason
        assert validate_notes(notes, QuestionSpec()) == [] and len(notes.chapter(7).questions) == 21  # D-4 (Phase 8.2 / D-10: 21, was 20)

    def test_fallback_can_be_forbidden_mid_run(self, free) -> None:
        fake = FakeOllama(chat=lambda b: requests.ConnectionError("gone"))
        with pytest.raises(BackendUnavailableError):
            NotesGenerator(backend(fake), NotesConfig(fallback_to_local=False)).generate(free)

    def test_model_removed_after_the_check(self, free) -> None:
        notes, fake = self.generate(free, FakeOllama(chat=lambda b: FakeResp({"error": "model 'qwen3.5:4b' not found"}, 404)))
        assert len(fake.posts) == 1 and notes.backend.name == "Local" and "ollama pull" in notes.fallback_reason

    def test_a_timeout_degrades_only_that_part(self, free) -> None:
        def chat(body: dict) -> Any:
            if guess_purpose(body["messages"][1]["content"]) == "analogies":
                return requests.ReadTimeout("slow")
            return model_reply(body)

        notes, _ = self.generate(free, FakeOllama(chat=chat))
        assert notes.backend.name == "Ollama"  # not abandoned
        assert any("did not finish within" in w for w in notes.generation_warnings)
        assert any("cannot create new examples" in i.text for i in notes.chapter(4).items())
        assert any(i.is_ai_generated for i in notes.chapter(3).items())

    def test_truncated_model_output_is_reported_not_trusted(self, free) -> None:
        def chat(body: dict) -> Any:
            if guess_purpose(body["messages"][1]["content"]) == "keyword explanations":
                return FakeResp(ok_reply('{"terms": [', done_reason="length"))
            return model_reply(body)

        notes, _ = self.generate(free, FakeOllama(chat=chat))
        assert any("cut off" in w for w in notes.generation_warnings)
        assert not any(i.is_ai_generated for i in notes.chapter(3).items())

    def test_invented_figures_from_the_model_are_rejected(self, free) -> None:
        bad = '{"terms": [{"term": "Chunk size", "explanation": "Experts say 92% of teams tune it.", "significance": "It matters."}]}'
        def chat(body: dict) -> Any:
            if guess_purpose(body["messages"][1]["content"]) == "keyword explanations":
                return FakeResp(ok_reply(bad))
            return model_reply(body)

        notes, _ = self.generate(free, FakeOllama(chat=chat))
        assert "92%" not in " ".join(i.plain_text for i in notes.all_items())
        assert any("92%" in w for w in notes.generation_warnings)

    def test_partial_articles_keep_their_warning_with_ollama(self) -> None:
        content = fixture_content("substack_paywalled.html")
        notes = NotesGenerator(backend(FakeOllama()), NotesConfig()).generate(content)
        assert notes.is_partial and all("PARTIAL" in c.notices[0].text for c in notes.chapters)
        assert verify_provenance(notes, content) == []

    def test_language_instruction_is_forwarded(self, free) -> None:
        _, fake = self.generate(free, FakeOllama(), language="Telugu")
        assert all("Telugu" in p["body"]["messages"][1]["content"] for p in fake.posts)
