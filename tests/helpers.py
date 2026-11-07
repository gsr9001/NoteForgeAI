"""Shared builders and test doubles for Phase 5 tests (no network, no real model)."""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Callable, Optional, Union

from src.article_extractor import Article, Block, BlockKind, Completeness, ListItem, SubstackExtractor
from src.backends.llm import LLMBackend
from src.content_processor import ProcessedContent, process_article
from src.notes_model import BackendInfo, BackendKind

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def P(text: str) -> Block:
    return Block(BlockKind.PARAGRAPH, text)


def H(text: str, level: int = 2) -> Block:
    return Block(BlockKind.HEADING, text, level=level)


def Q(text: str) -> Block:
    return Block(BlockKind.QUOTE, text)


def C(text: str, language: Optional[str] = None) -> Block:
    return Block(BlockKind.CODE, text, language=language)


def L(*items: str, ordered: bool = False, depths: Optional[list[int]] = None) -> Block:
    depths = depths or [0] * len(items)
    return Block(BlockKind.LIST, items=tuple(ListItem(t, d) for t, d in zip(items, depths)), ordered=ordered)


def T(*rows: tuple[str, ...], header: bool = True) -> Block:
    return Block(BlockKind.TABLE, rows=tuple(rows), has_header=header)


def art(*blocks: Block, completeness: Completeness = Completeness.COMPLETE, **kw: Any) -> Article:
    return Article(title=str(kw.pop("title", "Test Article")), blocks=list(blocks), completeness=completeness, **kw)


def content_of(*blocks: Block, **kw: Any) -> ProcessedContent:
    return process_article(art(*blocks, **kw))


_SYL = ["ba", "ko", "mi", "ra", "su", "te", "no", "li"]
_VOCAB = [a + b + c for a in _SYL for b in _SYL for c in _SYL]


def F(tag: str) -> str:
    """A prose paragraph unlike any other F(tag): used where tests need 'some other text'."""
    return " ".join(random.Random(tag).sample(_VOCAB, 24)) + "."


def fixture_content(name: str) -> ProcessedContent:
    ex = SubstackExtractor()
    if name.endswith(".txt"):
        return process_article(ex.extract_from_text(load(name)))
    return process_article(ex.extract_from_html(load(name)))


# --------------------------------------------------------------------------- scripted language model
def payload_of(user_prompt: str) -> Any:
    """The JSON material that LLMBackend puts after 'ARTICLE MATERIAL:' in every prompt."""
    return json.loads(user_prompt.split("ARTICLE MATERIAL:\n", 1)[1])


class ScriptedLLM(LLMBackend):
    """An LLMBackend whose 'model' is a Python function. Records every prompt it receives."""

    def __init__(self, responses: Optional[dict[str, Union[str, Callable[[str], str], Exception]]] = None,
                 kind: BackendKind = BackendKind.OLLAMA) -> None:
        super().__init__()
        self.responses = responses or {}
        self.calls: list[tuple[str, str, str]] = []
        self._kind = kind

    @property
    def info(self) -> BackendInfo:
        return BackendInfo("Scripted", self._kind, "fake-model", True, False, "test double")

    def _complete(self, system: str, user: str, purpose: str) -> str:
        self.calls.append((purpose, system, user))
        spec = self.responses.get(purpose)
        if isinstance(spec, Exception):
            raise spec
        if callable(spec):
            return spec(user)
        if isinstance(spec, str):
            return spec
        return self.default(purpose, user)

    @staticmethod
    def default(purpose: str, user: str) -> str:
        if purpose == "keyword explanations":
            return json.dumps({"terms": [{
                "term": t["term"],
                "explanation": f"In this article {t['term']} is an idea that learners should understand step by step.",
                "significance": f"Knowing {t['term']} helps a practitioner make better design decisions in practice."}
                for t in payload_of(user)]})
        if purpose == "section explanations":
            return json.dumps({"sections": [{"id": s["id"], "explanation": f"This section on {s['heading']} "
                                             "builds the reader's understanding step by step and links each idea "
                                             "to the next one."} for s in payload_of(user)]})
        if purpose == "analogies":
            return json.dumps({"examples": [
                item for t in payload_of(user)[:3] for item in (
                    {"concept": t["term"], "kind": "analogy",
                     "text": f"Think of {t['term']} like a librarian fetching the right book for a reader."},
                    {"concept": t["term"], "kind": "application",
                     "text": f"A support team could use {t['term']} to answer customer questions from its manuals."})]})
        if purpose == "short questions":
            return json.dumps({"questions": [{"question": f"Short question number {i}?", "answer": f"Short answer {i}."}
                                             for i in range(10)]})
        if purpose == "descriptive questions":
            return json.dumps({"questions": [{"question": f"Explain descriptive topic {i}.", "answer": f"Because {i}."}
                                             for i in range(5)]})
        if purpose == "mcq questions":
            return json.dumps({"questions": [{
                "question": f"Which option is right for item {i}?",
                "options": [f"Option A{i}", f"Option B{i}", f"Option C{i}", f"Option D{i}"],
                "correct_index": i % 4, "explanation": f"Option {'ABCD'[i % 4]}{i} follows from the article."}
                for i in range(10)]})
        raise AssertionError(f"unexpected purpose {purpose}")


_PURPOSE_MARKERS = {
    "keyword explanations": "(a) a detailed explanation", "section explanations": "For each section",
    "analogies": "two illustrations", "short questions": "short-answer",
    "descriptive questions": "descriptive questions", "mcq questions": "multiple-choice",
}


def guess_purpose(user_prompt: str) -> str:
    """Which task a prompt belongs to (lets a fake server answer like a model would)."""
    for purpose, marker in _PURPOSE_MARKERS.items():
        if marker in user_prompt:
            return purpose
    raise AssertionError("unrecognised prompt")


# --------------------------------------------------------------------------- fake OpenAI SDK client
class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


class FakeOpenAIClient:
    """Mimics ``client.chat.completions.create``; answers via ScriptedLLM.default. Records kwargs."""

    def __init__(self, fail: Optional[Exception] = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail
        self.chat = self
        self.completions = self

    def create(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        if self.fail:
            raise self.fail
        user = kwargs["messages"][1]["content"]
        return _Resp(ScriptedLLM.default(guess_purpose(user), user))


# --------------------------------------------------------------------------- fake page fetcher (no network)
from src.article_extractor import FetchedPage  # noqa: E402


class FakeFetcher:
    """Returns canned HTML (or raises a canned error) instead of downloading anything."""

    def __init__(self, html: str = "", final_url: Optional[str] = None, error: Optional[BaseException] = None) -> None:
        self.html, self.final_url, self.error, self.requested = html, final_url, error, []

    def fetch(self, url: str) -> FetchedPage:
        self.requested.append(url)
        if self.error is not None:
            raise self.error
        return FetchedPage(url=self.final_url or url, html=self.html)
