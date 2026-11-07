"""Notes generation (Phase 5): ProcessedContent -> seven-chapter educational notes.

Division of labour
------------------
* This module owns the chapter structure and everything that can be taken *verbatim* from
  the article (marked ORIGINAL, with the article block indices it came from).
* A :class:`~src.backends.base.NotesBackend` supplies only what needs language generation:
  keyword explanations, section explanations, analogies and questions. Local mode produces
  no new prose; Ollama / OpenAI output is always marked AI_GENERATED.
* Text written by this tool's rules (labels, structure, questions assembled from article
  sentences) is marked DERIVED.

Guarantees, each enforced by :func:`verify_provenance` / :func:`validate_notes` and by tests:
* ORIGINAL text really occurs in the article.
* Partial or unverified articles are flagged in a banner and in every chapter.
* No sentence is repeated within a chapter, and the takeaways, keyword and revision
  chapters do not repeat each other's sentences.
* Nothing is sent to a paid service unless the user explicitly selected it and confirmed.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence, TypeVar

from src.article_extractor import BlockKind, Completeness
from src.backends import BackendSelection, LocalNotesBackend, NotesBackend
from src.backends.base import (
    BackendError, BackendNotAuthorizedError, BackendUnavailableError, ExampleNote, GeneratedText,
    GenerationParams, QuestionResult, QuestionSpec, SectionPacket, SourceSentence, TermNote, TermPacket,
)
from src.backends.local_questions import definition_answer, term_regex
from src.content_processor import is_definitional
from src.content_processor import (
    Confidence, ContentItem, Definition, Keyword, KeyStatement, ProcessedContent, Section, StatementKind,
    normalize_key, split_sentences, tokenize,
)
from src.notes_model import (
    BackendInfo, Chapter, DetailLevel, ItemKind, Language, NoteItem, NoteProvenance, NoteSection, Notes,
    Question, QuestionKind, validate_question,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")

ORIG, DER, AI = NoteProvenance.ORIGINAL, NoteProvenance.DERIVED, NoteProvenance.AI_GENERATED

CHAPTER_TITLES = {
    1: "Article Overview",
    2: "Detailed Concept Explanation",
    3: "Important Technical Keywords",
    4: "Real-Life Analogies and Applications",
    5: "Key Takeaways",
    6: "Quick Revision Notes",
    7: "Questions and Answers",
}
_KEYWORDS_BY_DETAIL = {DetailLevel.BASIC: 8, DetailLevel.INTERMEDIATE: 15, DetailLevel.ADVANCED: 25}
_CONTEXTS_BY_DETAIL = {DetailLevel.BASIC: 1, DetailLevel.INTERMEDIATE: 2, DetailLevel.ADVANCED: 3}
_TAKEAWAYS_BY_DETAIL = {DetailLevel.BASIC: 6, DetailLevel.INTERMEDIATE: 10, DetailLevel.ADVANCED: 16}

_OBJECTIVE_CUES = re.compile(
    r"\b(in this (post|article|essay|piece|guide|newsletter|issue)|this (post|article|essay|piece|guide|newsletter) "
    r"(will |is about |explains?|explores?|argues?|shows?|walks|covers|looks)|i (will|want to|aim to|argue)|"
    r"we (will|aim to|explore|show)|(the|our|my) (goal|aim|purpose|objective) (is|of))\b", re.IGNORECASE)
_PROBLEM_CUES = re.compile(
    r"\b(problem|challenge|struggle|limitation|issue|fail(s|ure)?|difficult(y|ies)?|risk|bottleneck|pain point|"
    r"hard to|cannot|can't|do not know|doesn't know)\b", re.IGNORECASE)
_CONTRIBUTION_CUES = re.compile(
    r"\b(we (propose|introduce|present|show|build|develop)|i (propose|introduce|present|show)|"
    r"this (post|article) (shows?|presents?|introduces?)|key idea|the solution|the approach|our (approach|method))\b",
    re.IGNORECASE)
_EXAMPLE_CUES = re.compile(
    r"\b(for example|for instance|such as|e\.g\.|imagine|suppose|in practice|use case|used (for|to)|applications?)\b",
    re.IGNORECASE)
_FORMULA = re.compile(r"\\frac|\\sum|\$[^$\n]+\$|[∑∫√≈≠≤≥±×÷∂∇]"
                      r"|\b[A-Za-z_]\w*\s*=\s*[^=\n]*?(?:[+\-*/^]|\w\()")


# =========================================================================== errors / config
class NotesGenerationError(Exception):
    """Base class for notes-generation failures."""


class InvalidContentError(NotesGenerationError):
    """The object passed in is not usable ProcessedContent."""


@dataclass(frozen=True)
class NotesConfig:
    detail: DetailLevel = DetailLevel.INTERMEDIATE
    language: Language = Language.ENGLISH
    include_questions: bool = True
    short_questions: int = 10
    descriptive_questions: int = 5
    mcq_questions: int = 10
    max_keywords: Optional[int] = None  # default depends on the detail level
    fallback_to_local: bool = True
    ai_section_explanations: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", DetailLevel.parse(self.detail))
        object.__setattr__(self, "language", Language.parse(self.language))
        for name in ("short_questions", "descriptive_questions", "mcq_questions"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        if self.max_keywords is not None and self.max_keywords < 1:
            raise ValueError("max_keywords must be at least 1")


# =========================================================================== text helpers
def is_formula(text: str) -> bool:
    """Heuristic: LaTeX, math symbols, or an assignment with an operator/function call on the right."""
    return bool(_FORMULA.search(text))


def shorten(text: str, max_words: int = 22) -> tuple[str, bool]:
    """Cut ``text`` at a clause boundary within ``max_words`` words. Returns (text, was_cut)."""
    words = text.split()
    if len(words) <= max_words:
        return text, False
    head = " ".join(words[:max_words])
    cut = max(head.rfind(","), head.rfind(";"), head.rfind(" — "))
    if cut > len(head) // 2:
        head = head[:cut]
    return head.rstrip(" ,;—"), True


def _key(text: str) -> str:
    return normalize_key(text)


def _near_duplicate(a: frozenset[str], b: frozenset[str]) -> bool:
    """Questions that ask the same thing: one is wholly contained in the other (an extended wording of it),
    or they are almost identical (Jaccard >= 0.9). A single differing word in a longer question ("...large chunk
    size?" vs "...small chunk size?") does NOT count: that is a different question."""
    if len(b) < 5:
        return False
    return a <= b or b <= a or len(a & b) / len(a | b) >= 0.9


class _Ledger:
    """Tracks which sentences each chapter has used so nothing is repeated."""

    def __init__(self) -> None:
        self.per_chapter: dict[int, set[str]] = {}
        self.shared: set[str] = set()  # sentences claimed by one chapter and not to be reused by others

    def claim(self, chapter: int, text: str) -> bool:
        key = _key(text)
        seen = self.per_chapter.setdefault(chapter, set())
        if not key or key in seen:
            return False
        seen.add(key)
        return True

    def reserve(self, text: str) -> None:
        self.shared.add(_key(text))

    def reserved(self, text: str) -> bool:
        return _key(text) in self.shared


# =========================================================================== verification
def _source_blob(content: ProcessedContent) -> str:
    parts = [content.title, content.author or "", content.url or ""]
    for section in content.iter_sections():
        if section.heading:
            parts.append(section.heading)
        parts.extend(item.text for item in section.items)
    return _key(" \n ".join(parts))


def verify_provenance(notes: Notes, content: ProcessedContent) -> list[str]:
    """Check that every ORIGINAL item and answer really occurs in the article.

    Returns a list of problems (empty when the notes are faithful). AI_GENERATED and DERIVED
    items are not checked here: they are not claimed to be the article's words.
    """
    blob = _source_blob(content)
    problems: list[str] = []

    def present(fragment: str) -> bool:
        k = _key(fragment)
        return not k or k in blob

    for item in notes.all_items():
        if not item.is_original or item.kind is ItemKind.NOTICE:
            continue
        if item.kind in (ItemKind.TEXT, ItemKind.QUOTE, ItemKind.DEFINITION):
            parts = split_sentences(item.text) or [item.text]
        elif item.kind in (ItemKind.BULLETS, ItemKind.NUMBERED, ItemKind.CHECKLIST):
            parts = list(item.items)
        elif item.kind is ItemKind.CODE:
            parts = [item.text]
        elif item.kind is ItemKind.TABLE:
            parts = [c for row in item.rows for c in row]
        else:
            parts = [item.text]
        for part in parts:
            if not present(part):
                problems.append(f"ORIGINAL {item.kind.value} not found in article: {part[:80]!r}")
    for chapter in notes.chapters:
        for q in chapter.questions:
            if q.answer_provenance is not ORIG:
                continue
            for line in q.answer.split("\n"):
                line = re.sub(r"^\s*(?:\d+\.|-)\s+", "", line)
                if not present(line):
                    problems.append(f"ORIGINAL answer not found in article: {line[:80]!r}")
            for option in q.options:
                if not present(option):
                    problems.append(f"ORIGINAL option not found in article: {option[:80]!r}")
    return problems


def validate_notes(notes: Notes, spec: Optional[QuestionSpec] = None) -> list[str]:
    """Structural checks over the whole notes object (empty list means all invariants hold)."""
    problems: list[str] = []
    if [c.number for c in notes.chapters] != [1, 2, 3, 4, 5, 6, 7]:
        problems.append("notes must contain chapters 1-7 in order")
    for c in notes.chapters:
        if c.title != CHAPTER_TITLES.get(c.number):
            problems.append(f"chapter {c.number} has an unexpected title {c.title!r}")
        keys: set[str] = set()
        for section in c.sections:
            for item in section.items:
                if item.kind is ItemKind.TEXT and item.provenance is not DER:
                    k = _key(item.text)
                    if k in keys:
                        problems.append(f"chapter {c.number} repeats text: {item.text[:60]!r}")
                    keys.add(k)
        seen_q: set[str] = set()
        for q in c.questions:
            problems += [f"chapter {c.number} question {q.question[:40]!r}: {p}" for p in validate_question(q)]
            if _key(q.question) in seen_q:
                problems.append(f"chapter {c.number} repeats question {q.question[:60]!r}")
            seen_q.add(_key(q.question))
        if notes.is_partial and not any("PARTIAL" in n.text for n in c.notices):
            problems.append(f"chapter {c.number} is missing the partial-content notice")
    if notes.is_partial and not any("PARTIAL" in b.text for b in notes.banners):
        problems.append("notes are missing the partial-content banner")
    if spec is not None and notes.chapters:
        qs = notes.chapter(7).questions
        for kind, want in ((QuestionKind.SHORT, spec.short), (QuestionKind.DESCRIPTIVE, spec.descriptive),
                           (QuestionKind.MCQ, spec.mcq)):
            if sum(q.kind is kind for q in qs) > want:
                problems.append(f"more {kind.value} questions than requested")
    return problems


# =========================================================================== generator
class NotesGenerator:
    """Builds :class:`Notes` from :class:`ProcessedContent` using a chosen backend."""

    def __init__(
        self,
        backend: "NotesBackend | BackendSelection | None" = None,
        config: Optional[NotesConfig] = None,
    ) -> None:
        if isinstance(backend, BackendSelection):
            self._selection: Optional[BackendSelection] = backend
            self.backend: NotesBackend = backend.backend
        elif backend is None or isinstance(backend, NotesBackend):
            self._selection = None
            self.backend = backend or LocalNotesBackend()
        else:
            raise NotesGenerationError(
                f"backend must be a NotesBackend or BackendSelection (see resolve_backend), got {type(backend).__name__}."
            )
        if config is not None and not isinstance(config, NotesConfig):
            raise NotesGenerationError(f"config must be a NotesConfig, got {type(config).__name__}.")
        self.config = config or NotesConfig()

    def generate(self, content: ProcessedContent) -> Notes:
        if not isinstance(content, ProcessedContent):
            raise InvalidContentError(f"Expected ProcessedContent, got {type(content).__name__}.")
        if not any(True for _ in content.iter_items()):
            raise InvalidContentError("The processed content has no items to write notes from.")
        requested = self._selection.requested if self._selection else self.backend.info.kind.value
        fallback = self._selection.fallback_reason if self._selection else None
        run = _Run(content, self.backend, self.config, requested, fallback)
        return run.build()


def generate_notes(
    content: ProcessedContent,
    backend: "NotesBackend | BackendSelection | None" = None,
    config: Optional[NotesConfig] = None,
) -> Notes:
    """Convenience wrapper around :class:`NotesGenerator`."""
    return NotesGenerator(backend, config).generate(content)


# =========================================================================== one generation run
class _Run:
    def __init__(self, content: ProcessedContent, backend: NotesBackend, config: NotesConfig,
                 requested: str, fallback_reason: Optional[str]) -> None:
        self.content, self.config = content, config
        self.backend = backend
        self.requested, self.fallback_reason = requested, fallback_reason
        self.warnings: list[str] = []
        self.ledger = _Ledger()
        self.items: list[ContentItem] = list(content.iter_items())
        self.sections: list[Section] = list(content.iter_sections())
        self.section_of: dict[int, Section] = {i.source_index: s for s in self.sections for i in s.items}
        self.sentences: list[tuple[str, int]] = [
            (s.text, it.source_index) for it in self.items
            if it.kind in (BlockKind.PARAGRAPH, BlockKind.QUOTE, BlockKind.LIST) for s in it.sentences
        ]
        self.language = config.language
        self.intro_indices: set[int] = set()  # blocks quoted in Chapter 1; not repeated in Chapter 2's preamble
        self.params = GenerationParams(config.detail, config.language, _CONTEXTS_BY_DETAIL[config.detail])
        self.term_packets: list[TermPacket] = []
        self.takeaways: list[KeyStatement] = []
        self.takeaways_left_out: dict[StatementKind, int] = {}  # D-10: candidates cut by the size limit, per kind
        self.banners: list[NoteItem] = []
        self._resolve_backend()

    # ------------------------------------------------------------------ backend handling
    def _resolve_backend(self) -> None:
        availability = self.backend.check_available()
        if not availability.available:
            if not self.config.fallback_to_local:
                raise BackendUnavailableError(availability.reason)
            self._fall_back(f"{self.backend.info.name} is unavailable: {availability.reason}")
        if self.language is not Language.ENGLISH and not self.backend.supports_translation:
            self.warnings.append(f"{self.language.value} output was requested but the {self.backend.info.name} "
                                 "backend cannot translate; the notes are in English.")
            self.language = Language.ENGLISH
            self.params = GenerationParams(self.config.detail, Language.ENGLISH, self.params.contexts_per_term)

    def _fall_back(self, reason: str) -> None:
        logger.warning("Falling back to the local backend: %s", reason)
        self.warnings.append(f"Using the local backend instead: {reason}")
        self.fallback_reason = reason if not self.fallback_reason else f"{self.fallback_reason}; {reason}"
        self.backend = LocalNotesBackend()

    def _call(self, what: str, func: Callable[[NotesBackend], T], local_default: Callable[[NotesBackend], T]) -> T:
        """Call the backend; on failure fall back to the local backend for that part only."""
        backend = self.backend  # captured: a fallback below replaces self.backend
        try:
            return func(backend)
        except BackendNotAuthorizedError as exc:
            self._fall_back(f"{exc} (no further AI requests will be made)")
        except BackendUnavailableError as exc:
            # The server went away (or the model vanished) mid-run: retrying every remaining part would only
            # repeat the failure, so honour the fallback setting once for the rest of the run.
            if not self.config.fallback_to_local:
                raise
            self._fall_back(f"{exc} (no further requests will be made to it)")
        except BackendError as exc:
            self.warnings.append(f"The {self.backend.info.name} backend could not produce {what} ({exc}); "
                                 "local output was used for that part.")
            logger.warning("Backend failure during %s: %s", what, exc)
        finally:
            self.warnings.extend(backend.drain_warnings())
        return local_default(LocalNotesBackend())

    # ------------------------------------------------------------------ item builders
    @staticmethod
    def notice(text: str, level: str = "info") -> NoteItem:
        return NoteItem(ItemKind.NOTICE, DER, text=text, level=level)

    @staticmethod
    def quote_item(text: str, index: int, label: Optional[str] = None, excerpt: bool = False) -> NoteItem:
        return NoteItem(ItemKind.TEXT, ORIG, text=text, label=label, source_indices=(index,), excerpt=excerpt)

    def _sentences_with(self, term: str) -> list[tuple[str, int]]:
        """Distinct sentences mentioning ``term`` (an article may repeat a sentence in several places)."""
        rx = term_regex(term)
        seen: set[str] = set()
        out: list[tuple[str, int]] = []
        for text, index in self.sentences:
            k = _key(text)
            if k not in seen and rx.search(text):
                seen.add(k)
                out.append((text, index))
        return out

    # ------------------------------------------------------------------ build
    def build(self) -> Notes:
        c = self.content
        self.takeaways = self._select_takeaways()  # chosen first so later chapters can avoid repeating them
        for s in self.takeaways:
            self.ledger.reserve(s.text)
        ch3 = self._chapter3()
        chapters = [self._chapter1(), self._chapter2(), ch3, self._chapter4(), self._chapter5(),
                    self._chapter6(), self._chapter7()]
        self._add_status_notices(chapters)
        banners = self._banners()
        notes = Notes(
            title=c.title, author=c.author, published=c.published, url=c.url, completeness=c.completeness,
            completeness_reasons=c.completeness_reasons, source_warnings=tuple(c.all_warnings),
            backend=self.backend.info, requested_backend=self.requested, fallback_reason=self.fallback_reason,
            detail=self.config.detail, language=self.language, banners=banners, chapters=chapters,
            generation_warnings=self.warnings,
        )
        for problem in verify_provenance(notes, c):  # internal consistency check; never expected to fire
            logger.error("Provenance check failed: %s", problem)
            self.warnings.append(f"Internal check: {problem}")
        logger.info("Generated notes with %s: %d words, %d questions", self.backend.info.name,
                    sum(ch.word_count for ch in chapters), len(chapters[6].questions))
        return notes

    def _banners(self) -> list[NoteItem]:
        c = self.content
        out = [self.notice(f"Generated with: {self.backend.info.display()}")]
        if self.fallback_reason:
            out.append(self.notice(f"Requested backend '{self.requested}' was not used: {self.fallback_reason}", "warning"))
        if c.completeness is Completeness.PARTIAL:
            out.append(self.notice(self._partial_text(), "warning"))
        elif c.completeness is Completeness.UNVERIFIED:
            out.append(self.notice(self._unverified_text()))
        for w in self.warnings:
            if "cannot translate" in w:
                out.append(self.notice(w, "warning"))
        return out

    def _partial_text(self) -> str:
        reason = self.content.completeness_reasons[0] if self.content.completeness_reasons else "the article was truncated"
        return (f"PARTIAL CONTENT: these notes are based only on the accessible part of the article ({reason}) "
                "They do not cover anything that was inaccessible and must not be treated as a complete study "
                "of the article.")

    @staticmethod
    def _unverified_text() -> str:
        return "The completeness of the supplied text could not be verified; the notes cover only what was provided."

    def _add_status_notices(self, chapters: list[Chapter]) -> None:
        c = self.content
        for ch in chapters:
            if c.completeness is Completeness.PARTIAL:
                ch.notices.insert(0, self.notice("PARTIAL CONTENT: based only on the accessible part of the article; "
                                                 "this chapter does not cover the inaccessible remainder.", "warning"))
            elif c.completeness is Completeness.UNVERIFIED:
                ch.notices.insert(0, self.notice(self._unverified_text()))
            if self.backend.info.kind.value != "local" and ch.number in (3, 4, 7):
                ch.notices.append(self.notice("Items labelled AI-generated were written by a language model and are "
                                              "not the article's own words."))

    # ======================================================================= chapter 1
    def _chapter1(self) -> Chapter:
        c, d = self.content, self.config.detail
        ch = Chapter(1, CHAPTER_TITLES[1])
        details = NoteSection("Article details")
        details.items.append(NoteItem(ItemKind.TEXT, ORIG, text=c.title, label="Original title"))
        if c.author:
            details.items.append(NoteItem(ItemKind.TEXT, ORIG, text=c.author, label="Author"))
        if c.published:
            details.items.append(NoteItem(ItemKind.TEXT, DER, text=c.published.isoformat(), label="Published"))
        if c.url:
            details.items.append(NoteItem(ItemKind.TEXT, DER, text=c.url, label="Source URL"))
        status = {Completeness.COMPLETE: "Complete article extracted",
                  Completeness.PARTIAL: "PARTIAL: only the accessible part was extracted",
                  Completeness.UNVERIFIED: "Completeness not verified (text supplied by the user)"}[c.completeness]
        details.items.append(NoteItem(ItemKind.TEXT, DER, text=status, label="Extraction status"))
        ch.sections.append(details)

        paragraphs = [i for i in self.items if i.kind is BlockKind.PARAGRAPH and i.sentences]
        intro = NoteSection("Introduction and background")
        take = {DetailLevel.BASIC: 1, DetailLevel.INTERMEDIATE: 1, DetailLevel.ADVANCED: 2}[d]
        for item in paragraphs[:take]:
            texts = [s.text for s in item.sentences]
            if d is DetailLevel.BASIC and len(texts) > 3:
                texts, excerpt = texts[:3], True
            else:
                excerpt = False
            if self.ledger.claim(1, " ".join(texts)):
                intro.items.append(self.quote_item(" ".join(texts), item.source_index, excerpt=excerpt))
                self.intro_indices.add(item.source_index)
        if not intro.items:
            intro.items.append(self.notice("The article has no introductory paragraph."))
        ch.sections.append(intro)

        ch.sections.append(self._found_section(
            "Main objective", _OBJECTIVE_CUES, 1, "Passage that appears to state the article's objective",
            "The article does not state its objective explicitly. See 'Structure of the article' below for how "
            "its content is organised."))
        ch.sections.append(self._found_section(
            "Core problem addressed", _PROBLEM_CUES, 2, "Passage that appears to describe the problem",
            "The article does not clearly state a single core problem in its accessible text.", early=True))
        contributions = self._found_section(
            "Main contributions", _CONTRIBUTION_CUES, 2, "Passage that appears to state a main idea or contribution",
            "")
        if not contributions.items:
            concl = [s for s in self.takeaways if s.kind is StatementKind.CONCLUSION][:2]
            for s in concl:
                if self.ledger.claim(1, s.text):
                    contributions.items.append(self.quote_item(s.text, s.source_index, "Stated conclusion"))
        if not contributions.items:
            contributions.items.append(self.notice(
                "The article does not explicitly state its contributions or main ideas in one place; see "
                "Chapter 5 for its key points."))
        ch.sections.append(contributions)

        structure = NoteSection("Structure of the article")
        entries, depths = [], []
        for s in self.sections:
            label = s.heading or "Introduction (text before the first heading)"
            entries.append(f"{label} ({s.total_word_count} words)")
            depths.append(max(len(s.path) - 1, 0))
        structure.items.append(NoteItem(ItemKind.NUMBERED, DER, items=tuple(entries), depths=tuple(depths),
                                        label="Sections in order"))
        ch.sections.append(structure)
        return ch

    def _found_section(self, heading: str, cue: "re.Pattern[str]", limit: int, label: str, fallback: str,
                       early: bool = False) -> NoteSection:
        section = NoteSection(heading)
        pool = self.sentences[: max(12, len(self.sentences) // 3)] if early else self.sentences
        for text, index in pool:
            if len(section.items) >= limit:
                break
            if cue.search(text) and 6 <= len(text.split()) <= 60 and self.ledger.claim(1, text):
                section.items.append(self.quote_item(text, index, label))
        if not section.items and fallback:
            section.items.append(self.notice(fallback))
        return section

    # ======================================================================= chapter 2
    def _chapter2(self) -> Chapter:
        ch = Chapter(2, CHAPTER_TITLES[2])
        d = self.config.detail
        picked: set[tuple[int, Optional[int], int]] = {
            (s.source_index, s.list_item_index, s.sentence_index) for s in self.content.statements}
        picked |= {(x.source_index, x.list_item_index, x.sentence_index or 0) for x in self.content.definitions}
        packets: list[SectionPacket] = []
        by_id: dict[str, NoteSection] = {}
        for section in self.sections:
            ns = NoteSection(section.heading or "Introduction", level=max(1, min(len(section.path), 3)),
                             meta={"section_id": section.id})
            first_par = next((i for i in section.items if i.kind is BlockKind.PARAGRAPH), None)
            for item in section.items:
                if section.is_preamble and item.source_index in self.intro_indices and d is not DetailLevel.ADVANCED:
                    continue  # already shown in Chapter 1
                ni = self._concept_item(item, d, picked, item is first_par)
                if ni is not None:
                    ns.items.append(ni)
            if not ns.items:
                if section.is_preamble:
                    continue
                ns.items.append(self.notice("This section contains no further content."))
            ch.sections.append(ns)
            by_id[section.id] = ns
            text = "\n\n".join(self._item_prompt_text(i) for i in section.items)
            if len(text.split()) >= 25:
                packets.append(SectionPacket(section.id, section.heading or "Introduction", section.path, text,
                                             tuple(i.source_index for i in section.items)))
        if self.config.ai_section_explanations and packets:
            explained = self._call("section explanations",
                                   lambda b: b.explain_sections(self.content, packets, self.params),
                                   lambda b: {})
            for sid, texts in explained.items():
                for gt in texts:
                    if sid in by_id and gt.text.strip():
                        by_id[sid].items.append(NoteItem(ItemKind.TEXT, gt.provenance, text=gt.text,
                                                         label="AI-generated explanation",
                                                         source_indices=gt.source_indices))
        ch.sections.append(self._relationships())
        return ch

    @staticmethod
    def _item_prompt_text(item: ContentItem) -> str:
        if item.kind is BlockKind.CODE:
            return f"```{item.block.language or ''}\n{item.block.text}\n```"
        return item.text

    def _concept_item(self, item: ContentItem, detail: DetailLevel,
                      picked: set[tuple[int, Optional[int], int]], is_first: bool) -> Optional[NoteItem]:
        b, idx = item.block, (item.source_index,)
        if item.kind is BlockKind.CODE:
            return NoteItem(ItemKind.CODE, ORIG, text=b.text, language=b.language, source_indices=idx)
        if item.kind is BlockKind.TABLE:
            return NoteItem(ItemKind.TABLE, ORIG, rows=b.rows, has_header=b.has_header, source_indices=idx)
        if item.kind is BlockKind.LIST:
            kind = ItemKind.NUMBERED if b.ordered else ItemKind.BULLETS
            mixed = any(i.ordered is not None for i in b.items)
            return NoteItem(kind, ORIG, items=tuple(i.text for i in b.items), depths=tuple(i.depth for i in b.items),
                            ordered=tuple(b.ordered if i.ordered is None else i.ordered for i in b.items) if mixed else (),
                            label="Step-by-step procedure" if b.ordered else None, source_indices=idx)
        if item.kind is BlockKind.QUOTE:
            return NoteItem(ItemKind.QUOTE, ORIG, text=b.text, source_indices=idx)
        if item.kind is not BlockKind.PARAGRAPH:
            return None
        if detail is DetailLevel.ADVANCED:
            texts, excerpt = [s.text for s in item.sentences] or [b.text], False
        else:
            chosen = {s.index for s in item.sentences
                      if (item.source_index, None, s.index) in picked or is_formula(s.text)}
            if detail is DetailLevel.INTERMEDIATE or is_first:
                chosen.add(0)
            texts = [s.text for s in item.sentences if s.index in chosen]
            excerpt = len(texts) < len(item.sentences)
        text = " ".join(texts).strip()
        if not text or not self.ledger.claim(2, text):
            return None
        return NoteItem(ItemKind.TEXT, ORIG, text=text, source_indices=idx, excerpt=excerpt)

    def _relationships(self) -> NoteSection:
        section = NoteSection("How the concepts relate")
        hier = []
        for s in self.sections:
            kids = [k.heading for k in s.children if k.heading]
            if s.heading and kids:
                hier.append(f"“{s.heading}” contains: " + "; ".join(f"“{k}”" for k in kids))
        if hier:
            section.items.append(NoteItem(ItemKind.BULLETS, DER, items=tuple(hier), label="Structure of the ideas"))
        terms = [k.term for k in self.content.keywords[:12]]
        rx = {t: term_regex(t) for t in terms}
        same: dict[str, set[str]] = {}  # a term and its aliases are one concept, not two related ones
        for d in self.content.definitions:
            members = {normalize_key(d.term)} | {normalize_key(a) for a in d.aliases}
            for m in members:
                same.setdefault(m, set()).update(members)
        pairs: dict[tuple[str, str], tuple[int, str, int]] = {}
        for text, index in self.sentences:
            if len(text.split()) > 40:
                continue
            present = [t for t in terms if rx[t].search(text)]
            for i, a in enumerate(present):
                for b in present[i + 1:]:
                    if a.casefold() in b.casefold() or b.casefold() in a.casefold():
                        continue
                    if normalize_key(b) in same.get(normalize_key(a), ()):
                        continue
                    n, first, first_idx = pairs.get((a, b), (0, text, index))
                    pairs[(a, b)] = (n + 1, first, first_idx)
        ranked = sorted(pairs.items(), key=lambda kv: (-kv[1][0], kv[0]))[:6]
        if ranked:
            lines = tuple(f"“{a}” and “{b}” are discussed together ({n}x), e.g.: “{text}”"
                          for (a, b), (n, text, _) in ranked)
            section.items.append(NoteItem(ItemKind.BULLETS, DER, items=lines, label="Concepts that appear together",
                                          source_indices=tuple(sorted({i for _, (_, _, i) in ranked}))))
        if not section.items:
            section.items.append(self.notice("No relationships between concepts could be identified from the text."))
        return section

    # ======================================================================= chapter 3
    def _definition_for(self, kw: Keyword) -> Optional[Definition]:
        for d in self.content.definitions:
            if normalize_key(d.term) == kw.key or kw.key in {normalize_key(a) for a in d.aliases}:
                return d
        return None

    def _select_keywords(self) -> list[Keyword]:
        keys = {k.key for k in self.content.keywords}
        alias_of: dict[str, str] = {}
        for d in self.content.definitions:
            for a in d.aliases:
                alias_of[normalize_key(a)] = normalize_key(d.term)
        chosen = [k for k in self.content.keywords if not (k.key in alias_of and alias_of[k.key] in keys)]
        limit = self.config.max_keywords or _KEYWORDS_BY_DETAIL[self.config.detail]
        strong = [k for k in chosen if set(k.sources) != {"frequency"}]
        weak = [k for k in chosen if set(k.sources) == {"frequency"}]
        # Frequency-only words ("model", "system") are generic; use them only if little else exists.
        picked = strong if len(strong) >= min(5, limit) else strong + weak[: min(5, limit) - len(strong)]
        return picked[:limit]

    def _chapter3(self) -> Chapter:
        ch = Chapter(3, CHAPTER_TITLES[3])
        keywords = self._select_keywords()
        if not keywords:
            ch.notices.append(self.notice("No technical keywords could be identified in the article text."))
            return ch
        claimed: set[str] = set()
        packets: list[TermPacket] = []
        n_ctx = self.params.contexts_per_term
        for kw in keywords:
            d = self._definition_for(kw)
            defn_text = definition_answer(d) if d else None
            skip = {_key(d.statement), _key(defn_text or "")} if d else set()
            contexts = [SourceSentence(t, i) for t, i in self._sentences_with(kw.term)
                        if _key(t) not in skip and _key(t) not in claimed and not self.ledger.reserved(t)][: max(n_ctx, 1) + 2]
            for sent in contexts[:n_ctx]:
                claimed.add(_key(sent.text))
            sig = [SourceSentence(s.text, s.source_index) for s in self.content.statements
                   if s.kind in (StatementKind.RECOMMENDATION, StatementKind.FACT, StatementKind.EXPLANATION)
                   and term_regex(kw.term).search(s.text) and _key(s.text) not in skip
                   and _key(s.text) not in claimed and not self.ledger.reserved(s.text)][:2]
            for sent in sig:
                claimed.add(_key(sent.text))
            aliases = tuple(a for dd in self.content.definitions if normalize_key(dd.term) == kw.key for a in dd.aliases)
            packets.append(TermPacket(kw.term, defn_text, tuple(contexts), tuple(sig), aliases, kw.block_indices))
        self.term_packets = packets
        notes_by_term = self._call("keyword explanations",
                                   lambda b: b.explain_keywords(self.content, packets, self.params),
                                   lambda b: b.explain_keywords(self.content, packets, self.params))
        undefined: list[str] = []
        for kw, packet in zip(keywords, packets):
            d = self._definition_for(kw)
            section = NoteSection(kw.term, level=2, meta={"count": kw.count, "sources": kw.sources,
                                                          "aliases": packet.aliases})
            if d and packet.definition:
                low = not is_definitional(d)  # low confidence, or only an acronym/appositive mention
                section.items.append(NoteItem(
                    ItemKind.DEFINITION, ORIG, text=packet.definition, term=kw.term,
                    label="Possible definition (inferred from the sentence structure)" if low
                    else "Definition (stated in the article)", source_indices=(d.source_index,)))
            tn: TermNote = notes_by_term.get(kw.term, TermNote())
            for label, texts in (("Detailed explanation", tn.explanation), ("Practical significance", tn.significance)):
                for gt in texts:
                    if gt.text.strip():
                        section.items.append(NoteItem(ItemKind.TEXT, gt.provenance, text=gt.text, label=label,
                                                      source_indices=gt.source_indices))
            if not section.items:
                continue  # nothing the article (or backend) says about this term: no empty entry
            if not (d and packet.definition):
                undefined.append(kw.term)
            if packet.aliases:
                section.items.insert(0, NoteItem(ItemKind.TEXT, DER, text=", ".join(packet.aliases), label="Also written as"))
            ch.sections.append(section)
        if undefined:
            ch.notices.append(self.notice("The article does not define these terms explicitly, so they have no "
                                          "definition entry: " + ", ".join(undefined) + "."))
        if not ch.sections:
            ch.notices.append(self.notice("The article gives no definitions or explanations for its key terms."))
        return ch

    # ======================================================================= chapter 4
    def _chapter4(self) -> Chapter:
        ch = Chapter(4, CHAPTER_TITLES[4])
        original = NoteSection("Examples and applications mentioned in the article")
        for s in self.content.statements:
            if s.kind is StatementKind.EXAMPLE and not self.ledger.reserved(s.text) and self.ledger.claim(4, s.text):
                original.items.append(self.quote_item(s.text, s.source_index))
        for text, index in self.sentences:
            if len(original.items) >= 8:
                break
            if (_EXAMPLE_CUES.search(text) and 6 <= len(text.split()) <= 60 and not self.ledger.reserved(text)
                    and self.ledger.claim(4, text)):
                original.items.append(self.quote_item(text, index))
        if not original.items:
            original.items.append(self.notice("The article does not give explicit examples or applications."))
        ch.sections.append(original)

        ai = NoteSection("Analogies and applications added to aid understanding")
        examples = self._call("analogies",
                              lambda b: b.create_analogies(self.content, self.term_packets[:10], self.params),
                              lambda b: [])
        if examples:
            ai.items.append(self.notice("The items below are NOT from the article. They were written by a language "
                                        "model to help you understand the concepts, and may be imperfect.", "warning"))
            for ex in examples:
                if self.ledger.claim(4, ex.text):
                    ai.items.append(NoteItem(ItemKind.TEXT, ex.provenance, text=ex.text,
                                             label=f"AI-generated {ex.kind}: {ex.concept}" + (" – Review needed" if ex.review_needed else "")))
        else:
            ai.items.append(self.notice(
                "No analogies or real-world applications were added. The local backend cannot create new examples "
                "without inventing content. Select the Ollama or OpenAI backend to add AI-generated analogies; "
                "they will always be labelled as AI-generated and kept separate from the article's own content."))
        ch.sections.append(ai)
        return ch

    # ======================================================================= chapter 5
    def _select_takeaways(self) -> list[KeyStatement]:
        kinds = (StatementKind.CONCLUSION, StatementKind.RECOMMENDATION, StatementKind.FACT, StatementKind.EXPLANATION)
        pool = [s for s in self.content.statements if s.kind in kinds]
        cap = _TAKEAWAYS_BY_DETAIL[self.config.detail]
        ranked = sorted(pool, key=lambda s: (-s.score, s.source_index))
        # D-10: the best statement of every kind is kept first, so a lone recommendation or finding is never
        # pushed out by higher-scoring conclusions; the rest of the budget goes to the best remaining statements.
        top: list[KeyStatement] = []
        for kind in (StatementKind.RECOMMENDATION, StatementKind.FACT, StatementKind.CONCLUSION, StatementKind.EXPLANATION):
            best = next((s for s in ranked if s.kind is kind), None)
            if best is not None and len(top) < cap:
                top.append(best)
        for s in ranked:
            if len(top) >= cap:
                break
            if not any(s is t for t in top):
                top.append(s)
        self.takeaways_left_out = {
            kind: sum(1 for s in pool if s.kind is kind) - sum(1 for t in top if t.kind is kind) for kind in kinds}
        return sorted(top, key=lambda s: (s.source_index, s.list_item_index or 0, s.sentence_index))

    def _chapter5(self) -> Chapter:
        ch = Chapter(5, CHAPTER_TITLES[5])
        groups = [
            ("Important observations", StatementKind.EXPLANATION),
            ("Main findings", StatementKind.FACT),
            ("Conclusions", StatementKind.CONCLUSION),
            ("Practical lessons", StatementKind.RECOMMENDATION),
        ]
        for heading, kind in groups:
            section = NoteSection(heading)
            for s in self.takeaways:
                if s.kind is kind and self.ledger.claim(5, s.text):
                    section.items.append(self.quote_item(s.text, s.source_index, kind.value.capitalize()))
            if not section.items:
                section.items.append(self.notice("No sentences of this kind were identified in the article text."))
            elif self.takeaways_left_out.get(kind, 0) > 0:  # D-10: say so rather than silently dropping them
                n = self.takeaways_left_out[kind]
                section.items.append(self.notice(
                    f"{n} more sentence{'s' if n != 1 else ''} of this kind {'were' if n != 1 else 'was'} found in the article "
                    "but left out to keep this chapter within its size limit for the chosen detail level. "
                    "A higher detail level shows more."))
            ch.sections.append(section)
        if not self.takeaways:
            ch.notices.append(self.notice("The article text contains no sentences that signal conclusions, findings "
                                          "or recommendations, so no takeaways could be extracted without guessing."))
        return ch

    # ======================================================================= chapter 6
    def _chapter6(self) -> Chapter:
        ch = Chapter(6, CHAPTER_TITLES[6])
        defs = NoteSection("Short definitions")
        for d in self.content.definitions:
            if not is_definitional(d):
                continue
            text, cut = shorten(definition_answer(d))
            if self.ledger.claim(6, text):
                defs.items.append(NoteItem(ItemKind.DEFINITION, ORIG, text=text, term=d.term, excerpt=cut,
                                           label="Definition", source_indices=(d.source_index,)))
        if not defs.items:
            defs.items.append(self.notice("The article gives no explicit definitions to revise."))
        ch.sections.append(defs)

        points = NoteSection("Important points")
        remaining = [s for s in self.content.statements
                     if s.kind is not StatementKind.DEFINITION and not self.ledger.reserved(s.text)
                     and len(s.text.split()) <= 30]
        for s in sorted(remaining, key=lambda s: (-s.score, s.source_index))[:6]:
            if self.ledger.claim(6, s.text):
                points.items.append(self.quote_item(s.text, s.source_index))
        if not points.items:
            terms = tuple(k.term for k in self._select_keywords()[:10])
            if terms:
                points.items.append(NoteItem(ItemKind.BULLETS, DER, items=terms, label="Key terms to remember"))
            else:
                points.items.append(self.notice("No further important points beyond those in Chapter 5."))
        ch.sections.append(points)

        formulae = [(t, i) for t, i in self.sentences if is_formula(t)]
        if formulae:
            section = NoteSection("Formulae")
            for text, index in formulae[:8]:
                if self.ledger.claim(6, text):
                    section.items.append(self.quote_item(text, index, "Formula or expression"))
            ch.sections.append(section)

        checklist = []
        for s in self.sections:
            for item in s.items:
                where = s.heading or "the introduction"
                if item.kind is BlockKind.CODE:
                    checklist.append(f"I have read and understood the code example under “{where}”.")
                elif item.kind is BlockKind.TABLE:
                    checklist.append(f"I can explain the table under “{where}”.")
                elif item.kind is BlockKind.LIST and item.block.ordered:
                    checklist.append(f"I can recite the steps listed under “{where}”.")
        terms = [d.term for d in self.content.definitions if is_definitional(d)][:8] or \
                [k.term for k in self._select_keywords()[:8]]
        checklist = [f"I can explain “{t}” in my own words." for t in terms] + checklist
        section = NoteSection("Quick revision checklist")
        if checklist:
            section.items.append(NoteItem(ItemKind.CHECKLIST, DER, items=tuple(dict.fromkeys(checklist))[:20]))
        else:
            section.items.append(self.notice("Nothing specific to check off could be identified."))
        ch.sections.append(section)
        return ch

    # ======================================================================= chapter 7
    def _chapter7(self) -> Chapter:
        ch = Chapter(7, CHAPTER_TITLES[7])
        cfg = self.config
        if not cfg.include_questions:
            ch.notices.append(self.notice("Questions were not requested."))
            return ch
        spec = QuestionSpec(cfg.short_questions, cfg.descriptive_questions, cfg.mcq_questions)
        result: QuestionResult = self._call(
            "questions", lambda b: b.create_questions(self.content, spec, self.params),
            lambda b: b.create_questions(self.content, spec, self.params))
        seen: set[str] = set()
        ai_token_sets: list[frozenset[str]] = []  # near-duplicate check for model-written questions only
        for group, limit in ((result.short, spec.short), (result.descriptive, spec.descriptive), (result.mcq, spec.mcq)):
            kept = 0
            for q in group:
                problems = validate_question(q)
                if problems or _key(q.question) in seen:
                    self.warnings.append(f"Dropped a question: {problems[0] if problems else 'duplicate question'}.")
                    continue
                if q.question_provenance is AI:
                    tokens = frozenset(t.casefold() for t in tokenize(q.question))
                    if len(tokens) >= 5 and any(_near_duplicate(tokens, other) for other in ai_token_sets):
                        self.warnings.append("Dropped a question that asks nearly the same thing as an earlier one.")
                        continue
                    ai_token_sets.append(tokens)
                if kept < limit:
                    seen.add(_key(q.question))
                    ch.questions.append(q)
                    kept += 1
        for note in result.notes:
            ch.notices.append(self.notice(note))
        # A question that was dropped here (duplicate, malformed) leaves a gap the reader should hear about,
        # unless the backend already explained that shortfall itself.
        for kind, label, want in ((QuestionKind.SHORT, "short-answer", spec.short),
                                  (QuestionKind.DESCRIPTIVE, "descriptive", spec.descriptive),
                                  (QuestionKind.MCQ, "multiple-choice", spec.mcq)):
            have = sum(q.kind is kind for q in ch.questions)
            explained = any(label in n.text or kind.value in n.text for n in ch.notices)
            if have < want and not explained:
                ch.notices.append(self.notice(
                    f"Only {have} of {want} requested {label} questions are included: the rest were duplicates of "
                    "other questions or did not pass validation."))
        return ch
