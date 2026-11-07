"""Phase 9 regression tests: Windows PowerShell input (UTF-8 byte-order mark) and release-validation checks.

No network, no Ollama, no paid service.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src.main import strip_bom
from tests.test_main import FIX, ROOT, TITLE_DOCX, URL, Script, docx_files, html_flow, make_app, run

BOM = "﻿"


# ============================================================================ strip_bom
class TestStripBom:
    @pytest.mark.parametrize("raw, expected", [
        (BOM + "2", "2"),
        (BOM + BOM + "2", "2"),
        ("\xef\xbb\xbf2", "2"),                        # the same bytes decoded by a legacy code page
        ("2", "2"),
        ("", ""),
        (BOM, ""),
        ("   ", "   "),
        ("b", "b"),
        ("తెలుగు", "తెలుగు"),                           # non-ASCII input is untouched
        ("ünïcödé ✓", "ünïcödé ✓"),
        ("a" + BOM + "b", "a" + BOM + "b"),            # only a leading mark is removed
        (BOM + "తెలుగు", "తెలుగు"),
        ("https://x.substack.com/p/a", "https://x.substack.com/p/a"),
    ])
    def test_cases(self, raw: str, expected: str) -> None:
        assert strip_bom(raw) == expected

    def test_it_is_idempotent(self) -> None:
        assert strip_bom(strip_bom(BOM + "x")) == "x"


# ============================================================================ the application
class TestAppInput:
    def test_a_bom_prefixed_menu_choice_is_understood(self, tmp_path: Path) -> None:
        script = Script(BOM + "7")
        assert make_app(script, tmp_path).run() == 0
        assert "is not a menu option" not in "\n".join(script.out)

    def test_the_first_piped_line_with_a_bom_runs_the_whole_workflow(self, tmp_path: Path) -> None:
        flow = html_flow()
        flow[0] = BOM + flow[0]
        _, s, _ = run(tmp_path, *flow)
        assert "Your document is ready:" in s.text and len(docx_files(tmp_path)) == 1

    def test_normal_utf8_input_is_unchanged(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, *html_flow())
        assert "Your document is ready:" in s.text and "is not a menu option" not in s.text

    def test_empty_input_just_shows_the_menu_again(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "", BOM + "")
        assert "is not a menu option" not in s.text
        assert s.prompts.count("Choose an option (1-7): ") == 3

    def test_several_piped_inputs_in_a_row(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, BOM + "9", "", "x", BOM + "0")
        assert s.text.count("is not a menu option") == 3  # three bad answers, one empty line ignored
        assert "'9' is not" in s.text and "'x' is not" in s.text and "'0' is not" in s.text

    @pytest.mark.parametrize("bad", ["0", "8", "abc", "-1", "1.5"])
    def test_invalid_menu_choices_are_still_rejected(self, bad: str, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, BOM + bad)
        assert f"'{bad}' is not a menu option" in s.text  # the mark is gone, the user's text is not altered

    def test_a_bom_in_a_prompt_answer_is_removed_too(self, tmp_path: Path) -> None:
        flow = html_flow(path=BOM + str(FIX / "substack_free.html"))
        _, s, _ = run(tmp_path, *flow)
        assert "Your document is ready:" in s.text

    def test_back_commands_still_work(self, tmp_path: Path) -> None:
        for back in ("b", "back", BOM + "b"):
            _, s, _ = run(tmp_path, "2", back)
            assert "Cancelled. Back to the main menu." in s.text

    def test_ctrl_c_at_the_menu_exits_cleanly(self, tmp_path: Path) -> None:
        script = Script(KeyboardInterrupt)
        assert make_app(script, tmp_path).run() == 130
        assert "Interrupted. Goodbye." in script.text

    def test_ctrl_c_inside_an_action_returns_to_the_menu(self, tmp_path: Path) -> None:
        _, s, _ = run(tmp_path, "2", KeyboardInterrupt)
        assert "Cancelled by Ctrl+C. No document was created." in s.text and docx_files(tmp_path) == []

    def test_closed_input_exits_cleanly(self, tmp_path: Path) -> None:
        script = Script()
        assert make_app(script, tmp_path).run() == 0
        assert "No more input. Goodbye." in script.text

    def test_non_ascii_answers_survive(self, tmp_path: Path) -> None:
        text = tmp_path / "telugu.txt"
        text.write_text("# తెలుగు వ్యాసం\n\nతెలుగు భాష ఒక ద్రావిడ భాష. ఈ వ్యాసంలో RAG గురించి చర్చించాము.", encoding="utf-8")
        _, s, _ = run(tmp_path, BOM + "3", str(text), "", "", "", "", "", "")
        assert "Your document is ready:" in s.text


# ============================================================================ the real program
def _py(args: list[str], stdin_bytes: bytes, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], input=stdin_bytes, capture_output=True, cwd=cwd, timeout=120)


class TestRealProcess:
    def test_raw_bom_bytes_on_a_real_pipe(self, tmp_path: Path) -> None:
        r = _py(["src/main.py", "--output-dir", str(tmp_path)], b"\xef\xbb\xbf7\r\n")
        out = r.stdout.decode("utf-8", "replace")
        assert r.returncode == 0 and "Goodbye." in out and "is not a menu option" not in out

    def test_full_session_with_bom_on_the_first_line(self, tmp_path: Path) -> None:
        feed = f"﻿2\n{FIX / 'substack_free.html'}\n{URL}\n\n\n7\n".encode("utf-8")
        r = _py(["src/main.py", "--output-dir", str(tmp_path / "o")], feed)
        assert r.returncode == 0 and b"Your document is ready:" in r.stdout
        assert [p.name for p in (tmp_path / "o").glob("*.docx")] == [TITLE_DOCX]

    @pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell not available")
    def test_windows_powershell_with_a_forced_bom(self, tmp_path: Path) -> None:
        feed = ("$OutputEncoding = New-Object System.Text.UTF8Encoding $true; "
                f"'2','{FIX / 'substack_free.html'}','{URL}','','','7' | & '{sys.executable}' 'src/main.py' --output-dir '{tmp_path / 'ps'}'")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", feed],
                           capture_output=True, text=True, cwd=ROOT, timeout=120)
        assert r.returncode == 0, r.stderr
        assert "Your document is ready:" in r.stdout and (tmp_path / "ps" / TITLE_DOCX).exists()
        assert "is not a menu option" not in r.stdout



# ============================================================================ D-9 through the whole pipeline (fixture HTML)
LONG_ONE = ("Teams that treat retrieval as an afterthought discover months later that the index drifted away from the "
            "documents it was built from and that nobody owned the corpus or measured recall at any point in between")
MULTI = ("Retrieval systems break when the index drifts. Teams then notice stale answers. Fixing this takes a regular "
         "re-indexing job, monitoring of recall, and clear ownership of the corpus.")
BODY = "<p>" + " ".join(["This paragraph explains the topic with ordinary prose so that the extractor treats the page as a real article body."] * 3) + "</p>"
HEADING_PAGE = f"""<html><head><title>T</title><meta property="og:title" content="Heading test article"></head><body><article>
<h1>Heading test article</h1>
<h2>Short heading</h2>{BODY}
<h2>{LONG_ONE}</h2>{BODY}
<h2>{MULTI}</h2>{BODY}
<p><strong>{LONG_ONE}.</strong></p>{BODY}
<p><strong>Bold short label</strong></p>{BODY}
<h2>Parent section</h2><h3>Child section</h3>{BODY}<h4>Grandchild section</h4>{BODY}
<p>{LONG_ONE}.</p><p>{MULTI}</p>
</article></body></html>"""


@pytest.fixture(scope="module")
def article():  # type: ignore[no-untyped-def]
    from src.article_extractor import SubstackExtractor
    return SubstackExtractor().extract_from_html(HEADING_PAGE)


class TestHeadingClassificationPipeline:
    def kinds_of(self, article, fragment):  # type: ignore[no-untyped-def]
        return [(i, b.kind.value) for i, b in enumerate(article.blocks) if fragment in b.text]

    def test_normal_headings_stay_headings(self, article) -> None:  # type: ignore[no-untyped-def]
        assert [k for _, k in self.kinds_of(article, "Short heading")] == ["heading"]

    def test_a_long_single_sentence_heading_becomes_a_paragraph_with_its_text(self, article) -> None:  # type: ignore[no-untyped-def]
        hits = self.kinds_of(article, LONG_ONE)
        assert hits and {k for _, k in hits} == {"paragraph"}

    def test_a_multi_sentence_heading_becomes_a_paragraph(self, article) -> None:  # type: ignore[no-untyped-def]
        assert {k for _, k in self.kinds_of(article, "Teams then notice stale answers")} == {"paragraph"}

    def test_bold_paragraphs_are_paragraphs(self, article) -> None:  # type: ignore[no-untyped-def]
        assert {k for _, k in self.kinds_of(article, "Bold short label")} == {"paragraph"}

    def test_nested_headings_keep_their_levels(self, article) -> None:  # type: ignore[no-untyped-def]
        levels = {b.text: b.level for b in article.blocks if b.kind.value == "heading"}
        assert levels["Parent section"] == 2 and levels["Child section"] == 3 and levels["Grandchild section"] == 4

    def test_plain_paragraphs_are_unchanged(self, article) -> None:  # type: ignore[no-untyped-def]
        plain = [b for b in article.blocks if b.text == LONG_ONE + "." and b.kind.value == "paragraph"]
        assert plain and any(b.text.startswith("Retrieval systems break") and b.kind.value == "paragraph" for b in article.blocks)

    def test_no_text_is_lost(self, article) -> None:  # type: ignore[no-untyped-def]
        text = " ".join(b.text for b in article.blocks)
        for fragment in (LONG_ONE, "Retrieval systems break when the index drifts", "Fixing this takes a regular re-indexing job",
                         "Short heading", "Parent section", "Child section", "Grandchild section", "Bold short label"):
            assert fragment in text, fragment

    def test_the_demoted_text_is_not_in_the_sections_or_toc_headings(self, article) -> None:  # type: ignore[no-untyped-def]
        from src.content_processor import process_article
        headings = [s.heading for s in process_article(article).iter_sections()]
        assert headings == ["Short heading", "Parent section", "Child section", "Grandchild section"]

    def test_source_indices_and_provenance_stay_correct_in_the_notes(self, article) -> None:  # type: ignore[no-untyped-def]
        from src.content_processor import process_article
        from src.notes_generator import NotesConfig, NotesGenerator, verify_provenance
        from src.notes_model import NoteProvenance
        content = process_article(article)
        notes = NotesGenerator(config=NotesConfig()).generate(content)
        assert verify_provenance(notes, content) == []
        indices = {i.source_indices[0] for i in notes.all_items()
                   if i.provenance is NoteProvenance.ORIGINAL and i.source_indices}
        assert indices and all(0 <= i < len(article.blocks) for i in indices)

    def test_markdown_style_text_headings_are_not_demoted(self) -> None:
        from src.article_extractor import BlockKind, SubstackExtractor
        text = "# Title here\n\n## " + LONG_ONE[:150] + "\n\nSome body text follows here in prose form."
        blocks = SubstackExtractor().extract_from_text(text).blocks
        assert any(b.kind is BlockKind.HEADING and b.text.startswith("Teams that treat") for b in blocks)



# ============================================================================ D-6 keyword quality (Phase 9 evaluation)
def _kw(*blocks):  # type: ignore[no-untyped-def]
    from tests.helpers import content_of
    return {k.term: k for k in content_of(*blocks).keywords}


class TestKeywordQuality:
    def test_generic_everyday_words_are_not_key_terms(self) -> None:
        from tests.helpers import P
        text = "This post covers one example. Another post gives a second example. A third post has a third example."
        kws = _kw(P(text), P("Reading each post helps. Every example is short, and the example list grows."))
        assert "post" not in kws and "example" not in kws and "posts" not in kws

    def test_a_generic_word_that_the_article_defines_is_kept(self) -> None:
        from tests.helpers import P
        kws = _kw(P("A post is a unit of writing that a publication sends to its readers by email."),
                  P("Each post can be free or paid, and every post has a title."))
        assert any(k.lower() == "post" for k in kws)

    def test_technical_single_words_with_ordinary_names_survive(self) -> None:
        from tests.helpers import P
        text = " ".join(["The list stores values and the page cache stores pages."] * 3)
        kws = _kw(P(text))
        assert "page" in kws  # words outside the short generic list are never dropped for being common

    def test_meaningful_multiword_concepts_survive(self) -> None:
        from tests.helpers import P
        kws = _kw(P("Gradient descent updates weights. Gradient descent needs a learning rate."),
                  P("The learning rate scales every gradient descent step."))
        assert "gradient descent" in kws and "learning rate" in kws

    def test_undefined_two_letter_acronyms_are_dropped_but_longer_and_defined_ones_stay(self) -> None:
        from tests.helpers import P
        kws = _kw(P("AI is everywhere. The GPU speeds up training, and a TPU does too."),
                  P("We use retrieval-augmented generation (RAG) to ground answers, and RAG helps."))
        assert "AI" not in kws and "GPU" in kws and "RAG" in kws

    def test_code_identifiers_are_kept_and_compound_ones_rank_above_plain_names(self) -> None:
        from tests.helpers import C, P
        kws = _kw(P("The article is about a small storage class and nothing else of note appears here."),
                  C("class VectorStore:\n    def fetch(self, query):\n        pass"))
        assert "VectorStore" in kws and "fetch" in kws and kws["VectorStore"].score > kws["fetch"].score

    def test_heading_only_terms_never_outrank_terms_the_prose_uses(self) -> None:
        from tests.helpers import H, P
        kws = _kw(H("Common failure modes"), P("Latency matters. Latency again. Reducing latency needs caching."))
        assert kws["Common failure modes"].score < kws["latency"].score

    def test_ranking_is_deterministic_across_runs_and_fixtures(self) -> None:
        from tests.helpers import fixture_content
        for name in ("substack_free.html", "substack_paywalled.html", "user_notes.txt"):
            a = [k.term for k in fixture_content(name).keywords]
            b = [k.term for k in fixture_content(name).keywords]
            assert a == b and a

    def test_the_rag_fixture_keeps_its_technical_terms_and_drops_weak_ones(self) -> None:
        from tests.helpers import fixture_content
        terms = [k.term for k in fixture_content("substack_free.html").keywords]
        for wanted in ("Retrieval-Augmented Generation", "RAG", "Chunk size", "embedding", "chunk", "retrieval"):
            assert wanted in terms
        assert "AI" not in terms and terms.index("Common failure modes") == len(terms) - 1



# ============================================================================ D-7 grounding review (scripted model, no live generation)
SUPPORTED = "Small chunks are precise but lose surrounding context, while large chunks keep context but dilute the signal."
PARTLY = "Chunk size also determines how fast the whole indexing pipeline finishes on modest hardware."
UNSUPPORTED = "This approach is used instead of complex vector databases and keeps data away from external servers."


@pytest.fixture(scope="module")
def grounded_notes():  # type: ignore[no-untyped-def]
    import json
    from src.notes_generator import NotesConfig, NotesGenerator
    from tests.helpers import ScriptedLLM, fixture_content, payload_of

    def keywords(user: str) -> str:
        return json.dumps({"terms": [{"term": t["term"], "explanation": f"{SUPPORTED} {PARTLY} {UNSUPPORTED}",
                                      "significance": "Studies show it improves accuracy by 40 percent in most systems."}
                                     for t in payload_of(user)]})

    def analogies(user: str) -> str:
        terms = [x["term"] for x in payload_of(user)]
        return json.dumps({"examples": [{"concept": terms[0], "kind": "analogy",
                                         "text": "Think of the idea like a librarian fetching a book rather than a robot memorising a whole warehouse of shelves."}]})

    content = fixture_content("substack_free.html")
    notes = NotesGenerator(ScriptedLLM({"keyword explanations": keywords, "analogies": analogies}),
                           NotesConfig(include_questions=False)).generate(content)
    return notes, content


class TestGroundingReview:
    def _ai_text(self, notes):  # type: ignore[no-untyped-def]
        return " ".join(i.text or "" for i in notes.all_items() if i.provenance.value == "ai_generated")

    def test_supported_text_is_kept(self, grounded_notes) -> None:  # type: ignore[no-untyped-def]
        notes, _ = grounded_notes
        assert SUPPORTED in self._ai_text(notes)

    def test_unsupported_comparisons_are_removed_everywhere(self, grounded_notes) -> None:  # type: ignore[no-untyped-def]
        notes, _ = grounded_notes
        assert "vector databases and keeps data away" not in self._ai_text(notes)
        assert any("Removed an AI-written statement" in w and "does not support it" in w for w in notes.generation_warnings)

    def test_invented_numbers_and_attributions_reject_the_whole_text(self, grounded_notes) -> None:  # type: ignore[no-untyped-def]
        notes, _ = grounded_notes
        assert "40 percent" not in self._ai_text(notes) and "Studies show" not in self._ai_text(notes)
        assert any("Rejected an AI-generated significance" in w for w in notes.generation_warnings)

    def test_partly_supported_statements_are_kept_with_a_review_warning(self, grounded_notes) -> None:  # type: ignore[no-untyped-def]
        notes, _ = grounded_notes
        assert PARTLY in self._ai_text(notes)
        assert any(w.startswith("Review needed: the AI-written explanation") and "only partly" in w for w in notes.generation_warnings)

    def test_removed_text_is_not_repeated_in_the_warnings(self, grounded_notes) -> None:  # type: ignore[no-untyped-def]
        notes, _ = grounded_notes
        assert not any("instead of complex vector databases" in w for w in notes.generation_warnings)

    def test_comparison_analogies_get_a_review_marker_and_stay_labelled(self, grounded_notes) -> None:  # type: ignore[no-untyped-def]
        notes, _ = grounded_notes
        section = next(s for s in notes.chapter(4).sections if s.heading.startswith("Analogies"))
        items = [i for i in section.items if i.kind.value != "notice"]
        assert items and all(i.provenance.value == "ai_generated" and i.label.startswith("AI-generated") for i in items)
        assert all(i.label.endswith("Review needed") for i in items)

    def test_chapter_4_keeps_its_explicit_not_from_the_article_warning(self, grounded_notes) -> None:  # type: ignore[no-untyped-def]
        notes, _ = grounded_notes
        section = next(s for s in notes.chapter(4).sections if s.heading.startswith("Analogies"))
        first = section.items[0]
        assert first.kind.value == "notice" and first.level == "warning" and "NOT from the article" in first.text

    def test_the_warning_banner_is_in_the_word_document(self, grounded_notes) -> None:  # type: ignore[no-untyped-def]
        from io import BytesIO
        from docx import Document
        from src.word_generator import WordGenerator
        notes, _ = grounded_notes
        text = "\n".join(p.text for p in Document(BytesIO(WordGenerator().to_bytes(notes))).paragraphs)
        assert "NOT from the article" in text and "Review needed" in text and "AI-generated" in text

    def test_source_references_are_valid_and_original_text_is_faithful(self, grounded_notes) -> None:  # type: ignore[no-untyped-def]
        from src.notes_generator import validate_notes, verify_provenance
        from src.backends.base import QuestionSpec
        notes, content = grounded_notes
        assert verify_provenance(notes, content) == []
        blocks = len(content.article.blocks) if hasattr(content, "article") else 21
        for item in notes.all_items():
            assert all(0 <= i < blocks for i in item.source_indices)

    def test_the_support_check_is_documented_as_vocabulary_only(self) -> None:
        import src.backends.llm as llm
        doc = llm.assess_support.__doc__ or ""
        assert "proves nothing about truth" in doc
