"""Deterministic question builder used by the local backend (Phase 5).

Questions are assembled only from what the article itself contains: its definitions,
key statements, tables, lists and code. Answers are verbatim article text. MCQ distractors
are other terms, cells, items or headings from the same article, chosen in a fixed order
(no randomness), so the same article always gives the same questions.

When the article does not contain enough material, fewer questions are returned together
with a note saying so. Nothing is padded with invented content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterator, Optional, Sequence

import regex

from src.article_extractor import BlockKind, ListItem
from src.backends.base import QuestionResult, QuestionSpec
from src.content_processor import (
    Confidence,
    Definition,
    ProcessedContent,
    Section,
    StatementKind,
    is_definitional,
    normalize_key,
)
from src.notes_model import NoteProvenance, Question, QuestionKind

BLANK = "_____"
#: Diversity limits (D-4): at most this many questions may rest on the same article block. The strict value is
#: used first; the relaxed one only tops up when the requested number could not otherwise be reached. A fact
#: (a definition, a sentence, a table cell, a list...) is never asked about twice, whatever the question type.
STRICT_BLOCK_CAP = 2
RELAXED_BLOCK_CAP = 4
_DEF_OR_FUNC = re.compile(r"\b(?:def|function)\s+([A-Za-z_]\w{2,})|\bclass\s+([A-Za-z_]\w{2,})")
_REASON_CUES = re.compile(
    r"\b(because|therefore|which means|so that|as a result|due to|in order to)\b", re.IGNORECASE
)


def term_regex(term: str) -> "regex.Pattern[str]":
    """Case-insensitive, Unicode-aware whole-term pattern that tolerates a plural ending."""
    body = r"\s+".join(regex.escape(p) for p in term.split())
    return regex.compile(r"(?<![\p{L}\p{M}\p{N}])" + body + r"(?:s|es)?(?![\p{L}\p{M}\p{N}])", regex.IGNORECASE)


def mask_terms(text: str, terms: Sequence[str]) -> str:
    out = text
    for term in sorted({t for t in terms if t}, key=len, reverse=True):
        out = term_regex(term).sub(BLANK, out)
    return out


def definition_answer(d: Definition) -> str:
    """The definition text as written in the article (without the 'Term:' prefix for glossary entries)."""
    if d.pattern in ("table_row", "term_colon") and ": " in d.statement:
        return d.statement.split(": ", 1)[1].strip()
    return d.statement


def is_abbreviation(text: str) -> bool:
    return bool(text) and " " not in text and len(text) <= 8 and text.isupper()


def term_shape(term: str) -> str:
    if is_abbreviation(term):
        return "abbr"
    return "multi" if (" " in term or "-" in term) else "single"


def _contains_word(haystack: str, needle: str) -> bool:
    return bool(regex.search(r"(?<![\p{L}\p{M}\p{N}])" + regex.escape(needle) + r"(?![\p{L}\p{M}\p{N}])",
                             haystack, regex.IGNORECASE))


def top_level_items(items: Sequence[ListItem]) -> list[str]:
    """Texts of the list's main (outermost) items only; nested sub-points are not main items."""
    if not items:
        return []
    base = min(i.depth for i in items)
    return [i.text for i in items if i.depth <= base]


def outline_lines(items: Sequence[ListItem], ordered: bool) -> list[str]:
    """The list as indented text lines, keeping parent/child structure: ``1. main`` / ``   - sub-point``."""
    if not items:
        return []
    base = min(i.depth for i in items)
    counters: dict[int, int] = {}
    lines: list[str] = []
    for it in items:
        depth = max(0, it.depth - base)
        for deeper in [d for d in counters if d > depth]:
            del counters[deeper]  # a shallower item ends the deeper run: numbering restarts under the next parent
        numbered = ordered if it.ordered is None else it.ordered
        counters[depth] = counters.get(depth, 0) + 1
        marker = f"{counters[depth]}." if numbered else "-"
        lines.append(f"{'   ' * depth}{marker} {it.text}")
    return lines


@dataclass(frozen=True)
class _MCQSpec:
    stem: str
    correct: str
    pool: tuple[str, ...]
    explanation: str
    source_indices: tuple[int, ...]
    shape_filter: bool = False
    exclude: frozenset[str] = frozenset()  # normalized forms that are also correct (aliases) and must not be options
    fact: str = ""  # identity of the fact being tested (see STRICT_BLOCK_CAP)


class LocalQuestionBuilder:
    """Builds short-answer, descriptive and multiple-choice questions from a ProcessedContent."""

    def __init__(self, content: ProcessedContent, spec: QuestionSpec) -> None:
        self.content = content
        self.spec = spec
        self._asked: set[str] = set()  # question stems already used, across ALL kinds (no repeats)
        self._facts_used: set[str] = set()  # facts already tested, across ALL kinds
        self._block_use: dict[int, int] = {}  # questions per primary source block
        self._fact_of: dict[str, str] = {}  # question stem key -> fact (set by _q)
        self._cap = STRICT_BLOCK_CAP
        self.skipped_repeats = 0  # candidates dropped because they repeated a fact or an over-used block
        self.section_of: dict[int, Section] = {}
        for section in content.iter_sections():
            for item in section.items:
                self.section_of[item.source_index] = section
        self.items = list(content.iter_items())
        self.defs = [d for d in content.definitions if d.confidence is not Confidence.LOW]
        # Only genuine definitions may become answer keys; acronym introductions still give "stands for" pairs.
        self.answer_defs = [d for d in self.defs if is_definitional(d)]
        self.statements = list(content.statements)
        seen: set[str] = set()
        self.terms: list[str] = []
        for term in [k.term for k in content.keywords] + [d.term for d in content.definitions]:
            key = normalize_key(term)
            if key and key not in seen:
                seen.add(key)
                self.terms.append(term)
        # A term and its aliases ("RAG" / "Retrieval-Augmented Generation") are the same concept: if one is
        # the correct answer the others must never appear as distractors.
        self.alias_group: dict[str, set[str]] = {}
        for d in content.definitions:
            members = {normalize_key(d.term)} | {normalize_key(a) for a in d.aliases}
            for m in members:
                self.alias_group.setdefault(m, set()).update(members)
        self.headings = [s.heading for s in content.iter_sections() if s.heading]
        self.sentences: list[tuple[str, int]] = [
            (s.text, it.source_index)
            for it in self.items if it.kind in (BlockKind.PARAGRAPH, BlockKind.QUOTE, BlockKind.LIST)
            for s in it.sentences
        ]

    # ------------------------------------------------------------------ public
    def build(self) -> QuestionResult:
        result = QuestionResult()
        spec = self.spec
        # Pass 1 spreads questions over the article (strict per-block budget); pass 2 tops up only if needed.
        self._cap = STRICT_BLOCK_CAP
        d_short: list = []
        d_desc: list = []
        d_mcq: list = []
        result.short = self._rotate([self._short_definitions(), self._short_acronyms(), self._short_cloze(),
                                     self._short_tables(), self._short_lists(), self._short_code(),
                                     self._short_sections()], spec.short, d_short)
        result.descriptive = self._rotate([self._desc_conclusions(), self._desc_terms(), self._desc_lists(),
                                           self._desc_sections(), self._desc_reasoning(),
                                           self._desc_recommendations()], spec.descriptive, d_desc)
        result.mcq = self._rotate_mcq([self._mcq_definitions(), self._mcq_cloze(), self._mcq_sections(),
                                       self._mcq_tables(), self._mcq_lists(), self._mcq_acronyms()],
                                      spec.mcq, d_mcq)
        self._cap = RELAXED_BLOCK_CAP
        self._refill(result.short, d_short, spec.short, lambda q, n: q, self._meta_q)
        self._refill(result.descriptive, d_desc, spec.descriptive, lambda q, n: q, self._meta_q)
        self._refill(result.mcq, d_mcq, spec.mcq, self._finalize_mcq, self._meta_mcq)
        for label, got, want in (("short-answer", len(result.short), self.spec.short),
                                 ("descriptive", len(result.descriptive), self.spec.descriptive),
                                 ("multiple-choice", len(result.mcq), self.spec.mcq)):
            if got < want:
                result.notes.append(
                    f"Only {got} of {want} {label} questions could be built from the article's own "
                    "content. The local backend does not invent questions to reach the number; an "
                    "Ollama or OpenAI backend can generate more (labelled as AI-generated)."
                    + (" Questions that would repeat a fact already asked, or rely again on the same passage, "
                       "were left out on purpose." if self.skipped_repeats else "")
                )
        return result

    # ------------------------------------------------------------------ diversity
    def _meta_q(self, q: Question) -> tuple[str, Sequence[int], str]:
        return q.question, q.source_indices, self._fact_of.get(normalize_key(q.question), "")

    @staticmethod
    def _meta_mcq(spec: "_MCQSpec") -> tuple[str, Sequence[int], str]:
        return spec.stem, spec.source_indices, spec.fact

    def _verdict(self, indices: Sequence[int], fact: str) -> str:
        """``ok``, ``repeat`` (same fact already asked: never allowed) or ``cap`` (block used enough for now)."""
        if fact and fact in self._facts_used:
            return "repeat"
        primary = indices[0] if indices else -1
        return "cap" if self._block_use.get(primary, 0) >= self._cap else "ok"

    def _commit(self, indices: Sequence[int], fact: str) -> None:
        if fact:
            self._facts_used.add(fact)
        primary = indices[0] if indices else -1
        self._block_use[primary] = self._block_use.get(primary, 0) + 1

    def _collect(self, generators: list, limit: int, make: Callable, meta: Callable, deferred: list) -> list[Question]:
        """Round-robin over the generators; skip repeated facts; park over-used-block candidates for a later top-up."""
        out: list[Question] = []
        seen = self._asked
        active = list(generators)
        while active and len(out) < limit:
            for gen in list(active):
                if len(out) >= limit:
                    break
                item = next(gen, None)
                if item is None:
                    active.remove(gen)
                    continue
                stem, indices, fact = meta(item)
                key = normalize_key(stem)
                if key in seen:
                    continue
                verdict = self._verdict(indices, fact)
                if verdict == "repeat":
                    self.skipped_repeats += 1
                    continue
                if verdict == "cap":
                    deferred.append(item)
                    continue
                q = make(item, len(out))
                if q is not None:
                    seen.add(key)
                    self._commit(indices, fact)
                    out.append(q)
        return out

    def _refill(self, out: list[Question], deferred: list, limit: int, make: Callable, meta: Callable) -> None:
        """Second pass with the relaxed block budget; facts already asked are still never repeated."""
        for item in deferred:
            if len(out) >= limit:
                return
            stem, indices, fact = meta(item)
            key = normalize_key(stem)
            if key in self._asked:
                continue
            verdict = self._verdict(indices, fact)
            if verdict != "ok":
                self.skipped_repeats += 1
                continue
            q = make(item, len(out))
            if q is not None:
                self._asked.add(key)
                self._commit(indices, fact)
                out.append(q)

    def _rotate(self, generators: list[Iterator[Question]], limit: int, deferred: Optional[list] = None) -> list[Question]:
        return self._collect(generators, limit, lambda q, n: q, self._meta_q, [] if deferred is None else deferred)

    def _rotate_mcq(self, generators: list[Iterator[_MCQSpec]], limit: int, deferred: Optional[list] = None) -> list[Question]:
        return self._collect(generators, limit, self._finalize_mcq, self._meta_mcq, [] if deferred is None else deferred)

    # ------------------------------------------------------------------ MCQ assembly
    def _group(self, term: str) -> frozenset[str]:
        key = normalize_key(term)
        return frozenset(self.alias_group.get(key, {key}))

    def _finalize_mcq(self, spec: _MCQSpec, ordinal: int) -> Optional[Question]:
        pool = list(spec.pool)
        if spec.shape_filter:
            shape = term_shape(spec.correct)
            same = [p for p in pool if term_shape(p) == shape]
            if len(self._pick(same, spec.correct, spec.stem, ordinal, spec.exclude)) == 3:
                pool = same
        distractors = self._pick(pool, spec.correct, spec.stem, ordinal, spec.exclude)
        if len(distractors) < 3:
            return None
        position = ordinal % 4  # rotate the correct answer through A-D, deterministically
        options = distractors[:position] + [spec.correct] + distractors[position:]
        return Question(QuestionKind.MCQ, spec.stem, spec.correct, tuple(options), position, spec.explanation,
                        NoteProvenance.DERIVED, NoteProvenance.ORIGINAL, NoteProvenance.DERIVED,
                        spec.source_indices)

    @staticmethod
    def _pick(pool: Sequence[str], correct: str, stem: str, ordinal: int,
              exclude: frozenset[str] = frozenset()) -> list[str]:
        """Choose three distractors: distinct, not overlapping the answer (or its aliases), not named in the stem."""
        n = len(pool)
        chosen: list[str] = []
        cf = correct.casefold()
        for k in range(n):
            cand = pool[(ordinal * 3 + k) % n].strip()
            cc = cand.casefold()
            if not cand or cc == cf or cc in cf or cf in cc or _contains_word(stem, cand):
                continue
            if len(cc) > 3 and cc in stem.casefold():
                continue  # also inflected forms: "answer" must not be offered when the stem says "answers"
            if normalize_key(cand) in exclude:
                continue
            if any(cc in x.casefold() or x.casefold() in cc for x in chosen):
                continue
            chosen.append(cand)
            if len(chosen) == 3:
                break
        return chosen

    # ------------------------------------------------------------------ shared helpers
    def _heading_for(self, source_index: int) -> Optional[str]:
        section = self.section_of.get(source_index)
        return section.heading if section else None

    def _cloze_candidates(self, include_definitions: bool) -> Iterator[tuple[str, str, str, int]]:
        """Yield ``(masked_sentence, answer_term, original_sentence, source_index)``."""
        ranked = sorted(self.content.keywords, key=lambda k: -k.score)
        for s in self.statements:
            if not include_definitions and s.kind is StatementKind.DEFINITION:
                continue
            if not 6 <= len(s.text.split()) <= 45:
                continue
            for kw in ranked:
                if len(kw.term) < 3 or not term_regex(kw.term).search(s.text):
                    continue
                aliases = [a for d in self.content.definitions if normalize_key(d.term) == kw.key for a in d.aliases]
                masked = mask_terms(s.text, [kw.term, *aliases])
                if masked.count(BLANK) >= 1 and len(masked.replace(BLANK, "").split()) >= 5:
                    yield masked, kw.term, s.text, s.source_index
                    break  # one question per sentence

    def _tables(self) -> Iterator[tuple[int, tuple[tuple[str, ...], ...]]]:
        for it in self.items:
            if it.kind is BlockKind.TABLE and it.block.has_header and len(it.block.rows) >= 3 \
                    and len(it.block.rows[0]) >= 2:
                yield it.source_index, it.block.rows

    def _lists(self, min_items: int = 3, max_items: int = 10) -> Iterator[tuple[int, str, bool, list[str]]]:
        """Lists under a heading, as ``(index, heading, ordered, main_items)``.

        Only the outermost items count as the list's items; sub-points are shown under their parent in answers
        (see :meth:`_outline`) but are never counted, listed or offered as separate main items.
        """
        for it in self.items:
            if it.kind is not BlockKind.LIST:
                continue
            main = top_level_items(it.block.items)
            if min_items <= len(main) <= max_items:
                heading = self._heading_for(it.source_index)
                if heading:
                    yield it.source_index, heading, it.block.ordered, main

    def _outline(self, source_index: int) -> tuple[str, bool]:
        """``(text, has_sub_points)`` for the list block at ``source_index``, nested structure preserved."""
        block = next(it.block for it in self.items if it.source_index == source_index)
        base = min(i.depth for i in block.items)
        return "\n".join(outline_lines(block.items, block.ordered)), any(i.depth > base for i in block.items)

    def _sentences_with(self, term: str, exclude: str = "") -> list[tuple[str, int]]:
        rx = term_regex(term)
        return [(s, i) for s, i in self.sentences if s != exclude and rx.search(s)]

    def _q(self, kind: QuestionKind, question: str, answer: str, source: Sequence[int], fact: str = "") -> Question:
        if fact:
            self._fact_of[normalize_key(question)] = fact
        return Question(kind, question, answer, source_indices=tuple(dict.fromkeys(source)))

    @staticmethod
    def _sent_fact(index: int, sentence: str) -> str:
        return f"sent:{index}:{normalize_key(sentence)[:100]}"

    # ------------------------------------------------------------------ short answer
    def _short_definitions(self) -> Iterator[Question]:
        for d in self.answer_defs:
            answer = definition_answer(d)
            if len(answer.split()) >= 2:
                yield self._q(QuestionKind.SHORT, f"According to the article, what is {d.term}?", answer,
                              [d.source_index], fact=f"def:{normalize_key(d.term)}")

    def _acronym_pairs(self) -> Iterator[tuple[str, str, Definition]]:
        for d in self.defs:
            for alias in d.aliases:
                if is_abbreviation(d.term) and not is_abbreviation(alias):
                    yield d.term, alias, d
                elif is_abbreviation(alias) and not is_abbreviation(d.term):
                    yield alias, d.term, d

    def _short_acronyms(self) -> Iterator[Question]:
        for abbr, long, d in self._acronym_pairs():
            yield self._q(QuestionKind.SHORT, f"What does {abbr} stand for in the article?", long, [d.source_index],
                          fact=f"acr:{normalize_key(abbr)}")

    def _short_cloze(self) -> Iterator[Question]:
        for masked, term, original, index in self._cloze_candidates(include_definitions=False):
            yield self._q(QuestionKind.SHORT, f"Fill in the blank using the article's wording: “{masked}”",
                          term, [index], fact=self._sent_fact(index, original))

    def _short_tables(self) -> Iterator[Question]:
        for index, rows in self._tables():
            heading = self._heading_for(index) or "the article"
            header = rows[0]
            made = 0
            for row in rows[1:]:
                for col in range(1, min(len(row), len(header))):
                    if row[0] and row[col] and header[col] and len(row[col]) <= 100 and made < 2:
                        made += 1
                        yield self._q(
                            QuestionKind.SHORT,
                            f"In the table under “{heading}”, what is listed under “{header[col]}” "
                            f"for “{row[0]}”?", row[col], [index], fact=f"cell:{index}:{row[0]}:{header[col]}")
                        break

    def _short_lists(self) -> Iterator[Question]:
        for index, heading, ordered, items in self._lists():
            answer, nested = self._outline(index)
            extra = ", including their sub-points" if nested else ""
            if ordered:
                yield self._q(QuestionKind.SHORT, f"List, in order, the {len(items)} steps given under “{heading}”{extra}.",
                              answer, [index], fact=f"list:{index}")
            else:
                yield self._q(QuestionKind.SHORT, f"List the {len(items)} items given under “{heading}”{extra}.",
                              answer, [index], fact=f"list:{index}")

    def _short_code(self) -> Iterator[Question]:
        for it in self.items:
            if it.kind is BlockKind.CODE:
                m = _DEF_OR_FUNC.search(it.block.text)
                if m:
                    name = m.group(1) or m.group(2)
                    what = "class" if m.group(2) else "function"
                    lang = f"{it.block.language} " if it.block.language else ""
                    yield self._q(QuestionKind.SHORT,
                                  f"In the article's {lang}code example, what is the name of the first {what} defined?",
                                  name, [it.source_index], fact=f"code:{it.source_index}")

    def _short_sections(self) -> Iterator[Question]:
        for section in self.content.iter_sections():
            first = next((i for i in section.items if i.kind is BlockKind.PARAGRAPH and i.sentences), None)
            if section.heading and first:
                yield self._q(QuestionKind.SHORT,
                              f"According to the article, how does the section “{section.heading}” begin?",
                              first.sentences[0].text, [first.source_index],
                              fact=self._sent_fact(first.source_index, first.sentences[0].text))

    # ------------------------------------------------------------------ descriptive
    def _desc_conclusions(self) -> Iterator[Question]:
        picks = [s for s in self.statements if s.kind is StatementKind.CONCLUSION][:3]
        if picks:
            yield self._q(QuestionKind.DESCRIPTIVE, "What conclusions or main lessons does the article draw? "
                          "Answer in your own words, using the article's points.",
                          "\n".join(s.text for s in picks), [s.source_index for s in picks], fact="conclusions")

    def _desc_terms(self) -> Iterator[Question]:
        made = 0
        for d in self.answer_defs:
            parts = [d.statement if d.pattern not in ("table_row", "term_colon") else definition_answer(d)]
            extra = self._sentences_with(d.term, exclude=d.statement)[:2]
            if extra and made < 2:
                made += 1
                yield self._q(QuestionKind.DESCRIPTIVE,
                              f"Explain the term “{d.term}” and how it is used in the article.",
                              "\n".join(parts + [s for s, _ in extra]), [d.source_index, *[i for _, i in extra]],
                              fact=f"def:{normalize_key(d.term)}")

    def _desc_lists(self) -> Iterator[Question]:
        for index, heading, ordered, items in self._lists():
            if ordered:
                yield self._q(QuestionKind.DESCRIPTIVE, f"Describe the process set out under “{heading}”, step by step.",
                              self._outline(index)[0], [index], fact=f"list:{index}")

    def _desc_sections(self) -> Iterator[Question]:
        ranked = sorted((s for s in self.content.iter_sections() if s.heading and s.items),
                        key=lambda s: -s.word_count)
        for section in ranked:
            in_section = [s for s in self.statements if self.section_of.get(s.source_index) is section][:3]
            sentences = [(s.text, s.source_index) for s in in_section]
            if len(sentences) < 2:
                first = next((i for i in section.items if i.kind is BlockKind.PARAGRAPH), None)
                if first:
                    sentences = [(s.text, first.source_index) for s in first.sentences[:3]]
            if len(sentences) >= 2:
                yield self._q(QuestionKind.DESCRIPTIVE, f"Summarise what the article says in the section “{section.heading}”.",
                              "\n".join(t for t, _ in sentences), [i for _, i in sentences],
                              fact=f"section:{normalize_key(section.heading)}")

    def _desc_reasoning(self) -> Iterator[Question]:
        for s in self.statements:
            m = _REASON_CUES.search(s.text)
            if m and len(s.text[: m.start()].split()) >= 4:
                stem = s.text[: m.start()].rstrip(" ,;")
                yield self._q(QuestionKind.DESCRIPTIVE,
                              f"The article states: “{stem} …”. Explain the reasoning it gives.", s.text,
                              [s.source_index], fact=self._sent_fact(s.source_index, s.text))

    def _desc_recommendations(self) -> Iterator[Question]:
        picks = [s for s in self.statements if s.kind is StatementKind.RECOMMENDATION][:3]
        if picks:
            yield self._q(QuestionKind.DESCRIPTIVE, "What practical recommendations does the article make?",
                          "\n".join(s.text for s in picks), [s.source_index for s in picks], fact="recommendations")

    # ------------------------------------------------------------------ MCQ specs
    def _mcq_definitions(self) -> Iterator[_MCQSpec]:
        for d in self.answer_defs:
            aliases = list(d.aliases)
            text = definition_answer(d)
            masked = mask_terms(text, [d.term, *aliases])
            if len(masked.replace(BLANK, "").split()) < 3:
                continue
            yield _MCQSpec(f"Which term does the article define as follows? “{masked}”", d.term,
                           tuple(self.terms), f"The article describes this as: “{text}”",
                           (d.source_index,), shape_filter=True, exclude=self._group(d.term),
                           fact=f"def:{normalize_key(d.term)}")

    def _mcq_cloze(self) -> Iterator[_MCQSpec]:
        for masked, term, original, index in self._cloze_candidates(include_definitions=False):
            yield _MCQSpec(f"Which term best completes this statement from the article? “{masked}”", term,
                           tuple(self.terms), f"The article states: “{original}”", (index,), shape_filter=True,
                           exclude=self._group(term), fact=self._sent_fact(index, original))

    def _mcq_sections(self) -> Iterator[_MCQSpec]:
        if len(set(self.headings)) < 4:
            return
        for s in self.statements:
            heading = self._heading_for(s.source_index)
            if heading and len(s.text.split()) <= 40 and not _contains_word(s.text, heading):
                yield _MCQSpec(f"In which section of the article does this statement appear? “{s.text}”",
                               heading, tuple(self.headings),
                               f"The article says this in the section “{heading}”: “{s.text}”",
                               (s.source_index,), fact=self._sent_fact(s.source_index, s.text))

    def _mcq_tables(self) -> Iterator[_MCQSpec]:
        for index, rows in self._tables():
            header = rows[0]
            heading = self._heading_for(index) or "the article"
            for col in range(1, len(header)):
                for row in rows[1:]:
                    if len(row) <= col or not row[0] or not row[col] or len(row[col]) > 100:
                        continue
                    same_col = [r[col] for r in rows[1:] if len(r) > col and r[col] != row[col]]
                    others = [c for r in rows[1:] for c in r if c and c != row[col]]
                    yield _MCQSpec(
                        f"In the table under “{heading}”, what is listed under “{header[col]}” for "
                        f"“{row[0]}”?", row[col], tuple(dict.fromkeys(same_col + others)),
                        f"In the table, the row “{row[0]}” has “{row[col]}” under “{header[col]}”.",
                        (index,), fact=f"cell:{index}:{row[0]}:{header[col]}")

    def _mcq_lists(self) -> Iterator[_MCQSpec]:
        lists = list(self._lists(min_items=3, max_items=12))
        for n, (index, heading, _, items) in enumerate(lists):
            other = [t for j, (_, _, _, its) in enumerate(lists) if j != n for t in its if len(t) <= 100]
            correct = next((t for t in items if 2 <= len(t.split()) and len(t) <= 100), None)
            if correct and other:
                yield _MCQSpec(f"Which of the following is one of the items listed under “{heading}”?",
                               correct, tuple(other),
                               f"“{correct}” is one of the items listed under “{heading}”.", (index,),
                               fact=f"list:{index}")

    def _mcq_acronyms(self) -> Iterator[_MCQSpec]:
        multi = tuple(t for t in self.terms if term_shape(t) == "multi")
        for abbr, long, d in self._acronym_pairs():
            yield _MCQSpec(f"What does {abbr} stand for in the article?", long, multi,
                           f"The article gives “{long}” as the meaning of {abbr}: “{d.statement}”",
                           (d.source_index,), exclude=self._group(long), fact=f"acr:{normalize_key(abbr)}")
