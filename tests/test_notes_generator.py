"""Phase 5 tests: the seven chapters, questions, provenance, partial articles, fallbacks, edge cases."""
from __future__ import annotations

import dataclasses
import json
import re
import socket
import time
from typing import Any

import pytest

from src.article_extractor import Completeness
from src.backends import BackendSelection, LocalNotesBackend, OpenAIBackend, resolve_backend
from src.backends.base import (
    Availability, BackendError, BackendUnavailableError, QuestionSpec,
)
from src.content_processor import normalize_key
from src.notes_generator import (
    CHAPTER_TITLES, InvalidContentError, NotesConfig, NotesGenerationError, NotesGenerator, _Ledger,
    generate_notes, is_formula, shorten, validate_notes, verify_provenance,
)
from src.notes_model import (
    DetailLevel, ItemKind, Language, NoteItem, NoteProvenance, Notes, QuestionKind,
)
from tests.helpers import (
    C, F, FakeOpenAIClient, H, L, P, ScriptedLLM, T, content_of, fixture_content,
)

ORIG, DER, AI = NoteProvenance.ORIGINAL, NoteProvenance.DERIVED, NoteProvenance.AI_GENERATED
SPEC = QuestionSpec()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*a: Any, **k: Any) -> None:
        raise AssertionError("a real network connection was attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture(scope="module")
def free():
    return fixture_content("substack_free.html")


@pytest.fixture(scope="module")
def free_notes(free) -> Notes:
    return generate_notes(free)


def key(text: str) -> str:
    return normalize_key(text)


def texts(chapter, kinds=(ItemKind.TEXT, ItemKind.DEFINITION, ItemKind.QUOTE)) -> list[str]:
    return [i.text for i in chapter.items() if i.kind in kinds]


# ============================================================================ the seven chapters
class TestSevenChapters:
    def test_all_seven_chapters_in_order_with_the_required_titles(self, free_notes: Notes) -> None:
        assert [c.number for c in free_notes.chapters] == [1, 2, 3, 4, 5, 6, 7]
        assert [c.title for c in free_notes.chapters] == [
            "Article Overview", "Detailed Concept Explanation", "Important Technical Keywords",
            "Real-Life Analogies and Applications", "Key Takeaways", "Quick Revision Notes", "Questions and Answers"]

    def test_every_chapter_has_content(self, free_notes: Notes) -> None:
        for c in free_notes.chapters:
            assert c.word_count > 0 or c.questions, f"chapter {c.number} is empty"

    def test_notes_validate(self, free_notes: Notes) -> None:
        assert validate_notes(free_notes, SPEC) == []

    def test_chapter1_overview(self, free_notes: Notes, free) -> None:
        ch = free_notes.chapter(1)
        assert [s.heading for s in ch.sections] == [
            "Article details", "Introduction and background", "Main objective", "Core problem addressed",
            "Main contributions", "Structure of the article"]
        details = {i.label: i for i in ch.sections[0].items}
        assert details["Original title"].text == free.title and details["Original title"].provenance is ORIG
        assert details["Author"].text == "Priya Raman" and details["Author"].provenance is ORIG
        assert details["Published"].text == "2025-03-14" and details["Published"].provenance is DER
        assert details["Extraction status"].text == "Complete article extracted"
        intro = ch.sections[1].items[0]
        assert intro.provenance is ORIG and intro.text.startswith("Large language models are remarkably fluent")
        structure = ch.sections[5].items[0]
        assert structure.kind is ItemKind.NUMBERED and any("How the pipeline works" in s for s in structure.items)

    def test_chapter1_states_when_the_article_does_not_say(self, free_notes: Notes) -> None:
        objective = free_notes.chapter(1).sections[2]
        assert objective.items[0].kind is ItemKind.NOTICE and "does not state its objective" in objective.items[0].text

    def test_chapter2_follows_the_article_structure_in_order(self, free_notes: Notes) -> None:
        headings = [s.heading for s in free_notes.chapter(2).sections]
        assert headings[:7] == ["Why retrieval matters", "How the pipeline works", "A minimal example",
                                "Choosing a chunk size", "Common failure modes", "Conclusion"][:7] or \
            [h for h in headings if h != "How the concepts relate"] == [
                "Why retrieval matters", "How the pipeline works", "A minimal example", "Choosing a chunk size",
                "Common failure modes", "Conclusion"]
        assert headings[-1] == "How the concepts relate"

    def test_chapter2_keeps_code_tables_lists_and_steps(self, free_notes: Notes) -> None:
        items = list(free_notes.chapter(2).items())
        code = next(i for i in items if i.kind is ItemKind.CODE)
        assert code.language == "python" and code.text.startswith("def retrieve(question_vec, chunks, k=3):")
        table = next(i for i in items if i.kind is ItemKind.TABLE)
        assert table.has_header and table.rows[0] == ("Chunk size", "Strength", "Weakness")
        steps = next(i for i in items if i.kind is ItemKind.NUMBERED)
        assert steps.label == "Step-by-step procedure" and len(steps.items) == 4
        bullets = next(i for i in items if i.kind is ItemKind.BULLETS and i.label is None)
        assert list(bullets.depths) == [0, 0, 1, 0]

    def test_relationships_never_pair_a_term_with_its_own_alias(self, free_notes: Notes) -> None:
        rel = free_notes.chapter(2).sections[-1]
        lines = [line for i in rel.items for line in i.items]
        assert lines and not any("“RAG” and “Retrieval-Augmented Generation”" in x
                                 or "“Retrieval-Augmented Generation” and “RAG”" in x for x in lines)

    def test_chapter2_relationships_section_is_derived(self, free_notes: Notes) -> None:
        rel = free_notes.chapter(2).sections[-1]
        assert rel.heading == "How the concepts relate" and all(i.provenance is DER for i in rel.items)

    def test_chapter3_keywords_with_definitions(self, free_notes: Notes) -> None:
        ch = free_notes.chapter(3)
        sections = {s.heading: s for s in ch.sections}
        rag = sections["Retrieval-Augmented Generation"]
        assert rag.items[0].label == "Also written as" and rag.items[0].text == "RAG"
        defn = next(i for i in rag.items if i.kind is ItemKind.DEFINITION)
        assert defn.provenance is ORIG and defn.label == "Definition (stated in the article)" and "usually shortened to RAG" in defn.text
        assert "RAG" not in sections  # an alias is folded into its full term, not listed twice

    def test_chapter3_marks_inferred_definitions_as_possible(self, free_notes: Notes) -> None:
        emb = next(s for s in free_notes.chapter(3).sections if s.heading == "embedding")
        assert emb.items[0].label.startswith("Possible definition")

    def test_chapter3_has_no_empty_or_filler_entries(self, free_notes: Notes) -> None:
        for s in free_notes.chapter(3).sections:
            assert s.items and not any(i.kind is ItemKind.NOTICE for i in s.items)
        filler = [i for i in free_notes.chapter(3).items() if "does not separately discuss" in i.text]
        assert filler == []

    def test_chapter4_local_mode_adds_no_invented_examples(self, free_notes: Notes) -> None:
        ch = free_notes.chapter(4)
        assert not any(i.is_ai_generated for i in ch.items())
        assert any("cannot create new examples" in i.text for i in ch.items())
        assert all(i.provenance is ORIG for i in ch.sections[0].items if i.kind is ItemKind.TEXT)

    def test_chapter5_groups_takeaways_by_kind(self, free_notes: Notes) -> None:
        sections = {s.heading: s for s in free_notes.chapter(5).sections}
        assert list(sections) == ["Important observations", "Main findings", "Conclusions", "Practical lessons"]
        assert any("quality of the answer" in i.text for i in sections["Conclusions"].items)
        assert all(i.provenance is ORIG for s in sections.values() for i in s.items if i.kind is ItemKind.TEXT)

    def test_chapter6_revision_notes(self, free_notes: Notes) -> None:
        ch = free_notes.chapter(6)
        assert [s.heading for s in ch.sections] == ["Short definitions", "Important points", "Quick revision checklist"]
        checklist = ch.sections[-1].items[0]
        assert checklist.kind is ItemKind.CHECKLIST and any("Retrieval-Augmented Generation" in i for i in checklist.items)
        assert any("code example" in i for i in checklist.items) and any("table" in i for i in checklist.items)
        assert any("steps" in i for i in checklist.items)

    def test_chapter6_includes_formulae_only_when_the_article_has_them(self, free_notes: Notes) -> None:
        assert "Formulae" not in [s.heading for s in free_notes.chapter(6).sections]
        c = content_of(H("Maths"), P("The loss is computed as L = sum(w * x) + b over every training example."), P(F("x")))
        notes = generate_notes(c)
        formulae = next(s for s in notes.chapter(6).sections if s.heading == "Formulae")
        assert "L = sum(w * x) + b" in formulae.items[0].text and formulae.items[0].provenance is ORIG

    def test_chapter7_requested_question_counts(self, free_notes: Notes) -> None:
        qs = free_notes.chapter(7).questions
        assert sum(q.kind is QuestionKind.SHORT for q in qs) == 10
        assert sum(q.kind is QuestionKind.DESCRIPTIVE for q in qs) == 5
        # Phase 8.1 (D-4): a fact is never asked twice; Phase 8.2 (D-10): recognising the practical lessons supports a 6th MCQ (was 5)
        assert sum(q.kind is QuestionKind.MCQ for q in qs) == 6
        assert any("Only 6 of 10 multiple-choice questions" in n.text for n in free_notes.chapter(7).notices)

    def test_title_is_the_article_title(self, free_notes: Notes, free) -> None:
        assert free_notes.title == free.title and free_notes.author == "Priya Raman"
        assert free_notes.url == free.url and free_notes.published == free.published


# ============================================================================ questions
@pytest.fixture(scope="module")
def qs(free_notes: Notes):
    return free_notes.chapter(7).questions


class TestQuestions:
    def mcqs(self, qs):
        return [q for q in qs if q.kind is QuestionKind.MCQ]

    def test_every_mcq_has_four_distinct_options_one_correct_and_an_explanation(self, qs) -> None:
        for q in self.mcqs(qs):
            assert len(q.options) == 4 and len({o.casefold() for o in q.options}) == 4
            assert 0 <= q.correct_index <= 3 and q.options[q.correct_index] == q.answer == q.options["ABCD".index(q.correct_letter)]
            assert q.explanation.strip()

    def test_correct_answer_position_is_spread_over_all_four_letters(self, qs) -> None:
        letters = [q.correct_letter for q in self.mcqs(qs)]
        assert set(letters) == {"A", "B", "C", "D"}
        assert max(letters.count(x) for x in "ABCD") <= 4

    def test_mcq_answers_are_supported_by_the_article(self, qs, free) -> None:
        blob = key(" ".join([free.title] + [i.text for s in free.iter_sections() for i in s.items] +
                            [s.heading or "" for s in free.iter_sections()]))
        for q in self.mcqs(qs):
            assert key(q.answer) in blob, q.answer
            quoted = re.findall(r"“([^”]+)”", q.explanation)
            assert quoted and all(key(x) in blob for x in quoted), q.explanation

    def test_no_alias_of_the_correct_answer_appears_as_a_distractor(self, qs) -> None:
        for q in self.mcqs(qs):
            others = {key(o) for i, o in enumerate(q.options) if i != q.correct_index}
            if key(q.answer) == "rag":
                assert "retrieval augmented generation" not in others
            if key(q.answer) == "retrieval augmented generation":
                assert "rag" not in others

    def test_distractors_are_not_named_in_the_question(self, qs) -> None:
        for q in self.mcqs(qs):
            stem = q.question.casefold()
            for i, o in enumerate(q.options):
                if i != q.correct_index and len(o) > 3 and not o.startswith("RAG"):
                    assert o.casefold() not in stem, (q.question, o)

    def test_no_duplicate_questions(self, qs) -> None:
        stems = [key(q.question) for q in qs]
        assert len(stems) == len(set(stems))

    def test_short_and_descriptive_answers_are_verbatim_article_text(self, free_notes: Notes, free) -> None:
        assert verify_provenance(free_notes, free) == []
        for q in free_notes.chapter(7).questions:
            assert q.answer_provenance is ORIG and q.question_provenance is DER and q.source_indices

    def test_question_types_use_the_articles_own_structures(self, qs) -> None:
        stems = " ".join(q.question for q in qs)
        assert "stand for" in stems and "In the table under" in stems and "code example" in stems
        assert "items listed under" in stems or "items given under" in stems
        code_q = next(q for q in qs if "code example" in q.question)
        assert code_q.answer == "retrieve"

    def test_questions_carry_source_block_indices(self, qs, free) -> None:
        n_blocks = len(free.article.blocks)
        assert all(q.source_indices and all(0 <= i < n_blocks for i in q.source_indices) for q in qs)

    def test_custom_counts(self, free) -> None:
        notes = generate_notes(free, config=NotesConfig(short_questions=3, descriptive_questions=2, mcq_questions=4))
        qs = notes.chapter(7).questions
        assert [sum(q.kind is k for q in qs) for k in QuestionKind] == [3, 2, 4]

    def test_zero_counts(self, free) -> None:
        notes = generate_notes(free, config=NotesConfig(short_questions=0, descriptive_questions=0, mcq_questions=0))
        assert notes.chapter(7).questions == []

    def test_questions_can_be_switched_off(self, free) -> None:
        notes = generate_notes(free, config=NotesConfig(include_questions=False))
        ch = notes.chapter(7)
        assert ch.questions == [] and any("not requested" in n.text for n in ch.notices)

    def test_small_article_reports_a_shortfall_instead_of_inventing_questions(self) -> None:
        notes = generate_notes(content_of(P("Gradient descent is an optimization algorithm that repeatedly reduces a loss.")))
        ch = notes.chapter(7)
        assert len(ch.questions) < 25
        assert any("does not invent questions" in n.text for n in ch.notices)
        assert validate_notes(notes) == []

    def test_text_notes_article_generates_definition_questions(self) -> None:
        notes = generate_notes(fixture_content("user_notes.txt"))
        qs = notes.chapter(7).questions
        assert any(q.question == "According to the article, what is Learning rate?" and q.answer == "the step size" for q in qs)
        assert any("Epoch" in q.question or "Epoch" in q.options for q in qs if q.options) or any("Epoch" in q.question for q in qs)

    def test_generation_is_deterministic(self, free) -> None:
        a, b = generate_notes(free).to_dict(), generate_notes(free).to_dict()
        assert a == b


# ============================================================================ provenance
class TestProvenance:
    @pytest.mark.parametrize("name", ["substack_free.html", "substack_custom_domain.html", "substack_paywalled.html",
                                      "user_notes.txt"])
    def test_original_text_is_really_in_the_article(self, name: str) -> None:
        content = fixture_content(name)
        for detail in DetailLevel:
            notes = generate_notes(content, config=NotesConfig(detail=detail))
            assert verify_provenance(notes, content) == [], (name, detail)

    def test_tampering_with_original_text_is_detected(self, free_notes: Notes, free) -> None:
        notes = dataclasses.replace(free_notes, chapters=[dataclasses.replace(c) for c in free_notes.chapters])
        ch5 = notes.chapter(5)
        sec = next(s for s in ch5.sections if any(i.kind is ItemKind.TEXT for i in s.items))
        sec2 = dataclasses.replace(sec, items=[dataclasses.replace(i, text=i.text + " Studies show 87% improvement.")
                                               if i.kind is ItemKind.TEXT else i for i in sec.items])
        ch5.sections[ch5.sections.index(sec)] = sec2
        problems = verify_provenance(notes, free)
        assert problems and all("not found in article" in p for p in problems)

    def test_a_derived_item_is_never_claimed_original(self, free_notes: Notes) -> None:
        for i in free_notes.all_items():
            if i.label in ("Published", "Source URL", "Extraction status", "Sections in order"):
                assert i.provenance is DER
            if i.kind in (ItemKind.NOTICE, ItemKind.CHECKLIST):
                assert i.provenance is DER

    def test_every_original_item_points_at_article_blocks(self, free_notes: Notes, free) -> None:
        n = len(free.article.blocks)
        for i in free_notes.all_items():
            if i.is_original and i.kind is not ItemKind.NOTICE and i.label not in ("Original title", "Author"):
                assert i.source_indices and all(0 <= x < n for x in i.source_indices), i

    def test_local_mode_uses_no_ai(self, free_notes: Notes) -> None:
        assert not free_notes.used_ai and not free_notes.backend.is_paid and free_notes.backend.is_local
        assert not any(i.is_ai_generated for i in free_notes.all_items())

    def test_ai_output_is_labelled_and_separate(self, free) -> None:
        notes = NotesGenerator(ScriptedLLM(), NotesConfig()).generate(free)
        assert notes.used_ai
        ai_items = [i for i in notes.all_items() if i.is_ai_generated]
        assert ai_items and all(i.label and i.label.startswith(("AI-generated", "Detailed", "Practical")) for i in ai_items)
        for i in notes.chapter(4).sections[1].items:
            if i.kind is ItemKind.TEXT:
                assert i.provenance is AI and i.label.startswith(("AI-generated analogy", "AI-generated application"))
        warning = notes.chapter(4).sections[1].items[0]
        assert warning.kind is ItemKind.NOTICE and warning.level == "warning" and "NOT from the article" in warning.text
        assert any("language model" in n.text for n in notes.chapter(3).notices)

    def test_ai_questions_are_all_marked_ai_generated(self, free) -> None:
        notes = NotesGenerator(ScriptedLLM(), NotesConfig()).generate(free)
        assert all(q.question_provenance is AI and q.answer_provenance is AI for q in notes.chapter(7).questions)

    def test_original_items_still_verify_when_ai_is_mixed_in(self, free) -> None:
        notes = NotesGenerator(ScriptedLLM(), NotesConfig()).generate(free)
        assert verify_provenance(notes, free) == []

    def test_ai_section_explanations_follow_the_original_content(self, free) -> None:
        notes = NotesGenerator(ScriptedLLM(), NotesConfig()).generate(free)
        sec = next(s for s in notes.chapter(2).sections if s.heading == "How the pipeline works")
        assert sec.items[-1].label == "AI-generated explanation" and sec.items[-1].provenance is AI
        assert all(i.provenance is ORIG for i in sec.items[:-1])

    def test_section_explanations_can_be_switched_off(self, free) -> None:
        llm = ScriptedLLM()
        NotesGenerator(llm, NotesConfig(ai_section_explanations=False)).generate(free)
        assert "section explanations" not in [c[0] for c in llm.calls]

    def test_active_backend_is_displayed_in_the_notes(self, free_notes: Notes) -> None:
        banner = free_notes.banners[0].text
        assert banner.startswith("Generated with: Local") and "free" in banner and "no AI model used" in banner
        assert free_notes.backend.name == "Local" and free_notes.requested_backend == "local"


# ============================================================================ partial / unverified articles
@pytest.fixture(scope="module")
def partial():
    content = fixture_content("substack_paywalled.html")
    return content, generate_notes(content)


class TestPartialArticles:
    def test_banner_is_prominent(self, partial) -> None:
        content, notes = partial
        assert content.is_partial and notes.is_partial and notes.completeness is Completeness.PARTIAL
        banner = next(b for b in notes.banners if "PARTIAL" in b.text)
        assert banner.level == "warning" and banner.text.startswith("PARTIAL CONTENT")
        assert "must not be treated as a complete study" in banner.text
        assert "Paywall detected" in banner.text

    def test_every_chapter_carries_its_own_partial_notice(self, partial) -> None:
        _, notes = partial
        for c in notes.chapters:
            assert c.notices and c.notices[0].level == "warning" and "PARTIAL" in c.notices[0].text, c.number

    def test_overview_reports_partial_status(self, partial) -> None:
        _, notes = partial
        status = next(i for i in notes.chapter(1).items() if i.label == "Extraction status")
        assert status.text.startswith("PARTIAL")

    def test_completeness_and_reasons_are_preserved(self, partial) -> None:
        content, notes = partial
        assert notes.completeness_reasons == content.completeness_reasons and notes.completeness_reasons
        assert tuple(notes.source_warnings) == tuple(content.all_warnings)
        assert notes.to_dict()["completeness"] == "partial"

    def test_notes_do_not_claim_coverage_of_inaccessible_content(self, partial) -> None:
        _, notes = partial
        every = " ".join(i.plain_text for i in notes.all_items()).lower()
        for claim in ("full article", "entire article", "the whole article", "comprehensive coverage"):
            assert claim not in every

    def test_paywalled_text_never_enters_the_notes(self, partial) -> None:
        _, notes = partial
        every = " ".join(i.plain_text for i in notes.all_items())
        assert "paid subscribers" not in every

    def test_partial_notes_still_pass_validation_and_provenance(self, partial) -> None:
        content, notes = partial
        assert validate_notes(notes) == [] and verify_provenance(notes, content) == []

    def test_complete_articles_have_no_partial_wording(self, free_notes: Notes) -> None:
        assert not any("PARTIAL" in i.plain_text for i in free_notes.all_items())
        assert free_notes.banners[0].text.startswith("Generated with")

    def test_unverified_text_is_flagged_but_not_called_partial(self) -> None:
        notes = generate_notes(fixture_content("user_notes.txt"))
        assert notes.completeness is Completeness.UNVERIFIED and not notes.is_partial
        assert any("could not be verified" in b.text for b in notes.banners)
        assert all("could not be verified" in c.notices[0].text for c in notes.chapters)

    def test_ai_notes_from_partial_articles_keep_the_warning(self) -> None:
        notes = NotesGenerator(ScriptedLLM(), NotesConfig()).generate(fixture_content("substack_paywalled.html"))
        assert notes.is_partial and all("PARTIAL" in c.notices[0].text for c in notes.chapters)


# ============================================================================ backend fallback behaviour
class UnavailableBackend(LocalNotesBackend):
    def check_available(self) -> Availability:
        return Availability(False, "the model server is down")


class TestBackendFallbacks:
    def test_unavailable_backend_falls_back_to_local_and_says_so(self, free) -> None:
        notes = NotesGenerator(UnavailableBackend()).generate(free)
        assert notes.backend.name == "Local" and not notes.used_ai
        assert "the model server is down" in notes.fallback_reason
        assert any("Requested backend" in b.text and b.level == "warning" for b in notes.banners)
        assert any("Using the local backend instead" in w for w in notes.generation_warnings)

    def test_fallback_can_be_forbidden(self, free) -> None:
        with pytest.raises(BackendUnavailableError, match="server is down"):
            NotesGenerator(UnavailableBackend(), NotesConfig(fallback_to_local=False)).generate(free)

    def test_a_backend_selection_carries_its_fallback_reason_into_the_notes(self, free, monkeypatch) -> None:
        import requests
        monkeypatch.setattr(requests.Session, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("x")))
        selection = resolve_backend("ollama", env={})
        notes = NotesGenerator(selection).generate(free)
        assert notes.requested_backend == "ollama" and "not reachable" in notes.fallback_reason
        assert notes.backend.name == "Local" and not notes.used_ai

    def test_ollama_unavailable_still_produces_complete_local_notes(self, free, monkeypatch) -> None:
        import requests
        monkeypatch.setattr(requests.Session, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("x")))
        notes = generate_notes(free, backend=resolve_backend("ollama", env={}))
        assert validate_notes(notes, SPEC) == [] and len(notes.chapter(7).questions) == 21  # D-4: no repeated facts (8.2/D-10: 21, was 20)

    def test_one_failing_backend_method_degrades_only_that_part(self, free) -> None:
        llm = ScriptedLLM({"analogies": BackendError("model crashed")})
        notes = NotesGenerator(llm).generate(free)
        assert any("could not produce analogies" in w for w in notes.generation_warnings)
        assert any("cannot create new examples" in i.text for i in notes.chapter(4).items())
        assert any(i.is_ai_generated for i in notes.chapter(3).items())  # other AI parts still worked
        assert notes.backend.name == "Scripted"

    def test_garbage_model_output_falls_back_to_local_for_that_part(self, free) -> None:
        llm = ScriptedLLM({"keyword explanations": "I am sorry, I cannot do that."})
        notes = NotesGenerator(llm).generate(free)
        assert any("keyword explanations" in w for w in notes.generation_warnings)
        chunk = next(s for s in notes.chapter(3).sections if s.heading == "Chunk size")
        assert chunk.items and all(not i.is_ai_generated for i in chunk.items)

    def test_invented_statistics_from_a_model_are_rejected_and_reported(self, free) -> None:
        bad = json.dumps({"terms": [{"term": "Chunk size", "explanation": "Experts say 92% of teams tune it.",
                                     "significance": "It matters."}]})
        notes = NotesGenerator(ScriptedLLM({"keyword explanations": bad})).generate(free)
        every = " ".join(i.plain_text for i in notes.all_items())
        assert "92%" not in every
        assert any("92%" in w for w in notes.generation_warnings)

    def test_declined_openai_confirmation_uses_local_and_stops_asking(self, free) -> None:
        asked: list[Any] = []
        client = FakeOpenAIClient()
        backend = OpenAIBackend(api_key="sk-x", model="m", confirm=lambda r: asked.append(r) or False,
                                enabled=True, client=client)
        notes = NotesGenerator(backend).generate(free)
        assert client.calls == [] and len(asked) == 1
        assert notes.backend.name == "Local" and not notes.used_ai and not notes.backend.is_paid
        assert any("declined" in w for w in notes.generation_warnings)
        assert validate_notes(notes, SPEC) == []

    def test_approved_openai_run_is_labelled_paid_and_every_call_was_confirmed(self, free) -> None:
        asked: list[Any] = []
        client = FakeOpenAIClient()
        backend = OpenAIBackend(api_key="sk-x", model="m", confirm=lambda r: asked.append(r) or True,
                                enabled=True, client=client, max_calls=50)
        notes = NotesGenerator(backend).generate(free)
        assert notes.used_ai and notes.backend.is_paid and not notes.backend.is_local
        assert len(asked) == len(client.calls) > 0
        assert "PAID API" in notes.banners[0].text and "external service" in notes.banners[0].text

    def test_openai_is_not_used_unless_passed_in_even_with_a_key_in_the_environment(self, free, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-be-used")
        notes = generate_notes(free)
        assert notes.backend.name == "Local" and notes.requested_backend == "local" and not notes.used_ai

    def test_wrong_argument_types_are_rejected_clearly(self) -> None:
        with pytest.raises(NotesGenerationError, match="backend must be"):
            NotesGenerator("openai")  # type: ignore[arg-type]
        with pytest.raises(NotesGenerationError, match="config must be"):
            NotesGenerator(config={"detail": "Basic"})  # type: ignore[arg-type]

    def test_works_with_no_network_and_no_keys(self, free, monkeypatch) -> None:
        for var in ("OPENAI_API_KEY", "OLLAMA_HOST", "OLLAMA_MODEL", "NOTEFORGE_BACKEND"):
            monkeypatch.delenv(var, raising=False)
        assert generate_notes(free).chapter(7).questions  # the autouse fixture forbids any socket connection


# ============================================================================ empty / malformed input
class TestInvalidInput:
    @pytest.mark.parametrize("bad", [None, "text", 42, {"sections": []}, []])
    def test_non_content_rejected(self, bad: Any) -> None:
        with pytest.raises(InvalidContentError):
            generate_notes(bad)

    def test_content_without_items_rejected(self) -> None:
        content = content_of(P(F("a")))
        content.sections.clear()
        with pytest.raises(InvalidContentError, match="no items"):
            generate_notes(content)

    @pytest.mark.parametrize("kwargs", [
        {"detail": "Expert"}, {"language": "French"}, {"short_questions": -1}, {"descriptive_questions": -2},
        {"mcq_questions": -3}, {"max_keywords": 0},
    ])
    def test_invalid_config(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            NotesConfig(**kwargs)

    def test_config_accepts_strings_case_insensitively(self) -> None:
        cfg = NotesConfig(detail="advanced", language="telugu")
        assert cfg.detail is DetailLevel.ADVANCED and cfg.language is Language.TELUGU

    def test_one_paragraph_article_works(self) -> None:
        notes = generate_notes(content_of(P("A single short paragraph of plain prose appears here in the article.")))
        assert [c.number for c in notes.chapter and notes.chapters] == [1, 2, 3, 4, 5, 6, 7]
        assert validate_notes(notes) == []

    def test_article_with_no_headings(self) -> None:
        notes = generate_notes(content_of(P(F("a")), P(F("b")), P(F("c"))))
        assert validate_notes(notes) == []

    def test_code_only_article(self) -> None:
        notes = generate_notes(content_of(C("def only_code():\n    return 1", "python")))
        assert any(i.kind is ItemKind.CODE for i in notes.chapter(2).items())
        assert any("no technical keywords" in n.text.lower() or "no definitions" in n.text.lower()
                   for n in notes.chapter(3).notices) or notes.chapter(3).sections

    def test_table_only_article(self) -> None:
        notes = generate_notes(content_of(T(("A", "B"), ("1", "2"), ("3", "4"))))
        assert any(i.kind is ItemKind.TABLE for i in notes.chapter(2).items())

    def test_notes_helpers_on_missing_chapter(self, free_notes: Notes) -> None:
        with pytest.raises(KeyError):
            free_notes.chapter(99)


# ============================================================================ long articles
def long_content(n: int):
    blocks = []
    for i in range(n):
        blocks += [H(f"Section {i}"), P(F(f"para-{i}") + " " + F(f"more-{i}")), L(F(f"a{i}"), F(f"b{i}"), F(f"c{i}"))]
    return content_of(*blocks)


class TestLongArticles:
    def test_local_generation_scales(self) -> None:
        content = long_content(150)
        start = time.perf_counter()
        notes = generate_notes(content)
        assert time.perf_counter() - start < 30
        assert len([s for s in notes.chapter(2).sections if s.heading.startswith("Section")]) == 150
        assert validate_notes(notes, SPEC) == [] and verify_provenance(notes, content) == []

    def test_chapter3_is_capped_by_detail_level(self, free) -> None:
        counts = {d: len(generate_notes(free, config=NotesConfig(detail=d)).chapter(3).sections) for d in DetailLevel}
        assert counts[DetailLevel.BASIC] <= 8 and counts[DetailLevel.INTERMEDIATE] <= 15 and counts[DetailLevel.ADVANCED] <= 25
        assert generate_notes(free, config=NotesConfig(max_keywords=2)).chapter(3).sections.__len__() <= 2

    def test_llm_calls_are_batched_not_one_per_section(self) -> None:
        content = long_content(150)
        llm = ScriptedLLM()
        notes = NotesGenerator(llm).generate(content)
        calls = [c[0] for c in llm.calls]
        n_section_calls = calls.count("section explanations")
        assert 2 <= n_section_calls < 150 / 3
        assert calls.count("analogies") >= 1 and calls.count("short questions") == 1
        assert validate_notes(notes, SPEC) == []

    def test_a_single_huge_paragraph(self) -> None:
        notes = generate_notes(content_of(P(("The index lives in memory because search must be fast. ") * 3000)))
        assert validate_notes(notes) == []


# ============================================================================ detail levels + technical preservation
class TestDetailAndTechnicalContent:
    CODE = "def retrieve(question_vec, chunks, k=3):\n    scored = [(cosine(question_vec, c.vec), c) for c in chunks]\n\treturn scored[:k]"

    def article(self):
        return content_of(
            H("Setup"),
            P("The index holds 768 dimensions and 10,000 vectors. It needs about 3.5 GB of memory in total."),
            P(F("filler one")), P(F("filler two")),
            C(self.CODE, "python"),
            T(("Model", "Size"), ("small", "100 MB"), ("large", "2.5 GB")),
            L("first step", "second step with 99% accuracy", ordered=True),
            P("The loss is computed as L = sum(w * x) + b over all examples in the batch."),
        )

    @pytest.mark.parametrize("detail", list(DetailLevel))
    def test_code_tables_and_lists_are_preserved_at_every_detail_level(self, detail: DetailLevel) -> None:
        notes = generate_notes(self.article(), config=NotesConfig(detail=detail))
        items = list(notes.chapter(2).items())
        assert next(i for i in items if i.kind is ItemKind.CODE).text == self.CODE
        assert next(i for i in items if i.kind is ItemKind.TABLE).rows == (("Model", "Size"), ("small", "100 MB"), ("large", "2.5 GB"))
        steps = next(i for i in items if i.kind is ItemKind.NUMBERED)
        assert steps.items == ("first step", "second step with 99% accuracy")

    @pytest.mark.parametrize("detail", list(DetailLevel))
    def test_numbers_and_formulae_survive_every_detail_level(self, detail: DetailLevel) -> None:
        notes = generate_notes(self.article(), config=NotesConfig(detail=detail))
        text = " ".join(i.plain_text for i in notes.chapter(2).items())
        for token in ("768", "10,000", "3.5 GB", "99%", "100 MB", "2.5 GB", "L = sum(w * x) + b"):
            assert token in text, (detail, token)

    def test_detail_levels_are_monotonic_in_size(self, free) -> None:
        sizes = [generate_notes(free, config=NotesConfig(detail=d)).chapter(2).word_count for d in DetailLevel]
        assert sizes[0] <= sizes[1] <= sizes[2] and sizes[0] < sizes[2]

    def test_advanced_includes_every_paragraph_in_full(self, free) -> None:
        notes = generate_notes(free, config=NotesConfig(detail="Advanced"))
        chapter2 = key(" ".join(texts(notes.chapter(2))))
        for section in free.iter_sections():
            for item in section.items:
                if item.kind.value == "paragraph":
                    assert key(item.text) in chapter2

    def test_basic_marks_partial_paragraphs_as_excerpts(self, free) -> None:
        notes = generate_notes(free, config=NotesConfig(detail="Basic"))
        assert any(i.excerpt for i in notes.chapter(2).items())
        assert not any(i.excerpt for i in generate_notes(free, config=NotesConfig(detail="Advanced")).chapter(2).items())

    def test_technical_identifiers_and_acronyms_kept_verbatim(self) -> None:
        c = content_of(H("Config"), P("Set max_tokens=512 and call getUserData() through the REST API for v2.1 clients."))
        text = " ".join(i.plain_text for i in generate_notes(c, config=NotesConfig(detail="Advanced")).chapter(2).items())
        assert "max_tokens=512" in text and "getUserData()" in text and "REST API" in text and "v2.1" in text

    def test_formula_detection_helper(self) -> None:
        assert is_formula("The loss is L = sum(w * x) + b here.") and is_formula("Use $E=mc^2$ please.")
        assert is_formula("Area ≈ 3.14 r squared") and is_formula("\\frac{a}{b}")
        assert not is_formula("Set k=3 and move on with the plain sentence.") and not is_formula("Plain prose only.")

    def test_shorten_cuts_at_a_clause_boundary_and_never_adds_text(self) -> None:
        long = "Alpha beta gamma delta epsilon zeta eta theta, iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon."
        out, cut = shorten(long, 12)
        assert cut and long.startswith(out) and len(out.split()) <= 12
        assert shorten("Short one.", 12) == ("Short one.", False)


# ============================================================================ duplicates
class TestNoDuplicates:
    def test_no_repeated_text_within_any_chapter(self, free_notes: Notes) -> None:
        for c in free_notes.chapters:
            seen: set[str] = set()
            for i in c.items():
                if i.kind is ItemKind.TEXT and i.provenance is not DER:
                    assert key(i.text) not in seen, (c.number, i.text)
                    seen.add(key(i.text))

    def test_takeaways_are_not_repeated_in_keywords_or_revision(self, free_notes: Notes) -> None:
        takeaways = {key(t) for t in texts(free_notes.chapter(5))}
        keywords = {key(t) for t in texts(free_notes.chapter(3))}
        points = next(s for s in free_notes.chapter(6).sections if s.heading == "Important points")
        revision = {key(i.text) for i in points.items if i.kind is ItemKind.TEXT}
        assert takeaways and not takeaways & keywords and not takeaways & revision

    def test_chapter4_examples_do_not_repeat_other_chapters(self, free_notes: Notes) -> None:
        examples = {key(t) for t in texts(free_notes.chapter(4))}
        others = {key(t) for n in (3, 5) for t in texts(free_notes.chapter(n))}
        assert not examples & others

    def test_keyword_context_sentences_are_not_reused_across_keywords(self, free_notes: Notes) -> None:
        seen: set[str] = set()
        for s in free_notes.chapter(3).sections:
            for i in s.items:
                if i.label in ("Detailed explanation", "Practical significance"):
                    assert key(i.text) not in seen
                    seen.add(key(i.text))

    def test_the_chapter1_introduction_is_not_repeated_in_chapter2(self, free_notes: Notes) -> None:
        intro = key(free_notes.chapter(1).sections[1].items[0].text)
        assert intro not in {key(t) for t in texts(free_notes.chapter(2))}
        assert "Introduction" not in [s.heading for s in free_notes.chapter(2).sections]

    def test_aliases_are_not_listed_as_separate_keywords(self, free_notes: Notes) -> None:
        assert "RAG" not in [s.heading for s in free_notes.chapter(3).sections]

    def test_repeated_questions_from_a_backend_are_dropped(self, free) -> None:
        dup = json.dumps({"questions": [{"question": "Same question?", "answer": "A"}] * 6})
        notes = NotesGenerator(ScriptedLLM({"short questions": dup})).generate(free)
        shorts = [q for q in notes.chapter(7).questions if q.kind is QuestionKind.SHORT]
        assert [q.question for q in shorts] == ["Same question?"]
        assert any("duplicate question" in w for w in notes.generation_warnings)

    def test_invalid_backend_questions_are_dropped_with_a_warning(self, free) -> None:
        class Sloppy(LocalNotesBackend):
            def create_questions(self, content, spec, params):
                from src.backends.base import QuestionResult
                from src.notes_model import Question
                good = super().create_questions(content, spec, params)
                bad = Question(QuestionKind.MCQ, "Broken?", "x", options=("x", "y"), correct_index=0, explanation="e")
                return QuestionResult(good.short, good.descriptive, good.mcq + [bad], [])

        notes = NotesGenerator(Sloppy()).generate(free)
        assert "Broken?" not in [q.question for q in notes.chapter(7).questions]
        assert any("4 options" in w for w in notes.generation_warnings)

    def test_ledger_unit(self) -> None:
        led = _Ledger()
        assert led.claim(1, "Same text.") and not led.claim(1, "same   TEXT!") and led.claim(2, "Same text.")
        assert not led.claim(1, "") and not led.reserved("x")
        led.reserve("Reserved one.")
        assert led.reserved("reserved ONE")


# ============================================================================ unicode / language
TELUGU = ("తెలుగు భాష ఒక ద్రావిడ భాష. ఇది ఆంధ్ర ప్రదేశ్ మరియు తెలంగాణ రాష్ట్రాలలో మాట్లాడబడుతుంది. "
          "ఈ వ్యాసంలో మనం RAG గురించి నేర్చుకుంటాము.")


class TestUnicodeAndLanguage:
    def test_telugu_article_keeps_its_text_intact(self) -> None:
        content = content_of(H("పరిచయం"), P(TELUGU), P(TELUGU + " రెండవ భాగం."))
        notes = generate_notes(content, config=NotesConfig(detail="Advanced"))
        assert TELUGU in " ".join(i.plain_text for i in notes.chapter(2).items())
        assert verify_provenance(notes, content) == [] and validate_notes(notes) == []

    def test_local_backend_cannot_translate_so_english_is_used_with_a_warning(self, free) -> None:
        notes = generate_notes(free, config=NotesConfig(language="Telugu"))
        assert notes.language is Language.ENGLISH
        assert any("cannot translate" in b.text and b.level == "warning" for b in notes.banners)

    def test_english_request_produces_no_translation_warning(self, free_notes: Notes) -> None:
        assert not any("translate" in b.text for b in free_notes.banners)

    @pytest.mark.parametrize("language,needle", [("Telugu", "Telugu"), ("Bilingual", "twice")])
    def test_translation_capable_backend_receives_the_language_instruction(self, free, language: str, needle: str) -> None:
        llm = ScriptedLLM()
        notes = NotesGenerator(llm, NotesConfig(language=language)).generate(free)
        assert notes.language is Language(language)
        assert all(needle in user for _, _, user in llm.calls)

    def test_original_article_text_is_never_translated(self, free) -> None:
        notes = NotesGenerator(ScriptedLLM(), NotesConfig(language="Telugu")).generate(free)
        assert verify_provenance(notes, free) == []

    def test_mixed_script_and_emoji_survive(self) -> None:
        text = "Café naïve 🚀 Привет مرحبا 你好 — the RAG API handles these strings correctly always."
        content = content_of(H("Unicode"), P(text))
        notes = generate_notes(content, config=NotesConfig(detail="Advanced"))
        assert text in " ".join(i.plain_text for i in notes.chapter(2).items())


# ============================================================================ model plumbing
class TestNotesModel:
    def test_to_dict_is_json_serialisable_and_complete(self, free_notes: Notes) -> None:
        d = free_notes.to_dict()
        text = json.dumps(d, ensure_ascii=False)
        back = json.loads(text)
        assert [c["number"] for c in back["chapters"]] == [1, 2, 3, 4, 5, 6, 7]
        assert back["backend"] == {"name": "Local", "kind": "local", "model": None, "is_paid": False, "is_local": True}
        assert back["completeness"] == "complete" and back["language"] == "English"
        assert len(back["chapters"][6]["questions"]) == 21  # D-4: no repeated facts (8.2/D-10: 21, was 20)
        mcq = next(q for q in back["chapters"][6]["questions"] if q["kind"] == "mcq")
        assert len(mcq["options"]) == 4 and mcq["correct_index"] in range(4)

    def test_outline_lists_every_chapter(self, free_notes: Notes) -> None:
        out = free_notes.outline()
        for n, title in CHAPTER_TITLES.items():
            assert f"Chapter {n}: {title}" in out

    def test_note_item_helpers(self) -> None:
        item = NoteItem(ItemKind.BULLETS, ORIG, items=("a b", "c"))
        assert item.plain_text == "a b\nc" and item.word_count == 3 and item.is_original and not item.is_ai_generated
        assert NoteItem(ItemKind.TABLE, ORIG, rows=(("x", "y"),)).plain_text == "x | y"

    def test_enum_parsing(self) -> None:
        assert DetailLevel.parse("BASIC") is DetailLevel.BASIC and Language.parse(" bilingual ") is Language.BILINGUAL
        assert DetailLevel.parse(DetailLevel.ADVANCED) is DetailLevel.ADVANCED
        with pytest.raises(ValueError):
            DetailLevel.parse("x")
        with pytest.raises(ValueError):
            Language.parse("klingon")

    def test_validate_notes_catches_structural_damage(self, free_notes: Notes) -> None:
        broken = dataclasses.replace(free_notes, chapters=free_notes.chapters[:6])
        assert any("chapters 1-7" in p for p in validate_notes(broken))
        no_banner = dataclasses.replace(free_notes, completeness=Completeness.PARTIAL, banners=[])
        problems = validate_notes(no_banner)
        assert any("partial-content banner" in p for p in problems) and any("missing the partial-content notice" in p for p in problems)

    def test_generation_warnings_are_exposed(self, free) -> None:
        notes = NotesGenerator(UnavailableBackend()).generate(free)
        assert notes.generation_warnings and isinstance(notes.generation_warnings, list)
