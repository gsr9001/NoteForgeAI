"""Phase 5 tests: backend selection, Ollama, OpenAI opt-in/confirmation, grounding, local backend.

Nothing here touches the network or a real model: Ollama is a fake requests session and
OpenAI is a fake client. A test that would cause a real request fails loudly instead.
"""
from __future__ import annotations

import json
import logging
import socket
import sys
from typing import Any, Optional

import pytest
import requests

from src.backends import (
    BackendError, BackendNotAuthorizedError, BackendUnavailableError, ConfirmationRequest, LocalNotesBackend,
    OllamaBackend, OpenAIBackend, UnknownBackendError, resolve_backend,
)
from src.backends.base import GenerationParams, QuestionSpec, SectionPacket, SourceSentence, TermPacket
from src.backends.base import BackendResponseError
from src.backends.llm import (
    LLMBackend, article_digest, batch_by_size, check_grounding, parse_json_object, source_text,
)
from src.notes_model import BackendKind, DetailLevel, Language, NoteProvenance, QuestionKind, validate_question
from tests.helpers import FakeOpenAIClient, ScriptedLLM, fixture_content, payload_of

PARAMS = GenerationParams(DetailLevel.INTERMEDIATE, Language.ENGLISH, 2)
SECRET = "sk-test-SECRET-DO-NOT-LEAK-123456"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt to open a real connection (e.g. an accidental OpenAI/Ollama call) fails the test."""
    def blocked(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a real network connection was attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture(scope="module")
def content():
    return fixture_content("substack_free.html")


# ============================================================================ fake Ollama HTTP
class FakeResponse:
    def __init__(self, payload: Any = None, status: int = 200) -> None:
        self._payload, self.status_code = payload, status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, tags: Any = None, chat: Any = None, get_error: Optional[Exception] = None,
                 post_error: Optional[Exception] = None) -> None:
        self.tags, self.chat = tags, chat
        self.get_error, self.post_error = get_error, post_error
        self.gets: list[str] = []
        self.posts: list[dict[str, Any]] = []

    def get(self, url: str, **kw: Any) -> FakeResponse:
        self.gets.append(url)
        if self.get_error:
            raise self.get_error
        return FakeResponse(self.tags)

    def post(self, url: str, json: Any = None, **kw: Any) -> FakeResponse:
        self.posts.append({"url": url, "json": json, **kw})
        if self.post_error:
            raise self.post_error
        payload = self.chat(json) if callable(self.chat) else self.chat
        return FakeResponse(payload)


def tags(*names: str) -> dict[str, Any]:
    return {"models": [{"name": n} for n in names]}


# ============================================================================ selection rules
class TestBackendSelection:
    def test_default_is_local(self) -> None:
        sel = resolve_backend(env={})
        assert isinstance(sel.backend, LocalNotesBackend) and not sel.fell_back and sel.requested == "local"

    def test_openai_key_in_environment_never_activates_openai(self) -> None:
        env = {"OPENAI_API_KEY": SECRET, "OPENAI_MODEL": "some-model", "NOTEFORGE_BACKEND": "openai"}
        approve = lambda r: True  # noqa: E731  a real CLI always supplies a handler, so this is the dangerous case
        for call in (lambda: resolve_backend(env=env, confirm=approve, openai_model="m"),
                     lambda: resolve_backend("local", env=env, confirm=approve, openai_model="m"),
                     lambda: resolve_backend("", env=env, confirm=approve, openai_model="m"),
                     lambda: resolve_backend(None, env=env, confirm=approve, openai_model="m")):  # type: ignore[arg-type]
            sel = call()
            assert isinstance(sel.backend, LocalNotesBackend) and not sel.backend.info.is_paid
            assert sel.requested == "local" and not sel.fell_back  # not "openai that fell back": never considered

    def test_name_is_case_insensitive_and_unknown_names_rejected(self) -> None:
        assert resolve_backend(" LOCAL ", env={}).requested == "local"
        with pytest.raises(UnknownBackendError):
            resolve_backend("gpt-9000", env={})
        assert issubclass(UnknownBackendError, ValueError)

    def test_openai_requested_without_confirmation_handler_falls_back_to_local(self) -> None:
        sel = resolve_backend("openai", env={"OPENAI_API_KEY": SECRET, "OPENAI_MODEL": "m"})
        assert isinstance(sel.backend, LocalNotesBackend) and sel.fell_back
        assert "confirmation" in sel.fallback_reason.lower()

    @pytest.mark.parametrize("env,model,needle", [
        ({"OPENAI_API_KEY": SECRET}, None, "model"),
        ({"OPENAI_MODEL": "m"}, None, "key"),
        ({}, None, "key|model"),
    ])
    def test_openai_requested_but_not_fully_configured_falls_back(self, env: dict, model: Optional[str], needle: str) -> None:
        import re
        sel = resolve_backend("openai", env=env, confirm=lambda r: True, openai_model=model)
        assert isinstance(sel.backend, LocalNotesBackend)
        assert re.search(needle, sel.fallback_reason, re.IGNORECASE)

    def test_openai_explicit_and_fully_configured(self) -> None:
        sel = resolve_backend("openai", env={"OPENAI_API_KEY": SECRET}, confirm=lambda r: True, openai_model="m")
        assert isinstance(sel.backend, OpenAIBackend) and sel.backend.info.is_paid and not sel.fell_back
        assert sel.backend.calls_made == 0  # selecting it makes no request

    def test_selection_never_makes_a_request(self) -> None:
        resolve_backend("openai", env={"OPENAI_API_KEY": SECRET, "OPENAI_MODEL": "m"}, confirm=lambda r: True)

    def test_ollama_unavailable_falls_back_to_local_with_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(requests.Session, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("refused")))
        sel = resolve_backend("ollama", env={})
        assert isinstance(sel.backend, LocalNotesBackend) and sel.fell_back
        assert "not reachable" in sel.fallback_reason
        assert "using local instead" in sel.describe()

    def test_ollama_remote_host_from_env_is_refused_and_falls_back(self) -> None:
        sel = resolve_backend("ollama", env={"OLLAMA_HOST": "http://203.0.113.9:11434"})
        assert isinstance(sel.backend, LocalNotesBackend) and "not this machine" in sel.fallback_reason

    def test_describe_names_the_active_backend(self) -> None:
        text = resolve_backend(env={}).describe()
        assert text.startswith("Active backend: Local") and "free" in text and "no AI model" in text

    def test_local_backend_is_free_and_local(self) -> None:
        info = LocalNotesBackend().info
        assert info.kind is BackendKind.LOCAL and info.is_local and not info.is_paid and info.model is None


# ============================================================================ local backend
class TestLocalBackend:
    def test_works_offline_and_without_any_keys(self, content, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("OPENAI_API_KEY", "OLLAMA_HOST", "OLLAMA_MODEL"):
            monkeypatch.delenv(var, raising=False)
        backend = LocalNotesBackend()
        assert backend.check_available().available
        result = backend.create_questions(content, QuestionSpec(), PARAMS)  # the autouse fixture blocks sockets
        # Phase 8.1 (D-4): a fact is never asked twice, so this article supports 10 short + 5 descriptive + MCQ questions;
        # Phase 8.2 (D-10): the practical lessons are now recognised as statements, which supports a 6th MCQ (was 5)
        assert len(result.mcq) == 6

    def test_does_not_support_translation(self) -> None:
        assert LocalNotesBackend.supports_translation is False

    def test_keyword_explanations_are_verbatim_original_text(self) -> None:
        packet = TermPacket("chunk", "def", (SourceSentence("Chunks are split.", 3), SourceSentence("Another.", 4)),
                            (SourceSentence("Keep chunks small.", 5),))
        out = LocalNotesBackend().explain_keywords(None, [packet], PARAMS)["chunk"]  # type: ignore[arg-type]
        assert [t.text for t in out.explanation] == ["Chunks are split.", "Another."]
        assert [t.text for t in out.significance] == ["Keep chunks small."]
        assert all(t.provenance is NoteProvenance.ORIGINAL for t in out.explanation + out.significance)

    def test_contexts_per_term_limits_explanations(self) -> None:
        packet = TermPacket("x", None, tuple(SourceSentence(f"S{i}.", i) for i in range(5)))
        out = LocalNotesBackend().explain_keywords(None, [packet], GenerationParams(contexts_per_term=1))  # type: ignore[arg-type]
        assert len(out["x"].explanation) == 1

    def test_no_section_explanations_and_no_analogies(self, content) -> None:
        backend = LocalNotesBackend()
        assert backend.explain_sections(content, [SectionPacket("s1", "H", ("H",), "t", (0,))], PARAMS) == {}
        assert backend.create_analogies(content, [TermPacket("x", None)], PARAMS) == []

    def test_questions_are_deterministic(self, content) -> None:
        a = LocalNotesBackend().create_questions(content, QuestionSpec(), PARAMS)
        b = LocalNotesBackend().create_questions(content, QuestionSpec(), PARAMS)
        assert [q.question for q in a.short + a.descriptive + a.mcq] == [q.question for q in b.short + b.descriptive + b.mcq]
        assert [q.options for q in a.mcq] == [q.options for q in b.mcq]


# ============================================================================ Ollama
class TestOllamaAvailability:
    def test_unreachable(self) -> None:
        b = OllamaBackend(session=FakeSession(get_error=requests.ConnectionError("refused")))  # type: ignore[arg-type]
        a = b.check_available()
        assert not a.available and "not reachable" in a.reason and "11434" in a.reason

    def test_timeout_and_http_error_and_bad_json(self) -> None:
        for session in (FakeSession(get_error=requests.Timeout("slow")),
                        FakeSession(tags=ValueError("not json")),
                        FakeSession(tags=None)):
            a = OllamaBackend(session=session).check_available()  # type: ignore[arg-type]
            assert not a.available

    def test_running_but_no_models(self) -> None:
        a = OllamaBackend(session=FakeSession(tags=tags())).check_available()  # type: ignore[arg-type]
        assert not a.available and "ollama pull" in a.reason

    def test_model_not_installed(self) -> None:
        a = OllamaBackend(model="llama9", session=FakeSession(tags=tags("mistral:latest"))).check_available()  # type: ignore[arg-type]
        assert not a.available and "llama9" in a.reason and "mistral" in a.reason

    def test_picks_first_installed_model_when_none_given(self) -> None:
        b = OllamaBackend(session=FakeSession(tags=tags("qwen:7b", "mistral")))  # type: ignore[arg-type]
        assert b.check_available().available and b.model == "qwen:7b"

    @pytest.mark.parametrize("wanted", ["mistral", "mistral:latest"])
    def test_model_name_matching(self, wanted: str) -> None:
        b = OllamaBackend(model=wanted, session=FakeSession(tags=tags("mistral:latest")))  # type: ignore[arg-type]
        assert b.check_available().available

    def test_availability_is_cached_and_free(self) -> None:
        s = FakeSession(tags=tags("m"))
        b = OllamaBackend(session=s)  # type: ignore[arg-type]
        b.check_available(); b.check_available()
        assert s.gets == ["http://localhost:11434/api/tags"] and not s.posts

    @pytest.mark.parametrize("host", ["http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434"])
    def test_loopback_hosts_allowed(self, host: str) -> None:
        assert OllamaBackend(host=host).info.is_local

    @pytest.mark.parametrize("host", ["http://192.168.1.5:11434", "https://ollama.example.com", "http://10.0.0.2"])
    def test_remote_hosts_refused_by_default(self, host: str) -> None:
        with pytest.raises(BackendError, match="not this machine"):
            OllamaBackend(host=host)

    def test_remote_host_needs_explicit_opt_in(self) -> None:
        b = OllamaBackend(host="http://192.168.1.5:11434", allow_remote=True)
        assert not b.info.is_local

    @pytest.mark.parametrize("host", ["ftp://localhost", "http://", "http://localhost:notaport"])
    def test_invalid_hosts(self, host: str) -> None:
        with pytest.raises(BackendError):
            OllamaBackend(host=host)

    def test_info_is_free_and_labels_the_model(self) -> None:
        info = OllamaBackend(model="mistral").info
        assert info.kind is BackendKind.OLLAMA and not info.is_paid and info.model == "mistral"
        assert "free" in info.display()


class TestOllamaCalls:
    def chat_reply(self, content: str) -> dict[str, Any]:
        return {"message": {"role": "assistant", "content": content}}

    def test_chat_request_is_local_deterministic_json(self, content) -> None:
        reply = json.dumps({"terms": []})
        s = FakeSession(tags=tags("m"), chat=self.chat_reply(reply))
        b = OllamaBackend(model="m", session=s)  # type: ignore[arg-type]
        b.explain_keywords(content, [TermPacket("chunk", None)], PARAMS)
        body = s.posts[0]["json"]
        assert s.posts[0]["url"] == "http://localhost:11434/api/chat"
        assert body["model"] == "m" and body["stream"] is False
        assert body["options"]["temperature"] == 0.2 and body["options"]["num_ctx"] == 8192
        assert body["think"] is False and "think" not in body["options"]  # top-level: options.think is ignored
        assert body["keep_alive"] == "10m" and body["messages"][0]["role"] == "system"
        assert isinstance(body["format"], dict) and body["format"]["required"] == ["terms"]  # per-task JSON schema

    def test_system_prompt_forbids_invention(self, content) -> None:
        s = FakeSession(chat=self.chat_reply(json.dumps({"terms": []})))
        OllamaBackend(model="m", session=s).explain_keywords(content, [TermPacket("chunk", None)], PARAMS)  # type: ignore[arg-type]
        system = s.posts[0]["json"]["messages"][0]["content"]
        for phrase in ("ONLY information in the supplied article", "Never invent statistics", "citations", "equations"):
            assert phrase in system

    def test_output_is_labelled_ai_generated(self, content) -> None:
        reply = json.dumps({"terms": [{"term": "chunk", "explanation": "Chunks are the smaller pieces of text that a document is split into.",
                                       "significance": "Their size affects how precisely retrieval can find the right passage."}]})
        b = OllamaBackend(model="m", session=FakeSession(chat=self.chat_reply(reply)))  # type: ignore[arg-type]
        out = b.explain_keywords(content, [TermPacket("chunk", None, source_indices=(3,))], PARAMS)["chunk"]
        assert out.explanation[0].provenance is NoteProvenance.AI_GENERATED
        assert out.explanation[0].source_indices == (3,)
        assert out.significance[0].provenance is NoteProvenance.AI_GENERATED

    def test_no_model_selected(self, content) -> None:
        with pytest.raises(BackendUnavailableError, match="No Ollama model"):
            OllamaBackend(session=FakeSession()).explain_keywords(content, [TermPacket("x", None)], PARAMS)  # type: ignore[arg-type]

    def test_connection_lost_mid_run(self, content) -> None:
        b = OllamaBackend(model="m", session=FakeSession(post_error=requests.ConnectionError("gone")))  # type: ignore[arg-type]
        with pytest.raises(BackendUnavailableError, match="not running"):
            b.explain_keywords(content, [TermPacket("x", None)], PARAMS)

    @pytest.mark.parametrize("err", [requests.Timeout("slow"), requests.HTTPError("500")])
    def test_other_request_failures_are_backend_errors(self, content, err: Exception) -> None:
        b = OllamaBackend(model="m", session=FakeSession(post_error=err))  # type: ignore[arg-type]
        with pytest.raises(BackendError):
            b.explain_keywords(content, [TermPacket("x", None)], PARAMS)

    def test_malformed_reply_shape(self, content) -> None:
        b = OllamaBackend(model="m", session=FakeSession(chat={"unexpected": True}))  # type: ignore[arg-type]
        with pytest.raises(BackendError):
            b.explain_keywords(content, [TermPacket("x", None)], PARAMS)


# ============================================================================ OpenAI opt-in + confirmation
def make_openai(confirm: Any = None, **kw: Any) -> tuple[OpenAIBackend, FakeOpenAIClient]:
    client = kw.pop("client", None) or FakeOpenAIClient()
    backend = OpenAIBackend(api_key=kw.pop("api_key", SECRET), model=kw.pop("model", "test-model"),
                            confirm=confirm if confirm is not None else (lambda r: True),
                            enabled=kw.pop("enabled", True), client=client, **kw)
    return backend, client


class TestOpenAIOptIn:
    def test_not_enabled_by_default(self) -> None:
        b = OpenAIBackend(api_key=SECRET, model="m", confirm=lambda r: True)
        assert not b.enabled and not b.check_available().available
        assert "not explicitly enabled" in b.check_available().reason

    def test_disabled_backend_refuses_and_makes_no_call(self, content) -> None:
        client = FakeOpenAIClient()
        b = OpenAIBackend(api_key=SECRET, model="m", confirm=lambda r: True, enabled=False, client=client)
        with pytest.raises(BackendNotAuthorizedError):
            b.explain_keywords(content, [TermPacket("x", None)], PARAMS)
        assert client.calls == []

    @pytest.mark.parametrize("kwargs,needle", [
        ({"model": None}, "model"), ({"api_key": None, "client": None}, "key"), ({"confirm": None}, "confirmation"),
    ])
    def test_every_precondition_is_required(self, kwargs: dict, needle: str) -> None:
        args = dict(api_key=SECRET, model="m", confirm=lambda r: True, enabled=True, client=None)
        args.update(kwargs)
        a = OpenAIBackend(**args).check_available()  # type: ignore[arg-type]
        assert not a.available and needle in a.reason.lower()

    def test_info_is_clearly_paid_and_not_local(self) -> None:
        info = OpenAIBackend(SECRET, "m", lambda r: True, enabled=True).info
        assert info.is_paid and not info.is_local and info.kind is BackendKind.OPENAI
        assert "PAID" in info.display() and "external service" in info.display()

    def test_check_available_makes_no_request(self) -> None:
        b, client = make_openai()
        assert b.check_available().available and client.calls == []

    def test_declined_confirmation_sends_nothing(self, content) -> None:
        b, client = make_openai(confirm=lambda r: False)
        with pytest.raises(BackendNotAuthorizedError, match="declined"):
            b.explain_keywords(content, [TermPacket("chunk", None)], PARAMS)
        assert client.calls == [] and b.calls_made == 0

    def test_confirmation_is_asked_before_every_single_request(self, content) -> None:
        asked: list[ConfirmationRequest] = []

        def confirm(req: ConfirmationRequest) -> bool:
            asked.append(req)
            return True

        b, client = make_openai(confirm=confirm)
        b.create_questions(content, QuestionSpec(), PARAMS)  # three prompts: short, descriptive, mcq
        assert len(client.calls) == 3 and len(asked) == 3
        assert [r.call_number for r in asked] == [1, 2, 3]
        assert all(r.model == "test-model" and r.estimated_input_tokens > 0 for r in asked)
        assert {r.purpose for r in asked} == {"short questions", "descriptive questions", "mcq questions"}

    def test_confirmation_request_describes_cost_and_data_leaving_the_machine(self) -> None:
        text = ConfirmationRequest("m", "analogies", 1200, 2000, 2, 12).describe()
        assert "PAID" in text and "sends article text to OpenAI" in text and "1200" in text and "#2" in text

    def test_confirmation_happens_before_the_client_is_even_created(self, content, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "openai", None)  # importing it would raise ImportError
        b = OpenAIBackend(api_key=SECRET, model="m", confirm=lambda r: False, enabled=True)
        with pytest.raises(BackendNotAuthorizedError):  # declined, so the missing package is never reached
            b.explain_keywords(content, [TermPacket("x", None)], PARAMS)

    def test_missing_openai_package_after_confirmation(self, content, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "openai", None)
        b = OpenAIBackend(api_key=SECRET, model="m", confirm=lambda r: True, enabled=True)
        with pytest.raises(BackendUnavailableError, match="not installed"):
            b.explain_keywords(content, [TermPacket("x", None)], PARAMS)

    def test_hard_call_limit(self, content) -> None:
        b, client = make_openai(max_calls=2)
        b.explain_keywords(content, [TermPacket("chunk", None)], PARAMS)
        b.explain_keywords(content, [TermPacket("chunk", None)], PARAMS)
        with pytest.raises(BackendNotAuthorizedError, match="limit"):
            b.explain_keywords(content, [TermPacket("chunk", None)], PARAMS)
        assert len(client.calls) == 2

    def test_request_is_deterministic_and_uses_the_chosen_model(self, content) -> None:
        b, client = make_openai(model="chosen-model")
        b.explain_keywords(content, [TermPacket("chunk", None)], PARAMS)
        call = client.calls[0]
        assert call["model"] == "chosen-model" and call["temperature"] == 0
        assert [m["role"] for m in call["messages"]] == ["system", "user"]

    def test_api_key_never_appears_in_repr_logs_or_prompts(self, content, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        b, client = make_openai()
        b.explain_keywords(content, [TermPacket("chunk", None)], PARAMS)
        assert SECRET not in repr(b) and SECRET not in caplog.text
        assert SECRET not in json.dumps(client.calls)

    def test_constructing_the_backend_does_not_read_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        b = OpenAIBackend(api_key=None, model="m", confirm=lambda r: True, enabled=True)
        assert not b.check_available().available  # the key in the environment was ignored

    def test_sdk_failure_is_wrapped_without_leaking_details(self, content) -> None:
        b, _ = make_openai(client=FakeOpenAIClient(fail=RuntimeError(f"auth failed for {SECRET}")))
        with pytest.raises(BackendError) as exc:
            b.explain_keywords(content, [TermPacket("x", None)], PARAMS)
        assert SECRET not in str(exc.value)

    def test_output_is_labelled_ai_generated(self, content) -> None:
        b, _ = make_openai()
        out = b.explain_keywords(content, [TermPacket("chunk", None)], PARAMS)["chunk"]
        assert out.explanation and all(t.provenance is NoteProvenance.AI_GENERATED for t in out.explanation)


# ============================================================================ grounding / JSON / batching
class TestGrounding:
    SOURCE = "The index holds 768 dimensions and 10,000 vectors, 3.5 GB in total, 15% faster since 2020. k=3 and x = a + b."

    @pytest.mark.parametrize("text", [
        "The index holds 768 dimensions.", "It needs 3.5 GB.", "It is 15% faster.", "That dates from 2020.",
        "There are 10000 vectors.", "Step 1 then step 2 then 3.", "Set k=3 here.", "Plain prose with no figures at all.",
    ])
    def test_acceptable(self, text: str) -> None:
        assert check_grounding(text, self.SOURCE) == []

    @pytest.mark.parametrize("text,needle", [
        ("It holds 512 dimensions.", "512"), ("It is 40% faster.", "40%"), ("Released in 2023.", "2023"),
        ("It needs 7.25 GB.", "7.25"), ("See https://example.com/paper.", "URL"), ("As Smith et al. showed.", "citation"),
        ("(Jones, 2019) found this.", "citation"), ("See doi:10.1000/xyz.", "citation"), ("Per [12] it works.", "citation"),
        ("Then y = m * x + c holds.", "equation"), ("Use $E=mc^2$ here.", "equation"),
    ])
    def test_invented_specifics_are_flagged(self, text: str, needle: str) -> None:
        problems = check_grounding(text, self.SOURCE)
        assert problems and any(needle in p for p in problems)

    def test_source_text_covers_headings_prose_and_code(self, content) -> None:
        blob = source_text(content)
        assert "Why retrieval matters" in blob and "cosine" in blob and "k=3" in blob


class TestJsonParsing:
    @pytest.mark.parametrize("raw", [
        '{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! Here you go:\n{"a": 1}\nHope that helps.', '```\n{"a": 1}\n```',
    ])
    def test_accepts_common_shapes(self, raw: str) -> None:
        assert parse_json_object(raw) == {"a": 1}

    @pytest.mark.parametrize("raw", ["", "no json here", "{not valid}", "[1, 2, 3]", '"just a string"', "{"])
    def test_rejects_the_rest(self, raw: str) -> None:
        with pytest.raises(BackendResponseError):
            parse_json_object(raw)


class TestBatching:
    def test_batches_respect_the_budget(self) -> None:
        batches = batch_by_size(list(range(10)), lambda n: 10, 35)
        assert [len(b) for b in batches] == [3, 3, 3, 1] and sum(batches, []) == list(range(10))

    def test_oversized_item_gets_its_own_batch(self) -> None:
        assert batch_by_size([1, 2], lambda n: 100, 10) == [[1], [2]]

    def test_empty(self) -> None:
        assert batch_by_size([], lambda n: 1, 10) == []

    def test_digest_is_shortened_but_keeps_every_section(self) -> None:
        from tests.helpers import F, H, P, content_of
        big = content_of(*[b for i in range(12) for b in (H(f"Section {i}"), P(F(str(i)) * 30))])
        digest = article_digest(big, 3000)
        assert len(digest) < 6000 and all(f"## Section {i}" in digest for i in range(12))
        assert article_digest(big, 10**9).count("## ") == 12


# ============================================================================ LLM behaviour with a scripted model
class TestLLMBackendBehaviour:
    def test_invented_numbers_are_rejected_and_reported(self, content) -> None:
        reply = json.dumps({"terms": [{"term": "chunk", "explanation": "Studies show that 87% of all chunks matter a great deal.",
                                       "significance": "Chunks are useful because they make retrieval more precise overall."}]})
        llm = ScriptedLLM({"keyword explanations": reply})
        out = llm.explain_keywords(content, [TermPacket("chunk", None)], PARAMS)["chunk"]
        assert out.explanation == () and len(out.significance) == 1
        assert any("87%" in w for w in llm.drain_warnings())
        assert llm.drain_warnings() == []  # draining empties the channel

    def test_unknown_terms_in_the_reply_are_ignored(self, content) -> None:
        reply = json.dumps({"terms": [{"term": "unrelated", "explanation": "x", "significance": "y"}]})
        assert ScriptedLLM({"keyword explanations": reply}).explain_keywords(
            content, [TermPacket("chunk", None)], PARAMS) == {}

    def test_garbage_reply_raises_response_error(self, content) -> None:
        with pytest.raises(BackendResponseError):
            ScriptedLLM({"keyword explanations": "I cannot help with that."}).explain_keywords(
                content, [TermPacket("chunk", None)], PARAMS)

    def test_language_instruction_reaches_the_prompt(self, content) -> None:
        llm = ScriptedLLM()
        llm.explain_keywords(content, [TermPacket("chunk", None)], GenerationParams(language=Language.TELUGU))
        assert "Telugu" in llm.calls[0][2] and "technical terms in English" in llm.calls[0][2]
        llm2 = ScriptedLLM()
        llm2.explain_keywords(content, [TermPacket("chunk", None)], GenerationParams(language=Language.BILINGUAL))
        assert "twice" in llm2.calls[0][2]

    def test_analogies_are_ai_generated_and_only_for_known_terms(self, content) -> None:
        reply = json.dumps({"examples": [
            {"concept": "chunk", "kind": "application", "text": "A support team can use chunks to index its product manuals for search."},
            {"concept": "stranger", "kind": "analogy", "text": "Not requested."}]})
        out = ScriptedLLM({"analogies": reply}).create_analogies(content, [TermPacket("chunk", None)], PARAMS)
        assert [(e.concept, e.kind, e.provenance) for e in out] == [("chunk", "application", NoteProvenance.AI_GENERATED)]

    def test_analogy_with_invented_statistic_is_dropped(self, content) -> None:
        reply = json.dumps({"examples": [{"concept": "chunk", "kind": "analogy", "text": "Like 500 library books."}]})
        assert ScriptedLLM({"analogies": reply}).create_analogies(content, [TermPacket("chunk", None)], PARAMS) == []

    def test_questions_are_validated_and_labelled(self, content) -> None:
        r = ScriptedLLM().create_questions(content, QuestionSpec(), PARAMS)
        assert (len(r.short), len(r.descriptive), len(r.mcq)) == (10, 5, 10)
        assert all(q.question_provenance is NoteProvenance.AI_GENERATED and q.answer_provenance is NoteProvenance.AI_GENERATED
                   for q in r.short + r.descriptive + r.mcq)
        assert all(validate_question(q) == [] for q in r.mcq)

    def test_invalid_mcqs_are_rejected(self, content) -> None:
        bad = {"questions": [
            {"question": "Three options only?", "options": ["a", "b", "c"], "correct_index": 0, "explanation": "e"},
            {"question": "Duplicate options?", "options": ["a", "a", "b", "c"], "correct_index": 0, "explanation": "e"},
            {"question": "Index out of range?", "options": ["a", "b", "c", "d"], "correct_index": 7, "explanation": "e"},
            {"question": "Boolean index?", "options": ["a", "b", "c", "d"], "correct_index": True, "explanation": "e"},
            {"question": "No explanation?", "options": ["a", "b", "c", "d"], "correct_index": 1, "explanation": ""},
            "not even an object",
            {"question": "Fine?", "options": ["a", "b", "c", "d"], "correct_index": 2, "explanation": "Because."},
        ]}
        llm = ScriptedLLM({"mcq questions": json.dumps(bad)})
        r = llm.create_questions(content, QuestionSpec(short=0, descriptive=0, mcq=10), PARAMS)
        assert [q.question for q in r.mcq] == ["Fine?"] and r.mcq[0].correct_letter == "C"
        assert any("1 of 10" in n for n in r.notes)

    def test_unusable_question_reply_becomes_a_note_not_a_crash(self, content) -> None:
        r = ScriptedLLM({"short questions": "nonsense"}).create_questions(content, QuestionSpec(), PARAMS)
        assert r.short == [] and any("short questions could not be used" in n for n in r.notes)
        assert len(r.mcq) == 10  # the other kinds still worked

    def test_long_input_is_batched_and_every_prompt_stays_within_budget(self) -> None:
        from tests.helpers import F, H, P, content_of
        sections = [b for i in range(40) for b in (H(f"Section {i}"), P(F(str(i)) + " " + F(str(i + 100)) * 6))]
        big = content_of(*sections)

        def explain(user: str) -> str:  # Phase 8.1 (D-7): explanations must be supported by the article text
            from tests.helpers import payload_of
            return json.dumps({"sections": [{"id": s["id"], "explanation": "This part covers " + " ".join(s["text"].split()[:20])}
                                            for s in payload_of(user)]})

        llm = ScriptedLLM({"section explanations": explain})
        llm.max_prompt_chars = 4000
        packets = [SectionPacket(f"s{i}", f"Section {i}", (f"Section {i}",), F(str(i)) * 8, (i,)) for i in range(40)]
        out = llm.explain_sections(big, packets, PARAMS)
        assert len(out) == 40
        assert 2 <= len(llm.calls) <= 40  # batched rather than one call per section, and not one giant call
        assert all(len(user) < 4000 + 1500 for _, _, user in llm.calls)

    def test_single_oversized_section_is_truncated_with_a_warning(self, content) -> None:
        llm = ScriptedLLM()
        llm.max_prompt_chars = 500
        huge = SectionPacket("s1", "Huge", ("Huge",), "word " * 2000, (1,))
        llm.explain_sections(content, [huge], PARAMS)
        assert any("very long" in w for w in llm.drain_warnings())
        assert len(payload_of(llm.calls[0][2])[0]["text"]) <= 500


# ============================================================================ validate_question
class TestValidateQuestion:
    from src.notes_model import Question

    def mcq(self, **kw: Any):
        from src.notes_model import Question
        base = dict(kind=QuestionKind.MCQ, question="Q?", answer="b", options=("a", "b", "c", "d"), correct_index=1,
                    explanation="because")
        base.update(kw)
        return Question(**base)

    def test_valid(self) -> None:
        assert validate_question(self.mcq()) == []

    @pytest.mark.parametrize("kw,needle", [
        ({"options": ("a", "b", "c")}, "4 options"), ({"options": ("a", "A", "c", "d")}, "distinct"),
        ({"options": ("a", "", "c", "d"), "answer": ""}, "distinct"), ({"correct_index": 4}, "out of range"),
        ({"correct_index": None}, "out of range"), ({"answer": "zzz"}, "does not match"),
        ({"explanation": " "}, "explanation"), ({"question": ""}, "empty question"),
    ])
    def test_problems_detected(self, kw: dict, needle: str) -> None:
        assert any(needle in p for p in validate_question(self.mcq(**kw)))

    def test_non_mcq_may_not_carry_options(self) -> None:
        from src.notes_model import Question
        q = Question(QuestionKind.SHORT, "Q?", "A", options=("a", "b", "c", "d"), correct_index=0)
        assert any("only MCQs" in p for p in validate_question(q))
