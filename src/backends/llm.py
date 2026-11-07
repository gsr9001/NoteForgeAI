"""Shared logic for language-model backends (Ollama, OpenAI) (Phase 5).

Subclasses only implement :meth:`LLMBackend._complete` (how to send a prompt and get text back).
This module builds the prompts, parses the JSON replies and, most importantly, **checks the
replies against the article** before anything is accepted:

* numbers (also written out: "twice", "dozens"), URLs, citations, studies/experts attributions, quotations,
  version-like specifications and equations that do not occur in the article are rejected
* explanations are checked sentence by sentence against the article's vocabulary: a sentence that makes a
  comparison ("instead of", "rather than", "unlike"...) with something the article never mentions, or that is
  mostly made of words the article does not use, is removed; a sentence that is only partly supported is kept
  but reported for human review
* malformed or structurally invalid questions are rejected
* everything accepted is labelled AI-generated

The checks cannot prove a statement is true; they catch the most damaging kinds of
invention (fabricated figures, references and formulae) and keep the rest clearly labelled.
"""
from __future__ import annotations

import abc
import dataclasses
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

from src.backends.base import (
    BackendResponseError, ExampleNote, GeneratedText, GenerationParams, NotesBackend, QuestionResult,
    QuestionSpec, SectionPacket, TermNote, TermPacket,
)
from src.content_processor import ProcessedContent
from src.notes_model import (
    Language, NoteProvenance, Question, QuestionKind, validate_question,
)

logger = logging.getLogger(__name__)

AI = NoteProvenance.AI_GENERATED

SYSTEM_RULES = (
    "You write study notes from an article that the user supplies. Rules you must follow:\n"
    "1. Use ONLY information in the supplied article text. Do not add facts, examples, background knowledge "
    "or opinions from memory.\n"
    "2. Never invent statistics, numbers (including words such as 'twice' or 'dozens'), dates, quotations, "
    "citations, studies, experts, URLs, names, version numbers, specifications or equations.\n"
    "3. Do not compare the article's subject with anything the article does not mention (no 'instead of', "
    "'rather than', 'unlike' or 'better than' about things that are not in the text).\n"
    "4. If the article does not say something, leave it out; do not guess. Writing less is better than writing "
    "something the article does not support.\n"
    "5. Keep technical terms exactly as written in the article, in English.\n"
    "6. Be specific and avoid filler or repetition.\n"
    "7. Reply with a single JSON object and nothing else."
)

def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


_STR: dict[str, Any] = {"type": "string"}
#: JSON schemas for each task's structured output. Backends that support constrained output (Ollama's
#: ``format`` field) use them to force the shape the parsers expect, which matters most for small local
#: models. They constrain structure only: the content is still checked by ``check_grounding``.
SCHEMAS: dict[str, dict[str, Any]] = {
    "keyword explanations": _object({"terms": {"type": "array", "items": _object(
        {"term": _STR, "explanation": _STR, "significance": _STR}, ["term", "explanation", "significance"])}}, ["terms"]),
    "section explanations": _object({"sections": {"type": "array", "items": _object(
        {"id": _STR, "explanation": _STR}, ["id", "explanation"])}}, ["sections"]),
    "analogies": _object({"examples": {"type": "array", "items": _object(
        {"concept": _STR, "kind": {"type": "string", "enum": ["analogy", "application"]}, "text": _STR},
        ["concept", "kind", "text"])}}, ["examples"]),
    "short questions": _object({"questions": {"type": "array", "items": _object(
        {"question": _STR, "answer": _STR}, ["question", "answer"])}}, ["questions"]),
    "descriptive questions": _object({"questions": {"type": "array", "items": _object(
        {"question": _STR, "answer": _STR}, ["question", "answer"])}}, ["questions"]),
    "mcq questions": _object({"questions": {"type": "array", "items": _object(
        {"question": _STR, "options": {"type": "array", "items": _STR, "minItems": 4, "maxItems": 4},
         "correct_index": {"type": "integer", "minimum": 0, "maximum": 3}, "explanation": _STR},
        ["question", "options", "correct_index", "explanation"])}}, ["questions"]),
}

_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?%?")
_URL = re.compile(r"https?://\S+|www\.\S+")
_CITATION = re.compile(
    r"\b[A-Z][a-z]+ et al\.?|\(\s*[A-Z][A-Za-z]+,?\s+(?:19|20)\d{2}\s*\)|\bdoi:\S+|\barXiv:\S+|\[\d+\]"
)
_FORMULA = re.compile(r"[A-Za-z_]\w*\s*=\s*[^\s,;]+|\\[a-z]+\{|\$[^$\n]+\$")
_NUMBER_WORD = re.compile(
    # "one" .. "nine" are ignored, like single digits (counting the article's own items); larger and
    # multiplicative figures must come from the article.
    r"\b(?:ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
    r"fifty|sixty|seventy|eighty|ninety|hundreds?|thousands?|millions?|billions?|dozens?|twice|thrice|"
    r"double[sd]?|triple[sd]?|quadruple[sd]?|half)\b", re.IGNORECASE)
_ATTRIBUTION = re.compile(
    r"\b(?:according to (?:an? |the )?(?:recent )?(?:study|studies|report|survey|paper|research|researchers|experts|"
    r"analysts)|(?:studies|research|surveys|researchers|experts|analysts|scientists|engineers)\s+(?:show|have shown|"
    r"has shown|found|find|suggest|say|agree|report|estimate)|an? (?:recent )?(?:study|survey|report) (?:by|from|"
    r"shows?|found))\b", re.IGNORECASE)
_ATTRIBUTION_NOUN = re.compile(r"study|studies|report|survey|paper|research|researchers|experts|analysts|scientists|"
                               r"engineers", re.IGNORECASE)
_SPEC = re.compile(r"\b(?:[A-Za-z]{2,}-\d+(?:\.\d+)*[A-Za-z]*|v\d+(?:\.\d+)+|\d+(?:\.\d+)?\s?(?:ms|gb|mb|kb|tb|ghz|mhz)\b)",
                   re.IGNORECASE)
_QUOTED = re.compile(r"[\"\u201c]([^\"\u201c\u201d]{20,}?)[\"\u201d]")


def _norm_num(token: str) -> str:
    return token.replace(",", "").rstrip(".")


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def source_text(content: ProcessedContent) -> str:
    """All article text (headings, prose, lists, tables, code) used as the grounding reference."""
    parts = [content.title]
    for section in content.iter_sections():
        if section.heading:
            parts.append(section.heading)
        parts.extend(item.text for item in section.items)
    return "\n".join(parts)


def check_grounding(generated: str, source: str) -> list[str]:
    """Reasons ``generated`` contains specifics that the article does not (empty = acceptable).

    Single-digit integers are ignored (list numbering); every other number, plus URLs,
    citations and equation-like fragments, must appear in the article.
    """
    problems: list[str] = []
    source_numbers = {_norm_num(m) for m in _NUMBER.findall(source)}
    for token in _NUMBER.findall(generated):
        norm = _norm_num(token)
        if re.fullmatch(r"\d", norm):
            continue
        if norm not in source_numbers:
            problems.append(f"number {token!r} is not in the article")
    squashed = _squash(source)
    for pattern, label in ((_URL, "URL"), (_CITATION, "citation"), (_FORMULA, "equation"), (_SPEC, "specification")):
        for m in pattern.finditer(generated):
            if _squash(m.group(0)) not in squashed:
                problems.append(f"{label} {m.group(0)!r} is not in the article")
    for m in _NUMBER_WORD.finditer(generated):
        if not re.search(rf"\b{re.escape(m.group(0))}\b", source, re.IGNORECASE):
            problems.append(f"figure {m.group(0)!r} is not in the article")
    for m in _ATTRIBUTION.finditer(generated):
        nouns = {n.lower() for n in _ATTRIBUTION_NOUN.findall(m.group(0))}
        if not any(re.search(rf"\b{re.escape(n)}\b", source, re.IGNORECASE) for n in nouns):
            problems.append(f"attribution {m.group(0)!r} is not in the article")
    for m in _QUOTED.finditer(generated):
        if _squash(m.group(1)) not in squashed:
            problems.append(f"quotation {m.group(1)[:40]!r} is not in the article")
    return problems


# --------------------------------------------------------------------------- sentence-level support (D-7)
_STOP = frozenset("""
about above after again against also among because been before being below between both but cannot could does doing
down during each either else ever every from further have having here into itself just like many more most much must
never only other over rather same should since some such than that their them then there these they this those through
under until upon very were what when where which while whom whose will with within without would your yours
""".split())
#: Words a summary uses about the article itself ("this section explains ..."); they say nothing about the topic.
_META = frozenset("""
section article text author passage passages explains explain explained describes describe described discusses
discuss states state mentions mention according provides provide outlines outline covers cover helps help means mean
idea concept term terms important main learner learners reader readers notes note basic simple key overall following
several
""".split())
_SUFFIXES = ("ations", "ation", "ings", "ing", "edly", "ied", "ies", "ed", "es", "s", "ly")
_WORD = re.compile(r"[A-Za-z]{4,}")  # hyphenated words are split: "production-ready" -> production, ready
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(`\u201c])")
_CONTRAST = re.compile(
    r"\b(?:instead of|rather than|unlike|as opposed to|in contrast to|compared (?:to|with)|versus|vs\.?|"
    r"(?:more|less|better|worse|faster|slower|cheaper|simpler|larger|smaller|higher|lower|greater|fewer)\b"
    r"[^.;,]{0,40}?\bthan)\b", re.IGNORECASE)
SUPPORTED_RATIO = 0.6   # at least this share of a sentence's content words occur in the article: supported
PARTIAL_RATIO = 0.4     # below SUPPORTED_RATIO but not below this: partly supported (kept, flagged for review)


def _stem(word: str) -> str:
    w = word.lower()
    for suffix in _SUFFIXES:
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            return w[: -len(suffix)]
    return w


def _content_stems(text: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(text) if w.lower() not in _STOP and w.lower() not in _META]


@dataclass(frozen=True)
class SupportVerdict:
    """How well the article supports one generated sentence (a deterministic word-level check, not a fact check)."""

    sentence: str
    status: str  # "supported" | "partial" | "unsupported"
    ratio: float
    novel_words: tuple[str, ...] = ()
    unsupported_comparison: str = ""  # the comparison cue ("instead of"...) when the compared thing is not in the article


def assess_support(text: str, source: str) -> list[SupportVerdict]:
    """Judge each sentence of ``text`` against the vocabulary of the article.

    * a comparison cue ("instead of", "rather than", "unlike", "... than") followed by words the article never
      uses is always *unsupported* (the article was not comparing those things);
    * otherwise the share of the sentence's content words found in the article decides: at or above
      ``SUPPORTED_RATIO`` supported, at or above ``PARTIAL_RATIO`` partial, below that unsupported.

    The check proves nothing about truth: a supported sentence may still be wrong, which is why every AI-written
    text stays labelled and partly supported sentences are reported for review.
    """
    vocab = {_stem(w) for w in _WORD.findall(source)}
    verdicts: list[SupportVerdict] = []
    for sentence in (s.strip() for s in _SENTENCE_SPLIT.split(text.strip())):
        if not sentence:
            continue
        stems = _content_stems(sentence)
        novel = tuple(dict.fromkeys(w for w in stems if w not in vocab))
        ratio = 1.0 if len(stems) < 3 else 1 - sum(1 for w in stems if w not in vocab) / len(stems)
        comparison = ""
        for m in _CONTRAST.finditer(sentence):
            clause = re.split(r"[.;,:]", sentence[m.end():])[0]
            if [w for w in _content_stems(" ".join(clause.split()[:8])) if w not in vocab]:
                comparison = m.group(0).strip().lower()
                break
        status = ("unsupported" if comparison or ratio < PARTIAL_RATIO
                  else "partial" if ratio < SUPPORTED_RATIO else "supported")
        verdicts.append(SupportVerdict(sentence, status, round(ratio, 2), novel[:6], comparison))
    return verdicts


_NON_ANSWER = re.compile(
    r"^\W*(?:n/?a\b|none\b|nothing\b|no (?:further )?information\b|not (?:explicitly |separately )?"
    r"(?:discussed|mentioned|stated|specified|covered|addressed|provided|described|given|available)\b|"
    r"(?:the )?article (?:does not|doesn't|did not) (?:discuss|mention|say|state|cover|provide)\b)", re.IGNORECASE)
_COMPARISON = re.compile(
    r"\b(like|as if|similar to|imagine|think of|just as|akin to|resembles?|compare[sd]? to|analogous|the same way)\b",
    re.IGNORECASE)


def is_non_answer(text: str) -> bool:
    """True for replies such as 'Not discussed' or 'N/A' that say nothing (small models write them as filler)."""
    stripped = text.strip()
    return bool(_NON_ANSWER.match(stripped)) and len(stripped.split()) <= 12  # filler is short; real prose is not


def is_substantive(text: str, min_words: int = 6) -> bool:
    """Long enough to be a real sentence and not a 'nothing to say' reply."""
    return len(text.split()) >= min_words and not is_non_answer(text)


def copies_source(text: str, source: str) -> bool:
    """True if ``text`` is just a passage of the article (so it is not new AI-written content)."""
    return _squash(text) in _squash(source)


def parse_json_object(raw: str) -> dict[str, Any]:
    """Parse a model reply that should be one JSON object (tolerates code fences and chatter)."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise BackendResponseError("The model reply did not contain a JSON object.")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise BackendResponseError(f"The model reply was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BackendResponseError("The model reply JSON was not an object.")
    return data


def language_rule(language: Language) -> str:
    if language is Language.TELUGU:
        return "Write your own text in Telugu, but keep technical terms in English exactly as in the article."
    if language is Language.BILINGUAL:
        return ("Write each piece of your own text twice: first in English, then in Telugu, "
                "keeping technical terms in English.")
    return "Write in English."


def batch_by_size(items: Sequence[Any], size: Callable[[Any], int], budget: int) -> list[list[Any]]:
    """Group items so that each batch's total ``size`` stays within ``budget`` (one item minimum)."""
    batches: list[list[Any]] = []
    current: list[Any] = []
    used = 0
    for item in items:
        n = size(item)
        if current and used + n > budget:
            batches.append(current)
            current, used = [], 0
        current.append(item)
        used += n
    if current:
        batches.append(current)
    return batches


def article_digest(content: ProcessedContent, max_chars: int) -> str:
    """The article text for question prompts, shortened proportionally if it exceeds ``max_chars``."""
    sections = []
    for s in content.iter_sections():
        text = "\n".join(i.text for i in s.items)
        sections.append(((s.heading or "(introduction)"), text))
    total = sum(len(t) for _, t in sections) or 1
    if total <= max_chars:
        return "\n\n".join(f"## {h}\n{t}" for h, t in sections)
    ratio = max_chars / total
    out = []
    for h, t in sections:
        keep = max(200, int(len(t) * ratio))
        out.append(f"## {h}\n{t[:keep]}{' [...]' if len(t) > keep else ''}")
    return "\n\n".join(out)


# =========================================================================== question diversity (D-4)
SOURCE_MATCH_MIN = 0.35   # share of an answer's content words that a block must contain to be cited
MAX_CITED_BLOCKS = 2
MIN_ANSWER_WORDS = 3      # fewer content words than this cannot be matched to a passage with confidence


def locate_sources(content: ProcessedContent, text: str) -> tuple[int, ...]:
    """Indices of the article blocks that best contain the content words of ``text`` (empty if none is close).

    Deterministic and offline: it compares word stems, so it finds where an answer comes from without
    asking the model. It does not prove the answer is right; it only says which passage to check.
    """
    wanted = set(_content_stems(text))
    if len(wanted) < MIN_ANSWER_WORDS:
        return ()  # too little to locate reliably: claim no reference rather than a wrong one
    scored: list[tuple[float, int]] = []
    for section in content.iter_sections():
        for item in section.items:
            have = set(_content_stems(item.text))
            if have:
                scored.append((len(wanted & have) / len(wanted), item.source_index))
    if not scored:
        return ()
    best = max(r for r, _ in scored)
    if best < SOURCE_MATCH_MIN:
        return ()
    close = sorted((i for r, i in scored if r >= max(SOURCE_MATCH_MIN, best - 0.1)), key=lambda i: i)
    top = sorted(close, key=lambda i: (-next(r for r, j in scored if j == i), i))[:MAX_CITED_BLOCKS]
    return tuple(sorted(top))


def _same_fact(a: Question, b: Question) -> bool:
    """Two questions whose answers are (nearly) the same statement test the same fact.

    Near-identical *wording* is handled separately by the generator, which reports it (see ``_near_duplicate``).
    """
    sx, sy = set(_content_stems(a.answer)), set(_content_stems(b.answer))
    if len(sx) < 4 or len(sy) < 4:
        return False
    return sx <= sy or sy <= sx or len(sx & sy) / len(sx | sy) >= 0.85


def select_diverse_questions(
    validated: dict[str, list[Question]], wanted: dict[str, int]
) -> tuple[dict[str, list[Question]], dict[str, int]]:
    """Keep model-written questions that add something new; never pad to reach a requested count.

    * a fact is not asked twice, whatever the question type;
    * a strict pass allows ``STRICT_BLOCK_CAP`` questions per source block (the first cited block);
    * a second pass allows ``RELAXED_BLOCK_CAP`` only to get closer to the requested number, and still
      refuses repeated facts.

    Returns the kept questions per kind and how many were left out per kind. Questions without a located
    source block are not limited by the block cap (nothing is known about their passage).
    """
    from src.backends.local_questions import RELAXED_BLOCK_CAP, STRICT_BLOCK_CAP  # shared with the Local builder

    kept: dict[str, list[Question]] = {k: [] for k in validated}
    used: dict[int, int] = {}
    all_kept: list[Question] = []

    def take(kind: str, q: Question, cap: int) -> bool:
        if len(kept[kind]) >= wanted.get(kind, 0) or any(_same_fact(q, other) for other in all_kept):
            return False
        block = q.source_indices[0] if q.source_indices else None
        if block is not None and used.get(block, 0) >= cap:
            return False
        kept[kind].append(q)
        all_kept.append(q)
        if block is not None:
            used[block] = used.get(block, 0) + 1
        return True

    deferred: dict[str, list[Question]] = {k: [] for k in validated}
    for kind, questions in validated.items():
        for q in questions:
            if not take(kind, q, STRICT_BLOCK_CAP):
                deferred[kind].append(q)
    for kind in validated:
        for q in deferred[kind]:
            take(kind, q, RELAXED_BLOCK_CAP)
    left_out = {k: max(0, min(len(validated[k]), wanted.get(k, 0)) - len(kept[k])) for k in validated}
    return kept, left_out


class LLMBackend(NotesBackend):
    """Base for backends that talk to a language model."""

    supports_translation = True
    max_prompt_chars = 12000

    @abc.abstractmethod
    def _complete(self, system: str, user: str, purpose: str) -> str:
        """Send one prompt and return the model's text. Raises a BackendError on failure."""

    def _complete_json(self, system: str, user: str, purpose: str, schema: Optional[dict[str, Any]]) -> str:
        """Like :meth:`_complete`, but may constrain the output to ``schema`` if the backend supports it.

        The default ignores the schema, so simple backends only need to implement ``_complete``.
        """
        return self._complete(system, user, purpose)

    # ------------------------------------------------------------------ helpers
    def _ask(self, purpose: str, instructions: str, payload: str, params: GenerationParams) -> dict[str, Any]:
        user = f"{instructions}\n{language_rule(params.language)}\n\nARTICLE MATERIAL:\n{payload}"
        return parse_json_object(self._complete_json(SYSTEM_RULES, user, purpose, SCHEMAS.get(purpose)))

    def _accept(self, text: str, source: str, what: str) -> bool:
        problems = check_grounding(text, source)
        if problems:
            logger.warning("Rejected AI %s: %s", what, "; ".join(problems))
            self._warn(f"Rejected an AI-generated {what} that contained details not in the article "
                       f"({problems[0]}).")
            return False
        return True

    def _supported_text(self, text: str, source: str, what: str) -> str:
        """Keep only the sentences of an AI explanation that the article supports ("" = nothing left to keep).

        Unsupported sentences are removed (and reported); partly supported ones stay but are reported so a human
        can check them against the article. Omitting beats speculating.
        """
        kept: list[str] = []
        for v in assess_support(text, source):
            if v.status == "unsupported":
                reason = (f"it compares the subject with something the article never mentions ('{v.unsupported_comparison}')"
                          if v.unsupported_comparison else
                          f"only {int(v.ratio * 100)}% of its key words occur in the article")
                # The removed claim is logged but deliberately not repeated in the notes' warnings.
                logger.warning("Removed unsupported AI statement from %s: %s", what, v.sentence[:120])
                self._warn(f"Removed an AI-written statement from the {what} because the article does not support "
                           f"it ({reason}).")
                continue
            if v.status == "partial":
                self._warn(f"Review needed: the AI-written {what} contains a statement the article supports only "
                           f"partly (check it against the article): \u201c{v.sentence[:80]}\u201d.")
            kept.append(v.sentence)
        return " ".join(kept)

    # ------------------------------------------------------------------ keywords
    def explain_keywords(
        self, content: ProcessedContent, packets: Sequence[TermPacket], params: GenerationParams
    ) -> dict[str, TermNote]:
        source = source_text(content)
        out: dict[str, TermNote] = {}
        size = lambda p: len(p.term) + len(p.definition or "") + sum(len(c.text) for c in p.contexts) + 80  # noqa: E731
        for batch in batch_by_size(list(packets), size, self.max_prompt_chars):
            payload = json.dumps([{
                "term": p.term, "definition_in_article": p.definition,
                "sentences_from_article": [c.text for c in p.contexts],
                "practical_sentences_from_article": [c.text for c in p.significance_contexts],
            } for p in batch], ensure_ascii=False)
            data = self._ask(
                "keyword explanations",
                "For each term write (a) a detailed explanation of what it means in this article and "
                "(b) its practical significance according to the article. Write complete sentences in your own "
                "words, not the article's wording, and restate only what the article material says: every "
                "sentence must be supported by it. Do not add background knowledge or comparisons. Reply as "
                '{"terms": [{"term": "...", "explanation": "...", "significance": "..."}]}. '
                'If the article says nothing about a term practical significance, use an empty string "" for '
                'its "significance"; never write "not discussed" or similar.',
                payload, params)
            by_term = {p.term.casefold(): p for p in batch}
            for entry in data.get("terms", []):
                if not isinstance(entry, dict) or str(entry.get("term", "")).casefold() not in by_term:
                    continue
                packet = by_term[str(entry["term"]).casefold()]
                texts = []
                for key in ("explanation", "significance"):
                    value = str(entry.get(key, "")).strip()
                    if not is_substantive(value) or copies_source(value, source):
                        logger.debug("Dropped empty/filler/copied %s for %r: %r", key, packet.term, value[:60])
                        value = ""  # nothing useful: leave it out rather than print filler
                    ok = bool(value) and self._accept(value, source, f"{key} of '{packet.term}'")
                    if ok:
                        value = self._supported_text(value, source, f"{key} of '{packet.term}'")
                        ok = is_substantive(value)
                    texts.append((GeneratedText(value, AI, packet.source_indices),) if ok else ())
                out[packet.term] = TermNote(explanation=texts[0], significance=texts[1])
        return out

    # ------------------------------------------------------------------ sections
    def explain_sections(
        self, content: ProcessedContent, packets: Sequence[SectionPacket], params: GenerationParams
    ) -> dict[str, tuple[GeneratedText, ...]]:
        source = source_text(content)
        out: dict[str, tuple[GeneratedText, ...]] = {}
        trimmed = []
        for p in packets:
            if len(p.text) > self.max_prompt_chars:
                self._warn(f"Section '{p.heading}' is very long; only its first {self.max_prompt_chars} "
                           "characters were sent for explanation.")
                p = SectionPacket(p.id, p.heading, p.path, p.text[: self.max_prompt_chars], p.source_indices)
            trimmed.append(p)
        for batch in batch_by_size(trimmed, lambda p: len(p.text) + 60, self.max_prompt_chars):
            payload = json.dumps([{"id": p.id, "heading": p.heading, "text": p.text} for p in batch],
                                 ensure_ascii=False)
            data = self._ask(
                "section explanations",
                "For each section write a clear explanation that helps a learner understand it: explain "
                "terms the section uses, how its ideas connect step by step, and what a reader should take "
                "from it. Use only what the section text says: every sentence must be supported by it. Do not add "
                "background knowledge, examples or comparisons (such as 'instead of' or 'unlike') that the text "
                'does not contain. Reply as {"sections": [{"id": "...", "explanation": "..."}]}.',
                payload, params)
            by_id = {p.id: p for p in batch}
            for entry in data.get("sections", []):
                if isinstance(entry, dict) and entry.get("id") in by_id:
                    text = str(entry.get("explanation", "")).strip()
                    what = f"explanation of section '{by_id[entry['id']].heading}'"
                    if is_substantive(text, 12) and self._accept(text, source, what):
                        text = self._supported_text(text, source, what)
                        if is_substantive(text, 12):
                            out[entry["id"]] = (GeneratedText(text, AI, by_id[entry["id"]].source_indices),)
        return out

    # ------------------------------------------------------------------ analogies
    def create_analogies(
        self, content: ProcessedContent, packets: Sequence[TermPacket], params: GenerationParams
    ) -> list[ExampleNote]:
        source = source_text(content)
        out: list[ExampleNote] = []
        discarded = 0
        known = {p.term.casefold(): p.term for p in packets}
        # At most 4 terms per call (2 items each): a CPU-bound model's output budget is small.
        sized = batch_by_size(list(packets), lambda p: len(p.term) + len(p.definition or "") + 80, self.max_prompt_chars)
        batches = [b[i:i + 4] for b in sized for i in range(0, len(b), 4)]
        for batch in batches:
            payload = json.dumps([{"term": p.term, "definition_in_article": p.definition} for p in batch],
                                 ensure_ascii=False)
            data = self._ask(
                "analogies",
                "For EACH term below write two illustrations for a learner. (1) kind \"analogy\": an everyday "
                "comparison in one or two full sentences, written like 'Think of ... as ...' or '... is like ...', "
                "that also says why the comparison fits. (2) kind \"application\": one or two full sentences "
                "describing a concrete real-world situation where the idea is used. These are YOUR illustrations, "
                "not claims made by the article: do not include numbers, statistics, studies or company names, and "
                "do not repeat the sentences of the article. Reply as {\"examples\": [{\"concept\": \"...\", "
                "\"kind\": \"analogy\" or \"application\", \"text\": \"...\"}]}.",
                payload, params)
            for entry in data.get("examples", []):
                if not isinstance(entry, dict):
                    continue
                concept = known.get(str(entry.get("concept", "")).casefold())
                text = str(entry.get("text", "")).strip()
                kind = "application" if str(entry.get("kind", "")).lower() == "application" else "analogy"
                if not concept:
                    continue
                if (not is_substantive(text, 8) or copies_source(text, source)
                        or (kind == "analogy" and not _COMPARISON.search(text))):
                    discarded += 1
                    logger.debug("Discarded weak %s for %r: %r", kind, concept, text[:60])
                    continue
                if self._accept(text, source, f"{kind} for '{concept}'"):
                    # D-7: analogies are outside the support filter by design (they are the model's own
                    # illustrations). A contrast with something the article never mentions is the one claim
                    # shape that was seen going beyond the source, so it is kept but marked for review.
                    cue = next((v.unsupported_comparison for v in assess_support(text, source) if v.unsupported_comparison), "")
                    if cue:
                        self._warn(f"Review needed: the AI-generated {kind} for '{concept}' contrasts the idea with "
                                   f"something the article does not discuss (“{cue}”).")
                    out.append(ExampleNote(concept, kind, text, review_needed=bool(cue)))
        if discarded:
            self._warn(f"{discarded} AI-generated analogy/application item(s) were discarded because they were "
                       "too short, copied the article, or were not a real comparison.")
        return out

    # ------------------------------------------------------------------ questions
    def create_questions(
        self, content: ProcessedContent, spec: QuestionSpec, params: GenerationParams
    ) -> QuestionResult:
        source = source_text(content)
        digest = article_digest(content, self.max_prompt_chars)
        result = QuestionResult()
        validated: dict[str, list[Question]] = {}
        asks = [
            ("short", spec.short, 'short-answer questions answerable in one or two sentences. Reply as '
             '{"questions": [{"question": "...", "answer": "..."}]}.'),
            ("descriptive", spec.descriptive, 'descriptive questions needing a paragraph. Include a model '
             'answer based only on the article. Reply as {"questions": [{"question": "...", "answer": "..."}]}.'),
            ("mcq", spec.mcq, 'multiple-choice questions, each with exactly four options, one correct. Reply as '
             '{"questions": [{"question": "...", "options": ["...", "...", "...", "..."], "correct_index": 0, '
             '"explanation": "..."}]}.'),
        ]
        for kind, count, instruction in asks:
            if count <= 0:
                continue
            try:
                data = self._ask(f"{kind} questions", f"Write exactly {count} {instruction}", digest, params)
            except BackendResponseError as exc:
                result.notes.append(f"The model's {kind} questions could not be used: {exc}")
                continue
            accepted = self._validate_questions(kind, data.get("questions", []), source)
            validated[kind] = [self._with_sources(content, q) for q in accepted]
            if len(accepted) < count:
                result.notes.append(f"Only {len(accepted)} of {count} {kind} questions from the model passed "
                                    "validation.")
        # D-4: diversity control for model-written questions, shared across the three kinds
        wanted = {"short": spec.short, "descriptive": spec.descriptive, "mcq": spec.mcq}
        kept, left_out = select_diverse_questions(validated, wanted)
        for kind in validated:
            getattr(result, kind).extend(kept[kind])
            if left_out[kind]:
                n = left_out[kind]
                result.notes.append(
                    f"{n} {kind} question{'s' if n != 1 else ''} from the model {'were' if n != 1 else 'was'} left out on "
                    "purpose because they repeated a fact that was already asked or relied again on the same passage. "
                    "No question was added only to reach the requested number.")
        return result

    @staticmethod
    def _with_sources(content: ProcessedContent, q: Question) -> Question:
        """Attach the article blocks that support the answer (D-4: AI questions keep their source references)."""
        text = f"{q.answer}. {q.explanation}" if q.kind is QuestionKind.MCQ else q.answer
        refs = locate_sources(content, text)
        return dataclasses.replace(q, source_indices=refs) if refs else q

    def _validate_questions(self, kind: str, raw: Iterable[Any], source: str) -> list[Question]:
        q_kind = {"short": QuestionKind.SHORT, "descriptive": QuestionKind.DESCRIPTIVE, "mcq": QuestionKind.MCQ}[kind]
        out: list[Question] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                question = str(entry.get("question", "")).strip()
                if q_kind is QuestionKind.MCQ:
                    options = tuple(str(o).strip() for o in entry.get("options", []))
                    idx = entry.get("correct_index")
                    if isinstance(idx, bool) or not isinstance(idx, int):
                        continue
                    answer = options[idx] if 0 <= idx < len(options) else ""
                    q = Question(q_kind, question, answer, options, idx, str(entry.get("explanation", "")).strip(),
                                 AI, AI, AI)
                    text = " ".join([question, *options, q.explanation])
                else:
                    answer = str(entry.get("answer", "")).strip()
                    q = Question(q_kind, question, answer, question_provenance=AI, answer_provenance=AI)
                    text = f"{question} {answer}"
            except (TypeError, ValueError):
                continue
            problems = validate_question(q)
            if problems:
                self._warn(f"Rejected an AI-generated {kind} question: {problems[0]}.")
                continue
            if not self._accept(text, source, f"{kind} question"):
                continue
            answer_text = q.explanation if q_kind is QuestionKind.MCQ else q.answer
            verdicts = assess_support(f"{q.answer}. {answer_text}" if q_kind is QuestionKind.MCQ else answer_text, source)
            bad = next((v for v in verdicts if v.status == "unsupported"), None)
            if bad is not None:
                logger.warning("Rejected AI %s question, unsupported answer: %s", kind, bad.sentence[:120])
                self._warn(f"Rejected an AI-generated {kind} question because its answer is not supported by the "
                           f"article (\u201c{q.question[:60]}\u201d).")
                continue
            if any(v.status == "partial" for v in verdicts):
                self._warn(f"Review needed: the answer to an AI-generated {kind} question is only partly supported "
                           f"by the article (\u201c{q.question[:60]}\u201d).")
            out.append(q)
        return out
