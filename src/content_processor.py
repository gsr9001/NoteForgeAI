"""Content cleaning, sectioning, de-duplication and term analysis (Phase 4).

Takes the :class:`~src.article_extractor.Article` from Phase 3 and produces a
:class:`ProcessedContent`: cleaned blocks organised into a heading hierarchy, plus
definitions, keywords and important statements found in the text.

Principles
----------
* **Nothing is invented.** Definitions and statements are verbatim sentences (or list/table
  entries) from the article. Keywords are terms that occur in it. There is no generated text.
* **Traceability.** Every item, sentence, definition, keyword and statement carries indices
  into ``article.blocks``. Every block is either kept or listed in ``removed`` with a reason.
* **Safe de-duplication.** Only prose paragraphs and quotations can be removed. Code, tables,
  lists and headings are never removed. Near-duplicates must also share identical numbers
  and technical tokens, so differing technical detail is never lost.
* **Status is preserved.** Completeness, reasons and warnings from extraction are carried
  through unchanged; processing adds its own warnings separately.
* **Ready for AI notes.** :class:`Provenance` and :class:`Annotation` let later phases attach
  AI-generated material that is always distinguishable from source-derived content. Image,
  caption and footnote fields exist on the model but stay empty until extraction supplies them.
"""
from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date
from enum import Enum
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence, TypeVar

import regex

from src.article_extractor import Article, Block, BlockKind, Completeness, ListItem

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =========================================================================== exceptions
class ProcessingError(Exception):
    """Base class for content-processing failures."""


class InvalidArticleError(ProcessingError):
    """The object passed in is not a usable Article."""


class EmptyContentError(ProcessingError):
    """No content remained after cleaning."""


# =========================================================================== model
class Provenance(str, Enum):
    """Whether a piece of content comes from the article or was generated later."""

    SOURCE = "source"
    AI_GENERATED = "ai_generated"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RemovalReason(str, Enum):
    EMPTY = "empty"
    BOILERPLATE = "boilerplate"
    DUPLICATE_EXACT = "duplicate_exact"
    DUPLICATE_NEAR = "duplicate_near"


class StatementKind(str, Enum):
    DEFINITION = "definition"
    CONCLUSION = "conclusion"
    RECOMMENDATION = "recommendation"
    FACT = "fact"
    EXPLANATION = "explanation"
    EXAMPLE = "example"


@dataclass(frozen=True)
class Sentence:
    text: str
    index: int  # position within its block (or within its list item)
    list_item_index: Optional[int] = None  # set for sentences that come from a list block


@dataclass(frozen=True)
class ImageRef:
    """Reserved: populated once the extractor keeps images (url, alt text, caption)."""

    url: str
    alt: str = ""
    caption: str = ""


@dataclass(frozen=True)
class FootnoteRef:
    """Reserved: populated once the extractor links footnote markers to their text."""

    marker: str
    text: str


@dataclass(frozen=True)
class ContentItem:
    """A cleaned block, linked to its original position in ``article.blocks``."""

    source_index: int
    block: Block
    sentences: tuple[Sentence, ...] = ()
    images: tuple[ImageRef, ...] = ()
    footnotes: tuple[FootnoteRef, ...] = ()

    @property
    def kind(self) -> BlockKind:
        return self.block.kind

    @property
    def text(self) -> str:
        return self.block.plain_text

    @property
    def word_count(self) -> int:
        return self.block.word_count


@dataclass(frozen=True)
class Annotation:
    """A note attached to a section. Defaults to AI-generated so it can never pass as source."""

    text: str
    kind: str = "explanation"
    provenance: Provenance = Provenance.AI_GENERATED
    source_indices: tuple[int, ...] = ()  # article blocks the note is about


@dataclass
class Section:
    """A heading and the content under it, with sub-sections nested by heading level."""

    id: str
    heading: Optional[str]  # None for the preamble (content before the first heading)
    level: int  # 0 for the preamble, otherwise 1-6
    heading_index: Optional[int]  # index of the heading block in article.blocks
    path: tuple[str, ...]  # headings from the top level down to this section
    items: list[ContentItem] = field(default_factory=list)
    children: list["Section"] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)

    @property
    def is_preamble(self) -> bool:
        return self.heading is None

    @property
    def word_count(self) -> int:
        """Words in this section's own items (not its sub-sections)."""
        return sum(i.word_count for i in self.items)

    @property
    def total_word_count(self) -> int:
        return sum(s.word_count for s in self.walk())

    def walk(self) -> Iterator["Section"]:
        """This section then its descendants, in document order."""
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(frozen=True)
class RemovedItem:
    source_index: int
    reason: RemovalReason
    text: str
    duplicate_of: Optional[int] = None
    similarity: float = 0.0


@dataclass(frozen=True)
class Definition:
    """A definition found in the article. ``statement`` is verbatim source text."""

    term: str
    statement: str
    pattern: str
    confidence: Confidence
    source_index: int
    section_id: Optional[str] = None
    sentence_index: Optional[int] = None
    aliases: tuple[str, ...] = ()
    provenance: Provenance = Provenance.SOURCE
    list_item_index: Optional[int] = None  # which list item, when the block is a list


@dataclass(frozen=True)
class Keyword:
    term: str
    key: str  # normalised form used for lookups
    score: float
    count: int
    sources: tuple[str, ...]  # why it was chosen: definition, acronym, heading, code, ...
    block_indices: tuple[int, ...]  # article blocks where it occurs
    has_definition: bool = False

    @property
    def first_index(self) -> int:
        return self.block_indices[0] if self.block_indices else -1


@dataclass(frozen=True)
class KeyStatement:
    """An important sentence, verbatim from the article."""

    text: str
    kind: StatementKind
    score: float
    reasons: tuple[str, ...]
    source_index: int
    sentence_index: int
    section_id: Optional[str] = None
    provenance: Provenance = Provenance.SOURCE
    list_item_index: Optional[int] = None  # which list item, when the block is a list


@dataclass(frozen=True)
class ProcessingStats:
    blocks_in: int
    items_out: int
    words_in: int
    words_out: int
    removed_empty: int = 0
    removed_boilerplate: int = 0
    removed_duplicates: int = 0


@dataclass
class ProcessedContent:
    """Structured, cleaned article content ready for notes generation."""

    article: Article = field(repr=False)
    title: str
    author: Optional[str]
    published: Optional[date]
    url: Optional[str]
    site_name: Optional[str]
    source_kind: str
    extractor: str
    completeness: Completeness
    completeness_reasons: tuple[str, ...]
    warnings: tuple[str, ...]  # from extraction, unchanged
    processing_warnings: list[str]
    sections: list[Section]
    definitions: list[Definition]
    keywords: list[Keyword]
    statements: list[KeyStatement]
    removed: list[RemovedItem]
    stats: ProcessingStats

    @property
    def is_partial(self) -> bool:
        return self.completeness is Completeness.PARTIAL

    @property
    def is_complete(self) -> bool:
        return self.completeness is Completeness.COMPLETE

    @property
    def all_warnings(self) -> list[str]:
        return [*self.warnings, *self.processing_warnings]

    def iter_sections(self) -> Iterator[Section]:
        for top in self.sections:
            yield from top.walk()

    def iter_items(self) -> Iterator[ContentItem]:
        """All content items in original document order."""
        for section in self.iter_sections():
            yield from section.items

    def section_of(self, source_index: int) -> Optional[Section]:
        for section in self.iter_sections():
            if any(i.source_index == source_index for i in section.items):
                return section
            if section.heading_index == source_index:
                return section
        return None

    def get_keyword(self, term: str) -> Optional[Keyword]:
        key = normalize_key(term)
        return next((k for k in self.keywords if k.key == key), None)

    def get_definition(self, term: str) -> Optional[Definition]:
        key = normalize_key(term)
        return next((d for d in self.definitions if normalize_key(d.term) == key), None)

    def outline(self) -> str:
        """Human-readable section tree."""
        lines: list[str] = []
        for s in self.iter_sections():
            indent = "  " * max(s.level - 1, 0)
            label = s.heading if s.heading is not None else "(content before first heading)"
            kinds = Counter(i.kind.value for i in s.items)
            detail = ", ".join(f"{n} {k}" for k, n in kinds.items()) or "empty"
            lines.append(f"{indent}[{s.id}] {label} - {detail}, {s.word_count} words")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable summary (the original ``article`` object is not embedded)."""

        def section_dict(s: Section) -> dict[str, Any]:
            return {
                "id": s.id, "heading": s.heading, "level": s.level, "heading_index": s.heading_index,
                "path": list(s.path),
                "items": [{"source_index": i.source_index, "kind": i.kind.value, "text": i.text}
                          for i in s.items],
                "children": [section_dict(c) for c in s.children],
            }

        return {
            "title": self.title, "author": self.author,
            "published": self.published.isoformat() if self.published else None,
            "url": self.url, "completeness": self.completeness.value,
            "completeness_reasons": list(self.completeness_reasons),
            "warnings": self.all_warnings,
            "sections": [section_dict(s) for s in self.sections],
            "definitions": [{"term": d.term, "statement": d.statement, "pattern": d.pattern,
                             "confidence": d.confidence.value, "source_index": d.source_index,
                             "aliases": list(d.aliases)} for d in self.definitions],
            "keywords": [{"term": k.term, "score": round(k.score, 2), "count": k.count,
                          "sources": list(k.sources), "blocks": list(k.block_indices)}
                         for k in self.keywords],
            "statements": [{"kind": s.kind.value, "score": round(s.score, 2), "text": s.text,
                            "source_index": s.source_index} for s in self.statements],
            "removed": [{"source_index": r.source_index, "reason": r.reason.value,
                         "duplicate_of": r.duplicate_of, "text": r.text[:100]} for r in self.removed],
        }


@dataclass(frozen=True)
class ProcessorConfig:
    dedupe: bool = True
    remove_boilerplate: bool = True
    near_duplicate_threshold: float = 0.92
    min_duplicate_words: int = 5  # shorter paragraphs are never treated as duplicates
    max_keywords: int = 40
    min_keyword_count: int = 2  # multi-word phrases found only by frequency
    min_single_word_count: int = 3  # single words found only by frequency
    max_statements: int = 30
    min_statement_score: float = 1.5  # one strong cue (fact, explanation, recommendation) is enough

    def __post_init__(self) -> None:
        if not 0.5 <= self.near_duplicate_threshold <= 1.0:
            raise ValueError("near_duplicate_threshold must be between 0.5 and 1.0")
        for name in ("min_duplicate_words", "max_keywords", "min_keyword_count",
                     "min_single_word_count", "max_statements"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")


# =========================================================================== text utilities
_WORD = regex.compile(r"[\p{L}\p{N}][\p{L}\p{M}\p{N}]*(?:[-'’_][\p{L}\p{N}][\p{L}\p{M}\p{N}]*)*")
_LATIN_TOKEN = regex.compile(r"[\p{Latin}\p{N}'’_-]+")
_NON_ALNUM = regex.compile(r"[^\p{L}\p{M}\p{N}]+")
# Zero-width space / word joiner / BOM / soft hyphen / LRM / RLM are removed. ZWJ (U+200D) and
# ZWNJ (U+200C) are deliberately kept: Indic scripts such as Telugu need them for correct shaping.
_INVISIBLE = {ord(c): None for c in "\u200b\u2060\ufeff\u00ad\u200e\u200f"}
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?%)\]।])")
_SPACE_AFTER_OPEN = re.compile(r"([(\[])\s+")
_SPACE_BEFORE_CLITIC = re.compile(r"\s+(['’](?:s|t|re|ve|ll|d|m))\b")
_ABBREVIATIONS = ("e.g.", "i.e.", "vs.", "etc.", "et al.", "Dr.", "Mr.", "Mrs.", "Ms.", "Prof.",
                  "Fig.", "cf.", "approx.", "Inc.", "Ltd.", "Jr.", "Sr.", "St.", "No.", "U.S.", "U.K.")
_SENTENCE_SPLIT = regex.compile(r"(?<=[.!?।][\"'”’)\]]*)\s+(?=[^\s\p{Ll}])")


def normalize_key(text: str) -> str:
    """Case-folded, punctuation-free key used to compare terms and paragraphs."""
    return _NON_ALNUM.sub(" ", unicodedata.normalize("NFC", text).casefold()).strip()


def tokenize(text: str) -> list[str]:
    """Unicode-aware word tokens (keeps Indic vowel signs and hyphenated compounds whole)."""
    return _WORD.findall(text)


def clean_text(text: str) -> str:
    """Normalise prose: NFC, strip invisible characters, collapse whitespace, fix spacing.

    Fixes artefacts left by inline markup (``word ,`` and ``( text )``), never touches
    letters, digits or the characters Indic scripts rely on.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFC", text).translate(_INVISIBLE)
    t = re.sub(r"\s+", " ", t).strip()
    t = _SPACE_BEFORE_PUNCT.sub(r"\1", t)
    t = _SPACE_AFTER_OPEN.sub(r"\1", t)
    t = _SPACE_BEFORE_CLITIC.sub(r"\1", t)
    return t


def clean_code(text: str) -> str:
    """Minimal code clean-up: line endings, non-breaking spaces and surrounding blank lines only."""
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ").translate(_INVISIBLE)
    return t.strip("\n").rstrip()


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences, protecting common abbreviations and decimals."""
    text = text.strip()
    if not text:
        return []
    protected = text
    for abbr in _ABBREVIATIONS:
        protected = protected.replace(abbr, abbr.replace(".", "\u0001"))
    parts = _SENTENCE_SPLIT.split(protected)
    return [p.replace("\u0001", ".").strip() for p in parts if p.strip()]


# =========================================================================== boilerplate
_BOILERPLATE = [re.compile(p, re.IGNORECASE) for p in (
    r"^(thanks|thank you) for reading\b.*",
    r"^subscribe( now)?[.!]?$",
    r"^subscribed$",
    r"^share( this post)?[.!]?$",
    r"^leave a comment[.!]?$",
    r"^(like|comment|restack|share)([\s,/&]+(like|comment|restack|share))*[.!]?$",
    r"^get the .{0,40}app\b.*",
    r"^discussion about this post\b.*",
    r"^(upgrade|become) (to|a) paid\b.*",
    r"^this post is public so feel free to share it\b.*",
    r"^(if you (enjoyed|liked) this (post|article|newsletter)).{0,120}(subscribe|share|forward).*",
    r"^(click|tap) (here|the button below)\b.*",
    r"^sign up for (my|our|the) newsletter\b.*",
    r"^(read|continue reading) (more|on)[.!]?$",
    r"^this is a reader-supported publication\b.*",
)]
_BOILERPLATE_MAX_CHARS = 200


def is_boilerplate(text: str) -> bool:
    """True for short call-to-action lines such as 'Thanks for reading! Subscribe...'."""
    t = text.strip()
    return 0 < len(t) <= _BOILERPLATE_MAX_CHARS and any(p.match(t) for p in _BOILERPLATE)


# =========================================================================== cleaning blocks
def clean_block(block: Block) -> Optional[Block]:
    """Return a cleaned copy of ``block``, or None if nothing is left of it."""
    kind = block.kind
    if kind is BlockKind.CODE:
        text = clean_code(block.text)
        return replace(block, text=text) if text.strip() else None
    if kind is BlockKind.LIST:
        items = tuple(ListItem(t, i.depth, i.ordered) for i in block.items if (t := clean_text(i.text)))
        return replace(block, items=items) if items else None
    if kind is BlockKind.TABLE:
        rows = tuple(tuple(clean_text(c) for c in row) for row in block.rows)
        rows = tuple(r for r in rows if any(r))
        return replace(block, rows=rows) if rows else None
    text = clean_text(block.text)
    if not text:
        return None
    if kind is BlockKind.HEADING:
        return replace(block, text=text, level=min(max(block.level, 1), 6))
    return replace(block, text=text)


# =========================================================================== de-duplication
def _tech_signature(tokens: Iterable[str]) -> frozenset[str]:
    """Tokens that carry technical detail: numbers, ALL-CAPS, snake/kebab/camelCase."""
    sig: set[str] = set()
    for t in tokens:
        if (any(c.isdigit() for c in t) or "_" in t or "-" in t
                or (len(t) > 1 and t.isupper()) or re.search(r"[a-z][A-Z]", t)):
            sig.add(t.casefold())
    return frozenset(sig)


@dataclass
class _Kept:
    index: int
    key: str
    tokens: frozenset[str]
    signature: frozenset[str]


def find_duplicates(
    blocks: Sequence[tuple[int, Block]],
    threshold: float = 0.92,
    min_words: int = 5,
) -> dict[int, tuple[int, RemovalReason, float]]:
    """Find duplicate paragraphs/quotes. Returns ``{duplicate_index: (kept_index, reason, similarity)}``.

    Only paragraphs and quotations are considered. A near-duplicate must also have an
    identical set of numbers/technical tokens, so a paragraph that differs in a figure,
    a parameter name or an acronym is always kept.
    """
    found: dict[int, tuple[int, RemovalReason, float]] = {}
    kept: dict[BlockKind, list[_Kept]] = {BlockKind.PARAGRAPH: [], BlockKind.QUOTE: []}
    exact: dict[tuple[BlockKind, str], int] = {}
    for index, block in blocks:
        if block.kind not in kept:
            continue
        raw_tokens = tokenize(block.text)
        if len(raw_tokens) < min_words:
            continue
        key = normalize_key(block.text)
        if (block.kind, key) in exact:
            found[index] = (exact[(block.kind, key)], RemovalReason.DUPLICATE_EXACT, 1.0)
            continue
        tokens = frozenset(t.casefold() for t in raw_tokens)
        signature = _tech_signature(raw_tokens)
        match: Optional[tuple[int, float]] = None
        if len(raw_tokens) >= 8:
            for other in kept[block.kind]:
                if other.signature != signature or len(other.tokens) < 6:
                    continue
                union = len(tokens | other.tokens)
                sim = len(tokens & other.tokens) / union if union else 0.0
                if sim >= threshold:
                    match = (other.index, sim)
                    break
        if match:
            found[index] = (match[0], RemovalReason.DUPLICATE_NEAR, round(match[1], 3))
            continue
        exact[(block.kind, key)] = index
        kept[block.kind].append(_Kept(index, key, tokens, signature))
    return found


# =========================================================================== sections
_GENERIC_HEADINGS = frozenset({
    "introduction", "intro", "conclusion", "conclusions", "summary", "overview", "background",
    "references", "notes", "next steps", "final thoughts", "takeaways", "key takeaways",
    "further reading", "acknowledgements", "acknowledgments", "faq", "appendix", "contents",
    "key ideas", "key points", "key concepts", "main ideas", "main points", "the basics", "basics",
})
_CONCLUSION_HEADING = re.compile(
    r"conclusion|summary|takeaway|key points?|tl;?dr|final thoughts|wrap(ping)?[- ]up|lessons?",
    re.IGNORECASE,
)


def build_sections(entries: Sequence[tuple[int, Block]], items: Mapping[int, ContentItem]) -> list[Section]:
    """Nest content under headings by level, preserving document order.

    ``entries`` are ``(source_index, cleaned block)`` pairs in original order; ``items``
    maps a source index to its :class:`ContentItem` for non-heading blocks.
    Content before the first heading goes into a preamble section (heading ``None``).
    """
    roots: list[Section] = []
    stack: list[Section] = []
    counter = 0

    def new_section(heading: Optional[str], level: int, index: Optional[int]) -> Section:
        nonlocal counter
        counter += 1
        parent_path = stack[-1].path if stack else ()
        path = parent_path + ((heading,) if heading else ())
        return Section(id=f"s{counter}", heading=heading, level=level, heading_index=index, path=path)

    for index, block in entries:
        if block.kind is BlockKind.HEADING:
            # The preamble (level 0) is never a parent: the first heading starts a new top-level section.
            while stack and (stack[-1].level >= block.level or stack[-1].is_preamble):
                stack.pop()
            section = new_section(block.text, block.level, index)
            (stack[-1].children if stack else roots).append(section)
            stack.append(section)
        else:
            if not stack:
                preamble = new_section(None, 0, None)
                roots.append(preamble)
                stack.append(preamble)
            stack[-1].items.append(items[index])
    return roots


# =========================================================================== definitions
_STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being
below between both but by can cannot could did do does doing down during each either else even ever
few for from further get gets got had has have having he her here hers him his how however i if in
into is it its itself just like many may me might more most much must my no nor not now of off on
once one only onto or other our out over own per rather really same shall she should since so some
still such than that the their theirs them then there these they this those though through thus to
too under until up upon us use used using very via was we were what when where whether which while
who whom whose why will with within without would yet you your yours
""".split())
_VERBISH = frozenset("is are was were be been has have had do does did can will would should may might must".split())
_NON_TERM_START = frozenset("""
if when while since because although though as in on at for with by from after before this that
these those it its there here what which who whom whose how why you we i they he she one some any
each every many most all no not so but and or then also however thus step note tip warning example
caution important todo too very
""".split())
_ARTICLE_PREFIX = re.compile(r"^(?:the|an?)\s+", re.IGNORECASE)
_WORDS4 = r"[\w][\w\-]*(?:\s[\w][\w\-]*){0,3}"
_Q = "\"'“”‘’"

_RE_SHORTENED = re.compile(
    rf"^(?P<term>[^\n,;:.!?]{{2,70}}?),?\s+(?:which is\s+)?(?:usually|often|commonly|also|sometimes)?\s*"
    rf"(?:shortened to|abbreviated(?: as| to)?|also (?:known|called) as|also called|aka)\s+"
    rf"[{_Q}]?(?P<alias>[^\s,.;{_Q}]+)",
    re.IGNORECASE,
)
_RE_REFERS = re.compile(
    rf"^(?P<term>[^\n,;:.!?]{{2,70}}?)\s+(?:refers to|means|denotes|stands for|is defined as|is known as|"
    rf"is called|is termed|are called|are known as|is described as)\s+(?P<rest>.+)$",
    re.IGNORECASE,
)
_RE_CALLED = re.compile(
    rf"\b(?:called|termed|known as|referred to as|dubbed)\s+[{_Q}]?(?P<tail>[^\n]+)", re.IGNORECASE
)


def _called_term(tail: str) -> Optional[str]:
    """First one to three words after 'called'/'known as', stopping at punctuation or a stop-word."""
    words: list[str] = []
    for raw in tail.split():
        word = raw.strip(_Q)
        stop = raw[-1:] in ",.;:)" or raw.endswith("”")
        bare = word.rstrip(",.;:)")
        if not bare or (words and bare.casefold() in _STOPWORDS) or len(words) >= 3:
            break
        if not words and bare.casefold() in {"the", "a", "an"}:
            continue
        words.append(bare)
        if stop:
            break
    return _clean_term(" ".join(words)) if words else None
_RE_IS_A = re.compile(
    r"^(?P<term>[^\n,;:.!?]{2,70}?)\s+(?:is|are)\s+(?:an?|the)\s+(?P<rest>.+)$", re.IGNORECASE
)
_RE_APPOSITIVE = re.compile(
    r"\b(?:an?|the|each)\s+(?P<term>[A-Za-z][\w-]*(?: [\w-]+){0,2}),\s+(?:an?|the)\s+(?P<rest>[^.;]{15,})",
)
_RE_COLON = re.compile(r"^(?P<term>[^\n:]{2,50}):\s+(?P<rest>\S.*)$")
_RE_ABBR_PAREN = re.compile(r"\((?P<abbr>[A-Z][A-Za-z0-9]{1,9}?)s?\)")
_RE_PAREN_EXPANSION = re.compile(r"\b(?P<abbr>[A-Z][A-Z0-9]{1,8})s?\s+\((?P<long>[^()]{5,80})\)")
_CONNECTORS = frozenset({"of", "and", "for", "the", "in", "on", "to", "a", "an", "with", "de", "or", "by"})
_TERM_HEADERS = frozenset({"term", "keyword", "concept", "word", "name", "acronym", "abbreviation",
                           "key term", "terminology", "terms", "keywords", "concepts"})
_DEF_HEADERS = frozenset({"definition", "meaning", "description", "explanation", "what it means",
                          "expansion", "full form", "definitions", "meanings", "explanations"})


def _clean_term(raw: str) -> Optional[str]:
    term = raw.strip().strip(_Q).strip()
    term = _ARTICLE_PREFIX.sub("", term).strip(_Q + " ")
    term = re.sub(r"[\s,;:.]+$", "", term)
    words = term.split()
    if not words or len(words) > 6 or len(term) > 60:
        return None
    if words[0].casefold() in _NON_TERM_START:
        return None
    if all(w.casefold() in _STOPWORDS for w in words):
        return None
    if any(w.casefold() in _VERBISH for w in words):
        return None
    if not any(c.isalpha() for c in term):
        return None
    return term


def _initials(words: Iterable[str], skip_connectors: bool = True) -> str:
    """Initials of the words, splitting hyphenated words (optionally skipping 'of', 'and'...)."""
    letters: list[str] = []
    for word in words:
        for part in re.findall(r"[A-Za-z][A-Za-z0-9]*", word):
            if not (skip_connectors and part.casefold() in _CONNECTORS):
                letters.append(part[0].upper())
    return "".join(letters)


def _spells(words: Sequence[str], abbr: str) -> bool:
    """True if the words' initials spell ``abbr``, with or without connector words ('IoT' keeps the 'o')."""
    target = abbr.upper()
    return _initials(words, True) == target or _initials(words, False) == target


def _initials_match(long_phrase: str, abbr: str) -> bool:
    return _spells(long_phrase.split(), abbr)


def _expand_acronym(before: Sequence[str], abbr: str) -> Optional[str]:
    """Find the phrase just before ``(ABBR)`` whose initials spell the abbreviation."""
    for size in range(1, min(len(before), len(abbr) + 4) + 1):
        candidate = before[-size:]
        if _spells(candidate, abbr) and candidate[0].casefold() not in _CONNECTORS:
            return " ".join(candidate).strip(",;:")
    return None


# ---- definition quality (D-5): "X is a Y" is only a definition when Y actually says what X is -------------
_INTENSIFIERS = frozenset("very really highly extremely quite fairly more less most least truly particularly "
                          "especially incredibly".split())
#: Leading words of the predicate that make it an evaluation, a role or a position rather than a definition
#: ("is a trade-off", "is a key part of", "is the first step", "is an example of").
_NON_DEFINING_HEADS = frozenset("""
trade-off tradeoff trade-offs challenge problem issue mistake question matter key important crucial critical
essential vital good great bad powerful popular common major huge big difficult hard easy example instance
consequence result reason benefit drawback risk future step part piece first last only main primary biggest
largest best worst
""".split())
_PREDICTION = re.compile(
    r"\b(?:will|going to|expected to|set to|poised to|soon|in the future|someday|eventually|forecast|predict\w*|"
    r"by 20\d\d)\b", re.IGNORECASE)
_OPINION = re.compile(
    r"\b(?:i think|we think|we believe|in my opinion|best|worst|better|worse|favou?rite|overrated|underrated|amazing|"
    r"incredible|awesome|terrible)\b", re.IGNORECASE)
_HEDGE = re.compile(
    r"\b(?:may|might|could|can be|often|usually|typically|generally|commonly|sometimes|frequently|probably|likely|"
    r"perhaps|arguably|seems?|appears?|tends? to|for example|for instance|such as|e\.g\.)\b", re.IGNORECASE)
_IMPERATIVE_START = frozenset("""
identify understand explore learn discover dive research get find read join subscribe follow check see use try
start begin take make build create explain give share stay keep remember consider imagine think ask click visit
watch listen download sign
""".split())
_INTERROGATIVE_START = frozenset("who whom whose what why how when where which can could do does did is are will would should".split())
_LABEL_TERMS = frozenset("""
example examples note notes tip tips warning caution important remember takeaway takeaways summary conclusion
result results answer question step goal goals problem solution benefits drawbacks pros cons why how what when
tl;dr tldr update edit disclaimer source sources reference references
""".split())
_DEFINITIONAL_PATTERNS = frozenset({"shortened_to", "refers_to", "is_a", "called", "term_colon", "table_row"})


def _is_a_confidence(rest: str, sentence: str) -> Optional[Confidence]:
    """How far an "X is a/an/the <rest>" sentence reads as a definition: None = not one, LOW = flag for review."""
    stripped = sentence.rstrip()
    if stripped.endswith((":", "?")):
        return None  # a lead-in to a list or a question
    if _PREDICTION.search(stripped) or _OPINION.search(stripped):
        return None  # a forecast or a personal view: not a statement of what the term is
    words = rest.strip().rstrip(".!;").split()
    head = words[0].strip(",.;:()\"'").casefold() if words else ""
    if len(words) < 4 or head in _INTENSIFIERS or head in _NON_DEFINING_HEADS:
        # "Chunk size is a trade-off." names the term but does not say what it is. The term is still a useful
        # key-term cue, so it is kept as a LOW-confidence candidate: shown as "possible", never an answer key.
        return Confidence.LOW
    if len(stripped.split()) > 60 or _HEDGE.search(stripped):
        return Confidence.LOW  # may be a definition, but not certain enough to use as an answer key
    return Confidence.MEDIUM


def is_definitional(d: "Definition") -> bool:
    """True when ``d`` may be presented as a stated definition and used as an answer key.

    Acronym introductions and appositives are kept (aliases, key terms) but only *possibly* define their term.
    """
    return (d.confidence is not Confidence.LOW and d.pattern in _DEFINITIONAL_PATTERNS
            and not d.statement.rstrip().endswith(":"))


def _definitions_from_sentence(sentence: str) -> list[tuple[str, str, Confidence, tuple[str, ...]]]:
    """Return ``(term, pattern, confidence, aliases)`` tuples found in one sentence."""
    out: list[tuple[str, str, Confidence, tuple[str, ...]]] = []
    m = _RE_SHORTENED.match(sentence)
    if m and (term := _clean_term(m.group("term"))):
        out.append((term, "shortened_to", Confidence.HIGH, (m.group("alias"),)))
    elif (m := _RE_REFERS.match(sentence)) and (term := _clean_term(m.group("term"))):
        out.append((term, "refers_to", Confidence.HIGH, ()))
    elif (m := _RE_IS_A.match(sentence)) and (term := _clean_term(m.group("term"))) and len(term.split()) <= 5:
        if (conf := _is_a_confidence(m.group("rest"), sentence)) is not None:
            out.append((term, "is_a", conf, ()))
    m = _RE_CALLED.search(sentence)
    if m and (term := _called_term(m.group("tail"))) and not any(o[0].casefold() == term.casefold() for o in out):
        lead = sentence[: m.start()].split()
        if len(lead) >= 3:
            out.append((term, "called", Confidence.HIGH, ()))
    m = _RE_APPOSITIVE.search(sentence)
    if m and not out and (term := _clean_term(m.group("term"))):
        # Strict: an appositive term may not contain any stop-word ("chunk into an" is rejected).
        if not any(w.casefold() in _STOPWORDS for w in term.split()):
            out.append((term, "appositive", Confidence.LOW, ()))
    for m in _RE_ABBR_PAREN.finditer(sentence):
        abbr = m.group("abbr")
        long_term = _expand_acronym(sentence[: m.start()].split(), abbr)
        if long_term and long_term.casefold() != abbr.casefold():
            out.append((long_term, "acronym_expansion", Confidence.HIGH, (abbr,)))
    for m in _RE_PAREN_EXPANSION.finditer(sentence):
        if _initials_match(m.group("long"), m.group("abbr")):
            out.append((m.group("abbr"), "acronym_expansion", Confidence.HIGH, (m.group("long").strip(),)))
    return out


def _colon_definition(text: str) -> Optional[tuple[str, str]]:
    m = _RE_COLON.match(text.strip())
    if not m:
        return None
    term = _clean_term(m.group("term"))
    if not term or len(term.split()) > 4 or not term[0].isupper():
        return None
    if len(m.group("rest").split()) < 3 or re.match(r"^\d", term):
        return None
    if term.casefold() in _LABEL_TERMS:
        return None  # "Example: ...", "Note: ..." are labels, not terms
    first = m.group("rest").split()[0].strip(",.;:").casefold()
    if first in _IMPERATIVE_START:
        return None  # "Jupyter Notebooks: Understand ..." describes what to do, it does not define the term
    if m.group("rest").rstrip().endswith("?") or first in _INTERROGATIVE_START:
        return None  # "Knowing your readers: Who does your writing bring together?" poses questions, it defines nothing
    return term, m.group("rest")


def extract_definitions(
    items: Sequence[ContentItem], section_of: Optional[Mapping[int, str]] = None
) -> list[Definition]:
    """Find definitions using explicit patterns only (English patterns).

    Each result quotes the article's own wording. Where the same term is defined more than
    once, the highest-confidence and earliest definition is kept and aliases are merged.
    """
    section_of = section_of or {}
    found: list[Definition] = []
    for item in items:
        sid = section_of.get(item.source_index)
        block = item.block
        if block.kind is BlockKind.TABLE and len(block.rows) >= 2 and block.has_header:
            h0, h1 = (c.casefold().strip() for c in block.rows[0][:2]) if len(block.rows[0]) >= 2 else ("", "")
            if h0 in _TERM_HEADERS and h1 in _DEF_HEADERS:
                for row in block.rows[1:]:
                    if len(row) >= 2 and row[0] and row[1]:
                        found.append(Definition(row[0], f"{row[0]}: {row[1]}", "table_row",
                                                Confidence.HIGH, item.source_index, sid))
            continue
        if block.kind is BlockKind.LIST:
            for li, entry in enumerate(block.items):
                parsed = _colon_definition(entry.text)
                if parsed:
                    found.append(Definition(parsed[0], entry.text, "term_colon", Confidence.HIGH,
                                            item.source_index, sid, sentence_index=0, list_item_index=li))
        if block.kind not in (BlockKind.PARAGRAPH, BlockKind.QUOTE, BlockKind.LIST):
            continue
        if block.kind is BlockKind.PARAGRAPH and len(block.text.split()) < 80:
            parsed = _colon_definition(block.text)
            if parsed:
                found.append(Definition(parsed[0], block.text, "term_colon", Confidence.HIGH,
                                        item.source_index, sid, 0))
        for sent in item.sentences:
            for term, pattern, conf, aliases in _definitions_from_sentence(sent.text):
                found.append(Definition(term, sent.text, pattern, conf, item.source_index, sid,
                                        sentence_index=sent.index, aliases=aliases,
                                        list_item_index=sent.list_item_index))
    return _merge_definitions(found)


_CONF_RANK = {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}


def _merge_definitions(defs: list[Definition]) -> list[Definition]:
    best: dict[str, Definition] = {}
    order: list[str] = []
    for d in defs:
        key = normalize_key(d.term)
        if key not in best:
            best[key] = d
            order.append(key)
            continue
        cur = best[key]
        aliases = tuple(dict.fromkeys(cur.aliases + d.aliases))
        winner = d if _CONF_RANK[d.confidence] < _CONF_RANK[cur.confidence] else cur
        best[key] = replace(winner, aliases=aliases)
    return sorted((best[k] for k in order), key=lambda d: (d.source_index, d.list_item_index or 0, d.sentence_index or 0))


# =========================================================================== keywords
_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9]{1,7}s?\b")
_ACRONYM_IGNORE = frozenset({"OK", "US", "UK", "EU", "AM", "PM", "TV", "II", "III", "IV", "VI", "VII",
                             "VIII", "IX", "XI", "XII", "PS", "NB", "ID", "FAQ", "TLDR"})
_CODE_IDENT = [
    re.compile(r"\bdef\s+([A-Za-z_]\w{2,})"),
    re.compile(r"\bclass\s+([A-Za-z_]\w{2,})"),
    re.compile(r"\bfunction\s+([A-Za-z_]\w{2,})"),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_]\w{2,})\s*="),
    re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]{2,})", re.MULTILINE),
]
_SEGMENT_BREAK = re.compile(r"[,;:()\[\]\"“”—–!?.।]|\s-\s")
_CAP_WORD = re.compile(r"^[A-Z][a-z]+[A-Za-z]*$")
_COMPOUND = re.compile(r"^[A-Za-z]{2,}(?:[-_][A-Za-z0-9]{2,})+$|^[a-z]+[A-Z][A-Za-z]+$")
_SOURCE_WEIGHT = {"definition": 5.0, "acronym": 3.0, "appositive": 3.0, "heading": 2.5, "code": 2.0,
                  "named_term": 1.5, "compound": 1.5, "frequency": 1.0}
_PHRASE_CONNECTORS = frozenset({"of", "for", "in", "on", "per", "vs"})
_INTERROGATIVE = frozenset({"why", "how", "what", "when", "where", "who", "which"})


def _is_latin(token: str) -> bool:
    return bool(_LATIN_TOKEN.fullmatch(token))


def _count_term(term: str, texts: Sequence[tuple[int, str]], case_sensitive: bool) -> tuple[int, tuple[int, ...]]:
    flags = 0 if case_sensitive else regex.IGNORECASE
    pattern = regex.compile(
        r"(?<![\p{L}\p{M}\p{N}])" + r"\s+".join(map(regex.escape, term.split())) + r"s?(?![\p{L}\p{M}\p{N}])",
        flags,
    )
    total, blocks = 0, []
    for index, text in texts:
        n = len(pattern.findall(text))
        if n:
            total += n
            blocks.append(index)
    return total, tuple(blocks)


@dataclass
class _Candidate:
    term: str
    sources: set[str] = field(default_factory=set)
    case_sensitive: bool = False


def _heading_term(text: str) -> Optional[str]:
    t = re.sub(r"^\s*(?:\d+[.)]|[ivx]+[.)])\s*", "", text).strip().rstrip(":.")
    words = t.split()
    if not 1 <= len(words) <= 3 or t.casefold() in _GENERIC_HEADINGS:
        return None
    if words[0].casefold() in _INTERROGATIVE or words[0].casefold() in _STOPWORDS:
        return None
    return t


# Phase 9: everyday words that are frequent in any article but are never a study term on their own. Deliberately
# short: words such as "list", "page" or "type" are real technical terms in some articles and are not included.
_GENERIC_WORDS = frozenset({"post", "posts", "example", "examples", "thing", "things", "time", "times", "way", "ways",
                            "day", "days", "week", "weeks", "month", "months", "year", "years", "people"})
_COMPOUND_IDENT = re.compile(r"_|[a-z][A-Z]|^[A-Z]")
_WEAK_PENALTY = 2.5  # pushes unsupported heading/code-only terms below terms the prose actually uses


def _weak_keyword(cand: _Candidate, defined: bool, prose: Sequence[tuple[int, str]]) -> str:
    """D-6: how much evidence does a candidate have? Returns "drop", "demote" or "" (fine).

    * "drop": an acronym of two letters that the article never defines ("AI", "UI") is too ambiguous;
    * "drop": a word found only by frequency that is on the short generic-word list ("post", "example");
    * "demote": a term that exists only as a section title and is never used in the running text
      ("Common failure modes"), or a plain lower-case word seen only in code ("retrieve" is a function
      name). They stay available as key terms (the heading and code cues are deliberate) but rank
      below terms the article's prose actually uses, so they only appear when little else exists.

    Anything with a definition (or a defined alias) is never weak.
    """
    sources = cand.sources
    if defined or sources & {"definition", "appositive"}:
        return ""
    if sources == {"acronym"} and len(cand.term) <= 2:
        return "drop"
    if sources == {"frequency"} and cand.term.casefold() in _GENERIC_WORDS:
        return "drop"
    if sources == {"heading"} or (sources == {"code"} and not _COMPOUND_IDENT.search(cand.term)):
        return "demote" if _count_term(cand.term, prose, cand.case_sensitive)[0] == 0 else ""
    return ""


def extract_keywords(
    items: Sequence[ContentItem],
    headings: Sequence[tuple[int, str]],
    definitions: Sequence[Definition],
    config: Optional[ProcessorConfig] = None,
) -> list[Keyword]:
    """Rank technical terms using definitions, acronyms, headings, code identifiers and frequency.

    Terms are always taken from the article text. Frequency-based phrases come from
    Latin-script words only, so English technical terms inside Telugu (or other) text are
    found without stop-word noise from languages this module has no stop-word list for.
    """
    config = config or ProcessorConfig()
    prose: list[tuple[int, str]] = []
    everything: list[tuple[int, str]] = list(headings)
    code_blocks: list[tuple[int, str]] = []
    for item in items:
        if item.kind is BlockKind.CODE:
            code_blocks.append((item.source_index, item.block.text))
            everything.append((item.source_index, item.block.text))
            continue
        text = item.text
        everything.append((item.source_index, text))
        if item.kind is not BlockKind.TABLE:
            prose.append((item.source_index, text))

    cands: dict[str, _Candidate] = {}

    def add(term: str, source: str, case_sensitive: bool = False) -> None:
        key = normalize_key(term) if not case_sensitive else term
        if not key:
            return
        cand = cands.setdefault(key, _Candidate(term, case_sensitive=case_sensitive))
        cand.sources.add(source)

    for d in definitions:
        add(d.term, "appositive" if d.confidence is Confidence.LOW else "definition")
        for alias in d.aliases:
            if len(alias.split()) <= 6:
                add(alias, "acronym" if alias.isupper() else "definition", case_sensitive=alias.isupper())

    heading_texts = [t for _, t in headings]
    all_caps_headings = {w for t in heading_texts if t.isupper() for w in t.split()}
    for _, text in prose:
        for m in _ACRONYM.finditer(text):
            tok = m.group(0)
            base = tok[:-1] if tok.endswith("s") and len(tok) > 3 and tok[:-1].isupper() else tok
            if base in _ACRONYM_IGNORE or base in all_caps_headings or base.isdigit():
                continue
            add(base, "acronym", case_sensitive=True)

    for _, text in headings:
        term = _heading_term(text)
        if term:
            add(term, "heading")

    for _, code in code_blocks:
        for pattern in _CODE_IDENT:
            for m in pattern.finditer(code):
                add(m.group(1), "code", case_sensitive=True)

    # n-gram frequency, compounds and capitalised terms, over prose and headings.
    ngrams: Counter[tuple[str, ...]] = Counter()
    display: dict[tuple[str, ...], str] = {}
    compounds: Counter[str] = Counter()
    named: Counter[str] = Counter()
    for _, text in [*prose, *headings]:
        for segment in _SEGMENT_BREAK.split(text):
            raw = tokenize(segment)
            for pos, tok in enumerate(raw):
                if _COMPOUND.match(tok):
                    compounds[tok] += 1
                if pos > 0 and _CAP_WORD.match(tok) and tok.casefold() not in _STOPWORDS:
                    named[tok] += 1
            run: list[str] = []
            for tok in raw:
                if _is_latin(tok):
                    run.append(tok)
                    continue
                _count_ngrams(run, ngrams, display)
                run = []
            _count_ngrams(run, ngrams, display)
    for term, n in compounds.items():
        if n >= config.min_keyword_count:
            add(term, "compound")
    for term, n in named.items():
        if n >= config.min_keyword_count and normalize_key(term) not in cands:
            add(term, "named_term")
    for gram, n in ngrams.items():
        need = config.min_single_word_count if len(gram) == 1 else config.min_keyword_count
        if n >= need:
            add(display[gram], "frequency")
    for phrase in _capitalised_phrases(prose):
        add(phrase, "named_term")

    keywords: list[Keyword] = []
    defined = {normalize_key(d.term) for d in definitions} | {
        normalize_key(a) for d in definitions for a in d.aliases
    }
    for key, cand in cands.items():
        count, blocks = _count_term(cand.term, everything, cand.case_sensitive)
        if count == 0:
            continue
        weak = _weak_keyword(cand, normalize_key(cand.term) in defined, prose)  # acronym keys are case-sensitive: compare normalised
        if weak == "drop":
            continue
        strong = cand.sources & {"definition", "acronym", "appositive", "heading", "code"}
        words = len(cand.term.split())
        if not strong:
            need = config.min_single_word_count if words == 1 and cand.sources == {"frequency"} else config.min_keyword_count
            if count < need:
                continue
        score = (max(_SOURCE_WEIGHT[s] for s in cand.sources) + 0.5 * (len(cand.sources) - 1)
                 + math.log1p(count) + 0.3 * min(words - 1, 2) - (_WEAK_PENALTY if weak else 0.0))
        keywords.append(Keyword(
            term=cand.term, key=normalize_key(cand.term), score=score, count=count,
            sources=tuple(sorted(cand.sources)), block_indices=blocks,
            has_definition=normalize_key(cand.term) in defined,
        ))

    keywords = _drop_subsumed(keywords)
    keywords.sort(key=lambda k: (-k.score, k.first_index, k.term.casefold()))
    seen: set[str] = set()
    unique: list[Keyword] = []
    for k in keywords:
        singular = k.key[:-1] if k.key.endswith("s") and len(k.key) > 3 else k.key
        if k.key in seen or singular in seen or f"{k.key}s" in seen:
            continue  # same term in plural/singular form: keep the higher-ranked one
        seen.add(k.key)
        unique.append(k)
    return unique[: config.max_keywords]


def _count_ngrams(run: list[str], ngrams: Counter[tuple[str, ...]], display: dict[tuple[str, ...], str]) -> None:
    low = [t.casefold() for t in run]
    for n in (1, 2, 3):
        for i in range(len(run) - n + 1):
            gram = tuple(low[i:i + n])
            first, last = gram[0], gram[-1]
            if first in _STOPWORDS or last in _STOPWORDS:
                continue
            if n == 1 and (len(first) < 4 or first.isdigit()):
                continue
            inner = gram[1:-1]
            if any(g in _STOPWORDS and g not in _PHRASE_CONNECTORS for g in inner) or len(inner) > 1 and any(g in _STOPWORDS for g in inner):
                continue
            if any(g.isdigit() for g in gram):
                continue
            ngrams[gram] += 1
            phrase = " ".join(run[i:i + n])
            if phrase[:1].isupper() and phrase[1:].islower():  # only a sentence-initial capital
                phrase = phrase.lower()
            display.setdefault(gram, phrase)


def _capitalised_phrases(prose: Sequence[tuple[int, str]]) -> list[str]:
    """Runs of two or more capitalised words that are not at the start of a sentence."""
    found: Counter[str] = Counter()
    for _, text in prose:
        for sentence in split_sentences(text):
            words = sentence.split()
            run: list[str] = []
            for pos, w in enumerate(words):
                bare = w.strip(".,;:!?()\"'“”")
                if pos > 0 and bare[:1].isupper() and bare.casefold() not in _STOPWORDS and bare.isalpha():
                    run.append(bare)
                else:
                    if len(run) >= 2:
                        found[" ".join(run)] += 1
                    run = []
            if len(run) >= 2:
                found[" ".join(run)] += 1
    return list(found)


def _drop_subsumed(keywords: list[Keyword]) -> list[Keyword]:
    """Drop frequency-only n-grams that never occur outside a longer keyword."""
    result: list[Keyword] = []
    for k in keywords:
        if set(k.sources) == {"frequency"}:
            words = k.key.split()
            absorbed = any(
                other is not k and len(other.key.split()) > len(words)
                and f" {k.key} " in f" {other.key} " and other.count >= k.count
                for other in keywords
            )
            if absorbed:
                continue
        result.append(k)
    return result


# =========================================================================== key statements
_CUES: list[tuple[str, StatementKind, float, re.Pattern[str]]] = [
    ("conclusion_cue", StatementKind.CONCLUSION, 2.5, re.compile(
        r"\b(in conclusion|to conclude|to summari[sz]e|in summary|in short|the key (idea|point|takeaway|lesson)|"
        r"key takeaway|if you remember one|bottom line|the takeaway|the main (point|lesson)|overall,|"
        r"ultimately,|in the end)\b", re.IGNORECASE)),
    ("recommendation_cue", StatementKind.RECOMMENDATION, 1.5, re.compile(
        r"\b(should|must|need to|ought to|recommend(ed)?|best practice|make sure|avoid|in practice|"
        r"rule of thumb|better to|best to|start with|begin with|lessons?|tips?)\b", re.IGNORECASE)),
    ("fact_cue", StatementKind.FACT, 1.5, re.compile(
        r"\b\d[\d,.]*\s?(%|percent|ms|seconds?|minutes?|hours?|days?|years?|mb|gb|tb|kb|million|billion|"
        r"thousand|x|dimensions?|parameters?|words?|tokens?|bits?)\b|\b(19|20)\d{2}\b|according to|"
        r"research (shows?|found)|studies (show|found)", re.IGNORECASE)),
    ("explanation_cue", StatementKind.EXPLANATION, 1.5, re.compile(
        r"\b(because|therefore|thus|hence|as a result|which means|this is why|that is why|so that|due to|"
        r"in order to|this means|leads? to|results? in)\b", re.IGNORECASE)),
    ("example_cue", StatementKind.EXAMPLE, 1.0, re.compile(
        r"\b(for example|for instance|such as|e\.g\.|imagine|suppose)\b", re.IGNORECASE)),
]
# D-10: an instruction such as "Measure retrieval quality before you tune the prompt." Closed verb list, and never
# applied to list items, because the steps of a procedure ("Split the documents...") are not recommendations.
_IMPERATIVE = re.compile(
    r"^(?:(?:always|never|first|then|also|do not|don't)[, ]+)?(?:measure|use|avoid|keep|check|start|begin|test|add|choose|"
    r"prefer|review|evaluate|ensure|track|monitor|limit|try|consider|remember|validate|compare|separate|document|write|"
    r"log|store|prioriti[sz]e|plan|define|verify)\b", re.IGNORECASE)
_KIND_PRIORITY = [StatementKind.DEFINITION, StatementKind.CONCLUSION, StatementKind.RECOMMENDATION,
                  StatementKind.FACT, StatementKind.EXPLANATION, StatementKind.EXAMPLE]


def extract_statements(
    items: Sequence[ContentItem],
    section_of: Mapping[int, str],
    section_heading: Mapping[str, Optional[str]],
    definitions: Sequence[Definition],
    keywords: Sequence[Keyword],
    config: Optional[ProcessorConfig] = None,
) -> list[KeyStatement]:
    """Select important sentences by transparent cues (definitions, conclusions, facts...).

    Statements are returned verbatim and in document order, each with the cues that
    justified its score so later stages can audit or filter them.
    """
    config = config or ProcessorConfig()
    # Low-confidence (appositive) definitions are too speculative to promote to key statements.
    reliable = [d for d in definitions if d.confidence is not Confidence.LOW]
    defined = {(d.source_index, d.list_item_index, d.sentence_index) for d in reliable}
    defined_text = {d.statement for d in reliable}
    top_terms = [k.term for k in keywords[:20]]
    opening: dict[str, int] = {}  # section id -> source index of its first paragraph
    for item in items:
        sid_ = section_of.get(item.source_index)
        if sid_ is not None and item.kind is BlockKind.PARAGRAPH:
            opening.setdefault(sid_, item.source_index)
    scored: list[KeyStatement] = []

    for item in items:
        if item.kind not in (BlockKind.PARAGRAPH, BlockKind.QUOTE, BlockKind.LIST):
            continue
        sid = section_of.get(item.source_index)
        heading = section_heading.get(sid or "", None) or ""
        concl_section = bool(_CONCLUSION_HEADING.search(heading))
        for sent in item.sentences:
            words = sent.text.split()
            if not 6 <= len(words) <= 60 or sent.text.endswith("?"):
                continue
            reasons: list[str] = []
            kinds: set[StatementKind] = set()
            score = 0.0
            if sent.text in defined_text or (item.source_index, sent.list_item_index, sent.index) in defined:
                kinds.add(StatementKind.DEFINITION)
                reasons.append("definition")
                score += 3.0
            for name, kind, weight, pattern in _CUES:
                if pattern.search(sent.text):
                    kinds.add(kind)
                    reasons.append(name)
                    score += weight
            if item.kind is not BlockKind.LIST and _IMPERATIVE.match(sent.text):
                kinds.add(StatementKind.RECOMMENDATION)
                reasons.append("imperative_cue")
                score += 1.5
            if concl_section:
                # D-10: sitting in a conclusion section must not hide a more specific kind: a recommendation
                # stays a practical lesson and a fact stays a finding.
                if not kinds & {StatementKind.RECOMMENDATION, StatementKind.FACT}:
                    kinds.add(StatementKind.CONCLUSION)
                reasons.append("conclusion_section")
                score += 2.0
            if not kinds:
                continue
            hits = sum(1 for t in top_terms if _contains_term(sent.text, t))
            if hits:
                reasons.append(f"keywords:{min(hits, 3)}")
                score += 0.5 * min(hits, 3)
            if sid is not None and opening.get(sid) == item.source_index and sent.index == 0:
                reasons.append("section_opening")
                score += 0.75
            kind = next(k for k in _KIND_PRIORITY if k in kinds)
            scored.append(KeyStatement(sent.text, kind, score, tuple(reasons), item.source_index,
                                       sent.index, sid, list_item_index=sent.list_item_index))

    chosen = [s for s in scored if s.score >= config.min_statement_score]
    if len(chosen) > config.max_statements:
        chosen = sorted(chosen, key=lambda s: -s.score)[: config.max_statements]
    return sorted(chosen, key=lambda s: (s.source_index, s.list_item_index or 0, s.sentence_index))


def _contains_term(text: str, term: str) -> bool:
    return bool(regex.search(r"(?<![\p{L}\p{M}\p{N}])" + regex.escape(term) + r"(?![\p{L}\p{M}\p{N}])",
                             text, regex.IGNORECASE))


# =========================================================================== processor
def _make_item(index: int, block: Block) -> ContentItem:
    sentences: list[Sentence] = []
    if block.kind in (BlockKind.PARAGRAPH, BlockKind.QUOTE):
        sentences = [Sentence(s, i) for i, s in enumerate(split_sentences(block.text))]
    elif block.kind is BlockKind.LIST:
        for li, entry in enumerate(block.items):
            for si, s in enumerate(split_sentences(entry.text)):
                sentences.append(Sentence(s, si, list_item_index=li))
    return ContentItem(source_index=index, block=block, sentences=tuple(sentences))


def _guarded(step: str, default: T, func: Callable[[], T], warnings: list[str]) -> T:
    """Run an optional analysis step; on failure log it, warn and continue with ``default``."""
    try:
        return func()
    except Exception as exc:  # analysis is best-effort; structure must still be delivered
        logger.exception("Content analysis step %r failed", step)
        warnings.append(f"The {step} step failed ({type(exc).__name__}: {exc}); results omitted.")
        return default


class ContentProcessor:
    """Turns an :class:`Article` into :class:`ProcessedContent`. No network, no paid services."""

    def __init__(self, config: Optional[ProcessorConfig] = None) -> None:
        self.config = config or ProcessorConfig()

    def process(self, article: Article) -> ProcessedContent:
        if not isinstance(article, Article):
            raise InvalidArticleError(f"Expected an Article, got {type(article).__name__}.")
        if not isinstance(article.blocks, (list, tuple)):
            raise InvalidArticleError("Article.blocks must be a list of Block objects.")
        for i, b in enumerate(article.blocks):
            if not isinstance(b, Block):
                raise InvalidArticleError(f"Article.blocks[{i}] is not a Block ({type(b).__name__}).")
        if not article.blocks:
            raise EmptyContentError("The article has no content blocks.")

        cfg = self.config
        removed: list[RemovedItem] = []
        warnings: list[str] = []

        # 1. clean, 2. drop empty and boilerplate
        cleaned: list[tuple[int, Block]] = []
        for index, block in enumerate(article.blocks):
            new = clean_block(block)
            if new is None:
                removed.append(RemovedItem(index, RemovalReason.EMPTY, block.plain_text))
                continue
            if cfg.remove_boilerplate and self._is_boilerplate_block(new):
                removed.append(RemovedItem(index, RemovalReason.BOILERPLATE, new.plain_text))
                continue
            cleaned.append((index, new))

        # 3. de-duplicate prose
        if cfg.dedupe:
            dupes = find_duplicates(cleaned, cfg.near_duplicate_threshold, cfg.min_duplicate_words)
            for index, block in cleaned:
                if index in dupes:
                    kept, reason, sim = dupes[index]
                    removed.append(RemovedItem(index, reason, block.plain_text, kept, sim))
            cleaned = [(i, b) for i, b in cleaned if i not in dupes]

        if not any(b.kind is not BlockKind.HEADING for _, b in cleaned):
            raise EmptyContentError("No article content remained after cleaning.")

        # 4. items and section tree
        items = {i: _make_item(i, b) for i, b in cleaned if b.kind is not BlockKind.HEADING}
        sections = build_sections(cleaned, items)
        flat = [s for top in sections for s in top.walk()]
        section_of = {it.source_index: s.id for s in flat for it in s.items}
        section_heading = {s.id: s.heading for s in flat}
        ordered_items = [it for s in flat for it in s.items]
        if not any(b.kind is BlockKind.HEADING for _, b in cleaned):
            warnings.append("The article has no headings; all content is in a single section.")

        # 5. analysis (best effort)
        headings = [(s.heading_index, s.heading) for s in flat
                    if s.heading is not None and s.heading_index is not None]
        definitions = _guarded("definition extraction", [],
                               lambda: extract_definitions(ordered_items, section_of), warnings)
        keywords = _guarded("keyword extraction", [],
                            lambda: extract_keywords(ordered_items, headings, definitions, cfg), warnings)
        statements = _guarded("statement extraction", [],
                              lambda: extract_statements(ordered_items, section_of, section_heading,
                                                         definitions, keywords, cfg), warnings)

        removed.sort(key=lambda r: r.source_index)
        words_in = sum(b.word_count for b in article.blocks)
        stats = ProcessingStats(
            blocks_in=len(article.blocks), items_out=len(ordered_items) + len(headings),
            words_in=words_in,
            words_out=sum(it.word_count for it in ordered_items) + sum(len(h.split()) for _, h in headings),
            removed_empty=sum(r.reason is RemovalReason.EMPTY for r in removed),
            removed_boilerplate=sum(r.reason is RemovalReason.BOILERPLATE for r in removed),
            removed_duplicates=sum(r.reason in (RemovalReason.DUPLICATE_EXACT, RemovalReason.DUPLICATE_NEAR)
                                   for r in removed),
        )
        logger.info(
            "Processed %d blocks: %d sections, %d definitions, %d keywords, %d statements, "
            "%d removed (%d dup, %d boilerplate)",
            stats.blocks_in, len(flat), len(definitions), len(keywords), len(statements),
            len(removed), stats.removed_duplicates, stats.removed_boilerplate,
        )
        return ProcessedContent(
            article=article, title=clean_text(article.title) or "Untitled Article",
            author=article.author, published=article.published, url=article.url,
            site_name=article.site_name, source_kind=article.source_kind, extractor=article.extractor,
            completeness=article.completeness,
            completeness_reasons=tuple(article.completeness_reasons),
            warnings=tuple(article.warnings), processing_warnings=warnings,
            sections=sections, definitions=definitions, keywords=keywords, statements=statements,
            removed=removed, stats=stats,
        )

    @staticmethod
    def _is_boilerplate_block(block: Block) -> bool:
        if block.kind is BlockKind.PARAGRAPH:
            return is_boilerplate(block.text)
        if block.kind is BlockKind.LIST:
            return all(is_boilerplate(i.text) for i in block.items)
        return False


def process_article(article: Article, config: Optional[ProcessorConfig] = None) -> ProcessedContent:
    """Convenience wrapper around :class:`ContentProcessor`."""
    return ContentProcessor(config).process(article)
