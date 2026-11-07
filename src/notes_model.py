"""Typed models shared by the notes generator and the notes backends (Phase 5).

Every piece of text in the notes carries a :class:`NoteProvenance`:

* ``ORIGINAL``     - verbatim text from the article (quoted, not rewritten).
* ``DERIVED``      - written by this tool's deterministic rules: structure, labels, counts,
  questions assembled from article sentences. No language model involved.
* ``AI_GENERATED`` - produced by a language model (Ollama / OpenAI). Never the article's words.

Keeping this vocabulary in one module lets backends and the generator stay independent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Iterator, Optional

from src.article_extractor import Completeness


class NoteProvenance(str, Enum):
    ORIGINAL = "original"
    DERIVED = "derived"
    AI_GENERATED = "ai_generated"


class ItemKind(str, Enum):
    TEXT = "text"
    BULLETS = "bullets"
    NUMBERED = "numbered"
    CODE = "code"
    TABLE = "table"
    QUOTE = "quote"
    DEFINITION = "definition"
    CHECKLIST = "checklist"
    NOTICE = "notice"


class DetailLevel(str, Enum):
    BASIC = "Basic"
    INTERMEDIATE = "Intermediate"
    ADVANCED = "Advanced"

    @classmethod
    def parse(cls, value: "str | DetailLevel") -> "DetailLevel":
        if isinstance(value, DetailLevel):
            return value
        for member in cls:
            if member.value.casefold() == str(value).strip().casefold():
                return member
        raise ValueError(f"Unknown detail level {value!r}; choose Basic, Intermediate or Advanced.")


class Language(str, Enum):
    ENGLISH = "English"
    TELUGU = "Telugu"
    BILINGUAL = "Bilingual"

    @classmethod
    def parse(cls, value: "str | Language") -> "Language":
        if isinstance(value, Language):
            return value
        for member in cls:
            if member.value.casefold() == str(value).strip().casefold():
                return member
        raise ValueError(f"Unknown language {value!r}; choose English, Telugu or Bilingual.")


class BackendKind(str, Enum):
    LOCAL = "local"
    OLLAMA = "ollama"
    OPENAI = "openai"


@dataclass(frozen=True)
class BackendInfo:
    """What generated the notes. Shown to the user and stored in the notes."""

    name: str
    kind: BackendKind
    model: Optional[str] = None
    is_local: bool = True  # runs on this machine; nothing leaves it
    is_paid: bool = False
    description: str = ""

    def display(self) -> str:
        model = f", model {self.model}" if self.model else ""
        where = "runs on this machine" if self.is_local else "sends article text to an external service"
        cost = "PAID API" if self.is_paid else "free"
        return f"{self.name}{model} [{cost}; {where}] - {self.description}"


@dataclass(frozen=True)
class NoteItem:
    """One renderable piece of the notes, with provenance and source references."""

    kind: ItemKind
    provenance: NoteProvenance
    text: str = ""
    items: tuple[str, ...] = ()
    depths: tuple[int, ...] = ()
    #: Per-item list type for lists that mix numbered and bullet levels (empty = every item follows ``kind``).
    ordered: tuple[bool, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    has_header: bool = False
    language: Optional[str] = None  # code language
    term: Optional[str] = None  # DEFINITION: the term being defined
    label: Optional[str] = None  # e.g. "Definition", "AI-generated analogy"
    source_indices: tuple[int, ...] = ()  # indices into article.blocks
    level: str = "info"  # NOTICE only: "info" or "warning"
    excerpt: bool = False  # True when only selected sentences of a paragraph are shown

    @property
    def is_original(self) -> bool:
        return self.provenance is NoteProvenance.ORIGINAL

    @property
    def is_ai_generated(self) -> bool:
        return self.provenance is NoteProvenance.AI_GENERATED

    @property
    def plain_text(self) -> str:
        if self.items:
            return "\n".join(self.items)
        if self.rows:
            return "\n".join(" | ".join(r) for r in self.rows)
        return self.text

    @property
    def word_count(self) -> int:
        return len(self.plain_text.split())


@dataclass
class NoteSection:
    heading: str
    level: int = 2
    items: list[NoteItem] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class QuestionKind(str, Enum):
    SHORT = "short"
    DESCRIPTIVE = "descriptive"
    MCQ = "mcq"


@dataclass(frozen=True)
class Question:
    kind: QuestionKind
    question: str
    answer: str  # for MCQs: the text of the correct option
    options: tuple[str, ...] = ()  # MCQ: exactly four
    correct_index: Optional[int] = None
    explanation: str = ""
    question_provenance: NoteProvenance = NoteProvenance.DERIVED
    answer_provenance: NoteProvenance = NoteProvenance.ORIGINAL
    explanation_provenance: NoteProvenance = NoteProvenance.DERIVED
    source_indices: tuple[int, ...] = ()

    @property
    def correct_letter(self) -> Optional[str]:
        return "ABCD"[self.correct_index] if self.correct_index is not None and 0 <= self.correct_index < 4 else None


def validate_question(q: Question) -> list[str]:
    """Structural problems with a question (empty list means it is well-formed).

    MCQs must have exactly four distinct, non-empty options, a valid correct index, an answer
    equal to the correct option, and an explanation.
    """
    problems: list[str] = []
    if not q.question.strip():
        problems.append("empty question text")
    if not q.answer.strip():
        problems.append("empty answer")
    if q.kind is QuestionKind.MCQ:
        if len(q.options) != 4:
            problems.append(f"MCQ must have 4 options, found {len(q.options)}")
        elif len({o.strip().casefold() for o in q.options}) != 4 or any(not o.strip() for o in q.options):
            problems.append("MCQ options must be four distinct, non-empty strings")
        if q.correct_index is None or not 0 <= q.correct_index < len(q.options):
            problems.append("MCQ correct_index is missing or out of range")
        elif q.options[q.correct_index] != q.answer:
            problems.append("MCQ answer does not match the option at correct_index")
        if not q.explanation.strip():
            problems.append("MCQ has no explanation")
    elif q.options or q.correct_index is not None:
        problems.append("only MCQs may have options")
    return problems


@dataclass
class Chapter:
    number: int
    title: str
    sections: list[NoteSection] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)  # chapter 7 only
    notices: list[NoteItem] = field(default_factory=list)  # shown at the top of the chapter

    def items(self) -> Iterator[NoteItem]:
        yield from self.notices
        for section in self.sections:
            yield from section.items

    @property
    def word_count(self) -> int:
        return sum(i.word_count for i in self.items())


@dataclass
class Notes:
    """The complete generated notes: seven chapters plus the context needed to trust them."""

    title: str
    author: Optional[str]
    published: Optional[date]
    url: Optional[str]
    completeness: Completeness
    completeness_reasons: tuple[str, ...]
    source_warnings: tuple[str, ...]
    backend: BackendInfo
    requested_backend: str
    fallback_reason: Optional[str]
    detail: DetailLevel
    language: Language
    banners: list[NoteItem]
    chapters: list[Chapter]
    generation_warnings: list[str] = field(default_factory=list)

    @property
    def is_partial(self) -> bool:
        return self.completeness is Completeness.PARTIAL

    @property
    def used_ai(self) -> bool:
        return any(i.is_ai_generated for i in self.all_items()) or any(
            q.question_provenance is NoteProvenance.AI_GENERATED for c in self.chapters for q in c.questions
        )

    def chapter(self, number: int) -> Chapter:
        for c in self.chapters:
            if c.number == number:
                return c
        raise KeyError(f"No chapter {number}")

    def all_items(self) -> Iterator[NoteItem]:
        yield from self.banners
        for chapter in self.chapters:
            yield from chapter.items()

    def outline(self) -> str:
        lines = [f"{self.title} - notes via {self.backend.name} ({self.completeness.value})"]
        for c in self.chapters:
            lines.append(f"Chapter {c.number}: {c.title} - {len(c.sections)} sections, "
                         f"{len(c.questions)} questions, {c.word_count} words")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        def item(i: NoteItem) -> dict[str, Any]:
            d: dict[str, Any] = {"kind": i.kind.value, "provenance": i.provenance.value}
            for key in ("text", "label", "term", "language", "level"):
                value = getattr(i, key)
                if value and value != "info":
                    d[key] = value
            if i.items:
                d["items"] = list(i.items)
            if any(i.depths):  # keep the nesting in the JSON form too (a flat list would change the meaning)
                d["depths"] = list(i.depths)
            if i.ordered:
                d["ordered"] = list(i.ordered)
            if i.rows:
                d["rows"] = [list(r) for r in i.rows]
            if i.source_indices:
                d["source_indices"] = list(i.source_indices)
            return d

        def question(q: Question) -> dict[str, Any]:
            d = {"kind": q.kind.value, "question": q.question, "answer": q.answer,
                 "question_provenance": q.question_provenance.value,
                 "answer_provenance": q.answer_provenance.value, "source_indices": list(q.source_indices)}
            if q.options:
                d.update(options=list(q.options), correct_index=q.correct_index, explanation=q.explanation)
            return d

        return {
            "title": self.title, "author": self.author,
            "published": self.published.isoformat() if self.published else None, "url": self.url,
            "completeness": self.completeness.value, "completeness_reasons": list(self.completeness_reasons),
            "backend": {"name": self.backend.name, "kind": self.backend.kind.value, "model": self.backend.model,
                        "is_paid": self.backend.is_paid, "is_local": self.backend.is_local},
            "requested_backend": self.requested_backend, "fallback_reason": self.fallback_reason,
            "detail": self.detail.value, "language": self.language.value,
            "banners": [item(b) for b in self.banners],
            "warnings": [*self.source_warnings, *self.generation_warnings],
            "chapters": [{
                "number": c.number, "title": c.title,
                "notices": [item(n) for n in c.notices],
                "sections": [{"heading": s.heading, "level": s.level, "items": [item(i) for i in s.items]}
                             for s in c.sections],
                "questions": [question(q) for q in c.questions],
            } for c in self.chapters],
        }
