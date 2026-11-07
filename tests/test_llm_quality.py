"""Regression tests for problems found in the first REAL qwen3.5:4b run (not visible with idealised mocks).

Found with the real model:
* it wrote 'Not discussed' as a practical significance (my prompt asked for it) -> repeated filler
* its 'analogies' were fragments copied from the article ('A trade-off'), not analogies
* two of its questions asked the same thing in different words
"""
from __future__ import annotations

import json
import socket
from typing import Any

import pytest

from src.backends.base import GenerationParams, SectionPacket, TermPacket
from src.backends.llm import SCHEMAS, copies_source, is_non_answer, is_substantive, source_text
from src.notes_generator import NotesConfig, NotesGenerator, _near_duplicate, validate_notes
from src.notes_model import NoteProvenance, QuestionKind
from tests.helpers import C, F, H, P, ScriptedLLM, content_of, fixture_content, payload_of

PARAMS = GenerationParams()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))


@pytest.fixture(scope="module")
def free():
    return fixture_content("substack_free.html")


def terms(entries: list[dict[str, str]]) -> str:
    return json.dumps({"terms": entries})


def examples(entries: list[dict[str, str]]) -> str:
    return json.dumps({"examples": entries})


# ============================================================================ helper predicates
class TestNonAnswers:
    @pytest.mark.parametrize("text", [
        "Not discussed", "not discussed.", "Not mentioned in the article.", "N/A", "n/a", "None", "None.", "Nothing.",
        "No information", "No further information is given.", "Not explicitly stated", "Not specified in the text.",
        "The article does not discuss this.", "Article does not mention it.", "  Not covered  ", "- Not discussed -",
    ])
    def test_filler_is_detected(self, text: str) -> None:
        assert is_non_answer(text)

    @pytest.mark.parametrize("text", [
        "Not only is retrieval fast, it also keeps private data inside your own infrastructure at all times.",
        "Nothing in the pipeline works without a good chunking strategy, so teams tune it first of all.",
        "None of the retrieved passages were used because the question was answered from memory alone, which is risky.",
        "Chunk size is a trade-off between precision and context for retrieval.",
        "", "Retrieval grounds answers in documents.",
    ])
    def test_real_sentences_are_not_filler(self, text: str) -> None:
        assert not is_non_answer(text)

    def test_substantive_needs_length_and_content(self) -> None:
        assert is_substantive("Chunk size controls how precisely retrieval finds the right passage.")
        assert not is_substantive("A trade-off.") and not is_substantive("Not discussed") and not is_substantive("")
        assert is_substantive("five words are not enough here", 6) and not is_substantive("five words are not enough", 6)
        assert not is_substantive("word " * 5, 6) and is_substantive("word " * 8, 8)

    def test_copies_source_ignores_case_spacing_and_punctuation(self, free) -> None:
        source = source_text(free)
        assert copies_source("Chunk size is a TRADE-off.", source)
        assert copies_source("a model has a fixed   knowledge cutoff and a limited context window", source)
        assert not copies_source("Think of chunk size like cutting a book into index cards.", source)


# ============================================================================ keyword explanations
class TestKeywordExplanationFiltering:
    GOOD = "Chunks are the smaller pieces of text that a long document is split into before indexing."

    def ask(self, free, entries: list[dict[str, str]]):
        llm = ScriptedLLM({"keyword explanations": terms(entries)})
        return llm, llm.explain_keywords(free, [TermPacket("chunk", None)], PARAMS)

    def test_the_real_failure_not_discussed_is_dropped_silently(self, free) -> None:
        llm, out = self.ask(free, [{"term": "chunk", "explanation": self.GOOD, "significance": "Not discussed"}])
        assert [t.text for t in out["chunk"].explanation] == [self.GOOD] and out["chunk"].significance == ()
        # an expected, harmless omission: not a warning (a "Review needed" notice about the kept explanation's
        # partly supported paraphrase, added in Phase 8.1 / D-7, is a different matter and is allowed)
        assert [w for w in llm.drain_warnings() if "Review needed" not in w] == []

    @pytest.mark.parametrize("long_filler", [
        "The article does not discuss the practical significance of this term.",
        "Not explicitly stated anywhere in the article text provided here.",
        "No further information is given about this in the article itself.",
    ])
    def test_filler_long_enough_to_pass_the_length_check_is_still_dropped(self, free, long_filler: str) -> None:
        assert len(long_filler.split()) >= 8 and not is_substantive(long_filler)
        _, out = self.ask(free, [{"term": "chunk", "explanation": self.GOOD, "significance": long_filler}])
        assert out["chunk"].significance == () and len(out["chunk"].explanation) == 1

    @pytest.mark.parametrize("filler", ["", "Not mentioned in the article.", "A trade-off.", "N/A", "Short reply here."])
    def test_filler_and_fragments_never_become_items(self, free, filler: str) -> None:
        _, out = self.ask(free, [{"term": "chunk", "explanation": filler, "significance": filler}])
        assert out["chunk"].explanation == () and out["chunk"].significance == ()

    def test_text_copied_from_the_article_is_not_labelled_ai_generated(self, free) -> None:
        copied = "A model has a fixed knowledge cutoff and a limited context window."
        _, out = self.ask(free, [{"term": "chunk", "explanation": copied, "significance": self.GOOD}])
        assert out["chunk"].explanation == ()  # it is the article's text, so it must not be passed off as new AI text
        assert len(out["chunk"].significance) == 1

    def test_real_content_is_kept_and_labelled(self, free) -> None:
        _, out = self.ask(free, [{"term": "chunk", "explanation": self.GOOD,
                                  "significance": "Choosing the size well decides whether retrieval finds the right passage."}])
        assert len(out["chunk"].explanation) == 1 and len(out["chunk"].significance) == 1
        assert out["chunk"].explanation[0].provenance is NoteProvenance.AI_GENERATED

    def test_the_prompt_no_longer_asks_for_filler(self, free) -> None:
        llm, _ = self.ask(free, [])
        prompt = llm.calls[0][2]
        assert 'empty string ""' in prompt and 'never write "not discussed"' in prompt
        assert "write that it is not discussed" not in prompt


# ============================================================================ analogies
class TestAnalogyFiltering:
    PACKET = TermPacket("chunk", "a piece of a document")

    def make(self, free, entries: list[dict[str, str]], packets=None):
        llm = ScriptedLLM({"analogies": examples(entries)})
        return llm, llm.create_analogies(free, packets or [self.PACKET], PARAMS)

    def test_a_real_analogy_and_application_are_kept(self, free) -> None:
        _, out = self.make(free, [
            {"concept": "chunk", "kind": "analogy", "text": "Think of a chunk like one index card cut from a thick textbook."},
            {"concept": "chunk", "kind": "application", "text": "A law firm can split its policies into chunks so staff find answers faster."}])
        assert [(e.kind, e.provenance) for e in out] == [("analogy", NoteProvenance.AI_GENERATED)] * 1 + [("application", NoteProvenance.AI_GENERATED)]

    @pytest.mark.parametrize("text", [
        "A trade-off", "Gives a model access to external documents", "Chunks are pieces of text used here.",
        "Chunk size is a trade-off.", "Like a card.", "",
    ])
    def test_the_real_failures_are_discarded(self, free, text: str) -> None:
        llm, out = self.make(free, [{"concept": "chunk", "kind": "analogy", "text": text}])
        assert out == []

    def test_an_analogy_must_actually_compare(self, free) -> None:
        _, out = self.make(free, [{"concept": "chunk", "kind": "analogy", "text": "Chunks are smaller sections of a larger document in storage."}])
        assert out == []

    def test_an_application_need_not_use_comparison_words(self, free) -> None:
        _, out = self.make(free, [{"concept": "chunk", "kind": "application", "text": "A hospital could split its guidelines into chunks for quick clinical lookups."}])
        assert len(out) == 1

    def test_copying_the_article_is_discarded_even_if_long(self, free) -> None:
        copied = "A model has a fixed knowledge cutoff and a limited context window."
        _, out = self.make(free, [{"concept": "chunk", "kind": "application", "text": copied}])
        assert out == []

    def test_discards_are_reported_once(self, free) -> None:
        llm, _ = self.make(free, [{"concept": "chunk", "kind": "analogy", "text": "Short."},
                                  {"concept": "chunk", "kind": "analogy", "text": "Also short here."}])
        warnings = llm.drain_warnings()
        assert len(warnings) == 1 and "2 AI-generated analogy/application item(s) were discarded" in warnings[0]

    def test_unknown_concepts_are_ignored_not_counted_as_discards(self, free) -> None:
        llm, out = self.make(free, [{"concept": "stranger", "kind": "analogy", "text": "Think of a stranger like an unexpected guest at dinner."}])
        assert out == [] and llm.drain_warnings() == []

    def test_a_small_models_output_budget_is_respected_with_at_most_four_terms_per_call(self, free) -> None:
        packets = [TermPacket(f"term{i}", f"definition {i}") for i in range(10)]
        llm = ScriptedLLM({"analogies": lambda user: examples([])})
        llm.create_analogies(free, packets, PARAMS)
        assert [len(payload_of(c[2])) for c in llm.calls] == [4, 4, 2]

    def test_the_prompt_is_explicit_about_what_an_analogy_is(self, free) -> None:
        llm, _ = self.make(free, [])
        prompt = llm.calls[0][2]
        for phrase in ("Think of", "everyday comparison", "do not include numbers", "do not repeat the sentences"):
            assert phrase in prompt

    def test_invented_numbers_in_an_analogy_are_still_rejected(self, free) -> None:
        _, out = self.make(free, [{"concept": "chunk", "kind": "analogy", "text": "Think of a chunk like 500 index cards stacked in a box."}])
        assert out == []


class TestSectionExplanationFiltering:
    def test_short_or_filler_section_explanations_are_dropped(self, free) -> None:
        reply = json.dumps({"sections": [{"id": "s1", "explanation": "Covers retrieval."},
                                         {"id": "s2", "explanation": "Not discussed in the article at all here."},
                                         {"id": "s3", "explanation": "This section walks through why retrieval matters for models "
                                                                     "that cannot know recent facts, and how fetching documents helps."}]})
        packets = [SectionPacket(f"s{i}", f"H{i}", (f"H{i}",), "text " * 30, (i,)) for i in (1, 2, 3)]
        out = ScriptedLLM({"section explanations": reply}).explain_sections(free, packets, PARAMS)
        assert list(out) == ["s3"]


# ============================================================================ the real bug, end to end
class TestNoRepeatedFillerInTheNotes:
    def test_chapter3_has_no_filler_and_still_validates(self, free) -> None:
        def reply(user: str) -> str:
            return terms([{"term": t["term"], "explanation": "Not mentioned in the article.", "significance": "Not discussed"}
                          for t in payload_of(user)])

        notes = NotesGenerator(ScriptedLLM({"keyword explanations": reply}), NotesConfig()).generate(free)
        every = [i.text for i in notes.chapter(3).items()]
        assert not any("Not discussed" in t or "Not mentioned" in t for t in every)
        assert validate_notes(notes) == []  # this failed with 'chapter 3 repeats text' in the real run

    def test_the_other_ai_parts_are_unaffected(self, free) -> None:
        notes = NotesGenerator(ScriptedLLM(), NotesConfig()).generate(free)
        assert any(i.is_ai_generated for i in notes.chapter(2).items())
        assert any(i.is_ai_generated for i in notes.chapter(4).items())
        assert validate_notes(notes) == []


# ============================================================================ near-duplicate questions
class TestNearDuplicateQuestions:
    def toks(self, s: str) -> frozenset[str]:
        return frozenset(s.lower().replace("?", "").split())

    def test_the_real_pair_is_a_duplicate(self) -> None:
        a = self.toks("What are the three main benefits of using Retrieval-Augmented Generation (RAG)?")
        b = self.toks("What are the three main benefits of using Retrieval-Augmented Generation (RAG) compared to relying "
                      "solely on a model's training data?")
        assert _near_duplicate(a, b) and _near_duplicate(b, a)

    def test_a_single_differing_word_is_a_different_question(self) -> None:
        a = self.toks("According to the table what is the weakness of a large chunk size when retrieving passages?")
        b = self.toks("According to the table what is the weakness of a small chunk size when retrieving passages?")
        assert not _near_duplicate(a, b)

    def test_nearly_identical_wording_is_a_duplicate(self) -> None:
        words = "what does the article say about how chunk size affects the precision and the context of retrieval results".split()
        a, b = frozenset(words), frozenset(words + ["overall"])
        assert _near_duplicate(a, b)

    def test_unrelated_questions_are_not(self) -> None:
        assert not _near_duplicate(self.toks("What is a vector database used for in retrieval systems?"),
                                   self.toks("Why does chunk size matter when splitting long documents into passages?"))

    def test_very_short_questions_are_never_compared(self) -> None:
        assert not _near_duplicate(self.toks("What is RAG?"), self.toks("What is RAG for?"))

    def test_the_generator_drops_the_extended_duplicate_and_keeps_the_rest(self, free) -> None:
        short = {"questions": [
            {"question": "What are the three main benefits of using Retrieval-Augmented Generation (RAG)?", "answer": "A, B and C."},
            {"question": "What does cosine similarity measure between a question vector and each chunk vector?", "answer": "Closeness."}]}
        desc = {"questions": [
            {"question": "What are the three main benefits of using Retrieval-Augmented Generation (RAG) compared to relying "
                         "solely on a model's training data?", "answer": "A, B and C."}]}
        llm = ScriptedLLM({"short questions": json.dumps(short), "descriptive questions": json.dumps(desc)})
        notes = NotesGenerator(llm, NotesConfig()).generate(free)
        asked = [q.question for q in notes.chapter(7).questions]
        assert sum("three main benefits" in q for q in asked) == 1
        assert any("cosine similarity" in q for q in asked)
        assert any("nearly the same thing" in w for w in notes.generation_warnings)

    def test_a_dropped_question_leaves_a_visible_shortfall_notice(self, free) -> None:
        """Seen in the real run: the descriptive question was a duplicate, leaving 0 of 1 with no explanation."""
        short = {"questions": [{"question": "What are the three main benefits of using Retrieval-Augmented Generation (RAG)?",
                                "answer": "A, B and C."}]}
        desc = {"questions": [{"question": "What are the three main benefits of using Retrieval-Augmented Generation (RAG) "
                                           "compared to relying solely on a model's training data?", "answer": "A, B and C."}]}
        llm = ScriptedLLM({"short questions": json.dumps(short), "descriptive questions": json.dumps(desc)})
        notes = NotesGenerator(llm, NotesConfig(short_questions=1, descriptive_questions=1, mcq_questions=2)).generate(free)
        notices = [n.text for n in notes.chapter(7).notices]
        assert any("Only 0 of 1 requested descriptive questions" in n for n in notices)
        assert not any("short-answer" in n for n in notices)  # nothing was missing there

    def test_no_notice_when_every_requested_question_is_present(self, free) -> None:
        notes = NotesGenerator(config=NotesConfig()).generate(free)  # local: 10/5/10 all present
        assert not any("requested" in n.text for n in notes.chapter(7).notices)

    def test_no_second_notice_when_the_backend_already_explained_the_shortfall(self) -> None:
        notes = NotesGenerator(config=NotesConfig()).generate(content_of(P("Gradient descent is an optimization algorithm that repeatedly reduces a loss.")))
        shortfalls = [n.text for n in notes.chapter(7).notices if "short-answer" in n.text]
        assert len(shortfalls) == 1

    def test_local_template_questions_are_never_filtered_by_this_rule(self, free) -> None:
        notes = NotesGenerator(config=NotesConfig()).generate(free)  # local backend
        qs = notes.chapter(7).questions
        assert len(qs) == 21 and not any("nearly the same" in w for w in notes.generation_warnings)  # D-4
        assert all(q.question_provenance is NoteProvenance.DERIVED for q in qs)

    def test_distinct_ai_questions_that_share_a_template_are_all_kept(self, free) -> None:
        mcqs = {"questions": [{"question": f"Which option is right for item {i}?",
                               "options": [f"A{i}", f"B{i}", f"C{i}", f"D{i}"], "correct_index": i % 4,
                               "explanation": f"Because option {i} follows from the article."} for i in range(6)]}
        notes = NotesGenerator(ScriptedLLM({"mcq questions": json.dumps(mcqs)}), NotesConfig()).generate(free)
        assert sum(q.kind is QuestionKind.MCQ for q in notes.chapter(7).questions) == 6


# ============================================================================ schemas still match the filters
class TestSchemasUnchangedByTheFilters:
    def test_significance_may_be_empty_in_the_schema(self) -> None:
        item = SCHEMAS["keyword explanations"]["properties"]["terms"]["items"]
        assert "minLength" not in item["properties"]["significance"]  # "" must be allowed, as the prompt tells the model

    def test_code_blocks_survive_when_ai_explains_their_section(self) -> None:
        content = content_of(H("Example"), P(F("intro")), C("def retrieve(q):\n    return q", "python"))
        notes = NotesGenerator(ScriptedLLM(), NotesConfig(detail="Advanced")).generate(content)
        assert any(i.text.startswith("def retrieve") for i in notes.chapter(2).items())
