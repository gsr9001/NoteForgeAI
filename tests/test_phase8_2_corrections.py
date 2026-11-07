"""Phase 8.2 regression tests for defects D-1, D-2, D-4, D-6, D-7, D-8, D-9 and D-10.

No network, no Ollama, no paid service. Sections are appended defect by defect.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_main import Script, make_app


# ============================================================================ D-2: OLLAMA_MODEL
class TestD2OllamaModelEnvironment:
    def test_the_documented_variable_sets_the_starting_model(self, tmp_path: Path) -> None:
        app = make_app(Script(), tmp_path, env_source={"OLLAMA_MODEL": "llama3:8b"})
        assert app.settings.ollama_model == "llama3:8b"

    def test_unset_or_blank_variable_keeps_the_validated_default(self, tmp_path: Path) -> None:
        assert make_app(Script(), tmp_path, env_source={}).settings.ollama_model == "qwen3.5:4b"
        assert make_app(Script(), tmp_path, env_source={"OLLAMA_MODEL": "   "}).settings.ollama_model == "qwen3.5:4b"

    def test_the_environment_model_is_what_the_resolver_receives(self, tmp_path: Path) -> None:
        calls: list[str | None] = []

        def resolver(name: str, *, env=None, ollama_model=None, **kw):  # type: ignore[no-untyped-def]
            from src.backends import BackendSelection
            from src.backends.local import LocalNotesBackend
            calls.append(ollama_model)
            return BackendSelection(LocalNotesBackend(), name)

        app = make_app(Script("4", "2", "7"), tmp_path, env_source={"OLLAMA_MODEL": "llama3:8b"}, resolver=resolver)
        app.run()
        assert calls and calls[-1] == "llama3:8b"

    def test_a_menu_choice_still_beats_the_environment(self, tmp_path: Path) -> None:
        models = [{"name": "llama3:8b"}, {"name": "qwen3.5:4b"}]
        script = Script("5", "2", "7")
        app = make_app(script, tmp_path, env_source={"OLLAMA_MODEL": "llama3:8b"}, models_fn=lambda base: models)
        app.run()
        assert app.settings.ollama_model == "qwen3.5:4b"

    def test_local_stays_the_default_backend(self, tmp_path: Path) -> None:
        app = make_app(Script(), tmp_path, env_source={"OLLAMA_MODEL": "llama3:8b"})
        assert app.settings.backend == "local"

    def test_no_other_variable_is_read(self, tmp_path: Path) -> None:
        from tests.test_main import RecordingEnv
        import src.main as cli
        rec = RecordingEnv({"OPENAI_API_KEY": "sk-never", "OLLAMA_MODEL": "m"})
        make_app(Script(), tmp_path, env_source=rec)
        assert rec.accessed <= set(cli.OLLAMA_ENV_KEYS)


# ============================================================================ D-1: tagline headline
def _meta_of(head: str):  # type: ignore[no-untyped-def]
    from bs4 import BeautifulSoup
    from src.article_extractor import extract_metadata
    return extract_metadata(BeautifulSoup(f"<html><head>{head}</head><body><h1>Body heading</h1></body></html>", "lxml"))


def _ld(headline: str) -> str:
    import json
    return '<script type="application/ld+json">' + json.dumps({"@type": "NewsArticle", "headline": headline}) + "</script>"


TAGLINE = "Subscription platform for online creators - Substack"


class TestD1TaglineHeadline:
    def test_the_real_phase8_case_uses_og_title(self) -> None:
        md = _meta_of(f"<title>{TAGLINE}</title>{_ld(TAGLINE)}"
                      '<meta property="og:title" content="Substack Grow: Launching paid subscriptions">'
                      '<meta property="og:site_name" content="Substack">')
        assert md.title == "Substack Grow: Launching paid subscriptions"

    def test_headline_equal_to_the_page_title_tag_is_distrusted_when_og_title_differs(self) -> None:
        md = _meta_of(f"<title>Our tagline</title>{_ld('Our tagline')}<meta property='og:title' content='Real post'>")
        assert md.title == "Real post"

    def test_headline_equal_to_the_site_name_is_distrusted(self) -> None:
        md = _meta_of(f"{_ld('Substack')}<meta property='og:title' content='Real post'><meta property='og:site_name' content='Substack'>")
        assert md.title == "Real post"

    def test_site_suffix_only_on_the_headline_is_distrusted(self) -> None:
        md = _meta_of(f"{_ld('Generic - Substack')}<meta property='og:title' content='Real post'><meta property='og:site_name' content='Substack'>")
        assert md.title == "Real post"

    def test_a_normal_headline_still_wins_over_a_different_og_title(self) -> None:
        md = _meta_of(f"<title>Browser tab text</title>{_ld('Headline wins')}<meta property='og:title' content='Social card title'>")
        assert md.title == "Headline wins"

    def test_headline_without_og_title_is_kept_even_if_it_looks_generic(self) -> None:
        md = _meta_of(f"<title>{TAGLINE}</title>{_ld(TAGLINE)}")
        assert md.title == TAGLINE

    def test_equal_headline_and_og_title(self) -> None:
        md = _meta_of(f"{_ld('Same')}<meta property='og:title' content=' same '>")
        assert md.title == "Same"

    def test_blank_headline_falls_back_to_og_title(self) -> None:
        md = _meta_of(f"{_ld('   ')}<meta property='og:title' content='From og'>")
        assert md.title == "From og"

    def test_no_json_ld_is_unchanged(self) -> None:
        assert _meta_of("<meta property='og:title' content='Only og'>").title == "Only og"
        assert _meta_of("").title == "Body heading"


# ============================================================================ D-9: long "headings"
LONG_BODY = ("Retrieval systems break when the index drifts from the documents. Teams then notice stale answers. "
             "Fixing this takes a regular re-indexing job, monitoring of recall, and clear ownership of the corpus, "
             "which is why many projects stall after the demo.")


class TestD9LongHeadings:
    def test_html_path_demotes_a_paragraph_tagged_as_heading(self) -> None:
        from src.article_extractor import BlockKind, blocks_from_html
        assert len(LONG_BODY) > 200
        blocks = blocks_from_html(f"<h2>{LONG_BODY}</h2><p>Next.</p>")
        assert [b.kind for b in blocks] == [BlockKind.PARAGRAPH, BlockKind.PARAGRAPH]
        assert blocks[0].text == LONG_BODY  # word for word, nothing lost

    def test_xml_path_demotes_it_too(self) -> None:
        from src.article_extractor import BlockKind, blocks_from_trafilatura_xml
        blocks = blocks_from_trafilatura_xml(f'<doc><main><head rend="h2">{LONG_BODY}</head><p>Next.</p></main></doc>')
        assert [b.kind for b in blocks] == [BlockKind.PARAGRAPH, BlockKind.PARAGRAPH]
        assert blocks[0].text == LONG_BODY

    @pytest.mark.parametrize("text", [
        "Why retrieval matters", "What is RAG?", "A" * 100,
        "Chapter 3: Building a retrieval pipeline that survives production traffic and index drift over time",
    ])
    def test_normal_headings_are_untouched(self, text: str) -> None:
        from src.article_extractor import BlockKind, blocks_from_html
        b = blocks_from_html(f"<h3>{text}</h3>")[0]
        assert b.kind is BlockKind.HEADING and b.level == 3 and b.text == text

    def test_one_very_long_sentence_is_a_paragraph_too(self) -> None:
        from src.article_extractor import BlockKind, blocks_from_html
        text = ("Teams that treat retrieval as an afterthought discover months later that the index drifted away from the "
                "documents it was built from and that nobody owned the corpus or measured recall at any point in between")
        assert len(text) > 200 and ". " not in text
        assert blocks_from_html(f"<h2>{text}</h2>")[0].kind is BlockKind.PARAGRAPH

    def test_medium_length_multi_sentence_heading_is_a_paragraph(self) -> None:
        from src.article_extractor import BlockKind, blocks_from_html
        text = "It is simple. Indexing is the hard part and everything downstream depends on getting it right first, so plan for it early."
        assert 120 < len(text) <= 200
        assert blocks_from_html(f"<h2>{text}</h2>")[0].kind is BlockKind.PARAGRAPH

    def test_medium_length_single_sentence_heading_stays_a_heading(self) -> None:
        from src.article_extractor import BlockKind, blocks_from_html
        text = "Why indexing is the hard part and everything downstream depends on getting it right first of all"
        assert 90 < len(text) <= 200
        assert blocks_from_html(f"<h2>{text}</h2>")[0].kind is BlockKind.HEADING

    def test_markdown_headings_are_explicit_and_unchanged(self) -> None:
        from src.article_extractor import BlockKind, blocks_from_text
        blocks = blocks_from_text("# " + "Title " * 5 + "\n\n## " + LONG_BODY[:80] + "\n\nBody text here.")
        assert blocks[0].kind is BlockKind.HEADING


# ============================================================================ D-6: weak key terms
class TestD6WeakKeywords:
    @staticmethod
    def kws(*blocks):  # type: ignore[no-untyped-def]
        from tests.helpers import content_of
        return {k.term: k for k in content_of(*blocks).keywords}

    def test_an_undefined_two_letter_acronym_is_not_a_key_term(self) -> None:
        from tests.helpers import P
        kws = self.kws(P("AI is everywhere in this story about search systems and users today."),
                       P("Another sentence about something else entirely, mentioning UI once."))
        assert "AI" not in kws and "UI" not in kws

    def test_a_defined_two_letter_acronym_is_kept(self) -> None:
        from tests.helpers import P
        kws = self.kws(P("We use machine learning (ML) to rank results, and ML models need training data."))
        assert "ML" in kws

    def test_longer_undefined_acronyms_are_unchanged(self) -> None:
        from tests.helpers import P
        assert "BERT" in self.kws(P("The BERT encoder reads the whole sentence at once and outputs vectors."))

    def test_a_heading_only_term_ranks_below_terms_used_in_the_text(self) -> None:
        from tests.helpers import H, P
        text = "Latency matters. Latency is measured per request. Reducing latency needs caching."
        kws = self.kws(H("Common failure modes"), P(text), P("Latency depends on the network and model."))
        assert "Common failure modes" in kws  # still available (headings are a deliberate cue)
        assert kws["Common failure modes"].score < kws["latency"].score

    def test_a_heading_term_that_the_prose_uses_is_not_demoted(self) -> None:
        from tests.helpers import H, P
        used = self.kws(H("Chunk size"), P("Chunk size controls how much text each piece holds."))
        unused = self.kws(H("Chunk size"), P("Something unrelated is said about other subjects here."))
        assert used["Chunk size"].score > unused["Chunk size"].score

    def test_a_plain_function_name_only_in_code_is_demoted_but_present(self) -> None:
        from tests.helpers import C, P
        kws = self.kws(P("Some prose about unrelated subjects appears in this paragraph."),
                       C("def fetch(query):\n    pass\nclass VectorStore:\n    pass"))
        assert "fetch" in kws and "VectorStore" in kws
        assert kws["fetch"].score < kws["VectorStore"].score

    def test_ranking_is_deterministic(self) -> None:
        from tests.helpers import H, P
        blocks = (H("Common failure modes"), P("Latency matters. Latency again. Latency thrice."))
        assert list(self.kws(*blocks)) == list(self.kws(*blocks))

    def test_the_real_fixture_no_longer_leads_with_weak_terms(self) -> None:
        from tests.helpers import fixture_content
        terms = [k.term for k in fixture_content("substack_free.html").keywords]
        assert "AI" not in terms
        assert terms.index("Common failure modes") > terms.index("model") if "Common failure modes" in terms else True


# ============================================================================ D-10: Chapter 5 lessons and findings
def _ch5(*blocks, detail=None):  # type: ignore[no-untyped-def]
    from src.notes_generator import NotesConfig, NotesGenerator
    from tests.helpers import content_of
    cfg = NotesConfig() if detail is None else NotesConfig(detail=detail)
    return NotesGenerator(config=cfg).generate(content_of(*blocks))


def _section(notes, heading):  # type: ignore[no-untyped-def]
    return next(s for s in notes.chapter(5).sections if s.heading == heading)


def _texts(section):  # type: ignore[no-untyped-def]
    return [i.text for i in section.items if i.kind.value != "notice"]


class TestD10ChapterFive:
    def test_the_real_fixture_now_has_its_practical_lessons_and_a_finding(self) -> None:
        from tests.helpers import fixture_content
        from src.notes_generator import NotesConfig, NotesGenerator
        notes = NotesGenerator(config=NotesConfig()).generate(fixture_content("substack_free.html"))
        lessons = _texts(_section(notes, "Practical lessons"))
        assert any("Measure retrieval quality before you tune the prompt" in t for t in lessons)
        assert any(t.startswith("In practice, teams add a re-ranking step") for t in lessons)
        assert any("2020 research paper" in t for t in _texts(_section(notes, "Main findings")))

    def test_an_instruction_in_a_paragraph_is_a_recommendation(self) -> None:
        from tests.helpers import H, P
        notes = _ch5(H("Advice"), P("Measure retrieval quality before you tune the prompt for the final release."))
        assert _texts(_section(notes, "Practical lessons"))

    def test_steps_of_a_procedure_in_a_list_are_not_recommendations(self) -> None:
        from tests.helpers import H, L
        notes = _ch5(H("Pipeline"), L("Add the retrieved chunks to the prompt before calling the model.",
                                      "Use the answer text exactly as the model returned it to the user."))
        assert not _texts(_section(notes, "Practical lessons"))

    def test_a_recommendation_in_the_conclusion_section_stays_a_lesson(self) -> None:
        from tests.helpers import H, P
        notes = _ch5(H("Conclusion"), P("Teams should review failures by hand every single week of the year."))
        assert _texts(_section(notes, "Practical lessons")) and not _texts(_section(notes, "Conclusions"))

    def test_a_fact_in_the_conclusion_section_stays_a_finding(self) -> None:
        from tests.helpers import H, P
        notes = _ch5(H("Conclusion"), P("The method was introduced in 2020 by a research team at one lab."))
        assert _texts(_section(notes, "Main findings"))

    def test_plain_conclusion_sentences_are_still_conclusions(self) -> None:
        from tests.helpers import H, P
        notes = _ch5(H("Conclusion"), P("Retrieval does not make a model smarter, only better informed."))
        assert _texts(_section(notes, "Conclusions"))

    def _crowded(self):  # type: ignore[no-untyped-def]
        from tests.helpers import H, P
        conclusions = [P(f"In conclusion, point number {w} matters a great deal for the final design.") for w in
                       ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve")]
        return [H("Wrap up"), *conclusions, P("You should always keep a small set of test questions to hand.")]

    def test_a_lone_recommendation_survives_a_crowd_of_higher_scoring_conclusions(self) -> None:
        from src.notes_generator import DetailLevel
        notes = _ch5(*self._crowded(), detail=DetailLevel.BASIC)
        assert any("small set of test questions" in t for t in _texts(_section(notes, "Practical lessons")))

    def test_the_size_limit_holds_and_the_cut_is_reported(self) -> None:
        from src.notes_generator import DetailLevel, _TAKEAWAYS_BY_DETAIL
        notes = _ch5(*self._crowded(), detail=DetailLevel.BASIC)
        shown = sum(len(_texts(s)) for s in notes.chapter(5).sections)
        assert shown <= _TAKEAWAYS_BY_DETAIL[DetailLevel.BASIC]
        concl = _section(notes, "Conclusions")
        assert any("left out to keep this chapter within its size limit" in i.text for i in concl.items if i.kind.value == "notice")

    def test_no_cut_notice_when_nothing_was_cut(self) -> None:
        from tests.helpers import H, P
        notes = _ch5(H("Conclusion"), P("In conclusion, the technique works well for small document sets."))
        assert not any("size limit" in (i.text or "") for s in notes.chapter(5).sections for i in s.items)

    def test_a_takeaway_is_not_repeated_in_chapter_six_or_four(self) -> None:
        from tests.helpers import fixture_content
        from src.notes_generator import NotesConfig, NotesGenerator
        notes = NotesGenerator(config=NotesConfig()).generate(fixture_content("substack_free.html"))
        taken = {t for s in notes.chapter(5).sections for t in _texts(s)}
        for number in (4, 6):
            repeated = [i.text for s in notes.chapter(number).sections for i in s.items if i.text in taken]
            assert not repeated, (number, repeated)

    def test_chapter_five_never_exceeds_the_detail_limits(self) -> None:
        from tests.helpers import fixture_content
        from src.notes_generator import DetailLevel, NotesConfig, NotesGenerator, _TAKEAWAYS_BY_DETAIL
        content = fixture_content("substack_free.html")
        for level in DetailLevel:
            notes = NotesGenerator(config=NotesConfig(detail=level)).generate(content)
            assert sum(len(_texts(s)) for s in notes.chapter(5).sections) <= _TAKEAWAYS_BY_DETAIL[level]

    def test_every_chapter_five_item_cites_a_valid_source_block(self) -> None:
        from tests.helpers import fixture_content
        from src.notes_generator import NotesConfig, NotesGenerator
        content = fixture_content("substack_free.html")
        notes = NotesGenerator(config=NotesConfig()).generate(content)
        for s in notes.chapter(5).sections:
            for i in s.items:
                if i.kind.value != "notice":
                    assert i.source_indices and all(0 <= x < 21 for x in i.source_indices)



# ============================================================================ D-4: AI-written question diversity
def _ai_content():  # type: ignore[no-untyped-def]
    from tests.helpers import C, H, P, content_of
    return content_of(
        H("Chunking"),
        P("Small chunks are precise but lose the surrounding context of the document. "
          "Large chunks keep more context but dilute the retrieval signal considerably. "
          "Overlapping chunks reduce the chance of cutting an important sentence in half. "
          "Chunk boundaries should follow headings and paragraph breaks whenever possible."),
        H("Evaluation"),
        P("Evaluate retrieval separately from generation because silent failures hide behind fluent answers. "
          "A small set of test questions reviewed every week catches regressions early."),
        H("Embeddings"),
        P("An embedding is a list of numbers capturing the meaning of a passage inside a vector space."),
        C("def retrieve(question_vec, chunks, k=3):\n    return sorted(chunks)[:k]"),
    )


def _create(short, spec=None, desc=(), mcq=()):  # type: ignore[no-untyped-def]
    import json
    from src.backends.base import GenerationParams, QuestionSpec
    from src.notes_model import DetailLevel, Language
    from tests.helpers import ScriptedLLM
    llm = ScriptedLLM({
        "short questions": json.dumps({"questions": [{"question": q, "answer": a} for q, a in short]}),
        "descriptive questions": json.dumps({"questions": [{"question": q, "answer": a} for q, a in desc]}),
        "mcq questions": json.dumps({"questions": list(mcq)}),
    })
    spec = spec or QuestionSpec(len(short), len(desc), len(mcq))
    return llm.create_questions(_ai_content(), spec, GenerationParams(DetailLevel.INTERMEDIATE, Language.ENGLISH, 2))


CHUNK_QA = [
    ("Why do small chunks help retrieval precision?", "Small chunks are precise but lose the surrounding context of the document."),
    ("What is the downside of large chunks for the retrieval signal?", "Large chunks keep more context but dilute the retrieval signal considerably."),
    ("How do overlapping chunks protect important sentences?", "Overlapping chunks reduce the chance of cutting an important sentence in half."),
    ("Where should chunk boundaries fall when splitting a document?", "Chunk boundaries should follow headings and paragraph breaks whenever possible."),
]
EVAL_QA = [("Why evaluate retrieval separately from generation?",
            "Evaluate retrieval separately from generation because silent failures hide behind fluent answers.")]
EMBED_QA = [("What is an embedding in this article?",
             "An embedding is a list of numbers capturing the meaning of a passage inside a vector space.")]


class TestD4AiQuestionDiversity:
    def test_sources_are_located_for_every_kept_question(self) -> None:
        r = _create(CHUNK_QA[:1] + EMBED_QA)
        assert [q.source_indices for q in r.short] == [(1,), (5,)]

    def test_a_tiny_answer_gets_no_reference_instead_of_a_guess(self) -> None:
        from src.backends.llm import locate_sources
        assert locate_sources(_ai_content(), "Closeness.") == ()
        assert locate_sources(_ai_content(), "Entirely unrelated astronomy vocabulary about galaxies and nebulae.") == ()

    def test_an_mcq_is_located_from_its_correct_option_and_explanation(self) -> None:
        r = _create([], mcq=[{"question": "Which units keep more context but dilute the signal?",
                              "options": ["Large chunks", "Small chunks", "Tiny tokens", "Whole books"], "correct_index": 0,
                              "explanation": "Large chunks keep more context but dilute the retrieval signal considerably."}])
        assert len(r.mcq) == 1 and r.mcq[0].source_indices == (1,)

    def test_the_relaxed_pass_tops_up_to_the_requested_number_from_one_block(self) -> None:
        r = _create(CHUNK_QA + EVAL_QA)
        assert len(r.short) == 5 and [q.source_indices[0] for q in r.short].count(1) == 4

    def test_the_strict_cap_limits_a_block_when_the_requested_number_is_already_met(self) -> None:
        from src.backends.base import QuestionSpec
        from src.backends.local_questions import STRICT_BLOCK_CAP
        r = _create(CHUNK_QA + EVAL_QA, spec=QuestionSpec(3, 0, 0))
        assert len(r.short) == 3
        assert [q.source_indices[0] for q in r.short].count(1) <= STRICT_BLOCK_CAP  # the third comes from another block

    def test_never_more_than_the_relaxed_cap_from_one_block(self) -> None:
        from src.backends.local_questions import RELAXED_BLOCK_CAP
        five = CHUNK_QA + [("What must chunk boundaries respect in a split?",
                            "Chunk boundaries should follow headings and paragraph breaks whenever possible, always.")]
        r = _create(five)
        assert len([q for q in r.short if q.source_indices[:1] == (1,)]) <= RELAXED_BLOCK_CAP

    def test_the_shortfall_is_reported_honestly_and_nothing_is_added(self) -> None:
        five = CHUNK_QA + [("Which pieces of a document does the article discuss splitting?",
                            "Small chunks are precise but lose the surrounding context of the document, indeed.")]
        r = _create(five)
        kept = len(r.short)
        assert kept < len(five)
        note = next(n for n in r.notes if "left out on purpose" in n)
        assert f"{len(five) - kept} short question" in note and "No question was added only to reach the requested number" in note

    def test_a_repeated_fact_is_dropped_even_with_different_wording(self) -> None:
        r = _create([CHUNK_QA[0], ("What happens to context when chunks are small?", CHUNK_QA[0][1])])
        assert len(r.short) == 1 and any("left out on purpose" in n for n in r.notes)

    def test_the_same_fact_across_kinds_is_asked_once(self) -> None:
        r = _create([CHUNK_QA[0]], desc=[("Describe the trade-off of small chunks in detail.", CHUNK_QA[0][1])])
        assert len(r.short) + len(r.descriptive) == 1

    def test_different_blocks_all_survive(self) -> None:
        r = _create([CHUNK_QA[0], *EVAL_QA, *EMBED_QA])
        assert len(r.short) == 3 and not any("left out" in n for n in r.notes)

    def test_labels_and_provenance_are_unchanged(self) -> None:
        from src.notes_model import NoteProvenance
        r = _create(CHUNK_QA[:2])
        assert all(q.question_provenance is NoteProvenance.AI_GENERATED and q.answer_provenance is NoteProvenance.AI_GENERATED
                   for q in r.short)

    def test_the_validation_shortfall_message_is_kept(self) -> None:
        from src.backends.base import QuestionSpec
        r = _create([CHUNK_QA[0]], spec=QuestionSpec(3, 0, 0))
        assert any("Only 1 of 3 short questions from the model passed validation." in n for n in r.notes)

    def test_the_local_backend_is_untouched(self) -> None:
        from tests.helpers import fixture_content
        from src.backends.base import GenerationParams, QuestionSpec
        from src.backends.local import LocalNotesBackend
        from src.notes_model import DetailLevel, Language
        r = LocalNotesBackend().create_questions(fixture_content("substack_free.html"), QuestionSpec(),
                                                  GenerationParams(DetailLevel.INTERMEDIATE, Language.ENGLISH, 2))
        assert (len(r.short), len(r.descriptive), len(r.mcq)) == (10, 5, 6)

    def test_the_generator_keeps_references_and_word_prints_them(self) -> None:
        import json
        from io import BytesIO
        from docx import Document
        from src.notes_generator import NotesConfig, NotesGenerator
        from src.word_generator import WordGenerator
        from tests.helpers import ScriptedLLM
        llm = ScriptedLLM({"short questions": json.dumps({"questions": [{"question": CHUNK_QA[0][0], "answer": CHUNK_QA[0][1]}]}),
                           "descriptive questions": json.dumps({"questions": []}),
                           "mcq questions": json.dumps({"questions": []})})
        notes = NotesGenerator(llm, NotesConfig(short_questions=1, descriptive_questions=0, mcq_questions=0)).generate(_ai_content())
        q = notes.chapter(7).questions[0]
        assert q.source_indices == (1,)
        doc = Document(BytesIO(WordGenerator().to_bytes(notes)))
        assert any(CHUNK_QA[0][0] in p.text and "[¶2]" in p.text for p in doc.paragraphs)  # index 1 is shown 1-based as ¶2



# ============================================================================ D-7: analogy review warnings
def _analogy_notes(texts, kind="analogy"):  # type: ignore[no-untyped-def]
    """Generate notes whose model replies to the analogy request with ``texts`` (one per term, in order)."""
    import json
    from src.notes_generator import NotesConfig, NotesGenerator
    from tests.helpers import ScriptedLLM, fixture_content, payload_of

    def reply(user: str) -> str:
        terms = [t["term"] for t in payload_of(user)]
        return json.dumps({"examples": [{"concept": t, "kind": kind, "text": texts[i % len(texts)]} for i, t in enumerate(terms[:1])]})

    llm = ScriptedLLM({"analogies": reply})
    return NotesGenerator(llm, NotesConfig(include_questions=False)).generate(fixture_content("substack_free.html"))


def _ai_items(notes):  # type: ignore[no-untyped-def]
    section = next(s for s in notes.chapter(4).sections if s.heading.startswith("Analogies"))
    return [i for i in section.items if i.kind.value != "notice"]


CONTRAST = "Think of the idea like a librarian fetching a book for you rather than a robot memorising the whole shelf."
PLAIN = "Think of the idea like a librarian fetching the right book for a reader who asks a question."


class TestD7AnalogyReview:
    def test_a_contrast_with_something_the_article_never_mentions_is_marked(self) -> None:
        notes = _analogy_notes([CONTRAST])
        items = _ai_items(notes)
        assert items and all(i.label.endswith("Review needed") for i in items)
        assert any("Review needed: the AI-generated analogy" in w and "rather than" in w for w in notes.generation_warnings)

    def test_a_plain_analogy_is_not_marked(self) -> None:
        notes = _analogy_notes([PLAIN])
        assert _ai_items(notes) and not any("Review needed" in i.label for i in _ai_items(notes))
        assert not any("contrasts the idea" in w for w in notes.generation_warnings)

    def test_a_contrast_the_article_itself_makes_is_not_marked(self) -> None:
        # "retrieval ... rather than ... prompt" - every word after the cue occurs in the article
        text = "Think of retrieval like checking sources, done rather than tuning the prompt after the answer."
        notes = _analogy_notes([text])
        assert _ai_items(notes) and not any("Review needed" in i.label for i in _ai_items(notes))

    def test_applications_are_checked_too(self) -> None:
        text = "A helpdesk could answer policy questions instead of forwarding tickets to a legacy telephone exchange."
        notes = _analogy_notes([text], kind="application")
        assert _ai_items(notes) and all("Review needed" in i.label for i in _ai_items(notes))

    def test_the_item_is_kept_and_stays_labelled_as_ai(self) -> None:
        from src.notes_model import NoteProvenance
        notes = _analogy_notes([CONTRAST])
        item = _ai_items(notes)[0]
        assert item.provenance is NoteProvenance.AI_GENERATED and item.label.startswith("AI-generated analogy")
        assert CONTRAST in item.text  # not rewritten, not removed

    def test_the_filter_is_not_weakened_numbers_are_still_rejected(self) -> None:
        notes = _analogy_notes(["Think of the idea like a librarian who finds 400 books in under 3 seconds for readers."])
        assert not _ai_items(notes)

    def test_the_filter_is_not_weakened_copies_and_non_comparisons_are_still_dropped(self) -> None:
        notes = _analogy_notes(["The model gives answers to questions that people ask it every day without warning."])
        assert not _ai_items(notes)

    def test_the_warning_reaches_the_word_document(self) -> None:
        from io import BytesIO
        from docx import Document
        from src.word_generator import WordGenerator
        notes = _analogy_notes([CONTRAST])
        text = "\n".join(p.text for p in Document(BytesIO(WordGenerator().to_bytes(notes))).paragraphs)
        assert "Review needed" in text and "AI-generated" in text

    def test_explanations_still_use_the_existing_removal_policy(self) -> None:
        # D-7 safeguard for explanations is unchanged: an unsupported comparison is removed, not merely flagged
        from src.backends.llm import assess_support
        v = assess_support("The chunks are stored plainly instead of in complex vector databases.",
                           "Chunks are stored as plain lists of numbers in memory for this small example.")
        assert v[0].status == "unsupported" and v[0].unsupported_comparison == "instead of"



# ============================================================================ D-8: Word layout
def _word_doc():  # type: ignore[no-untyped-def]
    from io import BytesIO
    from docx import Document
    from src.notes_generator import NotesConfig, NotesGenerator
    from src.word_generator import WordGenerator
    from tests.helpers import fixture_content
    notes = NotesGenerator(config=NotesConfig()).generate(fixture_content("substack_free.html"))
    return Document(BytesIO(WordGenerator().to_bytes(notes))), notes


class TestD8WordLayout:
    def test_toc_styles_are_not_flagged_custom_so_word_uses_them(self) -> None:
        from docx.oxml.ns import qn
        doc, _ = _word_doc()
        for lvl in (1, 2, 3):
            style = doc.styles[f"TOC {lvl}"]
            # Word renames a "custom" style that collides with a built-in name ("TOC 21") and then ignores it
            assert style.element.get(qn("w:customStyle")) is None, lvl
            assert style.name == f"TOC {lvl}"

    def test_toc_entries_are_compact(self) -> None:
        from docx.shared import Pt
        doc, _ = _word_doc()
        for lvl in (1, 2, 3):
            pf = doc.styles[f"TOC {lvl}"].paragraph_format
            assert pf.line_spacing == 1.0
            assert pf.space_after <= Pt(2) and pf.space_before <= Pt(3)
        assert doc.styles["TOC 2"].font.size <= Pt(10.5)

    def test_a_typical_toc_fits_one_page_by_the_estimate(self) -> None:
        """~41 entries at the compact spacing need well under one 25 cm text column (about 709 pt)."""
        doc, _ = _word_doc()
        entries = [p for p in doc.paragraphs if p.style.name in ("TOC 1", "TOC 2", "TOC 3")]
        height = 0.0
        for p in entries:
            st = p.style
            height += st.font.size.pt * 1.25 + st.paragraph_format.space_after.pt + st.paragraph_format.space_before.pt
        assert 30 <= len(entries) <= 60 and height < 640, (len(entries), height)

    def test_boxed_items_are_kept_together_but_article_paragraphs_are_not(self) -> None:
        doc, _ = _word_doc()
        for name in ("NF AI", "NF Definition", "NF Takeaway", "NF Example", "NF Warning", "NF Info", "NF Quote",
                     "NF Answer", "NF Explanation"):
            assert doc.styles[name].paragraph_format.keep_together is True, name
        assert doc.styles["NF Original"].paragraph_format.keep_together in (None, False)  # long paragraphs may flow

    def test_chapters_still_start_on_a_new_page(self) -> None:
        doc, _ = _word_doc()
        assert doc.styles["Heading 1"].paragraph_format.page_break_before is True

    def test_the_toc_is_still_a_real_field_with_every_heading_listed(self) -> None:
        import re
        doc, _ = _word_doc()
        xml = doc.element.xml
        assert re.search(r'TOC \\o "1-3" \\h \\z \\u', xml)
        headings = [p.text for p in doc.paragraphs if re.fullmatch(r"Heading [1-3]", p.style.name)]
        entries = [p.text for p in doc.paragraphs if p.style.name in ("TOC 1", "TOC 2", "TOC 3")]
        assert [e for e in entries if e] == [h for h in headings if h]  # every heading, in order, nothing extra

    def test_the_document_still_passes_the_completeness_check(self) -> None:
        from src.word_generator import verify_document
        doc, notes = _word_doc()
        assert verify_document(doc, notes) == []
