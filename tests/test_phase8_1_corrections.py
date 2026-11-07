"""Phase 8.1 regression tests for the four educational-quality defects found in Phase 8.

D-3  nested list items must not be flattened or promoted to main items
D-4  generated questions must be spread over the article and never test the same fact twice
D-5  only genuine definitions may be presented as definitions or used as answer keys
D-7  AI explanations may not carry claims the article does not support

No network, no real model: language models are scripted test doubles.
"""
from __future__ import annotations

import json
import re
import socket
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup
from docx import Document
from docx.oxml.ns import qn

from src.article_extractor import Block, BlockKind, ListItem, SubstackExtractor, _html_list_items
from src.backends import LocalNotesBackend
from src.backends.base import GenerationParams, QuestionSpec, SectionPacket, TermPacket
from src.backends.llm import SYSTEM_RULES, assess_support, check_grounding, source_text
from src.backends.local_questions import LocalQuestionBuilder, outline_lines, top_level_items
from src.content_processor import (
    Confidence, _colon_definition, _definitions_from_sentence, extract_definitions, is_definitional,
)
from src.notes_generator import NotesConfig, NotesGenerator, validate_notes, verify_provenance
from src.notes_model import ItemKind, NoteProvenance
from src.word_generator import WordGenerator
from tests.helpers import C, H, L, P, T, ScriptedLLM, content_of, fixture_content

PARAMS = GenerationParams()
SPEC = QuestionSpec(short=10, descriptive=5, mcq=10)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*a: Any, **k: Any) -> None:
        raise AssertionError("a network connection was attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)


# ============================================================================ helpers
def LI(*entries: tuple, ordered: bool = False) -> Block:
    """A list block from ``(text, depth)`` or ``(text, depth, own_ordered)`` tuples."""
    items = tuple(ListItem(e[0], e[1], e[2] if len(e) > 2 else None) for e in entries)
    return Block(BlockKind.LIST, items=items, ordered=ordered)


SETUP_LIST = LI(("Split the documents into chunks", 0), ("Embed every chunk", 0), ("Store the vectors in an index", 0), ordered=True)

BENEFITS = LI(
    ("Fresh knowledge", 0),
    ("The document store can be updated without retraining the model.", 1),
    ("New documents become searchable once they are indexed.", 1),
    ("Traceable answers", 0),
    ("Answers can cite the passages they were based on.", 1),
    ("Data control", 0),
    ("Private data stays inside your own infrastructure.", 1),
)


def article_with_list(block: Block, heading: str = "Three main benefits"):
    return content_of(
        P("Retrieval systems let a language model answer from documents that it was never trained on."),
        H(heading), P("The sections below describe the points that matter for teams adopting the approach."),
        block,
        P("After the list, the article returns to ordinary prose and explains how teams measure the effect in practice."),
        H("Setup steps"), SETUP_LIST,
    )


def build_questions(content, spec: QuestionSpec = SPEC):
    return LocalQuestionBuilder(content, spec).build()


def all_questions(result):
    return [*result.short, *result.descriptive, *result.mcq]


# ============================================================================ D-3: nested lists
class TestNestedListExtraction:
    def test_html_nesting_depth_is_kept(self) -> None:
        soup = BeautifulSoup("<ul><li>A<ul><li>A1<ul><li>A1a</li></ul></li><li>A2</li></ul></li><li>B</li></ul>", "html.parser")
        items = _html_list_items(soup.find("ul"))
        assert [(i.text, i.depth) for i in items] == [("A", 0), ("A1", 1), ("A1a", 2), ("A2", 1), ("B", 0)]

    def test_plain_lists_carry_no_extra_markers(self) -> None:
        items = _html_list_items(BeautifulSoup("<ol><li>a<ol><li>b</li></ol></li><li>c</li></ol>", "html.parser").find("ol"))
        assert all(i.ordered is None for i in items)  # same kind throughout: nothing to record

    def test_a_bullet_sublist_inside_a_numbered_list_is_marked(self) -> None:
        items = _html_list_items(BeautifulSoup("<ol><li>Step<ul><li>detail</li></ul></li></ol>", "html.parser").find("ol"))
        assert [(i.text, i.ordered) for i in items] == [("Step", None), ("detail", False)]

    def test_a_numbered_sublist_inside_a_bullet_list_is_marked(self) -> None:
        items = _html_list_items(BeautifulSoup("<ul><li>Topic<ol><li>first</li></ol></li></ul>", "html.parser").find("ul"))
        assert [(i.text, i.ordered) for i in items] == [("Topic", None), ("first", True)]

    def test_the_real_extractor_path_keeps_nesting_and_mixed_kinds_end_to_end(self) -> None:
        html = ("<html><head><title>Lists</title></head><body><article><h1>Lists in depth</h1>"
                "<p>This article explains several kinds of lists that teams use when they plan their work every week in detail.</p>"
                "<h2>Workflow</h2><p>A typical workflow combines numbered stages with bulleted details and a nested list.</p>"
                "<ol><li>Prepare the data<ul><li>Remove duplicates</li><li>Fix text encodings</li></ul></li>"
                "<li>Train the model<ol><li>Choose a metric<ul><li>Accuracy on a held-out set</li></ul></li></ol></li>"
                "<li>Evaluate the model</li></ol>"
                "<h2>Options</h2><p>Several retrieval options exist and each has numbered variants worth knowing about.</p>"
                "<ul><li>Retrieval options<ol><li>Keyword search</li><li>Vector search</li></ol></li><li>Reranking</li>"
                "<li>Evaluation</li></ul>"
                "<p>The closing paragraph restates the main idea of the article in a few plain words for the reader.</p>"
                "</article></body></html>")
        article = SubstackExtractor().extract_from_html(html)
        lists = [b for b in article.blocks if b.kind is BlockKind.LIST]
        assert article.extractor == "trafilatura" and len(lists) == 2
        assert [(i.text, i.depth, i.ordered) for i in lists[0].items] == [
            ("Prepare the data", 0, None), ("Remove duplicates", 1, False), ("Fix text encodings", 1, False),
            ("Train the model", 0, None), ("Choose a metric", 1, None), ("Accuracy on a held-out set", 2, False),
            ("Evaluate the model", 0, None)]
        assert lists[0].ordered and not lists[1].ordered
        assert [(i.text, i.depth, i.ordered) for i in lists[1].items][:3] == [
            ("Retrieval options", 0, None), ("Keyword search", 1, True), ("Vector search", 1, True)]

    def test_markdown_text_lists_keep_depth_and_marker_kind(self) -> None:
        article = SubstackExtractor().extract_from_text(
            "# Title\n\n## Steps\n\n1. First step\n   - detail one\n   - detail two\n2. Second step\n\nClosing sentence here.\n")
        block = next(b for b in article.blocks if b.kind is BlockKind.LIST)
        assert block.ordered
        assert [(i.text, i.depth, i.ordered) for i in block.items] == [
            ("First step", 0, None), ("detail one", 1, False), ("detail two", 1, False), ("Second step", 0, None)]


class TestTopLevelCounting:
    def test_single_level(self) -> None:
        items = (ListItem("a"), ListItem("b"), ListItem("c"))
        assert top_level_items(items) == ["a", "b", "c"]

    def test_two_levels(self) -> None:
        assert top_level_items(BENEFITS.items) == ["Fresh knowledge", "Traceable answers", "Data control"]

    def test_three_levels(self) -> None:
        items = (ListItem("a", 0), ListItem("b", 1), ListItem("c", 2), ListItem("d", 0))
        assert top_level_items(items) == ["a", "d"]

    def test_a_list_that_starts_nested_uses_its_shallowest_level_as_main(self) -> None:
        items = (ListItem("x", 1), ListItem("y", 1), ListItem("z", 2))
        assert top_level_items(items) == ["x", "y"]

    def test_empty(self) -> None:
        assert top_level_items(()) == [] and outline_lines((), False) == []


class TestOutline:
    def test_unordered_two_levels(self) -> None:
        assert outline_lines(BENEFITS.items, False)[:3] == [
            "- Fresh knowledge",
            "   - The document store can be updated without retraining the model.",
            "   - New documents become searchable once they are indexed."]

    def test_unordered_three_levels_indent_each_level(self) -> None:
        items = (ListItem("a", 0), ListItem("b", 1), ListItem("c", 2))
        assert outline_lines(items, False) == ["- a", "   - b", "      - c"]

    def test_ordered_nested_numbering_restarts_under_each_parent(self) -> None:
        items = (ListItem("A", 0), ListItem("a1", 1), ListItem("a2", 1), ListItem("B", 0), ListItem("b1", 1))
        assert outline_lines(items, True) == ["1. A", "   1. a1", "   2. a2", "2. B", "   1. b1"]

    def test_mixed_numbered_parent_with_bullet_children(self) -> None:
        items = (ListItem("A", 0), ListItem("a1", 1, False), ListItem("B", 0))
        assert outline_lines(items, True) == ["1. A", "   - a1", "2. B"]

    def test_mixed_bullet_parent_with_numbered_children(self) -> None:
        items = (ListItem("A", 0), ListItem("a1", 1, True), ListItem("a2", 1, True))
        assert outline_lines(items, False) == ["- A", "   1. a1", "   2. a2"]

    def test_technical_terms_survive_unchanged(self) -> None:
        items = (ListItem("Embedding model (e.g. text-embedding-3-small)", 0), ListItem("Cosine similarity: dot(a, b) / (|a||b|)", 1))
        assert outline_lines(items, False) == ["- Embedding model (e.g. text-embedding-3-small)",
                                               "   - Cosine similarity: dot(a, b) / (|a||b|)"]


class TestQuestionsDoNotConfuseMainItemsWithSubItems:
    """Three main benefits with four supporting details (the Phase 8 defect)."""

    @pytest.fixture
    def result(self):
        return build_questions(article_with_list(BENEFITS))

    def test_the_question_counts_three_main_items_not_seven_or_four(self, result) -> None:
        q = next(q for q in result.short if q.question.startswith("List the"))
        assert q.question.startswith("List the 3 items given under “Three main benefits”")
        assert "including their sub-points" in q.question
        assert not any("List the 4 items" in q.question or "List the 7 items" in q.question for q in all_questions(result))

    def test_the_answer_shows_each_sub_point_under_its_parent(self, result) -> None:
        q = next(q for q in result.short if q.question.startswith("List the"))
        lines = q.answer.splitlines()
        assert len(lines) == 7
        assert [ln for ln in lines if not ln.startswith(" ")] == ["- Fresh knowledge", "- Traceable answers", "- Data control"]
        assert sum(ln.startswith("   - ") for ln in lines) == 4

    def test_a_sub_item_is_never_offered_as_a_main_item_in_a_choice_question(self, result) -> None:
        subs = {i.text for i in BENEFITS.items if i.depth > 0}
        for q in result.mcq:
            if "listed under" in q.question and "Three main benefits" in q.question:
                assert q.answer in {"Fresh knowledge", "Traceable answers", "Data control"}
            assert not ({q.answer, *q.options} & subs) or "listed under" not in q.question

    def test_source_reference_and_provenance_are_kept(self, result) -> None:
        q = next(q for q in result.short if q.question.startswith("List the"))
        assert len(q.source_indices) == 1 and q.answer_provenance is NoteProvenance.ORIGINAL

    def test_a_flat_list_is_unchanged(self) -> None:
        flat = LI(("Alpha item", 0), ("Beta item", 0), ("Gamma item", 0))
        result = build_questions(article_with_list(flat, "Flat items"))
        q = next(q for q in result.short if "Flat items" in q.question)
        assert q.question == "List the 3 items given under “Flat items”." and q.answer == "- Alpha item\n- Beta item\n- Gamma item"

    def test_an_ordered_list_with_nested_steps_asks_for_the_main_steps(self) -> None:
        steps = LI(("Prepare the data", 0), ("Clean it", 1), ("Split it", 1), ("Train the model", 0), ("Evaluate the model", 0), ordered=True)
        result = build_questions(article_with_list(steps, "How to proceed"))
        q = next(q for q in result.short if "How to proceed" in q.question)
        assert q.question.startswith("List, in order, the 3 steps given under “How to proceed”")
        assert q.answer.splitlines() == ["1. Prepare the data", "   1. Clean it", "   2. Split it", "2. Train the model", "3. Evaluate the model"]

    def test_a_list_with_only_two_main_items_is_not_padded_by_its_sub_items(self) -> None:
        two = LI(("First point", 0), ("detail a", 1), ("detail b", 1), ("detail c", 1), ("Second point", 0))
        result = build_questions(article_with_list(two, "Two points"))
        assert not any("Two points" in q.question and q.question.startswith("List") for q in all_questions(result))


class TestNestingSurvivesIntoNotesAndWord:
    @pytest.fixture
    def notes(self):
        return NotesGenerator(LocalNotesBackend(), NotesConfig()).generate(article_with_list(BENEFITS))

    def test_the_notes_item_keeps_depths_and_order(self, notes) -> None:
        item = next(i for i in notes.all_items() if i.kind is ItemKind.BULLETS and "Fresh knowledge" in i.items)
        assert item.items[0] == "Fresh knowledge" and item.depths == (0, 1, 1, 0, 1, 0, 1)
        assert item.provenance is NoteProvenance.ORIGINAL and item.source_indices

    def test_the_json_form_keeps_the_nesting_too(self, notes) -> None:
        d = json.loads(json.dumps(notes.to_dict()))
        found = []

        def walk(o: Any) -> None:
            if isinstance(o, dict):
                if "Fresh knowledge" in o.get("items", []):
                    found.append(o)
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(d)
        assert found and found[0]["depths"] == [0, 1, 1, 0, 1, 0, 1]

    def test_flat_lists_add_no_depth_noise_to_the_json(self) -> None:
        notes = NotesGenerator(LocalNotesBackend(), NotesConfig()).generate(article_with_list(LI(("Alpha item", 0), ("Beta item", 0), ("Gamma item", 0))))
        assert '"depths"' not in json.dumps(notes.to_dict())

    def test_word_indents_sub_points_and_validates(self, notes, tmp_path: Path) -> None:
        doc = Document(str(WordGenerator().save(notes, tmp_path).path))
        by_text = {p.text.strip(): p for p in doc.paragraphs if p.text.strip()}
        levels = {t: by_text[t]._p.find(qn("w:pPr") + "/" + qn("w:numPr") + "/" + qn("w:ilvl")).get(qn("w:val"))
                  for t in ("Fresh knowledge", "New documents become searchable once they are indexed.", "Data control")}
        assert levels == {"Fresh knowledge": "0", "New documents become searchable once they are indexed.": "1", "Data control": "0"}

    def test_three_levels_reach_word_as_three_levels(self, tmp_path: Path) -> None:
        deep = LI(("Main", 0), ("Middle", 1), ("Deepest", 2), ("Another main", 0), ("And one more", 0))
        notes = NotesGenerator(LocalNotesBackend(), NotesConfig()).generate(article_with_list(deep))
        doc = Document(str(WordGenerator().save(notes, tmp_path).path))
        p = next(p for p in doc.paragraphs if p.text.strip() == "Deepest")
        assert p._p.find(qn("w:pPr") + "/" + qn("w:numPr") + "/" + qn("w:ilvl")).get(qn("w:val")) == "2"


def _list_formats(doc: Any, texts: list[str]) -> dict[str, str]:
    """The numbering format (decimal, bullet, lowerLetter...) Word will use for each named list paragraph."""
    numbering = doc.part.numbering_part.element
    abstract = {a.get(qn("w:abstractNumId")): a for a in numbering.findall(qn("w:abstractNum"))}
    num_to_abs = {n.get(qn("w:numId")): n.find(qn("w:abstractNumId")).get(qn("w:val")) for n in numbering.findall(qn("w:num"))}
    out = {}
    for p in doc.paragraphs:
        t = p.text.strip()
        if t in texts:
            num_pr = p._p.find(qn("w:pPr") + "/" + qn("w:numPr"))
            level = num_pr.find(qn("w:ilvl")).get(qn("w:val"))
            abs_el = abstract[num_to_abs[num_pr.find(qn("w:numId")).get(qn("w:val"))]]
            lvl = next(lv for lv in abs_el.findall(qn("w:lvl")) if lv.get(qn("w:ilvl")) == level)
            out[t] = lvl.find(qn("w:numFmt")).get(qn("w:val"))
    return out


class TestMixedListSemanticsInWord:
    def test_numbered_parent_with_bullet_children(self, tmp_path: Path) -> None:
        mixed = LI(("Prepare the data", 0), ("Remove duplicates", 1, False), ("Fix encodings", 1, False), ("Train the model", 0),
                   ("Evaluate the model", 0), ordered=True)
        notes = NotesGenerator(LocalNotesBackend(), NotesConfig()).generate(article_with_list(mixed, "Workflow"))
        item = next(i for i in notes.all_items() if "Prepare the data" in i.items)
        assert item.kind is ItemKind.NUMBERED and item.ordered == (True, False, False, True, True)
        formats = _list_formats(Document(str(WordGenerator().save(notes, tmp_path).path)),
                                ["Prepare the data", "Remove duplicates", "Train the model"])
        assert formats["Prepare the data"] == "decimal" and formats["Train the model"] == "decimal"
        assert formats["Remove duplicates"] == "bullet"

    def test_bullet_parent_with_numbered_children(self, tmp_path: Path) -> None:
        mixed = LI(("Retrieval options", 0), ("Keyword search", 1, True), ("Vector search", 1, True), ("Reranking", 0), ("Evaluation", 0))
        notes = NotesGenerator(LocalNotesBackend(), NotesConfig()).generate(article_with_list(mixed, "Options"))
        formats = _list_formats(Document(str(WordGenerator().save(notes, tmp_path).path)),
                                ["Retrieval options", "Keyword search", "Reranking"])
        assert formats["Retrieval options"] == "bullet" and formats["Reranking"] == "bullet"
        assert formats["Keyword search"] != "bullet"

    def test_uniform_lists_do_not_get_per_item_flags(self) -> None:
        notes = NotesGenerator(LocalNotesBackend(), NotesConfig()).generate(article_with_list(BENEFITS))
        assert all(not i.ordered for i in notes.all_items())


# ============================================================================ D-4: question diversity
class TestQuestionDiversity:
    @pytest.mark.parametrize("fixture", ["substack_free.html", "substack_custom_domain.html", "substack_paywalled.html"])
    def test_no_passage_dominates_the_question_set(self, fixture: str) -> None:
        result = build_questions(fixture_content(fixture))
        per_block = Counter(q.source_indices[0] for q in all_questions(result))
        assert max(per_block.values(), default=0) <= 4  # relaxed cap; strict cap is 2

    def test_every_question_keeps_a_source_reference(self) -> None:
        result = build_questions(fixture_content("substack_free.html"))
        assert all(q.source_indices and all(i >= 0 for i in q.source_indices) for q in all_questions(result))

    def test_the_same_fact_is_not_asked_in_different_forms(self) -> None:
        result = build_questions(fixture_content("substack_free.html"))
        text = [q.question for q in all_questions(result)]
        sentence = "A basic _____ system has four steps"
        assert sum(sentence in t or "A basic RAG system has four steps" in t for t in text) <= 1
        # one question per defined term, whatever its form
        assert sum("Retrieval-Augmented Generation" in q.question and ("what is" in q.question or "define as follows" in q.question)
                   for q in all_questions(result)) <= 1

    def test_question_kinds_cover_several_aspects_of_the_article(self) -> None:
        result = build_questions(fixture_content("substack_free.html"))
        families = set()
        for q in all_questions(result):
            for tag, marker in (("definition", "what is"), ("abbreviation", "stand for"), ("fill-in", "Fill in the blank"),
                                ("table", "In the table"), ("list", "List"), ("code", "code example"), ("section", "section"),
                                ("conclusions", "conclusions"), ("process", "process"), ("cloze-mcq", "best completes")):
                if marker in q.question:
                    families.add(tag)
        assert len(families) >= 6

    def test_a_shortfall_is_reported_honestly_and_nothing_is_invented(self) -> None:
        content = fixture_content("substack_free.html")
        result = build_questions(content)
        assert len(result.mcq) < SPEC.mcq
        note = next(n for n in result.notes if "multiple-choice" in n)
        assert f"Only {len(result.mcq)} of {SPEC.mcq}" in note and "left out on purpose" in note
        notes = NotesGenerator(LocalNotesBackend(), NotesConfig()).generate(content)
        assert verify_provenance(notes, content) == [] and validate_notes(notes, SPEC) == []

    def test_many_sections_spread_questions_across_many_blocks(self) -> None:
        blocks = []
        for i in range(30):
            blocks += [H(f"Part {i}"), P(f"Concept{i} refers to the planning step number {i} that a team completes before launch week {i}.")]
        result = build_questions(content_of(*blocks), QuestionSpec(short=10, descriptive=5, mcq=10))
        qs = all_questions(result)
        per_block = Counter(q.source_indices[0] for q in qs)
        assert len(qs) >= 20 and len(per_block) >= 10 and max(per_block.values()) <= 2  # 20 questions, 2 per block

    def test_few_sections_give_fewer_questions_not_repeats(self) -> None:
        content = content_of(H("Only part"), P("Gradient descent refers to an algorithm that repeatedly reduces a loss by following slopes."))
        result = build_questions(content)
        qs = all_questions(result)
        assert 1 <= len(qs) <= 4 and len({q.question for q in qs}) == len(qs)
        assert any("Only" in n for n in result.notes)

    def test_limited_factual_content_gives_no_filler(self) -> None:
        content = content_of(P("Thanks for stopping by today and enjoy the rest of your week with friends and family."))
        result = build_questions(content)
        assert all_questions(result) == [] and len(result.notes) == 3

    def test_short_article_with_a_single_block_respects_the_block_budget(self) -> None:
        content = content_of(P("Latency refers to the delay before a response arrives. Throughput refers to the number of requests "
                               "handled per second. Jitter refers to the variation of the delay between responses. Packet loss refers "
                               "to data that never arrives at all."))
        qs = all_questions(build_questions(content))
        assert len(qs) <= 4  # all four facts live in one block: at most the relaxed per-block budget

    def test_a_repeated_concept_is_asked_once(self) -> None:
        blocks = [H("A"), P("Chunking refers to splitting long documents into smaller passages for retrieval."),
                  H("B"), P("Many teams revisit chunking because chunking choices change retrieval results in practice."),
                  H("C"), P("Chunking matters again when documents change and chunking has to be repeated by the team.")]
        qs = all_questions(build_questions(content_of(*blocks)))
        about_term = [q for q in qs if re.search(r"what is Chunking|define as follows|Explain the term .Chunking", q.question)]
        assert len(about_term) <= 1

    def test_long_article_reaches_the_requested_number_without_repeats(self) -> None:
        blocks = []
        for i in range(60):
            blocks += [H(f"Chapter {i}"), P(f"Topic{i} refers to the practice number {i} that engineers review during quarter {i}."),
                       P(f"Teams record the outcome of practice {i} because the record explains why quarter {i} went the way it did.")]
        result = build_questions(content_of(*blocks))
        assert len(result.short) == 10 and len(all_questions(result)) >= 15  # (these paragraphs offer no descriptive material)
        stems = [q.question for q in all_questions(result)]
        assert len(stems) == len(set(stems))

    def test_strict_budget_first_then_relaxed_only_to_fill(self) -> None:
        result = build_questions(fixture_content("substack_free.html"))
        per_block = Counter(q.source_indices[0] for q in all_questions(result))
        assert max(per_block.values()) > 2  # the relaxed budget was needed to reach the requested short/descriptive counts
        assert (len(result.short), len(result.descriptive)) == (10, 5)

    def test_results_are_deterministic(self) -> None:
        a = [q.question for q in all_questions(build_questions(fixture_content("substack_free.html")))]
        b = [q.question for q in all_questions(build_questions(fixture_content("substack_free.html")))]
        assert a == b

    def test_mcq_distractors_never_appear_inside_the_stem_even_inflected(self) -> None:
        for fixture in ("substack_free.html", "substack_custom_domain.html"):
            for q in build_questions(fixture_content(fixture)).mcq:
                for i, option in enumerate(q.options):
                    if i != q.correct_index and len(option) > 3:
                        assert option.casefold() not in q.question.casefold()


# ============================================================================ D-5: definitions
class TestDefinitionDetection:
    def test_a_clear_definition_is_accepted_verbatim(self) -> None:
        sentence = "An embedding is a numerical representation of text in a vector space."
        found = _definitions_from_sentence(sentence)
        assert [(t, p, c) for t, p, c, _ in found] == [("embedding", "is_a", Confidence.MEDIUM)]
        content = content_of(H("Embeddings"), P(sentence + " Teams store the vectors in an index."))
        d = next(d for d in content.definitions if d.term == "embedding")
        assert d.statement == sentence and is_definitional(d) and d.source_index >= 0

    @pytest.mark.parametrize("sentence", [
        "Embeddings are commonly used in search applications.",
        "Teams will use embeddings for most ranking problems soon.",
        "Embeddings are the best way to rank documents, in my opinion.",
        "Dense retrieval will become the default approach for most teams.",
    ])
    def test_ordinary_statements_are_not_definitions(self, sentence: str) -> None:
        assert _definitions_from_sentence(sentence) == []

    @pytest.mark.parametrize("sentence", [
        "Chunk size is a trade-off.",
        "Retrieval is a key part of the pipeline.",
        "RAG is an example of a hybrid system.",
        "Reranking is the first step after retrieval.",
        "Latency is a very real problem for large systems.",
        "Vector search is the future of information retrieval systems.",
    ])
    def test_statements_that_only_name_the_term_are_flagged_never_trusted(self, sentence: str) -> None:
        found = _definitions_from_sentence(sentence)
        assert all(conf is Confidence.LOW for _, _, conf, _ in found)  # kept as a key-term cue, not a definition

    def test_hedged_or_rambling_definitions_are_flagged(self) -> None:
        found = _definitions_from_sentence("Chunking is a technique that may help in some cases.")
        assert [c for _, _, c, _ in found] == [Confidence.LOW]

    def test_list_lead_ins_and_questions_are_not_definitions(self) -> None:
        assert _definitions_from_sentence("A retriever is a component that supports the following:") == []

    @pytest.mark.parametrize("text", [
        "Example: a small chunk loses context here.",
        "Note: chunk size matters for long documents.",
        "Jupyter Notebooks: Understand machine learning concepts with hands-on code.",
        "Machine Learning Deep Dives: Identify the important machine learning concepts.",
        "Knowing your readers: Who does your writing bring together? Why will people rally around your writing?",
        "Read more: Can I customize my subscription benefits?",
        "Refining your content strategy: What will you publish for paying readers?",
    ])
    def test_labels_and_calls_to_action_are_not_glossary_entries(self, text: str) -> None:
        assert _colon_definition(text) is None

    def test_a_real_glossary_line_is_still_a_definition(self) -> None:
        assert _colon_definition("Embedding: A list of numbers that captures meaning.") == ("Embedding", "A list of numbers that captures meaning.")

    def test_acronym_introductions_are_kept_but_not_definitional(self) -> None:
        sentence = ("Retrieval Augmented Generation (RAG) systems are rapidly gaining traction in enterprise environments, "
                    "delivering significant impact across a range of applications, including:")
        content = content_of(H("Overview"), P(sentence))
        d = next(d for d in content.definitions if d.pattern == "acronym_expansion")
        assert d.aliases == ("RAG",) and not is_definitional(d)

    @pytest.mark.parametrize("sentence", [
        "Retrieval Augmented Generation (RAG) systems are rapidly gaining traction in enterprise environments.",
        "Many teams now build Large Language Model (LLM) applications for customer support and internal search.",
    ])
    def test_an_acronym_in_an_ordinary_sentence_never_makes_that_sentence_a_definition(self, sentence: str) -> None:
        content = content_of(H("Overview"), P(sentence))
        d = next(d for d in content.definitions if d.pattern == "acronym_expansion")
        assert d.statement == sentence and not is_definitional(d)
        questions = all_questions(build_questions(content))
        defining = [q for q in questions if "what is" in q.question or "define as follows" in q.question]
        assert defining == []  # never an answer key for "what is ...?" / "which term is defined as ...?"
        assert any(q.question.startswith("What does") and "stand for" in q.question for q in questions)  # the acronym fact stays

    def test_shortened_to_definitions_remain_definitional(self) -> None:
        d = next(iter(extract_definitions(list(fixture_content("substack_free.html").iter_items()))))
        assert d.pattern == "shortened_to" and is_definitional(d)


class TestAnswerKeysUseOnlyRealDefinitions:
    CONTENT_TEXT = [
        H("Key ideas"),
        P("An embedding is a numerical representation of text in a vector space. Chunk size is a trade-off. "
          "Retrieval is a key part of the pipeline. Teams often revisit these choices when results disappoint them."),
        P("Retrieval Augmented Generation (RAG) systems are rapidly gaining traction in enterprise environments, delivering "
          "significant impact across a range of applications, including:"),
        L("search", "chat support", "document review"),
    ]

    def test_only_the_genuine_definition_becomes_an_answer(self) -> None:
        result = build_questions(content_of(*self.CONTENT_TEXT))
        answers = " || ".join(q.answer for q in all_questions(result))
        what_is = [q for q in all_questions(result) if "what is" in q.question]
        assert any("embedding" in q.question and q.answer == "An embedding is a numerical representation of text in a vector space."
                   for q in what_is)
        assert "Chunk size is a trade-off." not in [q.answer for q in what_is]
        assert "key part of the pipeline" not in " ".join(q.answer for q in what_is)
        assert not any("rapidly gaining traction" in q.answer for q in what_is)
        assert not any("including:" in q.answer for q in all_questions(result) if "what is" in q.question or "define as" in q.question)

    def test_the_acronym_still_yields_a_stands_for_question(self) -> None:
        result = build_questions(content_of(*self.CONTENT_TEXT))
        assert any(q.question == "What does RAG stand for in the article?" and q.answer == "Retrieval Augmented Generation"
                   for q in result.short)

    def test_no_choice_question_defines_a_term_with_a_non_definition(self) -> None:
        for q in build_questions(content_of(*self.CONTENT_TEXT)).mcq:
            assert "rapidly gaining traction" not in q.question and "trade-off" not in q.question.split("define")[-1]

    def test_flagged_candidates_are_labelled_possible_in_the_notes(self) -> None:
        notes = NotesGenerator(LocalNotesBackend(), NotesConfig()).generate(content_of(*self.CONTENT_TEXT))
        labels = {i.term: i.label for i in notes.chapter(3).items() if i.kind is ItemKind.DEFINITION}
        assert labels.get("embedding") == "Definition (stated in the article)"
        for term, label in labels.items():
            if term.casefold() in {"chunk size", "retrieval", "retrieval augmented generation"}:
                assert label.startswith("Possible definition")

    def test_revision_notes_list_only_real_definitions(self) -> None:
        notes = NotesGenerator(LocalNotesBackend(), NotesConfig()).generate(content_of(*self.CONTENT_TEXT))
        short_defs = [i for s in notes.chapter(6).sections for i in s.items if i.kind is ItemKind.DEFINITION]
        assert [i.term for i in short_defs] == ["embedding"]

    def test_the_phase_8_failures_are_gone(self) -> None:
        content = fixture_content("substack_free.html")
        result = build_questions(content)
        assert not any(q.answer == "Chunk size is a trade-off." for q in all_questions(result))
        assert all(q.answer_provenance is NoteProvenance.ORIGINAL for q in result.short + result.descriptive)


# ============================================================================ D-7: unsupported AI claims
FREE = fixture_content("substack_free.html")
SOURCE = source_text(FREE)

SUPPORTED = "A basic RAG system has four steps, and each stage feeds the next one, so the order of the steps matters."
PARTIAL = "Chunks are the smaller pieces of text that a long document is split into before indexing."
UNSUPPORTED_TECH = "Chunk size is tuned through HNSW graph parameters inside Elasticsearch clusters running on dedicated hardware."
UNSUPPORTED_COMPARISON = "A basic RAG system has four steps instead of complex vector databases and orchestration frameworks."
INVENTED_NUMBER = "Teams that tune chunk size improve recall by 35% in most cases."
INVENTED_NUMBER_WORD = "Retrieval makes the answers twice as accurate for most of the teams that adopt it."
FABRICATED_CITATION = "As shown by Lewis et al. (2020), retrieval improves answers across the board."
FABRICATED_STUDY = "Studies show that retrieval improves answer quality for enterprise teams."
FABRICATED_SPEC = "The retriever embeds each chunk with GPT-4 before the vectors are stored."


def ask_keywords(explanation: str, significance: str = ""):
    llm = ScriptedLLM({"keyword explanations": json.dumps(
        {"terms": [{"term": "chunk", "explanation": explanation, "significance": significance}]})})
    out = llm.explain_keywords(FREE, [TermPacket("chunk", None)], PARAMS)
    return llm, out["chunk"]


def ask_section(explanation: str):
    llm = ScriptedLLM({"section explanations": json.dumps({"sections": [{"id": "s1", "explanation": explanation}]})})
    packet = SectionPacket("s1", "How the pipeline works", ("How the pipeline works",), SOURCE, (6,))
    return llm, llm.explain_sections(FREE, [packet], PARAMS)


class TestSupportAssessment:
    def test_supported_sentence(self) -> None:
        [v] = assess_support(SUPPORTED, SOURCE)
        assert v.status == "supported" and v.ratio >= 0.6

    def test_partly_supported_sentence(self) -> None:
        [v] = assess_support(PARTIAL, SOURCE)
        assert v.status == "partial" and 0.4 <= v.ratio < 0.6

    def test_unsupported_technical_claim(self) -> None:
        [v] = assess_support(UNSUPPORTED_TECH, SOURCE)
        assert v.status == "unsupported" and "hnsw" in v.novel_words

    def test_unsupported_comparison_is_always_rejected(self) -> None:
        [v] = assess_support(UNSUPPORTED_COMPARISON, SOURCE)
        assert v.status == "unsupported" and v.unsupported_comparison.startswith("instead of")

    def test_a_comparison_made_of_article_words_is_allowed(self) -> None:
        [v] = assess_support("Small chunks are precise rather than noisy, while large chunks keep more context.", SOURCE)
        assert v.status != "unsupported"

    def test_mixed_text_is_judged_sentence_by_sentence(self) -> None:
        verdicts = assess_support(f"{SUPPORTED} {UNSUPPORTED_COMPARISON}", SOURCE)
        assert [v.status for v in verdicts] == ["supported", "unsupported"]

    def test_hyphenated_words_are_split_so_paraphrases_of_them_are_supported(self) -> None:
        [v] = assess_support("Retrieval augmented generation gives a model access to external documents.", SOURCE)
        assert v.status == "supported"


class TestGroundingChecks:
    @pytest.mark.parametrize("text,needle", [
        (INVENTED_NUMBER, "35%"), (INVENTED_NUMBER_WORD, "twice"), (FABRICATED_CITATION, "Lewis et al"),
        (FABRICATED_STUDY, "Studies show"), (FABRICATED_SPEC, "GPT-4"),
        ("The vendor publishes \"retrieval quality always decides the final answer quality\" in its guide.", "quotation"),
    ])
    def test_invented_specifics_are_flagged(self, text: str, needle: str) -> None:
        assert any(needle in p for p in check_grounding(text, SOURCE)), check_grounding(text, SOURCE)

    @pytest.mark.parametrize("text", [SUPPORTED, PARTIAL, "The article says there are three main benefits of the approach."])
    def test_text_that_stays_within_the_article_is_not_flagged(self, text: str) -> None:
        assert check_grounding(text, SOURCE) == []

    def test_figures_the_article_does_contain_are_allowed(self) -> None:
        assert check_grounding("The technique was popularized in 2020, and the table lists 100, 300 and 800 words.", SOURCE) == []

    def test_an_attribution_the_article_makes_is_allowed(self) -> None:
        assert check_grounding("According to the research paper mentioned, the technique became popular.", SOURCE) == []


class TestExplanationsAreFilteredAgainstTheArticle:
    def test_fully_supported_explanation_is_kept_and_labelled_ai(self) -> None:
        llm, note = ask_keywords(SUPPORTED)
        assert [t.text for t in note.explanation] == [SUPPORTED] and note.explanation[0].provenance is NoteProvenance.AI_GENERATED
        assert llm.drain_warnings() == []

    def test_partly_supported_explanation_is_kept_but_flagged_for_review(self) -> None:
        llm, note = ask_keywords(PARTIAL)
        assert [t.text for t in note.explanation] == [PARTIAL]
        warnings = llm.drain_warnings()
        assert any(w.startswith("Review needed") and "smaller pieces" in w for w in warnings)

    def test_unsupported_technical_claim_is_omitted_not_speculated(self) -> None:
        llm, note = ask_keywords(UNSUPPORTED_TECH)
        assert note.explanation == ()
        assert any("does not support" in w and "explanation of 'chunk'" in w for w in llm.drain_warnings())

    def test_unsupported_comparison_is_removed(self) -> None:
        llm, note = ask_keywords(UNSUPPORTED_COMPARISON)
        assert note.explanation == ()
        assert any("instead of" in w for w in llm.drain_warnings())

    @pytest.mark.parametrize("claim", [INVENTED_NUMBER, INVENTED_NUMBER_WORD, FABRICATED_CITATION, FABRICATED_STUDY, FABRICATED_SPEC])
    def test_invented_figures_citations_and_specifications_reject_the_whole_text(self, claim: str) -> None:
        llm, note = ask_keywords(claim, significance=SUPPORTED)
        assert note.explanation == () and len(note.significance) == 1  # only the bad part is lost
        assert any("not in the article" in w for w in llm.drain_warnings())

    def test_mixed_supported_and_unsupported_keeps_only_what_the_article_supports(self) -> None:
        llm, note = ask_keywords(f"{SUPPORTED} {UNSUPPORTED_COMPARISON}")
        assert [t.text for t in note.explanation] == [SUPPORTED]
        assert any("Removed an AI-written statement" in w and "instead of" in w for w in llm.drain_warnings())

    def test_the_phase_8_sentences_are_caught(self) -> None:
        text = ("This section provides a simple Python code example demonstrating the retrieval step using plain lists "
                "instead of complex vector databases. The function retrieve takes a question vector and a list of chunks.")
        _, out = ask_section(text)
        kept = " ".join(g.text for g in out.get("s1", ()))
        assert "vector databases" not in kept
        text2 = "Private data stays inside the organisation and is never sent to external servers rather than being sent to external servers."
        assert assess_support(text2, SOURCE)[0].status == "unsupported"

    def test_section_explanations_are_filtered_too(self) -> None:
        llm, out = ask_section(f"{SUPPORTED} {UNSUPPORTED_COMPARISON}")
        assert [g.text for g in out["s1"]] == [SUPPORTED]
        assert all(g.provenance is NoteProvenance.AI_GENERATED for g in out["s1"])

    def test_a_section_with_nothing_supported_gets_no_ai_text(self) -> None:
        llm, out = ask_section(f"{UNSUPPORTED_TECH} {UNSUPPORTED_COMPARISON}")
        assert out == {}

    def test_removed_text_never_reaches_the_notes_or_the_document(self, tmp_path: Path) -> None:
        bad = json.dumps({"sections": [{"id": "x", "explanation": f"{SUPPORTED} {UNSUPPORTED_COMPARISON}"}]})

        def respond(user: str) -> str:
            from tests.helpers import payload_of
            return json.dumps({"sections": [{"id": s["id"], "explanation": f"{SUPPORTED} {UNSUPPORTED_COMPARISON}"}
                                            for s in payload_of(user)]})

        notes = NotesGenerator(ScriptedLLM({"section explanations": respond}), NotesConfig()).generate(FREE)
        every = " ".join(i.plain_text for i in notes.all_items())
        assert "complex vector databases" not in every
        assert any("does not support it" in w for w in notes.generation_warnings)
        assert any(i.provenance is NoteProvenance.AI_GENERATED for i in notes.chapter(2).items())  # supported part kept
        doc = Document(str(WordGenerator().save(notes, tmp_path).path))
        assert "complex vector databases" not in "\n".join(p.text for p in doc.paragraphs)
        assert bad  # the scripted payload above is only here to show what a model could send


class TestAiQuestionsAreFilteredToo:
    @staticmethod
    def short_questions(answer: str):
        llm = ScriptedLLM({"short questions": json.dumps({"questions": [
            {"question": "What does the article say about the steps of a basic system?", "answer": answer}]})})
        result = llm.create_questions(FREE, QuestionSpec(short=1, descriptive=0, mcq=0), PARAMS)
        return llm, result

    def test_supported_answer_is_accepted(self) -> None:
        llm, result = self.short_questions(SUPPORTED)
        assert len(result.short) == 1 and llm.drain_warnings() == []

    def test_unsupported_answer_rejects_the_question(self) -> None:
        llm, result = self.short_questions(UNSUPPORTED_COMPARISON)
        assert result.short == [] and any("not supported by the article" in w for w in llm.drain_warnings())

    def test_partly_supported_answer_is_kept_and_flagged(self) -> None:
        llm, result = self.short_questions(PARTIAL)
        assert len(result.short) == 1 and any(w.startswith("Review needed") for w in llm.drain_warnings())

    def test_a_choice_question_with_an_unsupported_explanation_is_rejected(self) -> None:
        mcq = {"questions": [{"question": "How many steps does a basic system have?", "options": ["Two", "Three", "Four", "Five"],
                              "correct_index": 2, "explanation": UNSUPPORTED_TECH}]}
        llm = ScriptedLLM({"mcq questions": json.dumps(mcq)})
        result = llm.create_questions(FREE, QuestionSpec(short=0, descriptive=0, mcq=1), PARAMS)
        assert result.mcq == [] and any("not supported by the article" in w for w in llm.drain_warnings())


class TestPromptsRequireGrounding:
    def test_system_rules_forbid_background_knowledge_and_outside_comparisons(self) -> None:
        text = SYSTEM_RULES.lower()
        for needle in ("only information in the supplied article", "background knowledge", "twice", "studies", "do not compare",
                       "instead of", "leave it out"):
            assert needle in text, needle

    def test_keyword_and_section_prompts_demand_supported_sentences(self) -> None:
        llm, _ = ask_keywords(SUPPORTED)
        user = llm.calls[-1][2].lower()
        assert "every sentence must be supported" in user and "background knowledge" in user
        llm2, _ = ask_section(SUPPORTED)
        assert "every sentence must be supported" in llm2.calls[-1][2].lower()

    def test_the_local_backend_makes_no_ai_claims_at_all(self) -> None:
        notes = NotesGenerator(LocalNotesBackend(), NotesConfig()).generate(FREE)
        assert not any(i.provenance is NoteProvenance.AI_GENERATED for i in notes.all_items())


class TestOllamaAndOpenAiShareTheSameFilter:
    def test_all_language_model_backends_inherit_the_checks(self) -> None:
        from src.backends.llm import LLMBackend
        from src.backends.ollama import OllamaBackend
        from src.backends.openai_backend import OpenAIBackend
        assert issubclass(OllamaBackend, LLMBackend) and issubclass(OpenAIBackend, LLMBackend)
        assert OllamaBackend._supported_text is LLMBackend._supported_text and OpenAIBackend._validate_questions is LLMBackend._validate_questions
