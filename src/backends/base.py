"""Backend interface for the generative parts of the notes (Phase 5).

The notes generator owns the chapter structure and everything that can be taken verbatim
from the article. Backends supply only the parts that need language generation:

* explanations of keywords, and of sections
* analogies / real-world applications
* questions and answers

A backend never sees chapter layout, and the generator never sees prompts or HTTP.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional, Sequence

from src.content_processor import ProcessedContent
from src.notes_model import BackendInfo, DetailLevel, Language, NoteProvenance, Question


# =========================================================================== errors
class BackendError(Exception):
    """Base class for backend failures."""


class UnknownBackendError(BackendError, ValueError):
    """The requested backend name does not exist."""


class BackendUnavailableError(BackendError):
    """The backend cannot be used right now (server down, package missing, not configured)."""


class BackendNotAuthorizedError(BackendError):
    """The user has not enabled or has declined a call that would leave this machine or cost money."""


class BackendResponseError(BackendError):
    """The model answered, but not in a usable or trustworthy form."""


# =========================================================================== requests / results
@dataclass(frozen=True)
class Availability:
    available: bool
    reason: str = ""


@dataclass(frozen=True)
class GenerationParams:
    detail: DetailLevel = DetailLevel.INTERMEDIATE
    language: Language = Language.ENGLISH
    contexts_per_term: int = 2


@dataclass(frozen=True)
class SourceSentence:
    """A sentence quoted from the article with the block it came from."""

    text: str
    source_index: int


@dataclass(frozen=True)
class TermPacket:
    """Everything the article says about one keyword, handed to a backend."""

    term: str
    definition: Optional[str]  # verbatim definition text, if the article gives one
    contexts: tuple[SourceSentence, ...] = ()  # other sentences that mention the term
    significance_contexts: tuple[SourceSentence, ...] = ()  # recommendations / facts / conclusions mentioning it
    aliases: tuple[str, ...] = ()
    source_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class SectionPacket:
    id: str
    heading: str
    path: tuple[str, ...]
    text: str  # the section's own text (code kept in fences)
    source_indices: tuple[int, ...]


@dataclass(frozen=True)
class GeneratedText:
    text: str
    provenance: NoteProvenance
    source_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class TermNote:
    explanation: tuple[GeneratedText, ...] = ()
    significance: tuple[GeneratedText, ...] = ()


@dataclass(frozen=True)
class ExampleNote:
    concept: str
    kind: str  # "analogy" | "application" | "example"
    text: str
    provenance: NoteProvenance = NoteProvenance.AI_GENERATED
    review_needed: bool = False  # D-7: makes a comparison the article never makes; kept, but a human should check it


@dataclass(frozen=True)
class QuestionSpec:
    short: int = 10
    descriptive: int = 5
    mcq: int = 10


@dataclass
class QuestionResult:
    short: list[Question] = field(default_factory=list)
    descriptive: list[Question] = field(default_factory=list)
    mcq: list[Question] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # e.g. shortfall explanations


# =========================================================================== interface
class NotesBackend(abc.ABC):
    """Abstract notes backend. Implementations must be usable without the generator."""

    supports_translation: bool = False

    def __init__(self) -> None:
        self._warnings: list[str] = []

    @property
    @abc.abstractmethod
    def info(self) -> BackendInfo:
        """Static description of the backend (name, model, cost, locality)."""

    def check_available(self) -> Availability:
        """Cheap, side-effect-free check. Must never make a paid call."""
        return Availability(True)

    @abc.abstractmethod
    def explain_keywords(
        self, content: ProcessedContent, packets: Sequence[TermPacket], params: GenerationParams
    ) -> dict[str, TermNote]:
        """Explanation and practical significance for each term, keyed by ``packet.term``."""

    @abc.abstractmethod
    def explain_sections(
        self, content: ProcessedContent, packets: Sequence[SectionPacket], params: GenerationParams
    ) -> dict[str, tuple[GeneratedText, ...]]:
        """Additional explanation per section id (empty for backends that cannot write prose)."""

    @abc.abstractmethod
    def create_analogies(
        self, content: ProcessedContent, packets: Sequence[TermPacket], params: GenerationParams
    ) -> list[ExampleNote]:
        """Analogies / applications. Local backends return an empty list rather than invent any."""

    @abc.abstractmethod
    def create_questions(
        self, content: ProcessedContent, spec: QuestionSpec, params: GenerationParams
    ) -> QuestionResult:
        """Short-answer, descriptive and multiple-choice questions."""

    # ---- warnings channel (drained by the generator)
    def _warn(self, message: str) -> None:
        self._warnings.append(message)

    def drain_warnings(self) -> list[str]:
        out, self._warnings = self._warnings, []
        return out
