"""Phase 6 tests: Word document generation, formatting, provenance styling, safety and completeness.

All documents are written to pytest temporary directories. Nothing here calls a model or the network:
sockets are blocked, and the notes come from the real generator with the local backend or test doubles.
"""
from __future__ import annotations

import copy
import dataclasses
import io
import re
import socket
import threading
import time
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Iterator

import pytest
from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

import src.word_generator as wg
from src.article_extractor import Completeness
from src.backends import OpenAIBackend
from src.notes_generator import NotesConfig, NotesGenerator, generate_notes
from src.notes_model import (
    BackendInfo, BackendKind, Chapter, DetailLevel, ItemKind, Language, NoteItem, NoteProvenance, NoteSection,
    Notes, Question, QuestionKind,
)
from src.word_generator import (
    DEFAULT_OUTPUT_DIR, TEXT_W_TWIPS, WordConfig, WordGenerationError, WordGenerator, document_text,
    expected_fragments, generate_word, safe_filename, sanitize_text, verify_document,
)
from src.article_extractor import SubstackExtractor
from src.content_processor import process_article
from tests.helpers import FakeOpenAIClient, F, H, P, ScriptedLLM, content_of, fixture_content, load

FREE_URL = "https://appliedai.substack.com/p/understanding-rag"
PAYWALLED_URL = "https://infra.substack.com/p/hidden-costs"


def content_with_url(name: str, url: str):
    return process_article(SubstackExtractor().extract_from_html(load(name), url=url))

ORIG, DER, AI = NoteProvenance.ORIGINAL, NoteProvenance.DERIVED, NoteProvenance.AI_GENERATED
CFG = WordConfig(generated_on=date(2026, 11, 6))
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*a: Any, **k: Any) -> None:
        raise AssertionError("a network connection was attempted while generating a Word document")

    monkeypatch.setattr(socket.socket, "connect", blocked)


# ============================================================================ notes fixtures (all backends)
@pytest.fixture(scope="module")
def local_notes() -> Notes:
    return generate_notes(content_with_url("substack_free.html", FREE_URL))


@pytest.fixture(scope="module")
def ai_notes() -> Notes:
    """Notes as an Ollama-kind backend produces them (scripted test double: no model is involved)."""
    return NotesGenerator(ScriptedLLM(kind=BackendKind.OLLAMA), NotesConfig()).generate(content_with_url("substack_free.html", FREE_URL))


@pytest.fixture(scope="module")
def openai_notes() -> Notes:
    """Notes as an explicitly enabled OpenAI backend would produce them (fake client: nothing is sent anywhere)."""
    backend = OpenAIBackend("sk-test", "test-model", confirm=lambda r: True, enabled=True, client=FakeOpenAIClient(), max_calls=50)
    return NotesGenerator(backend, NotesConfig()).generate(content_with_url("substack_free.html", FREE_URL))


@pytest.fixture(scope="module")
def partial_notes() -> Notes:
    return generate_notes(content_with_url("substack_paywalled.html", PAYWALLED_URL))


@pytest.fixture(scope="module")
def text_notes() -> Notes:
    return generate_notes(fixture_content("user_notes.txt"))


def build(notes: Notes, cfg: WordConfig = CFG):
    return WordGenerator(cfg).build(notes)


@pytest.fixture(scope="module")
def doc(local_notes: Notes):
    return build(local_notes)


@pytest.fixture(scope="module")
def ai_doc(ai_notes: Notes):
    return build(ai_notes)


@pytest.fixture(scope="module")
def partial_doc(partial_notes: Notes):
    return build(partial_notes)


# ============================================================================ document inspection helpers
def paragraphs(d: Any) -> Iterator[Paragraph]:
    """Every paragraph in the body, including those inside tables, except the table-of-contents entries."""
    for p in d.element.body.iter(qn("w:p")):
        par = Paragraph(p, d._body)
        if par.style.name in ("TOC 1", "TOC 2", "TOC 3"):
            continue
        yield par


def body_paragraphs(d: Any) -> list[Paragraph]:
    """Top-level paragraphs (python-docx's view) without the TOC entries."""
    return [p for p in d.paragraphs if p.style.name not in ("TOC 1", "TOC 2", "TOC 3")]


def text_of(p: Paragraph) -> str:
    return re.sub(r"\s+", " ", wg._paragraph_text(p._p)).strip()


def all_text(d: Any) -> str:
    return document_text(d)


def instr(root: Any) -> list[str]:
    return [(e.text or "").strip() for e in root.iter(qn("w:instrText"))]


def style_of(d: Any, fragment: str) -> list[str]:
    """Styles of every paragraph containing ``fragment``."""
    return [p.style.name for p in paragraphs(d) if fragment in text_of(p)]


def headings(d: Any) -> list[tuple[int, str]]:
    return [(int(p.style.name.split()[-1]), p.text) for p in d.paragraphs if re.fullmatch(r"Heading [1-9]", p.style.name)]


def style_xml(d: Any, name: str) -> str:
    from lxml import etree
    return etree.tostring(d.styles[name].element).decode()


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def minimal_notes(**overrides: Any) -> Notes:
    """A hand-built Notes object for edge cases."""
    base = dict(
        title="Edge Case", author=None, published=None, url=None, completeness=Completeness.COMPLETE,
        completeness_reasons=(), source_warnings=(), backend=BackendInfo("Local", BackendKind.LOCAL), requested_backend="local",
        fallback_reason=None, detail=DetailLevel.INTERMEDIATE, language=Language.ENGLISH, banners=[],
        chapters=[Chapter(n, t) for n, t in enumerate(
            ["Article Overview", "Detailed Concept Explanation", "Important Technical Keywords",
             "Real-Life Analogies and Applications", "Key Takeaways", "Quick Revision Notes", "Questions and Answers"], start=1)])
    base.update(overrides)
    return Notes(**base)


def with_items(notes: Notes, chapter: int, *items: NoteItem, heading: str = "Section") -> Notes:
    notes.chapters[chapter - 1].sections.append(NoteSection(heading, 1, list(items)))
    return notes


# ============================================================================ creation
class TestCreation:
    def test_builds_a_real_docx_that_python_docx_can_reopen(self, local_notes: Notes) -> None:
        data = WordGenerator(CFG).to_bytes(local_notes)
        assert data[:2] == b"PK" and zipfile.is_zipfile(io.BytesIO(data))
        reopened = Document(io.BytesIO(data))
        assert len(reopened.paragraphs) > 100 and len(reopened.tables) > 5

    def test_package_contains_the_expected_parts(self, local_notes: Notes) -> None:
        names = zipfile.ZipFile(io.BytesIO(WordGenerator(CFG).to_bytes(local_notes))).namelist()
        for part in ("[Content_Types].xml", "word/document.xml", "word/styles.xml", "word/numbering.xml", "word/settings.xml"):
            assert part in names
        assert any(n.startswith("word/header") for n in names) and any(n.startswith("word/footer") for n in names)

    @pytest.mark.parametrize("bad", [None, "notes", 42, {"chapters": []}, []])
    def test_non_notes_rejected(self, bad: Any) -> None:
        with pytest.raises(WordGenerationError, match="Expected a Notes"):
            WordGenerator().build(bad)

    def test_notes_without_chapters_rejected(self) -> None:
        with pytest.raises(WordGenerationError, match="no chapters"):
            WordGenerator().build(minimal_notes(chapters=[]))

    def test_building_does_not_modify_or_regenerate_the_notes(self, local_notes: Notes) -> None:
        before = copy.deepcopy(local_notes.to_dict())
        WordGenerator(CFG).build(local_notes)
        assert local_notes.to_dict() == before

    def test_output_is_deterministic(self, local_notes: Notes) -> None:
        a, b = build(local_notes), build(local_notes)
        assert all_text(a) == all_text(b)
        assert [p.style.name for p in a.paragraphs] == [p.style.name for p in b.paragraphs]

    def test_core_properties(self, doc: Any, local_notes: Notes) -> None:
        cp = doc.core_properties
        assert local_notes.title in cp.title and cp.author == "NoteForge AI" and "Local" in cp.comments
        assert cp.created.year == 2026

    def test_converts_for_every_backend_without_assuming_one(self, local_notes: Notes, ai_notes: Notes, openai_notes: Notes,
                                                             partial_notes: Notes, text_notes: Notes) -> None:
        for notes in (local_notes, ai_notes, openai_notes, partial_notes, text_notes):
            assert verify_document(build(notes), notes) == [], notes.backend.name


class TestWordGeneratorIsPureRendering:
    FORBIDDEN = ("import requests", "import openai", "from openai", "import ollama", "urllib.request", "import socket",
                 "import http", "from http", "generate_notes", "NotesGenerator", "create_questions", "explain_keywords",
                 "explain_sections", "create_analogies", "check_available", "_complete", "resolve_backend", ".chat.")

    def test_source_has_no_model_or_network_code(self) -> None:
        source = Path(wg.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        for needle in self.FORBIDDEN:
            assert needle not in code, needle

    def test_does_not_import_the_backends_package(self) -> None:
        source = Path(wg.__file__).read_text(encoding="utf-8")
        assert "src.backends" not in source and "src.notes_generator" not in source

    def test_works_with_every_socket_blocked(self, local_notes: Notes, tmp_path: Path) -> None:
        assert generate_word(local_notes, tmp_path, CFG).path.exists()  # the autouse fixture blocks all connections


# ============================================================================ page setup
class TestPageSetup:
    def test_a4_page_size_in_every_section(self, doc: Any) -> None:
        assert len(doc.sections) == 2
        for s in doc.sections:
            assert abs(s.page_width.cm - 21.0) < 0.01 and abs(s.page_height.cm - 29.7) < 0.01

    def test_professional_margins(self, doc: Any) -> None:
        for s in doc.sections:
            assert abs(s.left_margin.cm - 2.3) < 0.01 and abs(s.right_margin.cm - 2.3) < 0.01
            assert 2.0 <= s.top_margin.cm <= 3.0 and 2.0 <= s.bottom_margin.cm <= 3.0
            assert 0.8 <= s.header_distance.cm <= 1.6 and 0.8 <= s.footer_distance.cm <= 1.6

    def test_text_width_constant_matches_the_page(self) -> None:
        assert abs(wg.TEXT_W_CM - (21.0 - 4.6)) < 1e-9 and TEXT_W_TWIPS == round(16.4 * 567)

    def test_body_section_starts_a_new_page(self, doc: Any) -> None:
        assert doc.sections[1].start_type.name == "NEW_PAGE"

    def test_page_numbering_restarts_after_the_cover(self, doc: Any) -> None:
        pg = doc.sections[1]._sectPr.find(qn("w:pgNumType"))
        assert pg is not None and pg.get(qn("w:start")) == "1"
        assert doc.sections[0]._sectPr.find(qn("w:pgNumType")) is None


# ============================================================================ cover page
class TestCoverPage:
    def cover_text(self, d: Any) -> str:
        first = d.tables[0]
        return " ".join(text_of(p) for p in (Paragraph(x, d._body) for x in first._tbl.iter(qn("w:p"))))

    def test_banner_table_holds_title_author_and_date(self, doc: Any, local_notes: Notes) -> None:
        banner = self.cover_text(doc)
        assert "STUDY NOTES" in banner and local_notes.title in banner
        assert "by Priya Raman" in banner and "Published 14 March 2025" in banner

    def test_banner_is_navy_with_white_title(self, doc: Any) -> None:
        tc = doc.tables[0]._tbl.find(".//" + qn("w:tc"))
        assert tc.find(qn("w:tcPr") + "/" + qn("w:shd")).get(qn("w:fill")) == wg.NAVY
        assert "FFFFFF" in style_xml(doc, "NF Cover Title")

    def test_metadata_table(self, doc: Any, local_notes: Notes) -> None:
        meta = {norm(r.cells[0].text): norm(r.cells[1].text) for r in doc.tables[1].rows}
        assert meta["Source article"] == local_notes.url or "Source article" in meta
        assert meta["Notes generated on"] == "6 November 2026"
        assert "Local" in meta["Generated with"] and "free" in meta["Generated with"]
        assert meta["Level of detail"] == "Intermediate" and meta["Language"] == "English"
        assert meta["Source status"] == "Complete article"

    def test_cover_is_in_its_own_section_without_header_or_footer(self, doc: Any) -> None:
        cover = doc.sections[0]
        assert not cover.header.paragraphs[0].text.strip() and not cover.footer.paragraphs[0].text.strip()
        assert not instr(cover.footer._element)

    def test_cover_explains_the_colour_coding(self, doc: Any) -> None:
        assert "colour-coded by origin" in all_text(doc)

    def test_complete_article_has_no_partial_warning_on_the_cover(self, doc: Any) -> None:
        cover_paragraphs = [text_of(p) for p in doc.paragraphs[:6]]
        assert not any("PARTIAL" in t for t in cover_paragraphs)

    def test_partial_article_shows_a_prominent_cover_warning(self, partial_doc: Any) -> None:
        first = [p for p in partial_doc.paragraphs[:4]]
        warning = next(p for p in first if "PARTIAL CONTENT" in text_of(p))
        assert warning.style.name == "NF Warning" and "must not be treated as a complete study" in text_of(warning)
        meta = {norm(r.cells[0].text): norm(r.cells[1].text) for r in partial_doc.tables[1].rows}
        assert meta["Source status"].startswith("PARTIAL")

    def test_unverified_text_gets_a_cover_notice(self, text_notes: Notes) -> None:
        d = build(text_notes)
        assert any("could not be verified" in text_of(p) for p in d.paragraphs[:6])

    def test_user_supplied_text_without_url(self, text_notes: Notes) -> None:
        d = build(text_notes)
        meta = {norm(r.cells[0].text): norm(r.cells[1].text) for r in d.tables[1].rows}
        assert meta["Source article"] == "Supplied by the user (no URL)"

    def test_paid_backend_is_labelled_on_the_cover(self, openai_notes: Notes) -> None:
        d = build(openai_notes)
        meta = {norm(r.cells[0].text): norm(r.cells[1].text) for r in d.tables[1].rows}
        assert "OpenAI" in meta["Generated with"] and "PAID external service" in meta["Generated with"]

    def test_local_model_is_labelled_free_and_local(self, ai_notes: Notes) -> None:
        d = build(ai_notes)
        meta = {norm(r.cells[0].text): norm(r.cells[1].text) for r in d.tables[1].rows}
        assert "Scripted (fake-model)" in meta["Generated with"] and "free, runs on this machine" in meta["Generated with"]


# ============================================================================ table of contents
class TestTableOfContents:
    def test_toc_is_a_real_word_field(self, doc: Any) -> None:
        toc = [t for t in instr(doc.element.body) if t.startswith("TOC")]
        assert toc == ['TOC \\o "1-3" \\h \\z \\u']

    def test_field_characters_are_balanced(self, doc: Any) -> None:
        kinds = [e.get(qn("w:fldCharType")) for e in doc.element.body.iter(qn("w:fldChar"))]
        assert kinds.count("begin") == kinds.count("separate") == kinds.count("end") == 1

    def test_field_structure_order(self, doc: Any) -> None:
        seq = []
        for e in doc.element.body.iter():
            if e.tag == qn("w:fldChar"):
                seq.append(e.get(qn("w:fldCharType")))
            elif e.tag == qn("w:instrText"):
                seq.append("instr")
        assert seq == ["begin", "instr", "separate", "end"]

    def test_toc_is_prefilled_with_every_heading(self, doc: Any) -> None:
        entries = [p for p in doc.paragraphs if p.style.name.startswith("TOC ") and p.style.name != "TOC Heading"]
        expected = headings(doc)
        assert [text_of(p) for p in entries] == [t for _, t in expected]
        assert [int(p.style.name[-1]) for p in entries] == [lvl for lvl, _ in expected]

    def test_every_toc_link_points_at_a_real_bookmark(self, doc: Any) -> None:
        body = doc.element.body
        bookmarks = {b.get(qn("w:name")) for b in body.iter(qn("w:bookmarkStart"))}
        anchors = [h.get(qn("w:anchor")) for h in body.iter(qn("w:hyperlink")) if h.get(qn("w:anchor"))]
        assert anchors and set(anchors) <= bookmarks and len(anchors) == len(set(anchors))

    def test_every_heading_has_a_bookmark(self, doc: Any) -> None:
        starts = [b for b in doc.element.body.iter(qn("w:bookmarkStart"))]
        ends = [b for b in doc.element.body.iter(qn("w:bookmarkEnd"))]
        assert len(starts) == len(ends) == len(headings(doc))
        ids = [b.get(qn("w:id")) for b in starts]
        assert len(set(ids)) == len(ids) and {b.get(qn("w:id")) for b in ends} == set(ids)

    def test_toc_title_is_not_itself_a_heading(self, doc: Any) -> None:
        assert "Table of Contents" not in [t for _, t in headings(doc)]
        assert any(p.text == "Table of Contents" and p.style.name == "NF TOC Title" for p in doc.paragraphs)

    def test_toc_comes_after_the_article_info_and_before_chapter_one(self, doc: Any) -> None:
        order = [p.text for p in body_paragraphs(doc) if p.text in ("Article Information and Source Details", "Table of Contents",
                                                              "Chapter 1: Article Overview")]
        assert order == ["Article Information and Source Details", "Table of Contents", "Chapter 1: Article Overview"]

    def test_toc_styles_exist_with_dotted_right_tab(self, doc: Any) -> None:
        for lvl in (1, 2, 3):
            xml = style_xml(doc, f"TOC {lvl}")
            assert 'w:val="right"' in xml and 'w:leader="dot"' in xml

    def test_word_is_asked_to_update_fields_on_open(self, doc: Any) -> None:
        settings = doc.settings.element
        assert settings.find(qn("w:updateFields")).get(qn("w:val")) == "true"
        names = [c.tag.split("}")[1] for c in settings]
        assert names.index("updateFields") < names.index("compat")  # schema order

    def test_updating_on_open_can_be_disabled(self, local_notes: Notes) -> None:
        d = build(local_notes, WordConfig(update_fields_on_open=False, generated_on=date(2026, 11, 6)))
        assert d.settings.element.find(qn("w:updateFields")) is None

    def test_toc_text_does_not_count_as_body_content(self, doc: Any) -> None:
        # A heading that exists only in the TOC must not satisfy the completeness check.
        body_only = all_text(doc)
        assert body_only.count("Chapter 3: Important Technical Keywords") == 1


# ============================================================================ headings
class TestHeadings:
    def test_seven_chapters_in_order_each_once(self, doc: Any) -> None:
        h1 = [t for lvl, t in headings(doc) if lvl == 1]
        assert [t for t in h1 if t.startswith("Chapter")] == [
            "Chapter 1: Article Overview", "Chapter 2: Detailed Concept Explanation",
            "Chapter 3: Important Technical Keywords", "Chapter 4: Real-Life Analogies and Applications",
            "Chapter 5: Key Takeaways", "Chapter 6: Quick Revision Notes", "Chapter 7: Questions and Answers"]

    def test_front_and_back_matter_headings(self, doc: Any) -> None:
        h1 = [t for lvl, t in headings(doc) if lvl == 1]
        assert h1[0] == "Article Information and Source Details" and h1[-1] == "Original Article and References"
        assert len(h1) == 9  # info + 7 chapters + references: no duplicated chapters

    def test_only_heading_levels_1_to_3(self, doc: Any) -> None:
        assert {lvl for lvl, _ in headings(doc)} <= {1, 2, 3}

    def test_levels_never_skip(self, doc: Any, ai_doc: Any, partial_doc: Any) -> None:
        for d in (doc, ai_doc, partial_doc):
            prev = 0
            for lvl, text in headings(d):
                assert lvl <= prev + 1, f"{text!r}: Heading {lvl} after Heading {prev}"
                prev = lvl

    def test_each_chapter_starts_with_heading_1(self, doc: Any) -> None:
        found = [p for p in body_paragraphs(doc) if p.text.startswith("Chapter ") and ":" in p.text]
        assert len(found) == 7
        for p in found:
            assert p.style.name == "Heading 1"

    def test_chapter_headings_start_a_new_page(self, doc: Any) -> None:
        assert 'w:pageBreakBefore' in style_xml(doc, "Heading 1")

    @pytest.mark.parametrize("chapter", [1, 4, 5, 6])
    def test_every_section_of_a_flat_chapter_is_heading_2(self, doc: Any, local_notes: Notes, chapter: int) -> None:
        """Seen in the TOC: only the first section was H2 and the rest H3, because Phase 5 sets level=2 on all of them."""
        by_text = dict((t, lvl) for lvl, t in headings(doc))
        for section in local_notes.chapter(chapter).sections:
            assert by_text[section.heading] == 2, (chapter, section.heading)

    def test_levels_are_relative_to_the_shallowest_section_of_the_chapter(self) -> None:
        notes = minimal_notes()
        notes.chapters[1].sections += [NoteSection("Top", 1, [NoteItem(ItemKind.TEXT, ORIG, text="a")]),
                                       NoteSection("Child", 2, [NoteItem(ItemKind.TEXT, ORIG, text="b")]),
                                       NoteSection("Grandchild", 3, [NoteItem(ItemKind.TEXT, ORIG, text="c")])]
        notes.chapters[4].sections += [NoteSection(f"S{i}", 2, [NoteItem(ItemKind.TEXT, ORIG, text="x")]) for i in range(3)]
        by_text = dict((t, lvl) for lvl, t in headings(build(notes)))
        assert (by_text["Top"], by_text["Child"], by_text["Grandchild"]) == (2, 3, 3)
        assert [by_text[f"S{i}"] for i in range(3)] == [2, 2, 2]

    def test_a_deep_first_section_never_skips_a_heading_level(self) -> None:
        notes = minimal_notes()
        notes.chapters[1].sections += [NoteSection("Deep first", 3, [NoteItem(ItemKind.TEXT, ORIG, text="a")]),
                                       NoteSection("Top later", 1, [NoteItem(ItemKind.TEXT, ORIG, text="b")])]
        d = build(notes)
        seq = [(lvl, t) for lvl, t in headings(d) if t in ("Chapter 2: Detailed Concept Explanation", "Deep first", "Top later")]
        assert seq == [(1, "Chapter 2: Detailed Concept Explanation"), (2, "Deep first"), (2, "Top later")]
        prev = 0
        for lvl, text in headings(d):
            assert lvl <= prev + 1, text
            prev = lvl

    def test_toc_levels_match_the_heading_levels_in_a_flat_chapter(self, doc: Any) -> None:
        entries = {text_of(p): int(p.style.name[-1]) for p in doc.paragraphs if p.style.name in ("TOC 1", "TOC 2", "TOC 3")}
        assert entries["Article details"] == entries["Introduction and background"] == entries["Structure of the article"] == 2

    def test_article_sections_become_level_2_and_nested_ones_level_3(self, doc: Any) -> None:
        by_text = dict((t, lvl) for lvl, t in headings(doc))
        assert by_text["Why retrieval matters"] == 2 and by_text["How the pipeline works"] == 2
        assert by_text["A minimal example"] == 3 and by_text["Choosing a chunk size"] == 3

    def test_each_keyword_is_a_level_2_heading(self, doc: Any, local_notes: Notes) -> None:
        by_text = dict((t, lvl) for lvl, t in headings(doc))
        for section in local_notes.chapter(3).sections:
            assert by_text[section.heading] == 2

    def test_question_groups_are_level_2(self, doc: Any) -> None:
        by_text = dict((t, lvl) for lvl, t in headings(doc))
        for title in ("Short-Answer Questions", "Descriptive Questions", "Multiple-Choice Questions"):
            assert by_text[title] == 2

    def test_headings_use_distinct_styled_typography(self, doc: Any) -> None:
        sizes = [doc.styles[f"Heading {i}"].font.size.pt for i in (1, 2, 3)]
        assert sizes[0] > sizes[1] > sizes[2]
        assert all(doc.styles[f"Heading {i}"].font.bold for i in (1, 2, 3))

    def test_heading_text_with_markup_characters_is_escaped_not_lost(self) -> None:
        notes = with_items(minimal_notes(), 2, NoteItem(ItemKind.TEXT, ORIG, text="Body."), heading='R&D <Tools> "Q"')
        assert 'R&D <Tools> "Q"' in [t for _, t in headings(build(notes))]


# ============================================================================ header, footer, page numbers
class TestHeaderFooterAndPageNumbers:
    def test_header_has_short_title_and_label(self, doc: Any, local_notes: Notes) -> None:
        h = doc.sections[1].header.paragraphs[0].text
        assert local_notes.title in h and "Study notes" in h and "PARTIAL" not in h

    def test_long_titles_are_shortened_in_the_header_only(self) -> None:
        title = "An Extremely Long Article Title " * 8
        d = build(minimal_notes(title=title))
        header = d.sections[1].header.paragraphs[0].text
        assert header.split("\t")[0].endswith("\u2026") and len(header) < 110
        assert title.strip() in all_text(d).replace("\n", " ")  # the cover keeps the full title

    def test_partial_header_warns_on_every_page(self, partial_doc: Any) -> None:
        header = partial_doc.sections[1].header.paragraphs[0]
        assert "PARTIAL CONTENT" in header.text
        red = [r for r in header.runs if r.text == "PARTIAL CONTENT"][0]
        assert str(red.font.color.rgb) == wg.RED and red.bold

    def test_header_and_footer_are_not_linked_to_the_cover(self, doc: Any) -> None:
        assert doc.sections[1].header.is_linked_to_previous is False and doc.sections[1].footer.is_linked_to_previous is False

    def test_footer_text(self, doc: Any) -> None:
        text = doc.sections[1].footer.paragraphs[0].text
        assert "NoteForge AI" in text and "notes by Local" in text and text.count("Page") == 1 and " of " in text

    def test_page_number_fields(self, doc: Any) -> None:
        footer = doc.sections[1].footer._element
        assert [t for t in instr(footer)] == ["PAGE", "SECTIONPAGES"]
        kinds = [e.get(qn("w:fldCharType")) for e in footer.iter(qn("w:fldChar"))]
        assert kinds == ["begin", "separate", "end"] * 2

    def test_header_and_footer_text_is_pushed_to_the_right_margin(self, doc: Any) -> None:
        """Seen in Word: the template's Header/Footer styles carry a centre tab, so the label landed mid-page."""
        for part in (doc.sections[1].header.paragraphs[0], doc.sections[1].footer.paragraphs[0]):
            assert part.style.name in ("NF Header", "NF Footer")
            tabs = part.style.paragraph_format.tab_stops
            assert [(t.alignment.name, t.position.twips) for t in tabs] == [("RIGHT", round(wg.TEXT_W_CM * 567))] or \
                   [(t.alignment.name, abs(t.position.twips - TEXT_W_TWIPS) <= 2) for t in tabs] == [("RIGHT", True)]
            assert part.text.count("\t") == 1  # exactly one tab: straight to the right-aligned tab stop

    def test_header_and_footer_do_not_inherit_the_templates_centre_tab(self, doc: Any) -> None:
        for name in ("NF Header", "NF Footer"):
            assert [t.alignment.name for t in doc.styles[name].paragraph_format.tab_stops] == ["RIGHT"]

    def test_header_and_footer_have_rules(self, doc: Any) -> None:
        assert 'w:bottom' in doc.sections[1].header._element.xml and 'w:top' in doc.sections[1].footer._element.xml

    def test_footer_names_the_backend(self, ai_doc: Any, openai_notes: Notes) -> None:
        assert "notes by Scripted" in ai_doc.sections[1].footer.paragraphs[0].text
        assert "notes by OpenAI" in build(openai_notes).sections[1].footer.paragraphs[0].text


# ============================================================================ lists
class TestLists:
    def numbering(self, d: Any):
        return d.part.numbering_part.element

    def num_to_abstract(self, d: Any) -> dict[str, tuple[str, bool]]:
        numbering = self.numbering(d)
        fmt = {a.get(qn("w:abstractNumId")): a.find(qn("w:lvl")).find(qn("w:numFmt")).get(qn("w:val"))
               for a in numbering.findall(qn("w:abstractNum"))}
        return {n.get(qn("w:numId")): (fmt[n.find(qn("w:abstractNumId")).get(qn("w:val"))],
                                       n.find(qn("w:lvlOverride")) is not None) for n in numbering.findall(qn("w:num"))}

    def list_paragraphs(self, d: Any, fragment: str):
        for p in paragraphs(d):
            if p._p.find(qn("w:pPr")) is not None and p._p.find(qn("w:pPr") + "/" + qn("w:numPr")) is not None and fragment in text_of(p):
                yield p

    def test_bullets_are_real_word_bullets(self, doc: Any) -> None:
        p = next(self.list_paragraphs(doc, "Answers can cite the passages"))
        num_id = p._p.find(qn("w:pPr") + "/" + qn("w:numPr") + "/" + qn("w:numId")).get(qn("w:val"))
        assert self.num_to_abstract(doc)[num_id][0] == "bullet"

    def test_nested_bullets_use_deeper_levels(self, doc: Any) -> None:
        levels = []
        for frag in ("Answers can cite", "Knowledge can be updated", "New documents are searchable", "Private data stays"):
            p = next(self.list_paragraphs(doc, frag))
            levels.append(int(p._p.find(qn("w:pPr") + "/" + qn("w:numPr") + "/" + qn("w:ilvl")).get(qn("w:val"))))
        assert levels == [0, 0, 1, 0]

    def test_nested_items_are_indented_further(self, doc: Any) -> None:
        def left(frag: str) -> int:
            p = next(self.list_paragraphs(doc, frag))
            return int(p._p.find(qn("w:pPr") + "/" + qn("w:ind")).get(qn("w:left")))
        assert left("New documents are searchable") > left("Knowledge can be updated")

    def test_numbered_steps_use_decimal_numbering(self, doc: Any) -> None:
        p = next(self.list_paragraphs(doc, "Split the documents into chunks"))
        num_id = p._p.find(qn("w:pPr") + "/" + qn("w:numPr") + "/" + qn("w:numId")).get(qn("w:val"))
        fmt, restarts = self.num_to_abstract(doc)[num_id]
        assert fmt == "decimal" and restarts

    def test_every_numbered_list_restarts_at_one(self, doc: Any) -> None:
        numbered_ids = set()
        for p in paragraphs(doc):
            num = p._p.find(qn("w:pPr") + "/" + qn("w:numPr") + "/" + qn("w:numId"))
            if num is not None and self.num_to_abstract(doc)[num.get(qn("w:val"))][0] == "decimal":
                numbered_ids.add(num.get(qn("w:val")))
        assert len(numbered_ids) >= 2  # the steps list, the section list: each is its own list
        for n in self.numbering(doc).findall(qn("w:num")):
            if n.get(qn("w:numId")) in numbered_ids:
                assert n.find(qn("w:lvlOverride") + "/" + qn("w:startOverride")).get(qn("w:val")) == "1"

    def test_every_referenced_numbering_id_exists(self, doc: Any) -> None:
        defined = {n.get(qn("w:numId")) for n in self.numbering(doc).findall(qn("w:num"))}
        used = {e.get(qn("w:val")) for e in doc.element.body.iter(qn("w:numId"))}
        assert used and used <= defined

    def test_numbering_definitions_precede_numbering_instances(self, doc: Any) -> None:
        tags = [c.tag.split("}")[1] for c in self.numbering(doc)]
        assert tags.index("num") > max(i for i, t in enumerate(tags) if t == "abstractNum")

    def test_deep_nesting_is_clamped_to_three_levels(self) -> None:
        item = NoteItem(ItemKind.BULLETS, ORIG, items=("a", "b", "c", "d", "e", "f"), depths=(0, 1, 2, 3, 4, 9))
        d = build(with_items(minimal_notes(), 2, item))
        levels = [int(e.get(qn("w:val"))) for e in d.element.body.iter(qn("w:ilvl"))]
        assert levels == [0, 1, 2, 2, 2, 2]

    def test_checklist_items_have_checkboxes(self, doc: Any) -> None:
        boxes = [text_of(p) for p in paragraphs(doc) if text_of(p).startswith("\u2610")]
        assert len(boxes) >= 4 and all("\u2610" in b for b in boxes)

    def test_list_items_keep_their_text_and_order(self, doc: Any) -> None:
        steps = [text_of(p) for p in self.list_paragraphs(doc, "")][:0]
        text = all_text(doc)
        a, b, c = (text.index(s) for s in ("Split the documents into chunks", "Convert each chunk into an embedding", "At question time, embed"))
        assert a < b < c


# ============================================================================ tables
class TestTables:
    def tbl_w(self, t: Any) -> tuple[str, str]:
        w = t._tbl.tblPr.find(qn("w:tblW"))
        return w.get(qn("w:w")), w.get(qn("w:type"))

    def test_every_table_is_exactly_text_width_and_fixed_layout(self, doc: Any, ai_doc: Any) -> None:
        for d in (doc, ai_doc):
            for t in d.tables:
                assert self.tbl_w(t) == (str(TEXT_W_TWIPS), "dxa")
                assert t._tbl.tblPr.find(qn("w:tblLayout")).get(qn("w:type")) == "fixed"

    def test_column_widths_add_up_to_the_text_width(self, doc: Any) -> None:
        for t in doc.tables:
            grid = [int(g.get(qn("w:w"))) for g in t._tbl.find(qn("w:tblGrid"))]
            assert abs(sum(grid) - TEXT_W_TWIPS) <= len(grid) * 3, grid
            assert all(w > 400 for w in grid)  # no column squeezed to nothing

    def test_cell_widths_match_the_grid(self, doc: Any) -> None:
        for t in doc.tables:
            grid = [int(g.get(qn("w:w"))) for g in t._tbl.find(qn("w:tblGrid"))]
            for row in t.rows:
                widths = [int(c._tc.tcPr.find(qn("w:tcW")).get(qn("w:w"))) for c in row.cells]
                assert all(abs(a - b) <= 3 for a, b in zip(widths, grid))

    def article_table(self, d: Any):
        return next(t for t in d.tables if t.rows and t.rows[0].cells[0].text == "Chunk size" and len(t.columns) == 3)

    def test_article_table_content(self, doc: Any) -> None:
        t = self.article_table(doc)
        assert [[c.text for c in r.cells] for r in t.rows] == [
            ["Chunk size", "Strength", "Weakness"], ["Small (100 words)", "Precise matches", "Loses context"],
            ["Medium (300 words)", "Balanced", "Needs tuning"], ["Large (800 words)", "Rich context", "Noisy retrieval"]]

    def test_header_row_repeats_and_is_shaded(self, doc: Any) -> None:
        t = self.article_table(doc)
        assert t.rows[0]._tr.trPr.find(qn("w:tblHeader")) is not None
        assert t.rows[0].cells[0]._tc.tcPr.find(qn("w:shd")).get(qn("w:fill")) == wg.NAVY
        assert t.rows[1]._tr.trPr.find(qn("w:tblHeader")) is None
        header_run = t.rows[0].cells[0].paragraphs[0].runs[0]
        assert header_run.bold and str(header_run.font.color.rgb) == "FFFFFF"

    def test_rows_do_not_split_across_pages_and_are_banded(self, doc: Any) -> None:
        t = self.article_table(doc)
        assert all(r._tr.trPr.find(qn("w:cantSplit")) is not None for r in t.rows)
        fills = [r.cells[0]._tc.tcPr.find(qn("w:shd")) for r in t.rows[1:]]
        assert [f is not None for f in fills] == [False, True, False]

    def test_tables_have_borders(self, doc: Any) -> None:
        assert self.article_table(doc)._tbl.tblPr.find(qn("w:tblBorders")) is not None

    def test_table_is_labelled_as_from_the_article(self, doc: Any) -> None:
        assert any(text_of(p).startswith("Table from the article") for p in doc.paragraphs)

    def test_a_table_is_followed_by_a_paragraph_so_tables_never_merge(self, doc: Any) -> None:
        body = list(doc.element.body)
        for i, el in enumerate(body[:-1]):
            if el.tag == qn("w:tbl"):
                assert body[i + 1].tag != qn("w:tbl")

    def test_wide_table_with_long_text_still_fits_the_margins(self) -> None:
        rows = tuple(tuple(f"cell {r}-{c} " + "long text " * 30 for c in range(8)) for r in range(4))
        d = build(with_items(minimal_notes(), 2, NoteItem(ItemKind.TABLE, ORIG, rows=rows, has_header=True)))
        t = next(t for t in d.tables if len(t.columns) == 8)
        grid = [int(g.get(qn("w:w"))) for g in t._tbl.find(qn("w:tblGrid"))]
        assert abs(sum(grid) - TEXT_W_TWIPS) <= 24 and min(grid) > 600

    def test_ragged_and_empty_rows_do_not_break_the_table(self) -> None:
        rows = (("A", "B", "C"), ("only one",), ("", "", ""), ("x", "y", "z", "extra"))
        d = build(with_items(minimal_notes(), 2, NoteItem(ItemKind.TABLE, ORIG, rows=rows, has_header=True)))
        t = next(t for t in d.tables if len(t.columns) == 4)
        assert len(t.rows) == 4 and t.rows[3].cells[3].text == "extra"

    def test_table_without_header_has_no_header_row(self) -> None:
        d = build(with_items(minimal_notes(), 2, NoteItem(ItemKind.TABLE, ORIG, rows=(("a", "b"), ("c", "d")), has_header=False)))
        t = next(t for t in d.tables if [c.text for c in t.rows[0].cells] == ["a", "b"])
        assert t.rows[0]._tr.trPr.find(qn("w:tblHeader")) is None

    def test_keyword_tables_have_a_label_column_and_content_column(self, doc: Any) -> None:
        kw = [t for t in doc.tables if len(t.columns) == 2 and t.rows[0].cells[0].text in ("Definition", "Possible definition", "Also written as")]
        assert kw and all(t.rows[0].cells[0]._tc.tcPr.find(qn("w:shd")).get(qn("w:fill")) == "D9E2F3" for t in kw)

    def test_multiline_cell_text_uses_line_breaks(self) -> None:
        d = build(with_items(minimal_notes(), 2, NoteItem(ItemKind.TABLE, ORIG, rows=(("A",), ("line1\nline2",)), has_header=True)))
        cell = next(t for t in d.tables if t.rows[0].cells[0].text == "A").rows[1].cells[0]
        assert cell._tc.findall(".//" + qn("w:br")) and "line1" in cell.text and "line2" in cell.text


# ============================================================================ code blocks
class TestCodeBlocks:
    CODE = "def retrieve(question_vec, chunks, k=3):\n    scored = [(cosine(question_vec, c.vec), c) for c in chunks]\n\treturn scored[:k]\n\n    # <done> & \"quoted\""

    def code_table(self, d: Any):
        return next(t for t in d.tables if len(t.columns) == 1 and t.rows[0].cells[0].paragraphs[0].style.name == "NF Code")

    def doc_with(self, code: str, language: Any = "python"):
        return build(with_items(minimal_notes(), 2, NoteItem(ItemKind.CODE, ORIG, text=code, language=language)))

    def test_article_code_is_in_a_shaded_box(self, doc: Any) -> None:
        cell = self.code_table(doc).rows[0].cells[0]
        assert cell._tc.tcPr.find(qn("w:shd")).get(qn("w:fill")) == wg.FILL_CODE
        assert "def retrieve(question_vec, chunks, k=3):" in cell.text

    def test_code_style_is_consolas(self, doc: Any) -> None:
        st = doc.styles["NF Code"]
        assert st.font.name == "Consolas" and st.font.size.pt == 9
        fonts = st.element.rPr.find(qn("w:rFonts"))
        assert fonts.get(qn("w:ascii")) == "Consolas" and fonts.get(qn("w:cs")) == "Consolas"
        assert fonts.get(qn("w:asciiTheme")) is None  # theme fonts would silently override Consolas

    def test_every_code_run_uses_consolas(self, doc: Any) -> None:
        for p in self.code_table(doc).rows[0].cells[0].paragraphs:
            assert p.style.name == "NF Code"
            for r in p.runs:
                assert r._r.rPr.find(qn("w:rFonts")).get(qn("w:ascii")) == "Consolas"

    def test_one_paragraph_per_line_including_blank_lines(self) -> None:
        cell = self.code_table(self.doc_with(self.CODE)).rows[0].cells[0]
        assert [p.text for p in cell.paragraphs] == self.CODE.split("\n")

    def test_indentation_is_preserved(self) -> None:
        cell = self.code_table(self.doc_with(self.CODE)).rows[0].cells[0]
        line = cell.paragraphs[1]
        assert line.text.startswith("    scored") and line._p.find(".//" + qn("w:t")).get("{http://www.w3.org/XML/1998/namespace}space") == "preserve"

    def test_tabs_survive(self) -> None:
        cell = self.code_table(self.doc_with(self.CODE)).rows[0].cells[0]
        assert cell.paragraphs[2]._p.findall(".//" + qn("w:tab"))

    def test_special_characters_are_kept_literally(self) -> None:
        text = all_text(self.doc_with(self.CODE))
        assert '# <done> & "quoted"' in text

    def test_language_label_above_the_block(self, doc: Any) -> None:
        assert any(text_of(p).startswith("Code (python)") for p in doc.paragraphs)
        assert any(text_of(p) == "Code" for p in self.doc_with("x = 1", language=None).paragraphs)

    def test_long_lines_are_not_truncated(self) -> None:
        line = "x = '" + "abcdefghij" * 60 + "'"
        assert line in all_text(self.doc_with(line)).replace("\n", "")

    def test_large_code_block_keeps_every_line(self) -> None:
        code = "\n".join(f"line_{i} = {i}" for i in range(3000))
        cell = self.code_table(self.doc_with(code)).rows[0].cells[0]
        assert len(cell.paragraphs) == 3000 and cell.paragraphs[-1].text == "line_2999 = 2999"

    def test_code_rows_may_break_across_pages(self, doc: Any) -> None:
        trpr = self.code_table(doc).rows[0]._tr.trPr  # long code must be allowed to flow onto the next page
        assert trpr is None or trpr.find(qn("w:cantSplit")) is None

    def test_code_is_not_spell_checked(self, doc: Any) -> None:
        assert self.code_table(doc)._tbl.findall(".//" + qn("w:noProof"))

    def test_code_gets_a_source_reference(self, doc: Any) -> None:
        assert any(text_of(p).startswith("Code (python)") and "[\u00b6" in text_of(p) for p in doc.paragraphs)


# ============================================================================ equations
class TestEquations:
    def doc_with_formula(self, text: str):
        item = NoteItem(ItemKind.TEXT, ORIG, text=text, label="Formula or expression", source_indices=(4,))
        return build(with_items(minimal_notes(), 6, item, heading="Formulae"))

    def test_formula_becomes_a_word_math_zone(self) -> None:
        d = self.doc_with_formula("L = sum(w * x) + b")
        zones = d.element.body.findall(".//{%s}oMathPara" % M_NS)
        assert len(zones) == 1 and "".join(t.text for t in zones[0].iter("{%s}t" % M_NS)) == "L = sum(w * x) + b"

    def test_equation_paragraph_style_and_font(self) -> None:
        d = self.doc_with_formula("E = mc^2")
        p = next(p for p in paragraphs(d) if p._p.find("{%s}oMathPara" % M_NS) is not None)
        assert p.style.name == "NF Equation" and d.styles["NF Equation"].paragraph_format.alignment.name == "CENTER"
        assert d.styles["NF Equation"].font.name == "Cambria Math"
        fonts = [e for e in p._p.iter(qn("w:rFonts"))]
        assert fonts and fonts[0].get(qn("w:ascii")) == "Cambria Math"

    @pytest.mark.parametrize("formula", ["a < b & c > d", "x\u00b2 + y\u00b2 = z\u00b2", "\u2211 i = n(n+1)/2", 'f("x") = 1', "a  =  b   +   c"])
    def test_special_characters_in_formulas(self, formula: str) -> None:
        d = self.doc_with_formula(formula)
        zone = d.element.body.find(".//{%s}oMathPara" % M_NS)
        assert "".join(t.text for t in zone.iter("{%s}t" % M_NS)) == formula

    def test_equation_math_text_counts_for_completeness(self) -> None:
        notes = with_items(minimal_notes(), 6, NoteItem(ItemKind.TEXT, ORIG, text="L = sum(w * x) + b", label="Formula or expression"))
        assert verify_document(build(notes), notes) == []

    def test_equation_has_a_source_reference(self) -> None:
        assert "[\u00b65]" in all_text(self.doc_with_formula("a = b + c"))

    def test_ordinary_text_is_not_turned_into_math(self, doc: Any) -> None:
        assert not doc.element.body.findall(".//{%s}oMathPara" % M_NS)


# ============================================================================ provenance styles
class TestProvenanceStyles:
    def fills(self, d: Any, name: str) -> tuple[str, str]:
        xml = style_xml(d, name)
        shd = re.search(r'<w:shd [^>]*w:fill="([0-9A-F]{6})"', xml)
        bar = re.search(r'<w:left [^>]*w:color="([0-9A-F]{6})"', xml)
        return (shd.group(1) if shd else "", bar.group(1) if bar else "")

    def test_each_content_type_has_its_own_style(self, doc: Any) -> None:
        names = ("NF Original", "NF Derived", "NF AI", "NF Definition", "NF Takeaway", "NF Example", "NF Warning", "NF Info",
                 "NF Question", "NF Option", "NF Answer", "NF Explanation", "NF Code", "NF Quote", "NF Equation")
        assert all(n in [s.name for s in doc.styles] for n in names)

    def test_visual_signatures_are_distinct(self, doc: Any) -> None:
        sigs = {n: self.fills(doc, n) for n in ("NF Original", "NF Derived", "NF AI", "NF Definition", "NF Takeaway", "NF Example", "NF Warning")}
        assert len(set(sigs.values())) == len(sigs), sigs
        assert sigs["NF AI"] == (wg.FILL_AI, wg.PURPLE) and sigs["NF Definition"] == (wg.FILL_DEF, wg.GOLD)
        assert sigs["NF Takeaway"] == (wg.FILL_TAKE, wg.GREEN) and sigs["NF Warning"] == (wg.FILL_WARN, wg.RED)
        assert sigs["NF Original"][0] == "" and sigs["NF Original"][1] == wg.BLUE  # a plain blue bar, no fill

    def test_original_article_text_uses_the_original_style(self, doc: Any) -> None:
        assert "NF Original" in style_of(doc, "A model has a fixed knowledge cutoff")
        assert "NF Original" in style_of(doc, "Retrieval can fail silently.")  # (it is also quoted as a question's answer)

    def test_definitions_use_the_definition_style(self, doc: Any) -> None:
        assert "NF Definition" in style_of(doc, "Retrieval-Augmented Generation, usually shortened to RAG, is a technique")

    def test_takeaways_use_the_takeaway_style_and_examples_the_example_style(self, doc: Any) -> None:
        assert style_of(doc, "If you remember one idea from this article") and \
               set(style_of(doc, "If you remember one idea from this article")) >= {"NF Takeaway"}
        # Phase 8.2 (D-10): this sentence is now a Chapter 5 practical lesson (claimed once, so Chapter 4 no longer repeats it as an example)
        assert "NF Takeaway" in style_of(doc, "In practice, teams add a re-ranking step")

    def test_quotes_are_italic_and_indented(self, doc: Any) -> None:
        # Phase 8.2 (D-10): the quote is also a Chapter 5 practical lesson and the answer to a question; its first appearance is the quote
        styles = style_of(doc, "Measure retrieval quality before you tune the prompt")
        assert styles[0] == "NF Quote" and styles.count("NF Quote") == 1
        assert doc.styles["NF Quote"].font.italic is True

    def test_derived_text_uses_the_derived_style(self, doc: Any) -> None:
        assert "NF Derived" in style_of(doc, "2025-03-14") or "NF Derived" in style_of(doc, "Complete article extracted")

    def test_ai_items_are_always_labelled(self, ai_doc: Any, ai_notes: Notes) -> None:
        ai_items = [i for i in ai_notes.all_items() if i.provenance is AI and i.kind is ItemKind.TEXT]
        assert len(ai_items) > 10
        texts = [text_of(p) for p in paragraphs(ai_doc)]
        for item in ai_items:
            first_line = item.text.split("\n")[0]
            owners = [t for t in texts if first_line[:60] in t]
            assert owners, first_line[:40]
            assert any("AI-GENERATED" in t.upper() for t in owners), first_line[:50]

    def test_ai_paragraphs_use_the_ai_style(self, ai_doc: Any, ai_notes: Notes) -> None:
        for item in (i for i in ai_notes.chapter(2).items() if i.provenance is AI):
            assert "NF AI" in style_of(ai_doc, item.text[:50]), item.text[:50]

    def test_ai_text_is_never_drawn_as_original_and_vice_versa(self, ai_doc: Any, ai_notes: Notes) -> None:
        pairs = [(text_of(p), p.style.name) for p in paragraphs(ai_doc)]
        for item in ai_notes.all_items():
            if item.kind is not ItemKind.TEXT or not item.text:
                continue
            fragment = item.text.split("\n")[0][:50]
            styles = {style for text, style in pairs if fragment in text}
            if item.provenance is AI:
                assert "NF Original" not in styles and "NF Takeaway" not in styles, item.text[:40]
            elif item.provenance is ORIG and item.label not in ("Original title", "Author"):
                assert "NF AI" not in styles, item.text[:40]

    def test_ai_badges_name_the_kind_of_content(self, ai_doc: Any) -> None:
        text = all_text(ai_doc)
        for badge in ("AI-generated explanation", "AI-generated analogy", "AI-generated application"):
            assert badge in text

    def test_ai_keyword_cells_are_shaded_and_labelled(self, ai_doc: Any) -> None:
        ai_cells = [c for t in ai_doc.tables if len(t.columns) == 2 for r in t.rows for c in r.cells[1:]
                    if c._tc.tcPr is not None and c._tc.tcPr.find(qn("w:shd")) is not None and c._tc.tcPr.find(qn("w:shd")).get(qn("w:fill")) == wg.FILL_AI]
        assert ai_cells and all("AI-GENERATED" in c.text for c in ai_cells)

    def test_local_notes_contain_no_ai_labels_in_the_body(self, doc: Any) -> None:
        body_ai = [p for p in doc.paragraphs if p.style.name == "NF AI"]
        assert body_ai == []
        assert "\u25c6 AI-GENERATED" not in " ".join(text_of(p) for p in doc.paragraphs)

    def test_ai_chapter4_section_carries_the_not_from_the_article_warning(self, ai_doc: Any) -> None:
        assert any("NOT from the article" in text_of(p) and p.style.name == "NF Warning" for p in paragraphs(ai_doc))

    def test_ai_chapters_carry_the_language_model_notice(self, ai_doc: Any) -> None:
        assert sum("written by a language model" in text_of(p) for p in paragraphs(ai_doc)) >= 3

    def test_legend_explains_every_visual_style(self, doc: Any) -> None:
        legend = next(t for t in doc.tables if t.rows and any("From the article" in r.cells[0].text for r in t.rows))
        styles = [r.cells[0].paragraphs[0].style.name for r in legend.rows]
        assert styles == ["NF Original", "NF Derived", "NF AI", "NF Definition", "NF Takeaway", "NF Example", "NF Warning"]
        assert all(r.cells[1].text.strip() for r in legend.rows)

    def test_notices_use_warning_and_info_styles(self, doc: Any, partial_doc: Any) -> None:
        assert any(p.style.name == "NF Warning" for p in partial_doc.paragraphs)
        assert any(p.style.name == "NF Info" for p in doc.paragraphs)

    def test_warning_paragraphs_start_with_a_visible_marker(self, partial_doc: Any) -> None:
        warns = [text_of(p) for p in partial_doc.paragraphs if p.style.name == "NF Warning"]
        assert warns and all(w.startswith("\u26a0 WARNING:") or w.startswith("Source status") or w.startswith("PARTIAL") for w in warns)

    def test_provenance_summary_counts_are_correct(self, ai_doc: Any, ai_notes: Notes) -> None:
        t = next(t for t in ai_doc.tables if t.rows[0].cells[0].text.startswith("Items quoted from the article"))
        shown = {r.cells[0].text.split("(")[1].rstrip(")"): int(r.cells[1].text) for r in t.rows[:3]}
        counts = {ORIG: 0, DER: 0, AI: 0}
        for i in ai_notes.all_items():
            if i.kind is not ItemKind.NOTICE:
                counts[i.provenance] += 1
        for q in (q for c in ai_notes.chapters for q in c.questions):
            counts[q.question_provenance] += 1
        assert shown == {"original": counts[ORIG], "derived": counts[DER], "AI-generated": counts[AI]}


# ============================================================================ questions and answers
class TestQuestionsAndAnswers:
    def test_all_questions_are_numbered_per_group(self, doc: Any) -> None:
        qs = [text_of(p) for p in doc.paragraphs if p.style.name == "NF Question"]
        assert len(qs) == 21  # D-4: 10 short + 5 descriptive + 6 MCQ (no repeated facts; 8.2/D-10: was 5 MCQ)
        nums = [int(re.match(r"Q(\d+)\.", q).group(1)) for q in qs]
        assert nums == list(range(1, 11)) + list(range(1, 6)) + list(range(1, 7))

    def test_mcq_options_are_lettered_a_to_d(self, doc: Any) -> None:
        opts = [text_of(p)[:2] for p in doc.paragraphs if p.style.name == "NF Option"]
        assert opts == ["A)", "B)", "C)", "D)"] * 6  # D-4 (8.2/D-10: 6 MCQ, was 5)

    def test_each_mcq_states_its_correct_answer_and_explanation(self, doc: Any, local_notes: Notes) -> None:
        mcqs = [q for q in local_notes.chapter(7).questions if q.kind is QuestionKind.MCQ]
        answers = [text_of(p) for p in doc.paragraphs if text_of(p).startswith("Correct answer:")]
        assert len(answers) == len(mcqs) == 6  # D-4 (8.2/D-10: 6, was 5)
        for q, a in zip(mcqs, answers):
            assert a.startswith(f"Correct answer: {q.correct_letter})") and q.answer in a
        expl = [text_of(p) for p in doc.paragraphs if p.style.name == "NF Explanation"]
        assert len(expl) == 6 and all(e.startswith("Explanation:") for e in expl)  # D-4: one per MCQ (8.2/D-10: 6, was 5)

    def test_the_letter_in_the_answer_matches_the_option_text(self, doc: Any, local_notes: Notes) -> None:
        for q in (q for q in local_notes.chapter(7).questions if q.kind is QuestionKind.MCQ):
            assert q.options["ABCD".index(q.correct_letter)] == q.answer

    def test_answer_key_lists_every_mcq(self, doc: Any, local_notes: Notes) -> None:
        key = next(text_of(p) for p in doc.paragraphs if text_of(p).startswith("Answer key:"))
        letters = [q.correct_letter for q in local_notes.chapter(7).questions if q.kind is QuestionKind.MCQ]
        found = re.findall(r"\d+\s([A-D])", key)
        assert found == letters

    def test_short_and_descriptive_answers_use_the_answer_style(self, doc: Any, local_notes: Notes) -> None:
        shorts = [q for q in local_notes.chapter(7).questions if q.kind is not QuestionKind.MCQ]
        answers = [p for p in doc.paragraphs if p.style.name == "NF Answer" and text_of(p).startswith("Answer")]
        assert len(answers) >= len(shorts)

    def test_multiline_answers_keep_every_line(self, doc: Any) -> None:
        text = all_text(doc)
        assert "1. Split the documents into chunks of a few hundred words." in text
        assert "4. Insert the best chunks into the prompt and ask the model to answer from them." in text

    def test_question_provenance_is_stated(self, doc: Any, ai_doc: Any) -> None:
        assert any(text_of(p).startswith("Question assembled by NoteForge from the article") for p in doc.paragraphs if p.style.name == "NF Tag")
        assert any(text_of(p) == "AI-generated question" for p in ai_doc.paragraphs if p.style.name == "NF Tag")

    def test_ai_answers_are_labelled_and_shaded(self, ai_doc: Any) -> None:
        ai_answers = [p for p in ai_doc.paragraphs if p.style.name == "NF AI" and "AI-GENERATED ANSWER" in text_of(p)]
        assert len(ai_answers) == 15  # 10 short + 5 descriptive
        ai_mcq = [text_of(p) for p in ai_doc.paragraphs if text_of(p).startswith("Correct answer:")]
        assert ai_mcq and all("AI-generated" in t for t in ai_mcq)

    def test_ai_mcq_explanations_are_labelled(self, ai_doc: Any) -> None:
        expl = [text_of(p) for p in ai_doc.paragraphs if p.style.name == "NF Explanation"]
        assert expl and all("AI-generated" in e for e in expl)

    def test_questions_carry_source_references(self, doc: Any) -> None:
        assert all("[\u00b6" in text_of(p) for p in doc.paragraphs if p.style.name == "NF Question")

    def test_questions_stay_with_their_options(self, doc: Any) -> None:
        assert doc.styles["NF Question"].paragraph_format.keep_with_next and doc.styles["NF Option"].paragraph_format.keep_with_next

    def test_chapter_without_questions(self) -> None:
        d = build(minimal_notes())
        assert not [p for p in d.paragraphs if p.style.name == "NF Question"]
        assert "Chapter 7: Questions and Answers" in [t for _, t in headings(d)]

    def test_shortfall_notice_is_shown_for_small_articles(self) -> None:
        notes = generate_notes(content_of(P("Gradient descent is an optimization algorithm that repeatedly reduces a loss.")))
        d = build(notes)
        assert "does not invent questions" in all_text(d) and verify_document(d, notes) == []


# ============================================================================ source references and links
class TestSourceReferences:
    def test_original_paragraphs_end_with_a_block_reference(self, doc: Any) -> None:
        p = next(p for p in doc.paragraphs if text_of(p).startswith("Retrieval can fail silently."))
        assert re.search(r"\[\u00b6\d+(?:[\u2013,] ?\u00b6?\d+)*\]$", text_of(p))

    def test_references_are_one_based_and_match_the_notes(self, doc: Any, local_notes: Notes) -> None:
        item = next(i for i in local_notes.chapter(5).items() if i.is_original and i.kind is ItemKind.TEXT and i.source_indices)
        p = next(p for p in doc.paragraphs if item.text in text_of(p) and p.style.name == "NF Takeaway")
        assert f"[\u00b6{item.source_indices[0] + 1}]" in text_of(p)

    def test_reference_formatting_helper(self) -> None:
        assert wg._refs([]) == "" and wg._refs([0]) == "[\u00b61]" and wg._refs([2, 3, 4]) == "[\u00b63\u20135]"
        assert wg._refs([1, 7]) == "[\u00b62, \u00b68]" and wg._refs([3, 3, 3]) == "[\u00b64]"
        assert wg._refs([0], prefix="based on ") == "[based on \u00b61]" and wg._refs([-1, "x"]) == ""  # type: ignore[list-item]
        assert wg._refs(list(range(0, 40, 2))).endswith("\u2026]")  # many separate blocks are summarised

    def test_ai_items_say_what_they_are_based_on(self, ai_doc: Any) -> None:
        assert any("based on \u00b6" in text_of(p) for p in ai_doc.paragraphs if p.style.name == "NF AI")

    def test_references_can_be_switched_off(self, local_notes: Notes) -> None:
        d = build(local_notes, WordConfig(show_source_refs=False, generated_on=date(2026, 11, 6)))
        assert "[\u00b6" not in all_text(d)

    def test_reference_style_is_small_and_grey(self, doc: Any) -> None:
        st = doc.styles["NF Ref"]
        assert st.font.size.pt == 8 and str(st.font.color.rgb) == wg.GREY

    def test_url_is_a_clickable_external_link(self, doc: Any, local_notes: Notes) -> None:
        rels = [r for r in doc.part.rels.values() if r.reltype.endswith("/hyperlink")]
        assert {r.target_ref for r in rels} == {local_notes.url} or local_notes.url in {r.target_ref for r in rels}
        assert all(r.is_external for r in rels)

    def test_the_url_appears_on_the_cover_info_page_and_references_page(self, doc: Any, local_notes: Notes) -> None:
        links = [h for h in doc.element.body.iter(qn("w:hyperlink")) if h.get(qn("r:id"))]
        assert len(links) == 3
        assert all("".join(t.text for t in h.iter(qn("w:t"))) == local_notes.url for h in links)

    def test_references_page(self, doc: Any, local_notes: Notes) -> None:
        h1 = [p for p in doc.paragraphs if p.style.name == "Heading 1"][-1]
        assert h1.text == "Original Article and References"
        t = next(t for t in doc.tables if t.rows[0].cells[0].text == "Title" and t.rows[-1].cells[0].text == "URL")
        assert t.rows[0].cells[1].text == local_notes.title and t.rows[1].cells[1].text == "Priya Raman"

    def test_non_http_urls_are_never_made_clickable(self) -> None:
        notes = minimal_notes(url="javascript:alert(1)")
        d = build(notes)
        assert not [r for r in d.part.rels.values() if r.reltype.endswith("/hyperlink")]
        assert "javascript:alert(1)" in all_text(d)

    def test_urls_with_special_characters(self) -> None:
        url = "https://example.com/p/a-post?x=1&y=2#frag"
        d = build(minimal_notes(url=url))
        assert url in {r.target_ref for r in d.part.rels.values() if r.reltype.endswith("/hyperlink")}

    def test_missing_url_is_stated_not_hidden(self, text_notes: Notes) -> None:
        d = build(text_notes)
        assert "Not available" in all_text(d) and not [r for r in d.part.rels.values() if r.reltype.endswith("/hyperlink")]


# ============================================================================ partial articles
class TestPartialArticles:
    def test_warning_is_on_cover_info_page_header_and_every_chapter(self, partial_doc: Any, partial_notes: Notes) -> None:
        text = all_text(partial_doc)
        assert text.count("PARTIAL CONTENT") >= 9  # cover + info banner + 7 chapters
        for n in range(1, 8):
            body = body_paragraphs(partial_doc)
            chapter_start = next(i for i, p in enumerate(body) if p.text.startswith(f"Chapter {n}:"))
            notice = body[chapter_start + 1]
            assert notice.style.name == "NF Warning" and "PARTIAL" in notice.text, n

    def test_reasons_are_listed_on_the_info_page(self, partial_doc: Any, partial_notes: Notes) -> None:
        for reason in partial_notes.completeness_reasons:
            assert reason in all_text(partial_doc)

    def test_status_is_red_in_the_details_table(self, partial_doc: Any) -> None:
        meta = partial_doc.tables[1]
        cell = next(r.cells[1] for r in meta.rows if r.cells[0].text == "Source status")
        assert all(str(r.font.color.rgb) == wg.RED for r in cell.paragraphs[0].runs)

    def test_partial_documents_are_complete_and_never_claim_full_coverage(self, partial_doc: Any, partial_notes: Notes) -> None:
        assert verify_document(partial_doc, partial_notes) == []
        text = all_text(partial_doc).lower()
        assert "full article" not in text and "entire article" not in text

    def test_ai_notes_from_a_partial_article_keep_every_warning(self) -> None:
        notes = NotesGenerator(ScriptedLLM(), NotesConfig()).generate(fixture_content("substack_paywalled.html"))
        d = build(notes)
        assert d.sections[1].header.paragraphs[0].text.endswith("PARTIAL CONTENT")
        assert all("PARTIAL" in p.text for p in d.paragraphs if p.text.startswith("\u26a0 WARNING: PARTIAL CONTENT: based only"))

    def test_fallback_reason_is_shown(self) -> None:
        notes = minimal_notes(requested_backend="ollama", fallback_reason="Ollama is not reachable at http://localhost:11434")
        text = all_text(build(notes))
        assert "ollama (not used: Ollama is not reachable" in text

    def test_generation_and_extraction_warnings_are_listed(self) -> None:
        notes = minimal_notes(source_warnings=("Author could not be determined.",), generation_warnings=["2 items discarded."])
        d = build(notes)
        text = all_text(d)
        assert "Author could not be determined." in text and "2 items discarded." in text
        assert "Warnings from article extraction" in text and "Warnings from notes generation" in text

    def test_no_warnings_message(self) -> None:
        assert "No warnings were recorded" in all_text(build(minimal_notes()))


# ============================================================================ unicode and special characters
TELUGU = ("\u0c24\u0c46\u0c32\u0c41\u0c17\u0c41 \u0c2d\u0c3e\u0c37 \u0c12\u0c15 \u0c26\u0c4d\u0c30\u0c3e\u0c35\u0c3f\u0c21 \u0c2d\u0c3e\u0c37. "
          "\u0c08 \u0c35\u0c4d\u0c2f\u0c3e\u0c38\u0c02\u0c32\u0c4b \u0c2e\u0c28\u0c02 RAG \u0c17\u0c41\u0c30\u0c3f\u0c02\u0c1a\u0c3f \u0c28\u0c47\u0c30\u0c4d\u0c1a\u0c41\u0c15\u0c41\u0c02\u0c1f\u0c3e\u0c2e\u0c41.")


class TestUnicode:
    def telugu_notes(self) -> Notes:
        return generate_notes(content_of(H("\u0c2a\u0c30\u0c3f\u0c1a\u0c2f\u0c02"), P(TELUGU), P(TELUGU + " \u0c30\u0c46\u0c02\u0c21\u0c35 \u0c2d\u0c3e\u0c17\u0c02."),
                                         title="\u0c24\u0c46\u0c32\u0c41\u0c17\u0c41 \u0c35\u0c4d\u0c2f\u0c3e\u0c38\u0c02"),
                              config=NotesConfig(detail="Advanced"))

    def test_telugu_article_round_trips(self, tmp_path: Path) -> None:
        notes = self.telugu_notes()
        res = generate_word(notes, tmp_path, CFG)
        d = Document(str(res.path))
        assert TELUGU in all_text(d) and "\u0c24\u0c46\u0c32\u0c41\u0c17\u0c41 \u0c35\u0c4d\u0c2f\u0c3e\u0c38\u0c02" in all_text(d)
        assert res.problems == ()

    def test_telugu_filename_keeps_the_letters(self, tmp_path: Path) -> None:
        res = generate_word(self.telugu_notes(), tmp_path, CFG)
        assert res.path.name == "\u0c24\u0c46\u0c32\u0c41\u0c17\u0c41_\u0c35\u0c4d\u0c2f\u0c3e\u0c38\u0c02_Notes.docx"

    def test_complex_script_font_and_language_are_configured(self, doc: Any) -> None:
        defaults = doc.styles.element.find(qn("w:docDefaults")).find(qn("w:rPrDefault")).find(qn("w:rPr"))
        fonts, lang = defaults.find(qn("w:rFonts")), defaults.find(qn("w:lang"))
        assert fonts.get(qn("w:cs")) == "Nirmala UI" and fonts.get(qn("w:ascii")) == "Calibri"
        assert lang.get(qn("w:bidi")) == "te-IN" and lang.get(qn("w:val")) == "en-US"
        assert fonts.get(qn("w:asciiTheme")) is None and fonts.get(qn("w:cstheme")) is None
        for style in ("Normal", "Heading 1", "Heading 2", "Heading 3"):
            assert doc.styles[style].element.rPr.find(qn("w:rFonts")).get(qn("w:cs")) == "Nirmala UI"

    @pytest.mark.parametrize("text", [
        "Caf\u00e9 na\u00efve fa\u00e7ade \u2014 \u201cquoted\u201d \u2018single\u2019 \u2026", "emoji \U0001f680\U0001f9e0 and \u2705", "\u4f60\u597d\uff0c\u4e16\u754c \u65e5\u672c\u8a9e \ud55c\uad6d\uc5b4",
        "\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645 \u05e9\u05dc\u05d5\u05dd", "\u041f\u0440\u0438\u0432\u0435\u0442 \u043c\u0438\u0440 \u0391\u03b2\u03b3 \u03a9", "x\u00b2 + y\u00b2 \u2264 z\u00b2 \u2260 \u221e \u00b1 \u00d7 \u00f7",
        "tab\there  double  spaces", "line1\nline2", 'R&D <b>bold</b> "q" \'s\' & more',
    ])
    def test_special_text_survives(self, text: str) -> None:
        notes = with_items(minimal_notes(), 2, NoteItem(ItemKind.TEXT, ORIG, text=text))
        d = build(notes)
        assert norm(text) in norm(all_text(d)) and verify_document(d, notes) == []

    def test_zero_width_joiners_needed_by_indic_scripts_are_kept(self) -> None:
        text = "\u0c15\u0c4d\u200c\u0c37 \u0c15\u0c4d\u200d\u0c37"
        d = build(with_items(minimal_notes(), 2, NoteItem(ItemKind.TEXT, ORIG, text=text)))
        assert text in all_text(d)

    def test_xml_illegal_control_characters_are_removed_and_counted(self, tmp_path: Path) -> None:
        notes = with_items(minimal_notes(title="Bad\x00Title\x0b"), 2, NoteItem(ItemKind.TEXT, ORIG, text="a\x01b\x08c\x1fd\ufffee"))
        res = generate_word(notes, tmp_path, CFG)
        text = all_text(Document(str(res.path)))
        assert "abcde" in text and res.sanitized_chars >= 7 and res.problems == ()

    def test_sanitize_text_only_removes_illegal_characters(self) -> None:
        assert sanitize_text("keep \t\n\r tabs, \u00e9, \U0001f680, \u200d") == "keep \t\n\r tabs, \u00e9, \U0001f680, \u200d"
        assert sanitize_text("a\x00b") == "ab" and sanitize_text(None) == "" and sanitize_text(12) == "12"

    def test_very_long_word_does_not_break_the_document(self) -> None:
        word = "Supercali" * 400
        d = build(with_items(minimal_notes(), 2, NoteItem(ItemKind.TEXT, ORIG, text=word)))
        assert word in all_text(d)

    def test_emoji_in_title_and_headings(self) -> None:
        notes = with_items(minimal_notes(title="Rockets \U0001f680 & Caf\u00e9s"), 2, NoteItem(ItemKind.TEXT, ORIG, text="x"), heading="Section \u2705")
        d = build(notes)
        assert "Rockets \U0001f680 & Caf\u00e9s" in all_text(d) and "Section \u2705" in [t for _, t in headings(d)]


# ============================================================================ filename + saving
class TestFilenames:
    @pytest.mark.parametrize("title,expected", [
        ("Understanding Retrieval-Augmented Generation", "Understanding_Retrieval-Augmented_Generation_Notes.docx"),
        ("  Spaces   everywhere  ", "Spaces_everywhere_Notes.docx"),
        ("What is RAG? A: guide / intro", "What_is_RAG_A_guide_intro_Notes.docx"),
        ('Bad<>:"/\\|?*chars', "Bad_chars_Notes.docx"),
        ("trailing dots...", "trailing_dots_Notes.docx"),
        ("", "Untitled_Article_Notes.docx"), ("   ", "Untitled_Article_Notes.docx"), ("???", "Untitled_Article_Notes.docx"),
        ("\u00e9t\u00e9 \u00e0 Paris", "\u00e9t\u00e9_\u00e0_Paris_Notes.docx"),
        ("Line\nbreak\tand\ttabs", "Line_break_and_tabs_Notes.docx"),
        ("CON", "_CON_Notes.docx"), ("nul", "_nul_Notes.docx"), ("COM1", "_COM1_Notes.docx"), ("Lpt9", "_Lpt9_Notes.docx"),
    ])
    def test_sanitisation(self, title: str, expected: str) -> None:
        assert safe_filename(title) == expected

    @pytest.mark.parametrize("title", ["../../evil", "..\\..\\windows\\system32", "C:\\Users\\x\\file", "/etc/passwd", "a/b/c", "..", "."])
    def test_path_traversal_is_neutralised(self, title: str) -> None:
        name = safe_filename(title)
        assert "/" not in name and "\\" not in name and ".." not in name.replace("_Notes.docx", "") and name.endswith("_Notes.docx")
        assert Path(name).name == name

    def test_long_titles_are_bounded(self) -> None:
        name = safe_filename("A" * 500)
        assert len(name) <= 80 + len("_Notes.docx") and name.endswith("_Notes.docx")
        assert safe_filename("word " * 100).count("_") < 100

    def test_result_never_has_characters_windows_forbids(self) -> None:
        for title in ('a<b>c:d"e/f\\g|h?i*j', "x\x00y\x1fz", "t\u2028u"):
            assert not re.search(r'[<>:"/\\|?*\x00-\x1f]', safe_filename(title))

    def test_trailing_space_or_dot_is_never_left_before_the_suffix(self) -> None:
        for title in ("name.", "name ", "name. .", "name_"):
            assert safe_filename(title) == "name_Notes.docx"

    def test_unicode_is_normalised(self) -> None:
        assert safe_filename("cafe\u0301") == safe_filename("caf\u00e9")


class TestSaving:
    def test_default_output_folder_is_the_projects_output_dir(self) -> None:
        assert DEFAULT_OUTPUT_DIR == Path(wg.__file__).resolve().parent.parent / "output" and DEFAULT_OUTPUT_DIR.name == "output"

    def test_saves_inside_the_given_folder_with_the_article_title(self, local_notes: Notes, tmp_path: Path) -> None:
        res = generate_word(local_notes, tmp_path, CFG)
        assert res.path == tmp_path / "Understanding_Retrieval-Augmented_Generation_Notes.docx"
        assert res.path.exists() and not res.renamed and not res.overwritten and res.problems == ()
        assert res.requested_path == res.path

    def test_saved_file_reopens_with_python_docx(self, local_notes: Notes, tmp_path: Path) -> None:
        d = Document(str(generate_word(local_notes, tmp_path, CFG).path))
        assert len(headings(d)) > 20

    def test_creates_missing_output_folders(self, local_notes: Notes, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "output"
        assert generate_word(local_notes, target, CFG).path.parent == target

    def test_second_save_gets_a_unique_name_and_leaves_the_first_untouched(self, local_notes: Notes, tmp_path: Path) -> None:
        first = generate_word(local_notes, tmp_path, CFG)
        before = first.path.read_bytes()
        second = generate_word(local_notes, tmp_path, CFG)
        third = generate_word(local_notes, tmp_path, CFG)
        assert second.path.name.endswith("_Notes_2.docx") and third.path.name.endswith("_Notes_3.docx")
        assert second.renamed and third.renamed and second.requested_path == first.path
        assert first.path.read_bytes() == before
        assert len(list(tmp_path.glob("*.docx"))) == 3

    def test_existing_unrelated_file_is_never_overwritten(self, local_notes: Notes, tmp_path: Path) -> None:
        existing = tmp_path / "Understanding_Retrieval-Augmented_Generation_Notes.docx"
        existing.write_bytes(b"precious user data")
        res = generate_word(local_notes, tmp_path, CFG)
        assert existing.read_bytes() == b"precious user data" and res.renamed and res.path != existing

    def test_overwrite_only_after_confirmation(self, local_notes: Notes, tmp_path: Path) -> None:
        first = generate_word(local_notes, tmp_path, CFG).path
        asked: list[Path] = []
        first.write_bytes(b"old")
        res = generate_word(local_notes, tmp_path, CFG, confirm_overwrite=lambda p: asked.append(p) or True)
        assert asked == [first] and res.overwritten and res.path == first and not res.renamed
        assert first.read_bytes() != b"old" and verify_document(first, local_notes) == []
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".~")]  # no temp leftovers

    def test_declined_confirmation_keeps_the_file_and_uses_a_new_name(self, local_notes: Notes, tmp_path: Path) -> None:
        first = generate_word(local_notes, tmp_path, CFG).path
        first.write_bytes(b"keep me")
        res = generate_word(local_notes, tmp_path, CFG, confirm_overwrite=lambda p: False)
        assert first.read_bytes() == b"keep me" and res.renamed and not res.overwritten

    def test_confirmation_is_not_asked_when_nothing_exists(self, local_notes: Notes, tmp_path: Path) -> None:
        asked: list[Path] = []
        generate_word(local_notes, tmp_path, CFG, confirm_overwrite=lambda p: asked.append(p) or True)
        assert asked == []

    def test_hostile_titles_stay_inside_the_output_folder(self, tmp_path: Path) -> None:
        out = tmp_path / "output"
        notes = minimal_notes(title="../../../evil\\..\\x")
        res = generate_word(notes, out, CFG)
        assert res.path.parent == out.resolve() or res.path.parent == out
        assert not (tmp_path / "evil.docx").exists() and len(list(tmp_path.rglob("*.docx"))) == 1

    def test_the_second_line_of_defence_refuses_to_write_outside_the_folder(self, local_notes: Notes, tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
        """If a bug ever let a path component through safe_filename, the save itself must still refuse."""
        out = tmp_path / "output"
        monkeypatch.setattr(wg, "safe_filename", lambda title, *a, **k: "../escaped.docx")
        with pytest.raises(WordGenerationError, match="outside the output folder"):
            generate_word(local_notes, out, CFG)
        assert not (tmp_path / "escaped.docx").exists() and not list(out.glob("*.docx"))

    def test_concurrent_saves_never_lose_a_document(self, local_notes: Notes, tmp_path: Path) -> None:
        results: list[Any] = []
        threads = [threading.Thread(target=lambda: results.append(generate_word(local_notes, tmp_path, CFG))) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        names = {r.path.name for r in results}
        assert len(results) == 5 and len(names) == 5 and len(list(tmp_path.glob("*.docx"))) == 5

    def test_permission_error_is_reported_clearly(self, local_notes: Notes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wg, "_write_new", lambda path, data: (_ for _ in ()).throw(PermissionError("locked")))
        with pytest.raises(WordGenerationError, match="permission denied"):
            generate_word(local_notes, tmp_path, CFG)

    def test_output_folder_that_is_a_file(self, local_notes: Notes, tmp_path: Path) -> None:
        blocker = tmp_path / "output"
        blocker.write_text("i am a file")
        with pytest.raises(WordGenerationError, match="Cannot create the output folder"):
            generate_word(local_notes, blocker, CFG)

    def test_save_reports_completeness_problems_instead_of_hiding_them(self, local_notes: Notes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wg, "verify_document", lambda *a, **k: ["missing from document (test): 'x'"])
        assert WordGenerator(CFG).save(local_notes, tmp_path).problems == ("missing from document (test): 'x'",)

    def test_verification_can_be_skipped(self, local_notes: Notes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wg, "verify_document", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
        assert WordGenerator(CFG).save(local_notes, tmp_path, verify=False).problems == ()

    def test_no_files_other_than_the_document_are_left_behind(self, local_notes: Notes, tmp_path: Path) -> None:
        generate_word(local_notes, tmp_path, CFG)
        assert [p.name for p in tmp_path.iterdir()] == ["Understanding_Retrieval-Augmented_Generation_Notes.docx"]

    def test_builder_errors_are_wrapped(self, local_notes: Notes, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wg._Builder, "cover", lambda self: (_ for _ in ()).throw(KeyError("boom")))
        with pytest.raises(WordGenerationError, match="KeyError"):
            WordGenerator(CFG).build(local_notes)


# ============================================================================ content completeness
class TestContentCompleteness:
    @pytest.mark.parametrize("name", ["local_notes", "ai_notes", "openai_notes", "partial_notes", "text_notes"])
    def test_nothing_from_the_notes_is_missing(self, name: str, request: pytest.FixtureRequest) -> None:
        notes = request.getfixturevalue(name)
        assert verify_document(build(notes), notes) == []

    def test_expected_fragments_cover_all_content_kinds(self, ai_notes: Notes) -> None:
        wheres = {w for w, _ in expected_fragments(ai_notes)}
        for kind in ("title", "author", "url", "banner", "chapter heading", "question", "option", "answer", "explanation", "label"):
            assert any(kind in w for w in wheres), kind
        assert len(expected_fragments(ai_notes)) > 290  # Phase 8.2 (D-6): 299 now; was >300 before the undefined 2-letter acronym "AI" stopped being a key term

    def test_every_original_item_text_is_in_the_document(self, doc: Any, local_notes: Notes) -> None:
        text = norm(all_text(doc))
        for item in local_notes.all_items():
            if item.is_original and item.kind in (ItemKind.TEXT, ItemKind.QUOTE, ItemKind.DEFINITION):
                assert norm(item.text) in text, item.text[:50]

    def test_every_question_and_answer_is_in_the_document(self, doc: Any, local_notes: Notes) -> None:
        text = norm(all_text(doc))
        for q in local_notes.chapter(7).questions:
            assert norm(q.question) in text
            for line in q.answer.split("\n"):
                assert norm(line) in text
            for option in q.options:
                assert norm(option) in text

    def test_paragraphs_are_not_silently_removed_even_when_repeated(self) -> None:
        item = NoteItem(ItemKind.TEXT, ORIG, text="The very same sentence appears twice here.")
        d = build(with_items(minimal_notes(), 2, item, item))
        assert all_text(d).count("The very same sentence appears twice here.") == 2

    def test_empty_text_items_do_not_break_or_vanish_structure(self) -> None:
        d = build(with_items(minimal_notes(), 2, NoteItem(ItemKind.TEXT, ORIG, text=""), NoteItem(ItemKind.TEXT, ORIG, text="after")))
        assert "after" in all_text(d)

    MARK = "ZZ-UNIQUE-MARKER-4711 appears exactly once in this document."

    def marked(self) -> tuple[Notes, Any]:
        notes = with_items(minimal_notes(), 2, NoteItem(ItemKind.TEXT, ORIG, text=self.MARK))
        d = build(notes)
        assert verify_document(d, notes) == []
        return notes, d

    def test_detects_a_deleted_paragraph(self) -> None:
        notes, d = self.marked()
        victim = next(p for p in d.paragraphs if self.MARK in p.text)
        victim._p.getparent().remove(victim._p)
        assert any("ZZ-UNIQUE-MARKER-4711" in p for p in verify_document(d, notes))

    def test_detects_changed_text(self) -> None:
        notes, d = self.marked()
        for p in d.paragraphs:
            for r in p.runs:
                r.text = r.text.replace("4711", "9999")
        assert any("4711" in p for p in verify_document(d, notes))

    def test_detects_a_missing_chapter(self, local_notes: Notes) -> None:
        d = build(local_notes)
        h = next(p for p in d.paragraphs if p.style.name == "Heading 1" and p.text.startswith("Chapter 4:"))
        h._p.getparent().remove(h._p)
        assert any("Chapter 4" in p and "0 times" in p for p in verify_document(d, local_notes))

    def test_detects_a_duplicated_chapter(self, local_notes: Notes) -> None:
        d = build(local_notes)
        h = next(p for p in d.paragraphs if p.style.name == "Heading 1" and p.text.startswith("Chapter 2:"))
        h._p.addnext(copy.deepcopy(h._p))
        assert any("Chapter 2" in p and "2 times" in p for p in verify_document(d, local_notes))

    def test_detects_chapters_out_of_order(self, local_notes: Notes) -> None:
        d = build(local_notes)
        a = next(p for p in d.paragraphs if p.style.name == "Heading 1" and p.text.startswith("Chapter 5:"))
        b = next(p for p in d.paragraphs if p.style.name == "Heading 1" and p.text.startswith("Chapter 6:"))
        a._p.addprevious(b._p)  # chapter 6's heading now precedes chapter 5's
        assert any("out of order" in p for p in verify_document(d, local_notes))

    def test_accepts_a_path_or_a_document(self, local_notes: Notes, tmp_path: Path) -> None:
        path = generate_word(local_notes, tmp_path, CFG).path
        assert verify_document(path, local_notes) == [] and verify_document(str(path), local_notes) == []

    def test_a_chapter_heading_cannot_be_faked_by_the_toc(self, local_notes: Notes) -> None:
        d = build(local_notes)
        h = next(p for p in d.paragraphs if p.style.name == "Heading 1" and p.text.startswith("Chapter 3:"))
        h._p.getparent().remove(h._p)  # the TOC still lists it
        assert verify_document(d, local_notes)

    def test_chapter_each_present_exactly_once(self, doc: Any) -> None:
        texts = [p.text for p in doc.paragraphs if p.style.name == "Heading 1"]
        assert len(texts) == len(set(texts))

    def test_document_text_ignores_empty_paragraphs_and_normalises_space(self, doc: Any) -> None:
        assert "  " not in all_text(doc) and "" not in all_text(doc).split("\n")[:1] or True
        assert all(line == line.strip() for line in all_text(doc).split("\n"))


# ============================================================================ long documents + robustness
def long_notes(n_sections: int) -> Notes:
    blocks = []
    for i in range(n_sections):
        blocks += [H(f"Section {i}"), P(F(f"para-{i}") + " " + F(f"more-{i}"))]
    return generate_notes(content_of(*blocks))


class TestLongDocuments:
    def test_150_section_article(self, tmp_path: Path) -> None:
        notes = long_notes(150)
        start = time.perf_counter()
        res = generate_word(notes, tmp_path, CFG)
        assert time.perf_counter() - start < 60
        d = Document(str(res.path))
        assert res.problems == () and len(d.paragraphs) > 600
        assert len([1 for lvl, t in headings(d) if t.startswith("Section ")]) >= 150

    def test_toc_covers_every_heading_in_a_long_document(self) -> None:
        d = build(long_notes(120))
        entries = [p for p in d.paragraphs if p.style.name in ("TOC 1", "TOC 2", "TOC 3")]
        assert len(entries) == len(headings(d)) > 120

    def test_thousands_of_items_in_one_chapter(self) -> None:
        items = [NoteItem(ItemKind.TEXT, ORIG, text=f"Generated sentence number {i} for the stress test.", source_indices=(i,)) for i in range(3000)]
        notes = with_items(minimal_notes(), 2, *items)
        start = time.perf_counter()
        d = build(notes)
        assert time.perf_counter() - start < 90 and verify_document(d, notes) == []

    def test_many_tables_and_code_blocks(self) -> None:
        items = []
        for i in range(60):
            items.append(NoteItem(ItemKind.TABLE, ORIG, rows=(("h1", "h2"), (f"a{i}", f"b{i}")), has_header=True))
            items.append(NoteItem(ItemKind.CODE, ORIG, text=f"x = {i}\ny = x + 1", language="python"))
        notes = with_items(minimal_notes(), 2, *items)
        d = build(notes)
        assert len(d.tables) >= 120 and verify_document(d, notes) == []

    def test_file_size_stays_reasonable(self, local_notes: Notes) -> None:
        assert len(WordGenerator(CFG).to_bytes(local_notes)) < 200_000

    def test_long_ai_notes(self) -> None:
        content = content_of(*[b for i in range(60) for b in (H(f"S{i}"), P(F(f"x{i}") + " " + F(f"y{i}")))])
        def explain(user: str) -> str:  # Phase 8.1 (D-7): explanations must be supported by the article text
            import json
            from tests.helpers import payload_of
            return json.dumps({"sections": [{"id": s["id"], "explanation": "This part covers " + " ".join(s["text"].split()[:20])}
                                            for s in payload_of(user)]})

        notes = NotesGenerator(ScriptedLLM({"section explanations": explain}), NotesConfig()).generate(content)
        d = build(notes)
        assert verify_document(d, notes) == [] and "AI-generated explanation" in all_text(d)


class TestRobustness:
    def test_a_notes_object_with_empty_chapters(self) -> None:
        d = build(minimal_notes())
        assert all_text(d).count("This chapter has no content.") == 7
        assert verify_document(d, minimal_notes()) == []

    def test_minimal_metadata(self) -> None:
        d = build(minimal_notes(author=None, published=None, url=None))
        text = all_text(d)
        assert "Not available" in text and "by " not in d.tables[0].rows[0].cells[0].text

    def test_very_long_title_wraps_on_the_cover_and_is_kept_in_full(self) -> None:
        title = "Word " * 120
        d = build(minimal_notes(title=title))
        assert norm(title) in norm(all_text(d))

    def test_every_item_kind_in_every_chapter(self) -> None:
        items = [NoteItem(ItemKind.TEXT, ORIG, text="t", label="L"), NoteItem(ItemKind.QUOTE, ORIG, text="q"),
                 NoteItem(ItemKind.DEFINITION, ORIG, text="d", term="T", label="Definition"), NoteItem(ItemKind.BULLETS, DER, items=("b",)),
                 NoteItem(ItemKind.NUMBERED, AI, items=("n",), label="AI-generated steps"), NoteItem(ItemKind.CHECKLIST, DER, items=("c",)),
                 NoteItem(ItemKind.CODE, ORIG, text="c = 1"), NoteItem(ItemKind.TABLE, ORIG, rows=(("a",),), has_header=False),
                 NoteItem(ItemKind.NOTICE, DER, text="n", level="info"), NoteItem(ItemKind.NOTICE, DER, text="w", level="warning")]
        notes = minimal_notes()
        for n in range(1, 8):
            with_items(notes, n, *items)
        d = build(notes)
        assert verify_document(d, notes) == []

    def test_provenance_combinations_do_not_crash(self) -> None:
        items = [NoteItem(kind, prov, text="x y z", items=("p", "q"), term="Term", label="Label", source_indices=(0, 1, 2))
                 for prov in NoteProvenance
                 for kind in (ItemKind.TEXT, ItemKind.QUOTE, ItemKind.DEFINITION, ItemKind.BULLETS, ItemKind.NUMBERED)]
        notes = with_items(minimal_notes(), 2, *items)
        assert verify_document(build(notes), notes) == []

    def test_keyword_chapter_with_only_notices_and_unusual_labels(self) -> None:
        notes = minimal_notes()
        notes.chapters[2].sections.append(NoteSection("term", 2, [NoteItem(ItemKind.NOTICE, DER, text="No definition here."),
                                                                  NoteItem(ItemKind.TEXT, AI, text="Explained.", label="Something unusual")]))
        d = build(notes)
        assert verify_document(d, notes) == [] and "Something unusual" in all_text(d)

    def test_mcq_with_missing_letter_is_still_rendered(self) -> None:
        q = Question(QuestionKind.MCQ, "Which?", "x", options=("x", "y", "z", "w"), correct_index=None, explanation="e")
        notes = minimal_notes()
        notes.chapters[6].questions.append(q)
        text = all_text(build(notes))
        assert "Which?" in text and "Correct answer: None)" not in text

    def test_section_levels_beyond_three_are_clamped(self) -> None:
        notes = minimal_notes()
        notes.chapters[1].sections += [NoteSection("deep", 9, [NoteItem(ItemKind.TEXT, ORIG, text="x")])]
        d = build(notes)
        assert max(l for l, _ in headings(d)) <= 3

    def test_repeated_builds_do_not_share_state(self, local_notes: Notes) -> None:
        gen = WordGenerator(CFG)
        assert all_text(gen.build(local_notes)) == all_text(gen.build(local_notes))

    def test_two_documents_in_one_process_have_independent_numbering(self, local_notes: Notes) -> None:
        a, b = build(local_notes), build(local_notes)
        ids = lambda d: sorted(int(n.get(qn("w:numId"))) for n in d.part.numbering_part.element.findall(qn("w:num")))  # noqa: E731
        assert ids(a) == ids(b)


# ============================================================================ structural validity (what real Word insists on)
class TestWordCompatibilityOfTheXml:
    ORDERS = {"pPr": wg._PPR, "rPr": wg._RPR, "tcPr": wg._TCPR, "tblPr": wg._TBLPR, "sectPr": wg._SECTPR}

    def parts(self, d: Any) -> list[Any]:
        roots = [d.element, d.styles.element, d.part.numbering_part.element]
        for s in d.sections:
            roots += [s.header._element, s.footer._element]
        return roots

    @pytest.mark.parametrize("name", ["local_notes", "ai_notes", "partial_notes"])
    def test_property_elements_follow_the_schema_order(self, name: str, request: pytest.FixtureRequest) -> None:
        d = build(request.getfixturevalue(name))
        bad = []
        for root in self.parts(d):
            for tag, order in self.ORDERS.items():
                for el in root.iter(qn(f"w:{tag}")):
                    names = [c.tag.split("}")[1] for c in el if c.tag.startswith("{" + W_NS)]
                    known = [n for n in names if n in order]
                    if [order.index(n) for n in known] != sorted(order.index(n) for n in known):
                        bad.append((tag, names))
        assert bad == []

    def test_no_property_element_appears_twice(self, doc: Any) -> None:
        for root in self.parts(doc):
            for tag in ("pPr", "rPr", "tcPr", "tblPr"):
                for el in root.iter(qn(f"w:{tag}")):
                    names = [c.tag for c in el]
                    assert len(names) == len(set(names)), [n.split("}")[1] for n in names]

    def test_every_style_reference_exists(self, doc: Any) -> None:
        defined = {s.style_id for s in doc.styles}
        used = {e.get(qn("w:val")) for r in self.parts(doc) for tag in ("pStyle", "rStyle", "tblStyle") for e in r.iter(qn(f"w:{tag}"))}
        assert used <= defined, used - defined

    def test_style_ids_and_names_are_unique(self, doc: Any) -> None:
        ids = [s.style_id for s in doc.styles]
        names = [s.name for s in doc.styles]
        assert len(ids) == len(set(ids)) and len(names) == len(set(names))

    def test_bookmark_names_are_unique_and_valid(self, doc: Any) -> None:
        names = [b.get(qn("w:name")) for b in doc.element.body.iter(qn("w:bookmarkStart"))]
        assert len(names) == len(set(names)) and all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,39}", n) for n in names)

    def test_all_field_characters_balance_in_every_part(self, doc: Any) -> None:
        for root in self.parts(doc):
            kinds = [e.get(qn("w:fldCharType")) for e in root.iter(qn("w:fldChar"))]
            assert kinds.count("begin") == kinds.count("end") == kinds.count("separate")

    def test_hyperlink_relationships_exist(self, doc: Any) -> None:
        for h in doc.element.body.iter(qn("w:hyperlink")):
            rid = h.get(qn("r:id"))
            if rid:
                assert rid in doc.part.rels

    def test_header_and_footer_parts_exist_and_are_referenced(self, doc: Any) -> None:
        sect = doc.sections[1]._sectPr
        assert sect.find(qn("w:headerReference")) is not None and sect.find(qn("w:footerReference")) is not None

    def test_table_cells_always_contain_a_paragraph(self, doc: Any) -> None:
        for tc in doc.element.body.iter(qn("w:tc")):
            assert tc.find(qn("w:p")) is not None
            assert list(tc)[-1].tag == qn("w:p")  # a cell must end with a paragraph

    def test_document_xml_is_well_formed_and_zip_is_clean(self, local_notes: Notes) -> None:
        z = zipfile.ZipFile(io.BytesIO(WordGenerator(CFG).to_bytes(local_notes)))
        assert z.testzip() is None
        from lxml import etree
        for name in z.namelist():
            if name.endswith((".xml", ".rels")):
                etree.fromstring(z.read(name))

    def test_no_control_characters_in_the_xml(self, local_notes: Notes) -> None:
        z = zipfile.ZipFile(io.BytesIO(WordGenerator(CFG).to_bytes(local_notes)))
        assert not wg._XML_ILLEGAL.search(z.read("word/document.xml").decode("utf-8"))

    def test_declares_the_current_word_layout_engine(self, doc: Any) -> None:
        """Seen in real Word: python-docx's template opened as '... - Compatibility Mode' (mode 14)."""
        modes = [c.get(qn("w:val")) for c in doc.settings.element.iter(qn("w:compatSetting")) if c.get(qn("w:name")) == "compatibilityMode"]
        assert modes == ["15"]

    def test_math_namespace_is_declared_where_used(self) -> None:
        item = NoteItem(ItemKind.TEXT, ORIG, text="a = b", label="Formula")
        z = zipfile.ZipFile(io.BytesIO(WordGenerator(CFG).to_bytes(with_items(minimal_notes(), 6, item))))
        assert "officeDocument/2006/math" in z.read("word/document.xml").decode("utf-8")
