"""Phase 8 validation tests: properties confirmed during real-world QA, kept as regression guards.

These tests never write to the project's output/ folder; existing sample documents are only read.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document

from src.article_extractor import Completeness, SubstackExtractor
from src.backends import resolve_backend
from src.content_processor import process_article
from src.notes_generator import NotesConfig, NotesGenerator, validate_notes, verify_provenance
from src.word_generator import DEFAULT_OUTPUT_DIR, WordGenerator
from tests.helpers import load

SAMPLES = sorted(DEFAULT_OUTPUT_DIR.glob("Understanding_Retrieval-Augmented_Generation_Notes*.docx"))


@pytest.mark.skipif(not SAMPLES, reason="no sample documents present")
@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_existing_sample_documents_open_and_have_seven_chapters(path: Path) -> None:
    doc = Document(str(path))
    chapters = [p.text for p in doc.paragraphs if p.style.name == "Heading 1" and p.text.startswith("Chapter")]
    assert len(chapters) == 7 and chapters[0].startswith("Chapter 1")


def _notes(name: str, backend: str = "local"):
    art = SubstackExtractor().extract_from_html(load(name))
    content = process_article(art)
    return art, content, NotesGenerator(resolve_backend(backend), NotesConfig(detail="Intermediate")).generate(content)


@pytest.mark.parametrize("fixture", ["substack_free.html", "substack_paywalled.html", "short_post.html"])
def test_every_source_reference_points_at_a_real_block(fixture: str) -> None:
    art, content, notes = _notes(fixture)
    n = len(art.blocks)
    refs = [i for item in notes.all_items() for i in item.source_indices]
    assert refs and all(0 <= i < n for i in refs)
    assert validate_notes(notes) == [] and verify_provenance(notes, content) == []


def test_no_content_block_is_silently_dropped_from_the_notes() -> None:
    """Every non-heading article block must be cited somewhere in the notes (headings appear as section titles)."""
    art, _, notes = _notes("substack_free.html")
    cited = {i for item in notes.all_items() for i in item.source_indices}
    for q in (q for c in notes.chapters for q in c.questions):
        cited.update(q.source_indices)
    missing = [i + 1 for i, b in enumerate(art.blocks) if b.kind.value != "heading" and i not in cited]
    assert missing == []


def test_partial_article_keeps_its_warning_in_the_cover_header_and_every_chapter(tmp_path: Path) -> None:
    art, _, notes = _notes("substack_paywalled.html")
    assert art.completeness is Completeness.PARTIAL and art.completeness_reasons
    doc = Document(str(WordGenerator().save(notes, tmp_path).path))
    assert "PARTIAL CONTENT" in doc.sections[1].header.paragraphs[0].text
    banners = [p.text for p in doc.paragraphs if p.text.startswith("WARNING: PARTIAL CONTENT") or "PARTIAL CONTENT:" in p.text]
    assert len(banners) >= 7


def test_derived_numbers_are_only_structure_counts_or_dates() -> None:
    """Non-original text may not contain figures that are absent from the article (word counts and dates are exempt)."""
    import re
    art, _, notes = _notes("substack_free.html")
    source = art.text.lower()
    for item in notes.all_items():
        if item.provenance.value == "original":
            continue
        text = item.plain_text
        text = re.sub(r"\(\d+ words?\)|\d{4}-\d{2}-\d{2}|\b\d+x\b|\b[1-9]\d?\b", "", text)  # counts, dates, small ordinals
        for number in re.findall(r"\b\d[\d,.]*%?", text):
            assert number in source, f"figure {number!r} not in the article: {item.plain_text[:80]!r}"


def test_list_question_item_count_matches_the_articles_top_level_items() -> None:
    _, _, notes = _notes("substack_free.html")
    q = next(q for q in notes.chapter(7).questions if q.question.startswith("List the"))
    assert "List the 3 items" in q.question  # the article says "three main benefits"
