"""Phase 4 tests: cleaning, de-duplication, sections, definitions, keywords, statements."""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Optional

import pytest

import src.content_processor as cp
from src.article_extractor import Article, Block, BlockKind, Completeness, ListItem, SubstackExtractor
from src.content_processor import (
    Annotation,
    Confidence,
    ContentItem,
    ContentProcessor,
    EmptyContentError,
    InvalidArticleError,
    ProcessorConfig,
    Provenance,
    RemovalReason,
    StatementKind,
    build_sections,
    clean_block,
    clean_code,
    clean_text,
    extract_definitions,
    extract_keywords,
    find_duplicates,
    is_boilerplate,
    process_article,
    split_sentences,
    tokenize,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- builders
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


def art(*blocks: Block, completeness: Completeness = Completeness.COMPLETE, **kw: object) -> Article:
    return Article(title=str(kw.pop("title", "Test Article")), blocks=list(blocks),
                   completeness=completeness, **kw)  # type: ignore[arg-type]


FILLER = ("This paragraph exists to give the article enough ordinary prose to process "
          "without triggering any special handling at all.")


_SYL = ["ba", "ko", "mi", "ra", "su", "te", "no", "li"]
_VOCAB = [a + b + c for a in _SYL for b in _SYL for c in _SYL]  # 512 distinct pseudo-words, no digits


def F(tag: str) -> str:
    """A prose paragraph that is genuinely different from every other F(tag) and from FILLER."""
    import random
    rng = random.Random(tag)
    return " ".join(rng.sample(_VOCAB, 24)) + "."


def process(*blocks: Block, config: Optional[ProcessorConfig] = None, **kw: object) -> cp.ProcessedContent:
    return process_article(art(*blocks, **kw), config)


def definitions_of(*blocks: Block) -> list[cp.Definition]:
    return process(P(FILLER), *blocks).definitions


# ============================================================================= cleaning
class TestCleanText:
    def test_collapses_whitespace_and_newlines(self) -> None:
        assert clean_text("  a \n\t b   c  ") == "a b c"

    def test_non_breaking_and_unicode_spaces(self) -> None:
        assert clean_text("a\u00a0b\u2009c\u3000d") == "a b c d"

    def test_removes_zero_width_space_bom_soft_hyphen(self) -> None:
        assert clean_text("a\u200bb\ufeffc\u00add\u2060e") == "abcde"

    def test_keeps_zwj_and_zwnj_needed_by_indic_scripts(self) -> None:
        s = "\u0c15\u0c4d\u200c\u0c37 \u0c15\u0c4d\u200d\u0c37"
        assert clean_text(s) == s

    def test_normalizes_to_nfc(self) -> None:
        decomposed = "cafe\u0301"
        assert clean_text(decomposed) == "caf\u00e9"
        assert unicodedata.is_normalized("NFC", clean_text(decomposed))

    @pytest.mark.parametrize("raw,expected", [
        ("the RAG , which works .", "the RAG, which works."),
        ("( see below )", "(see below)"),
        ("really ?", "really?"),
        ("100 %", "100%"),
        ("Priya \u2019s post", "Priya\u2019s post"),
        ("don \u2019t", "don\u2019t"),
        ("list [ 1 ]", "list [1]"),
    ])
    def test_fixes_spacing_artifacts_from_inline_markup(self, raw: str, expected: str) -> None:
        assert clean_text(raw) == expected

    def test_does_not_glue_words_or_touch_quoted_words(self) -> None:
        assert clean_text("she said 'so what' now") == "she said 'so what' now"

    def test_idempotent(self) -> None:
        once = clean_text(" a ,  b \u00a0( c ) ")
        assert clean_text(once) == once

    @pytest.mark.parametrize("empty", ["", "   ", "\n\t", "\u200b\ufeff"])
    def test_empty_inputs(self, empty: str) -> None:
        assert clean_text(empty) == ""

    def test_telugu_text_is_unchanged(self) -> None:
        s = "తెలుగు భాష ఒక ద్రావిడ భాష."
        assert clean_text(s) == s


class TestCleanCode:
    def test_preserves_indentation_tabs_and_inner_blank_lines(self) -> None:
        code = "def f():\n\tif x:\n\t\treturn 1\n\n    return 2"
        assert clean_code(code) == code

    def test_normalizes_line_endings_and_nbsp(self) -> None:
        assert clean_code("a\r\nb\rc\u00a0d") == "a\nb\nc d"

    def test_strips_only_surrounding_blank_lines(self) -> None:
        assert clean_code("\n\n  x = 1\n\n") == "  x = 1"

    def test_empty(self) -> None:
        assert clean_code("") == "" and clean_code("\n\n") == ""


class TestSplitSentences:
    def test_basic(self) -> None:
        assert split_sentences("One is here. Two is here! Three is here? Yes.") == [
            "One is here.", "Two is here!", "Three is here?", "Yes."]

    def test_abbreviations_and_decimals_do_not_split(self) -> None:
        s = "Use tools such as e.g. Python 3.12 for Dr. Lee. The next one follows."
        parts = split_sentences(s)
        assert len(parts) == 2 and parts[0].endswith("Dr. Lee.")

    def test_lowercase_after_period_does_not_split(self) -> None:
        assert len(split_sentences("version 2.x. is fine, i.e. stable. Done.")) == 2

    def test_quotes_and_brackets_after_terminator(self) -> None:
        assert split_sentences('He said "stop." Then he left.') == ['He said "stop."', "Then he left."]

    def test_telugu_sentences(self) -> None:
        parts = split_sentences("ఇది మొదటి వాక్యం. ఇది రెండవ వాక్యం.")
        assert parts == ["ఇది మొదటి వాక్యం.", "ఇది రెండవ వాక్యం."]

    def test_no_terminal_punctuation_and_empty(self) -> None:
        assert split_sentences("no end here") == ["no end here"]
        assert split_sentences("   ") == []


class TestTokenize:
    def test_telugu_words_stay_whole(self) -> None:
        assert tokenize("తెలుగు వచనం") == ["తెలుగు", "వచనం"]

    def test_hyphenated_and_apostrophes(self) -> None:
        assert tokenize("re-ranking isn\u2019t snake_case") == ["re-ranking", "isn\u2019t", "snake_case"]


# ============================================================================= boilerplate / blocks
class TestBoilerplate:
    @pytest.mark.parametrize("text", [
        "Thanks for reading The Applied AI Letter! Subscribe for free to receive new posts.",
        "Subscribe now", "Subscribe", "Share", "Leave a comment", "Like Comment Share",
        "Discussion about this post", "This post is public so feel free to share it.",
        "If you enjoyed this post, please subscribe and share it with a friend.",
        "Get the Substack app", "Upgrade to paid", "Read more",
    ])
    def test_detected(self, text: str) -> None:
        assert is_boilerplate(text)

    @pytest.mark.parametrize("text", [
        "Subscribers of the service pay monthly and the share price rose.",
        "You can share the embeddings between workers to save memory in the cluster.",
        "Thanks to caching, the second request is much faster than the first one.",
        "x" * 300,
        "",
    ])
    def test_not_detected(self, text: str) -> None:
        assert not is_boilerplate(text)

    def test_long_text_starting_like_boilerplate_is_kept(self) -> None:
        long = "Thanks for reading " + "and this is real content " * 20
        assert not is_boilerplate(long)

    def test_removed_and_recorded_by_processor(self) -> None:
        pc = process(P(FILLER), P("Thanks for reading! Subscribe for free."), H("Next"), P(F("Again.")))
        assert [r.reason for r in pc.removed] == [RemovalReason.BOILERPLATE]
        assert pc.removed[0].source_index == 1
        assert "Subscribe" not in " ".join(i.text for i in pc.iter_items())

    def test_list_of_only_boilerplate_is_removed(self) -> None:
        pc = process(P(FILLER), L("Like", "Comment", "Share"))
        assert any(r.reason is RemovalReason.BOILERPLATE for r in pc.removed)

    def test_can_be_disabled(self) -> None:
        pc = process(P(FILLER), P("Subscribe now"), config=ProcessorConfig(remove_boilerplate=False))
        assert not pc.removed


class TestCleanBlock:
    def test_paragraph(self) -> None:
        assert clean_block(P(" a ,  b ")).text == "a, b"  # type: ignore[union-attr]

    @pytest.mark.parametrize("block", [
        P("  \u200b "), H(""), Q("  "), C("\n\n"), L(), L("", " "), T(), T(("", ""), (" ", "")),
    ])
    def test_blocks_with_nothing_left_are_dropped(self, block: Block) -> None:
        assert clean_block(block) is None

    def test_list_keeps_depths_and_drops_empty_items(self) -> None:
        cleaned = clean_block(L(" a ", "", "b", depths=[0, 1, 1]))
        assert [(i.text, i.depth) for i in cleaned.items] == [("a", 0), ("b", 1)]  # type: ignore[union-attr]

    def test_table_cells_cleaned_and_empty_rows_dropped(self) -> None:
        cleaned = clean_block(T((" A ", "B"), ("", ""), ("1 ,", "2")))
        assert cleaned.rows == (("A", "B"), ("1,", "2"))  # type: ignore[union-attr]

    def test_code_untouched_apart_from_edges(self) -> None:
        cleaned = clean_block(C("\nx  =   1  \n", "python"))
        assert cleaned.text == "x  =   1" and cleaned.language == "python"  # type: ignore[union-attr]

    def test_heading_level_clamped(self) -> None:
        assert clean_block(H("x", 9)).level == 6  # type: ignore[union-attr]
        assert clean_block(H("x", 0)).level == 1  # type: ignore[union-attr]


# ============================================================================= de-duplication
LONG_A = ("Large language models are remarkably fluent but they do not know what they were never taught "
          "during training which is why external documents help answer questions about private company policies "
          "written last week by the legal team")


class TestDeduplication:
    def test_exact_duplicate_removed_and_recorded(self) -> None:
        pc = process(P(LONG_A), H("Other"), P("Different content that sits between the two repeated paragraphs here."), P(LONG_A))
        dup = [r for r in pc.removed if r.reason is RemovalReason.DUPLICATE_EXACT]
        assert len(dup) == 1 and dup[0].source_index == 3 and dup[0].duplicate_of == 0
        assert sum(LONG_A in i.text for i in pc.iter_items()) == 1

    def test_first_occurrence_is_the_one_kept(self) -> None:
        pc = process(P(LONG_A), P("Filler between them that is long enough to be a real paragraph here."), P(LONG_A))
        assert 0 in {i.source_index for i in pc.iter_items()} and 2 not in {i.source_index for i in pc.iter_items()}

    def test_case_and_punctuation_differences_still_exact(self) -> None:
        pc = process(P(LONG_A + "."), P("Spacer paragraph with enough words to count as prose, really."), P(LONG_A.upper() + "!!"))
        assert pc.stats.removed_duplicates == 1

    def test_near_duplicate_removed(self) -> None:
        near = LONG_A.replace("remarkably", "very")
        pc = process(P(LONG_A), P("Spacer paragraph with enough words to count as prose, really."), P(near))
        removed = [r for r in pc.removed if r.reason is RemovalReason.DUPLICATE_NEAR]
        assert len(removed) == 1 and removed[0].similarity >= 0.92

    def test_near_duplicate_with_different_number_is_kept(self) -> None:
        a = LONG_A + " in 2019"
        b = LONG_A + " in 2024"
        pc = process(P(a), P("Spacer paragraph with enough words to count as prose, really."), P(b))
        assert pc.stats.removed_duplicates == 0

    def test_near_duplicate_with_different_acronym_or_identifier_is_kept(self) -> None:
        base = LONG_A + " using the {} setting"
        for x, y in [("GPU", "CPU"), ("max_tokens", "min_tokens"), ("re-rank", "re-sort")]:
            pc = process(P(base.format(x)), P("Spacer paragraph with enough words to count as prose, really."), P(base.format(y)))
            assert pc.stats.removed_duplicates == 0, (x, y)

    def test_code_is_never_deduplicated(self) -> None:
        code = "for i in range(10):\n    print(i)"
        pc = process(P(FILLER), C(code, "python"), P("Between the two identical snippets, more prose appears here."), C(code, "python"))
        assert sum(i.kind is BlockKind.CODE for i in pc.iter_items()) == 2

    def test_tables_lists_and_headings_are_never_deduplicated(self) -> None:
        tbl = T(("A", "B"), ("1", "2"))
        lst = L("same item one here", "same item two here")
        pc = process(H("Summary"), P(FILLER), tbl, lst, H("Summary"), P(F("Second.")), tbl, lst)
        kinds = [i.kind for i in pc.iter_items()]
        assert kinds.count(BlockKind.TABLE) == 2 and kinds.count(BlockKind.LIST) == 2
        assert sum(1 for s in pc.iter_sections() if s.heading == "Summary") == 2

    def test_short_paragraphs_are_never_duplicates(self) -> None:
        pc = process(P(FILLER), P("Yes, exactly."), P("Yes, exactly."), P("Why?"), P("Why?"))
        assert pc.stats.removed_duplicates == 0

    def test_quote_and_paragraph_with_same_text_are_not_duplicates(self) -> None:
        pc = process(P(LONG_A), Q(LONG_A))
        assert pc.stats.removed_duplicates == 0

    def test_dedupe_can_be_disabled(self) -> None:
        pc = process(P(LONG_A), P(LONG_A), config=ProcessorConfig(dedupe=False))
        assert pc.stats.removed_duplicates == 0 and len(list(pc.iter_items())) == 2

    def test_find_duplicates_ignores_non_prose(self) -> None:
        blocks = [(0, C("x = 1 and more code here to pass the length rule")), (1, C("x = 1 and more code here to pass the length rule"))]
        assert find_duplicates(blocks) == {}

    def test_three_copies_all_point_at_the_first(self) -> None:
        pc = process(P(LONG_A), P(LONG_A), P(LONG_A))
        assert [(r.source_index, r.duplicate_of) for r in pc.removed] == [(1, 0), (2, 0)]


# ============================================================================= sections
class TestSections:
    def test_nesting_by_heading_level(self) -> None:
        pc = process(H("A", 2), P(FILLER), H("A1", 3), P(F("1")), H("A2", 3), P(F("2")),
                     H("B", 2), P(F("3")), H("B1", 3), P(F("4")))
        assert [s.heading for s in pc.sections] == ["A", "B"]
        assert [c.heading for c in pc.sections[0].children] == ["A1", "A2"]
        assert [c.heading for c in pc.sections[1].children] == ["B1"]

    def test_preamble_before_first_heading(self) -> None:
        pc = process(P("Intro text " + FILLER), H("First"), P(FILLER))
        pre = pc.sections[0]
        assert pre.is_preamble and pre.heading is None and pre.level == 0 and pre.items[0].text.startswith("Intro")
        assert pc.sections[1].heading == "First"

    def test_no_headings_gives_single_section_and_warning(self) -> None:
        pc = process(P(FILLER), P(F("More.")))
        assert len(pc.sections) == 1 and pc.sections[0].is_preamble
        assert any("no headings" in w for w in pc.processing_warnings)

    def test_skipped_levels_nest_under_nearest_parent(self) -> None:
        pc = process(H("Top", 2), P(FILLER), H("Deep", 4), P(F("d")), H("Back", 3), P(F("b")))
        top = pc.sections[0]
        assert [c.heading for c in top.children] == ["Deep", "Back"]

    def test_sibling_at_same_or_higher_level_closes_the_previous(self) -> None:
        pc = process(H("A", 3), P(FILLER), H("B", 2), P(F("b")), H("C", 1), P(F("c")))
        assert [s.heading for s in pc.sections] == ["A", "B", "C"] or [s.level for s in pc.sections] == [3, 2, 1]
        assert all(not s.children for s in pc.sections)

    def test_first_heading_deeper_than_later_ones(self) -> None:
        pc = process(H("Deep", 3), P(FILLER), H("Shallow", 2), P(F("s")), H("Child", 3), P(F("c")))
        assert [s.heading for s in pc.sections] == ["Deep", "Shallow"]
        assert [c.heading for c in pc.sections[1].children] == ["Child"]

    def test_heading_without_content_is_kept(self) -> None:
        pc = process(H("Empty", 2), H("Full", 2), P(FILLER))
        empty = pc.sections[0]
        assert empty.heading == "Empty" and empty.items == []

    def test_paths(self) -> None:
        pc = process(H("A", 2), P(FILLER), H("B", 3), P(F("b")), H("C", 4), P(F("c")))
        assert [s.path for s in pc.iter_sections()] == [("A",), ("A", "B"), ("A", "B", "C")]

    def test_ids_are_unique_and_sequential(self) -> None:
        pc = process(H("A"), P(FILLER), H("B"), P(F("b")))
        assert [s.id for s in pc.iter_sections()] == ["s1", "s2"]

    def test_walk_is_document_order(self) -> None:
        pc = process(P("Pre " + FILLER), H("A", 2), P(F("a")), H("A1", 3), P(F("a1")),
                     H("B", 2), P(F("b")))
        order = [i.source_index for i in pc.iter_items()]
        assert order == sorted(order)

    def test_headings_with_only_whitespace_are_dropped(self) -> None:
        pc = process(H("   "), P(FILLER))
        assert pc.sections[0].is_preamble

    def test_word_counts(self) -> None:
        pc = process(H("A", 2), P("one two three"), H("B", 3), P("four five"))
        assert pc.sections[0].word_count == 3 and pc.sections[0].total_word_count == 5

    def test_build_sections_direct(self) -> None:
        blocks = [(0, H("A")), (1, P("x y z"))]
        items = {1: cp._make_item(1, P("x y z"))}
        roots = build_sections(blocks, items)
        assert roots[0].items[0].source_index == 1


# ============================================================================= traceability
class TestTraceability:
    def test_every_block_is_kept_or_recorded_as_removed(self) -> None:
        blocks = [H("A"), P(FILLER), P("  "), P("Subscribe now"), P(LONG_A), P(LONG_A), C("x = 1"), L("a b c d")]
        pc = process(*blocks)
        kept = {i.source_index for i in pc.iter_items()} | {s.heading_index for s in pc.iter_sections() if s.heading_index is not None}
        removed = {r.source_index for r in pc.removed}
        assert kept | removed == set(range(len(blocks)))
        assert kept.isdisjoint(removed)

    def test_item_points_back_to_the_right_original_block(self) -> None:
        article = art(H("A"), P("First paragraph " + FILLER), C("code()", "python"), L("x y z w"))
        pc = process_article(article)
        for item in pc.iter_items():
            original = article.blocks[item.source_index]
            assert original.kind is item.kind

    def test_section_of_lookup(self) -> None:
        pc = process(H("A"), P(FILLER), H("B"), P(F("b")))
        assert pc.section_of(1).heading == "A" and pc.section_of(3).heading == "B"  # type: ignore[union-attr]
        assert pc.section_of(2).heading == "B" and pc.section_of(99) is None  # type: ignore[union-attr]

    def test_sentences_are_linked_to_blocks(self) -> None:
        pc = process(P("One sentence is here. Another sentence is there."), L("List item one. Second part here."))
        para, lst = list(pc.iter_items())
        assert [s.text for s in para.sentences] == ["One sentence is here.", "Another sentence is there."]
        assert [(s.list_item_index, s.index) for s in lst.sentences] == [(0, 0), (0, 1)]

    def test_original_article_is_not_modified(self) -> None:
        blocks = [H("A"), P(" messy  ,  text " + FILLER), P(LONG_A), P(LONG_A)]
        article = art(*blocks, warnings=["w"], completeness_reasons=["r"])
        before = [(b.kind, b.text) for b in article.blocks]
        process_article(article)
        assert [(b.kind, b.text) for b in article.blocks] == before
        assert article.warnings == ["w"] and article.completeness_reasons == ["r"]

    def test_definitions_keywords_statements_carry_source_indices(self) -> None:
        pc = process(H("A"), P("RAG stands for retrieval augmented generation in this article, as defined here."))
        d = pc.definitions[0]
        assert d.source_index == 1 and d.section_id == "s1"
        kw = pc.get_keyword("RAG")
        assert kw is not None and 1 in kw.block_indices


# ============================================================================= definitions
class TestDefinitions:
    @pytest.mark.parametrize("sentence,term,pattern", [
        ("Latency refers to the delay between a request and its response.", "Latency", "refers_to"),
        ("Quantization means storing each weight with fewer bits than before.", "Quantization", "refers_to"),
        ("TTL stands for time to live in most caching systems today.", "TTL", "refers_to"),
        ("An embedding is defined as a vector that captures meaning.", "embedding", "refers_to"),
        ("A vector database is a system that stores embeddings for search.", "vector database", "is_a"),
        ("Gradient descent is an optimization algorithm for training models.", "Gradient descent", "is_a"),
        ("Overfitting is the tendency of a model to memorize its training data.", "Overfitting", "is_a"),
        ("Retrieval-Augmented Generation, usually shortened to RAG, grounds answers in documents.",
         "Retrieval-Augmented Generation", "shortened_to"),
        ("Large Language Model, abbreviated as LLM, is used throughout this article.", "Large Language Model", "shortened_to"),
        ("This approach is often called chunking in the literature on search.", "chunking", "called"),
        ("The stored numbers are known as embeddings by most practitioners.", "embeddings", "called"),
    ])
    def test_pattern(self, sentence: str, term: str, pattern: str) -> None:
        defs = definitions_of(P(sentence))
        match = next((d for d in defs if d.term == term), None)
        assert match is not None, [d.term for d in defs]
        assert match.pattern == pattern
        assert match.statement == sentence

    def test_acronym_expansion_with_hyphenated_words(self) -> None:
        defs = definitions_of(P("We use Retrieval-Augmented Generation (RAG) to answer questions here."))
        d = next(d for d in defs if d.pattern == "acronym_expansion")
        assert d.term == "Retrieval-Augmented Generation" and d.aliases == ("RAG",)

    def test_acronym_expansion_with_connector_words(self) -> None:
        defs = definitions_of(P("The Internet of Things (IoT) connects many small devices together."))
        assert any(d.term == "Internet of Things" and d.aliases == ("IoT",) for d in defs)

    def test_reverse_acronym_expansion(self) -> None:
        defs = definitions_of(P("The CPU (central processing unit) runs the program instructions here."))
        d = next(d for d in defs if d.pattern == "acronym_expansion")
        assert d.term == "CPU" and d.aliases == ("central processing unit",)

    def test_parenthetical_that_is_not_an_acronym_is_ignored(self) -> None:
        defs = definitions_of(P("The service (ABC) was slow during the whole long weekend test."))
        assert not [d for d in defs if d.pattern == "acronym_expansion"]

    def test_list_item_term_colon(self) -> None:
        defs = definitions_of(L("Loss function: measures how wrong the model is", "Learning rate: the step size"))
        assert {(d.term, d.pattern) for d in defs} == {("Loss function", "term_colon"), ("Learning rate", "term_colon")}

    @pytest.mark.parametrize("item", [
        "Too large: training diverges", "Note: this applies to all versions of the software",
        "Step 1: install the package and then run the tests", "Result: ok", "Why: because", "The rule is simple: do the work now",
    ])
    def test_colon_lines_that_are_not_definitions(self, item: str) -> None:
        assert not [d for d in definitions_of(L(item)) if d.pattern == "term_colon"]

    def test_table_rows_with_term_definition_headers(self) -> None:
        defs = definitions_of(T(("Term", "Meaning"), ("Epoch", "One pass over the data"), ("Batch", "A subset")))
        assert [(d.term, d.statement, d.pattern) for d in defs] == [
            ("Epoch", "Epoch: One pass over the data", "table_row"), ("Batch", "Batch: A subset", "table_row")]
        assert all(d.confidence is Confidence.HIGH for d in defs)

    def test_table_without_definition_headers_is_not_mined(self) -> None:
        assert not definitions_of(T(("Chunk size", "Strength"), ("Small", "Precise")))

    def test_appositive_is_low_confidence(self) -> None:
        defs = definitions_of(P("Convert each chunk into an embedding, a list of numbers that captures its meaning."))
        d = next(d for d in defs if d.term == "embedding")
        assert d.confidence is Confidence.LOW and d.pattern == "appositive"

    @pytest.mark.parametrize("sentence", [
        "This is a problem that many teams face in practice every day.",
        "It is a long road to reach production quality software today.",
        "If the model is a transformer then the cost grows quickly overall.",
        "There is a reason this works well in most of the cases we saw.",
        "What matters is a clear measure of retrieval quality here.",
        "We are the first team to publish results of this kind yet.",
        "In 2020, a research paper described the main technique used here.",
        "The quick brown fox jumps over the lazy dog near the river bank.",
    ])
    def test_non_definitions_are_not_reported(self, sentence: str) -> None:
        assert not definitions_of(P(sentence))

    def test_statement_is_always_verbatim_source_text(self) -> None:
        article = art(P("Latency refers to the delay between request and response."),
                      L("Epoch: one pass over the training data"), P(FILLER))
        pc = process_article(article)
        source = "\n".join(b.plain_text for b in article.blocks)
        for d in pc.definitions:
            assert d.statement in source and d.provenance is Provenance.SOURCE

    def test_same_term_defined_twice_keeps_best_and_merges_aliases(self) -> None:
        pc = process(P("RAG is a technique for grounding answers in documents."),
                     P("Retrieval Augmented Generation, shortened to RAG, grounds the model."),
                     P("RAG stands for retrieval augmented generation in this text."))
        rag = [d for d in pc.definitions if d.term == "RAG"]
        assert len(rag) == 1 and rag[0].confidence is Confidence.HIGH

    def test_non_english_text_yields_no_definitions_and_no_crash(self) -> None:
        assert definitions_of(P("తెలుగు భాష ఒక ద్రావిడ భాష మరియు ఇది చాలా పాతది.")) == []

    def test_get_definition_lookup_is_case_insensitive(self) -> None:
        pc = process(P("Latency refers to the delay between request and response."))
        assert pc.get_definition("latency") is not None and pc.get_definition("nope") is None


# ============================================================================= keywords
def keywords_of(*blocks: Block, config: Optional[ProcessorConfig] = None) -> dict[str, cp.Keyword]:
    pc = process(*blocks, config=config)
    return {k.term: k for k in pc.keywords}


class TestKeywords:
    def test_acronyms_found_case_sensitively(self) -> None:
        kws = keywords_of(P("The GPU and the API talk to the LLMs. A gpu or api in lowercase is not one."))
        assert {"GPU", "API", "LLM"} <= set(kws)
        assert all("acronym" in kws[t].sources for t in ("GPU", "API", "LLM"))

    def test_acronym_stoplist_and_all_caps_headings(self) -> None:
        kws = keywords_of(H("WHY IT MATTERS"), P("OK, the US team said that THIS works here fine today."))
        assert "OK" not in kws and "US" not in kws and "WHY" not in kws and "MATTERS" not in kws

    def test_definition_terms_and_aliases_are_keywords(self) -> None:
        kws = keywords_of(P("Retrieval-Augmented Generation, usually shortened to RAG, grounds answers in documents."))
        assert kws["Retrieval-Augmented Generation"].has_definition
        assert "RAG" in kws

    def test_code_identifiers(self) -> None:
        kws = keywords_of(P(FILLER), C("class VectorStore:\n    def retrieve(self, query):\n        pass\nimport numpy"),
                          C("function renderChart(data) {}\nconst maxTokens = 3"))
        for name in ("VectorStore", "retrieve", "renderChart", "maxTokens", "numpy"):
            assert name in kws and "code" in kws[name].sources

    def test_heading_terms_but_not_generic_or_long_headings(self) -> None:
        kws = keywords_of(H("Chunk size"), P(FILLER), H("Introduction"), P(F("1")), H("Conclusion"), P(F("2")),
                          H("Why this whole thing matters so much to us"), P(F("3")), H("How it works"), P(F("4")),
                          H("Key ideas"), P(F("5")))
        assert "Chunk size" in kws and "heading" in kws["Chunk size"].sources
        for bad in ("Introduction", "Conclusion", "Why this whole thing matters so much to us", "How it works", "Key ideas"):
            assert bad not in kws

    def test_frequency_phrases_need_repetition(self) -> None:
        once = keywords_of(P("Cosine similarity ranks candidate passages for the question in this system."))
        assert "cosine similarity" not in once
        twice = keywords_of(P("Cosine similarity ranks passages."), P("We compute cosine similarity for every pair of vectors."))
        assert "cosine similarity" in twice and twice["cosine similarity"].count == 2

    def test_single_words_need_three_occurrences(self) -> None:
        two = keywords_of(P("Sharding helps. Sharding scales. Other words follow in this sentence."))
        three = keywords_of(P("Sharding helps. Sharding scales. Sharding costs. Other words follow."))
        assert "sharding" not in two and "sharding" in three

    def test_stopwords_and_short_words_excluded(self) -> None:
        kws = keywords_of(P("the the the the and and and also also also very very very this this this " * 3))
        assert not kws

    def test_plural_and_singular_are_merged(self) -> None:
        kws = keywords_of(P("A chunk is small. Chunks are many. Each chunk matters. Many chunks exist. One chunk."))
        assert ("chunk" in kws) != ("chunks" in kws)
        assert next(iter(kws.values())).count >= 5

    def test_phrase_is_preferred_over_words_that_only_occur_inside_it(self) -> None:
        kws = keywords_of(P("Vector databases are used. We use vector databases daily. Teams adopt vector databases widely."))
        assert "vector databases" in kws and "vector" not in kws

    def test_compound_terms(self) -> None:
        kws = keywords_of(P("The re-ranking step helps. Use re-ranking often. A snake_case name and snake_case again."))
        assert "re-ranking" in kws and "compound" in kws["re-ranking"].sources
        assert "snake_case" in kws

    def test_capitalised_phrases_and_names(self) -> None:
        kws = keywords_of(P("Research from Facebook AI Research helped. Later Facebook AI Research published more."))
        assert "Facebook AI Research" in kws and "named_term" in kws["Facebook AI Research"].sources

    def test_block_indices_point_to_real_occurrences(self) -> None:
        article = art(P(FILLER), P("The API is used here."), C("api_call()"), P("The API again."))
        pc = process_article(article)
        kw = pc.get_keyword("API")
        assert kw is not None and kw.block_indices == (1, 3) and kw.count == 2 and kw.first_index == 1

    def test_definitions_rank_above_plain_frequency(self) -> None:
        pc = process(P("Quantization means storing weights with fewer bits. Model model model model model."))
        terms = [k.term for k in pc.keywords]
        assert terms.index("Quantization") < terms.index("model")

    def test_max_keywords_cap_and_ordering(self) -> None:
        text = " ".join(f"{a} {b}." for a, b in [("API", "GPU"), ("CPU", "RAM"), ("SSD", "HDD"), ("LLM", "NLP")])
        pc = process(P(text + " " + FILLER), config=ProcessorConfig(max_keywords=3))
        assert len(pc.keywords) == 3
        assert [k.score for k in pc.keywords] == sorted((k.score for k in pc.keywords), reverse=True)

    def test_scores_are_positive_and_sources_nonempty(self) -> None:
        pc = process(P("RAG means retrieval augmented generation. The GPU runs the RAG model. More RAG text here."))
        assert pc.keywords and all(k.score > 0 and k.sources and k.count >= 1 for k in pc.keywords)

    def test_empty_prose_gives_no_keywords(self) -> None:
        assert not keywords_of(C("x = 1"))

    def test_extract_keywords_direct_call(self) -> None:
        items = [cp._make_item(0, P("The API returns JSON. The API is fast."))]
        result = extract_keywords(items, [], [], ProcessorConfig())
        assert any(k.term == "API" for k in result)


# ============================================================================= statements
class TestStatements:
    def statements(self, *blocks: Block, config: Optional[ProcessorConfig] = None) -> list[cp.KeyStatement]:
        return process(*blocks, config=config).statements

    def test_kinds_by_cue(self) -> None:
        sts = self.statements(
            P("Latency refers to the delay between a request and its response time."),
            P("Embeddings of dimension 768 need about 3 GB of memory for ten million vectors."),
            P("The index lives in memory because nearest neighbour search is fast there."),
            P("Teams should always evaluate retrieval separately from generation in production."),
            P("For example, a support bot answers questions about refund policies each day."),
        )
        kinds = {s.kind for s in sts}
        assert {StatementKind.DEFINITION, StatementKind.FACT, StatementKind.EXPLANATION,
                StatementKind.RECOMMENDATION} <= kinds

    def test_statements_are_verbatim_sentences(self) -> None:
        article = art(P("The index lives in memory because search must be fast. Other filler words are here today."),
                      P(FILLER))
        pc = process_article(article)
        source = " ".join(b.plain_text for b in article.blocks)
        assert pc.statements
        for s in pc.statements:
            assert s.text in source and s.provenance is Provenance.SOURCE and s.reasons

    def test_conclusion_section_sentences_are_flagged(self) -> None:
        sts = self.statements(H("Conclusion"), P("Everything here comes down to retrieval quality in the end, honestly."))
        assert sts and sts[0].kind is StatementKind.CONCLUSION and "conclusion_section" in sts[0].reasons

    def test_conclusion_phrase_outside_conclusion_heading(self) -> None:
        sts = self.statements(H("Body"), P("If you remember one idea from this article, let it be quality first."))
        assert sts and sts[0].kind is StatementKind.CONCLUSION

    def test_plain_sentences_without_cues_are_skipped(self) -> None:
        assert not self.statements(P("The quick brown fox jumps over the lazy dog near the river."))

    def test_questions_and_extreme_lengths_are_skipped(self) -> None:
        sts = self.statements(
            P("Why does the index live in memory because search must be fast enough?"),
            P("Because fast."),
            P("The index lives in memory because " + "search must be fast " * 20 + "enough."),
        )
        assert not sts

    def test_code_and_tables_are_never_statements(self) -> None:
        assert not self.statements(C("# because the index must be fast in 2020 for all users here"),
                                   T(("because search must be fast enough here", "100 ms")))

    def test_returned_in_document_order(self) -> None:
        sts = self.statements(
            P("The index lives in memory because search must be fast enough."),
            P("Teams should evaluate retrieval separately from generation always."),
            P("According to the 2020 paper, performance improved by 15 percent overall."),
        )
        assert [s.source_index for s in sts] == sorted(s.source_index for s in sts)

    def test_max_statements_keeps_highest_scores(self) -> None:
        blocks = [P(f"{F(str(i))[:-1]} because it matters here.") for i in range(12)]
        blocks.append(P("In conclusion, the key idea is that retrieval quality limits answer quality overall."))
        everything = self.statements(*blocks, config=ProcessorConfig(max_statements=100))
        capped = self.statements(*blocks, config=ProcessorConfig(max_statements=2))
        assert len(everything) > 2 and len(capped) == 2
        best = sorted(everything, key=lambda s: -s.score)[:2]
        assert {s.source_index for s in capped} == {s.source_index for s in best}
        assert min(s.score for s in capped) >= max(s.score for s in everything if s not in capped)

    def test_threshold_is_configurable(self) -> None:
        blocks = [P(FILLER), P("For example, a support bot answers questions about refund policies each day.")]
        assert not self.statements(*blocks)  # a lone example cue (1.0) is below the default 1.5
        assert self.statements(*blocks, config=ProcessorConfig(min_statement_score=0.5))

    def test_low_confidence_definitions_are_not_promoted(self) -> None:
        sts = self.statements(P("Convert each chunk into an embedding, a list of numbers that captures meaning."))
        assert not any(s.kind is StatementKind.DEFINITION for s in sts)

    def test_definition_in_one_list_item_does_not_taint_other_items(self) -> None:
        """Regression: every list item has a 'sentence 0', so position alone was ambiguous."""
        lst = L("Software development engineers (SDEs) and managers must be close to customers.",
                "The rest of the company, and other startups, could follow soon after that.",
                "Teams should evaluate retrieval separately from generation in production.")
        pc = process(P(FILLER), lst)
        defs = [d for d in pc.definitions if d.list_item_index is not None]
        assert defs and all(d.list_item_index == 0 for d in defs)
        by_item = {s.list_item_index: s.kind for s in pc.statements}
        assert by_item.get(1) is None  # no cue in the second item, so it is not a statement at all
        assert by_item.get(2) is StatementKind.RECOMMENDATION
        assert all(s.kind is not StatementKind.DEFINITION for s in pc.statements if s.list_item_index != 0)

    def test_colon_definition_in_list_records_its_item_index(self) -> None:
        pc = process(P(FILLER), L("Alpha beta gamma delta epsilon.", "Learning rate: the step size taken"))
        d = pc.get_definition("Learning rate")
        assert d is not None and d.list_item_index == 1 and d.sentence_index == 0

    def test_list_item_sentences_can_be_statements(self) -> None:
        sts = self.statements(L("The index lives in memory because search must be fast enough."))
        assert sts and sts[0].sentence_index == 0


# ============================================================================= technical preservation
class TestTechnicalPreservation:
    CODE = "def retrieve(question_vec, chunks, k=3):\n    scored = [(cosine(question_vec, c.vec), c) for c in chunks]\n\treturn scored[:k]"

    def test_code_block_is_preserved_exactly(self) -> None:
        pc = process(P(FILLER), C(self.CODE, "python"))
        code = next(i for i in pc.iter_items() if i.kind is BlockKind.CODE)
        assert code.block.text == self.CODE and code.block.language == "python"

    def test_table_list_quote_preserved_with_structure(self) -> None:
        table = T(("Size", "Strength"), ("Small (100 words)", "Precise"), ("Large (800 words)", "Rich context"))
        lst = L("a one", "b two", "c three", ordered=True, depths=[0, 1, 0])
        pc = process(P(FILLER), table, lst, Q("Measure retrieval quality first."))
        got = {i.kind: i.block for i in pc.iter_items()}
        assert got[BlockKind.TABLE].rows == table.rows and got[BlockKind.TABLE].has_header
        assert got[BlockKind.LIST].ordered and [i.depth for i in got[BlockKind.LIST].items] == [0, 1, 0]
        assert got[BlockKind.QUOTE].text == "Measure retrieval quality first."

    def test_numbers_units_and_identifiers_survive(self) -> None:
        text = "Set max_tokens=512 and temperature=0.2 on the v2.1 API; ten million 768-dimension vectors need 3.5 GB."
        pc = process(P(text))
        assert pc.sections[0].items[0].text == text

    def test_end_to_end_no_number_is_lost_from_the_free_fixture(self) -> None:
        article = SubstackExtractor().extract_from_html(load("substack_free.html"))
        pc = process_article(article)
        import re
        src_numbers = set(re.findall(r"\d+", article.text))
        out_numbers = set(re.findall(r"\d+", " ".join(i.text for i in pc.iter_items())))
        assert src_numbers <= out_numbers

    def test_nothing_removed_from_a_clean_article(self) -> None:
        article = SubstackExtractor().extract_from_html(load("substack_free.html"))
        pc = process_article(article)
        assert pc.removed == [] and pc.stats.removed_duplicates == 0

    def test_a_clean_article_loses_no_words(self) -> None:
        article = SubstackExtractor().extract_from_html(load("substack_free.html"))
        pc = process_article(article)
        assert pc.stats.words_out == pc.stats.words_in  # cleaning only touches whitespace/spacing


# ============================================================================= status preservation
class TestStatusPreservation:
    def test_partial_status_reasons_and_warnings_preserved(self) -> None:
        article = SubstackExtractor().extract_from_html(load("substack_paywalled.html"))
        pc = process_article(article)
        assert pc.is_partial and not pc.is_complete and pc.completeness is Completeness.PARTIAL
        assert list(pc.completeness_reasons) == article.completeness_reasons
        assert any("Paywall detected" in r for r in pc.completeness_reasons)
        assert list(pc.warnings) == article.warnings

    def test_complete_status_preserved(self) -> None:
        article = SubstackExtractor().extract_from_html(load("substack_free.html"))
        pc = process_article(article)
        assert pc.is_complete and pc.completeness_reasons == ()

    def test_unverified_text_status_preserved(self) -> None:
        article = SubstackExtractor().extract_from_text(load("user_notes.txt"))
        pc = process_article(article)
        assert pc.completeness is Completeness.UNVERIFIED and not pc.is_complete and not pc.is_partial
        assert pc.source_kind == "user_text"

    def test_metadata_copied(self) -> None:
        article = SubstackExtractor().extract_from_html(load("substack_free.html"), url="https://a.substack.com/p/x")
        pc = process_article(article)
        assert (pc.title, pc.author, pc.published, pc.url) == (article.title, article.author, article.published, article.url)
        assert pc.extractor == article.extractor and pc.site_name == article.site_name

    def test_processing_warnings_are_separate_and_additive(self) -> None:
        article = art(P(FILLER), warnings=["from extraction"])
        pc = process_article(article)
        assert pc.warnings == ("from extraction",)
        assert pc.processing_warnings and pc.all_warnings[0] == "from extraction"
        assert article.warnings == ["from extraction"]  # the article is untouched

    def test_partial_status_is_not_upgraded_by_processing(self) -> None:
        pc = process(P(FILLER), completeness=Completeness.PARTIAL, completeness_reasons=["paywall"])
        assert pc.completeness is Completeness.PARTIAL and pc.completeness_reasons == ("paywall",)


# ============================================================================= empty / malformed input
class TestInvalidInput:
    @pytest.mark.parametrize("bad", [None, "text", 42, [], {"blocks": []}, object()])
    def test_non_article_rejected(self, bad: object) -> None:
        with pytest.raises(InvalidArticleError):
            process_article(bad)  # type: ignore[arg-type]

    def test_blocks_must_be_block_objects(self) -> None:
        bad = Article(title="t", blocks=["a string"], completeness=Completeness.COMPLETE)  # type: ignore[list-item]
        with pytest.raises(InvalidArticleError, match=r"blocks\[0\]"):
            process_article(bad)

    def test_blocks_must_be_a_sequence(self) -> None:
        bad = Article(title="t", blocks="nope", completeness=Completeness.COMPLETE)  # type: ignore[arg-type]
        with pytest.raises(InvalidArticleError):
            process_article(bad)

    def test_no_blocks(self) -> None:
        with pytest.raises(EmptyContentError):
            process_article(art())

    @pytest.mark.parametrize("blocks", [
        [P("   "), P("\u200b"), L(), T()],
        [H("Only a heading")],
        [H("A"), H("B")],
        [P("Subscribe now"), P("Thanks for reading! Subscribe for free.")],
    ])
    def test_nothing_left_after_cleaning(self, blocks: list[Block]) -> None:
        with pytest.raises(EmptyContentError):
            process_article(art(*blocks))

    def test_degenerate_blocks_do_not_crash(self) -> None:
        pc = process(P("word"), L("x"), T(("a",)), C("x"), Q("q"), H("h"))
        assert pc.stats.items_out >= 5

    def test_very_long_paragraph(self) -> None:
        pc = process(P(("The index lives in memory because search must be fast. " * 3000)))
        assert pc.stats.words_out > 20000

    def test_analysis_failure_degrades_gracefully(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*a: object, **k: object) -> None:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(cp, "extract_keywords", boom)
        pc = process(H("A"), P(FILLER))
        assert pc.keywords == [] and len(pc.sections) == 1
        assert any("keyword extraction step failed" in w and "kaboom" in w for w in pc.processing_warnings)

    def test_definition_failure_does_not_block_other_steps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cp, "extract_definitions", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
        pc = process(H("A"), P("The index lives in memory because search must be fast enough here."))
        assert pc.definitions == [] and pc.statements and pc.keywords is not None

    @pytest.mark.parametrize("kwargs", [
        {"near_duplicate_threshold": 0.2}, {"near_duplicate_threshold": 1.5}, {"max_keywords": 0},
        {"max_statements": 0}, {"min_keyword_count": 0}, {"min_duplicate_words": 0},
    ])
    def test_invalid_config(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            ProcessorConfig(**kwargs)


# ============================================================================= unicode
TELUGU = ("తెలుగు భాష ఒక ద్రావిడ భాష. ఇది ఆంధ్ర ప్రదేశ్ మరియు తెలంగాణ రాష్ట్రాలలో మాట్లాడబడుతుంది. "
          "ఈ వ్యాసంలో మనం RAG గురించి నేర్చుకుంటాము.")


class TestUnicode:
    def test_telugu_article_processes_and_text_is_intact(self) -> None:
        pc = process(H("పరిచయం"), P(TELUGU), P(TELUGU + " రెండవ భాగం."))
        assert pc.sections[0].heading == "పరిచయం"
        assert pc.sections[0].items[0].text == TELUGU
        assert all(unicodedata.is_normalized("NFC", i.text) for i in pc.iter_items())

    def test_telugu_sentences_split(self) -> None:
        pc = process(P(TELUGU))
        assert len(pc.sections[0].items[0].sentences) == 3

    def test_english_terms_inside_telugu_are_found(self) -> None:
        pc = process(P(TELUGU), P("RAG అంటే retrieval augmented generation. ఇక్కడ RAG ఉపయోగిస్తారు."))
        assert "RAG" in {k.term for k in pc.keywords}

    def test_telugu_function_words_do_not_become_keywords(self) -> None:
        pc = process(P(TELUGU * 5), P(TELUGU * 5 + " మరియు మరియు మరియు మరియు"))
        assert not [k for k in pc.keywords if any("\u0c00" <= c <= "\u0c7f" for c in k.term) and "heading" not in k.sources]

    def test_telugu_heading_is_a_keyword_and_stays_whole(self) -> None:
        pc = process(H("తెలుగు భాష"), P(TELUGU))
        kw = pc.get_keyword("తెలుగు భాష")
        assert kw is not None and "heading" in kw.sources and kw.count >= 1

    def test_zwnj_survives_the_whole_pipeline(self) -> None:
        text = "\u0c15\u0c4d\u200c\u0c37 \u0c15\u0c4d\u200d\u0c37 " + FILLER
        pc = process(P(text))
        assert pc.sections[0].items[0].text == text

    def test_mixed_scripts_and_emoji(self) -> None:
        text = "Café naïve 🚀 Привет مرحبا 你好 — the API handles these strings correctly always."
        pc = process(P(text))
        assert pc.sections[0].items[0].text == text and "API" in {k.term for k in pc.keywords}

    def test_decomposed_input_is_normalised(self) -> None:
        pc = process(P("cafe\u0301 society " + FILLER))
        assert pc.sections[0].items[0].text.startswith("caf\u00e9 society")

    def test_non_latin_duplicates_are_detected(self) -> None:
        para = ("ఈ పేరా చాలా పొడవుగా ఉంది మరియు ఇది రెండు సార్లు పునరావృతం అవుతుంది ఎందుకంటే "
                "ఇది పరీక్ష కోసం ఉద్దేశించబడింది మరియు ఇది ఒకే విధంగా ఉంటుంది.")
        pc = process(P(para), P("మధ్యలో వేరే వచనం ఉంది మరియు అది చాలా భిన్నమైనది ఈసారి."), P(para))
        assert pc.stats.removed_duplicates == 1

    def test_telugu_boilerplate_is_not_misdetected(self) -> None:
        assert not is_boilerplate(TELUGU)


# ============================================================================= model helpers
class TestModel:
    def test_annotations_default_to_ai_generated_and_can_be_attached(self) -> None:
        pc = process(H("A"), P(FILLER))
        note = Annotation("An analogy goes here", kind="analogy", source_indices=(1,))
        pc.sections[0].annotations.append(note)
        assert note.provenance is Provenance.AI_GENERATED
        assert pc.sections[0].annotations == [note]

    def test_media_and_footnote_fields_exist_and_are_empty(self) -> None:
        item = next(iter(process(P(FILLER)).iter_items()))
        assert item.images == () and item.footnotes == ()
        assert cp.ImageRef("https://x/y.png", alt="a", caption="c").caption == "c"
        assert cp.FootnoteRef("1", "text").marker == "1"

    def test_source_derived_objects_are_marked_source(self) -> None:
        pc = process(P("Latency refers to the delay between request and response in the system."))
        assert all(d.provenance is Provenance.SOURCE for d in pc.definitions)
        assert all(s.provenance is Provenance.SOURCE for s in pc.statements)

    def test_outline_lists_sections(self) -> None:
        pc = process(P("Pre " + FILLER), H("A", 2), P(F("a")), H("B", 3), C("x = 1"))
        out = pc.outline()
        assert "(content before first heading)" in out and "[s2] A" in out and "  [s3] B - 1 code" in out

    def test_to_dict_is_json_serialisable(self) -> None:
        article = SubstackExtractor().extract_from_html(load("substack_free.html"))
        data = process_article(article).to_dict()
        text = json.dumps(data, ensure_ascii=False)
        assert json.loads(text)["completeness"] == "complete"
        assert data["sections"] and data["definitions"] and data["keywords"]

    def test_content_item_properties(self) -> None:
        item = ContentItem(0, P("a b c"))
        assert item.kind is BlockKind.PARAGRAPH and item.text == "a b c" and item.word_count == 3

    def test_processor_is_reusable_and_deterministic(self) -> None:
        article = SubstackExtractor().extract_from_html(load("substack_free.html"))
        proc = ContentProcessor()
        a, b = proc.process(article), proc.process(article)
        assert a.to_dict() == b.to_dict()


# ============================================================================= end to end
@pytest.fixture(scope="module")
def pc() -> cp.ProcessedContent:
    return process_article(SubstackExtractor().extract_from_html(load("substack_free.html")))


class TestEndToEnd:
    def test_section_tree_matches_the_article_headings(self, pc: cp.ProcessedContent) -> None:
        assert [s.heading for s in pc.sections if not s.is_preamble] == [
            "Why retrieval matters", "How the pipeline works", "Common failure modes", "Conclusion"]
        pipeline = next(s for s in pc.sections if s.heading == "How the pipeline works")
        assert [c.heading for c in pipeline.children] == ["A minimal example", "Choosing a chunk size"]

    def test_key_terms_found(self, pc: cp.ProcessedContent) -> None:
        terms = {k.term for k in pc.keywords}
        assert {"RAG", "Retrieval-Augmented Generation", "Chunk size", "retrieve"} <= terms

    def test_rag_definition_is_verbatim_and_linked_to_its_block(self, pc: cp.ProcessedContent) -> None:
        d = pc.get_definition("Retrieval-Augmented Generation")
        assert d is not None and "RAG" in d.aliases and d.confidence is Confidence.HIGH
        assert d.statement in pc.article.blocks[d.source_index].text

    def test_code_table_and_lists_are_in_the_right_sections(self, pc: cp.ProcessedContent) -> None:
        example = next(s for s in pc.iter_sections() if s.heading == "A minimal example")
        assert [i.kind for i in example.items] == [BlockKind.PARAGRAPH, BlockKind.CODE]
        chunk = next(s for s in pc.iter_sections() if s.heading == "Choosing a chunk size")
        assert BlockKind.TABLE in [i.kind for i in chunk.items]

    def test_conclusion_statement_found(self, pc: cp.ProcessedContent) -> None:
        assert any(s.kind is StatementKind.CONCLUSION and "quality of the answer" in s.text for s in pc.statements)

    def test_text_input_pipeline(self) -> None:
        pc = process_article(SubstackExtractor().extract_from_text(load("user_notes.txt")))
        terms = {d.term for d in pc.definitions}
        assert {"Gradient descent", "Loss function", "Learning rate", "Epoch", "Batch"} <= terms

    def test_custom_domain_fixture(self) -> None:
        pc = process_article(SubstackExtractor().extract_from_html(load("substack_custom_domain.html")))
        assert pc.is_complete and any(s.heading == "Quantization in one paragraph" for s in pc.iter_sections())
        assert pc.get_keyword("Quantization") is not None

    def test_repeated_boilerplate_and_duplicates_from_a_noisy_article(self) -> None:
        blocks = [H("Intro"), P(LONG_A), P("Thanks for reading! Subscribe for free."), H("Body"), P(LONG_A),
                  P("Share"), P("Distinct closing paragraph with enough words to be kept as real prose.")]
        pc = process(*blocks)
        reasons = sorted(r.reason.value for r in pc.removed)
        assert reasons == ["boilerplate", "boilerplate", "duplicate_exact"]
        assert [i.source_index for i in pc.iter_items()] == [1, 6]
