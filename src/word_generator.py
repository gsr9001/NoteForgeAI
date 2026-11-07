"""Word (.docx) document generation (Phase 6).

Turns a :class:`~src.notes_model.Notes` object into a professionally formatted Word document.

This module only *renders*. It never calls a language model, makes no network request and does
not regenerate, rewrite or drop any content: every item, question, notice and warning in the
Notes appears in the document, and :func:`verify_document` can prove it by re-reading the file.

Design points
-------------
* Works with notes from any backend (Local, Ollama, OpenAI): it only looks at the provenance
  attached to each item. ORIGINAL, DERIVED and AI_GENERATED content have different visual styles,
  and AI-generated content always carries a visible label.
* The table of contents is a real Word field (``TOC \\o "1-3"``). It is pre-filled with the heading
  list so it is usable even in viewers that do not update fields, and Word refreshes it (with page
  numbers) when the file is opened. Page numbers are ``PAGE`` / ``SECTIONPAGES`` fields.
* Existing files are never overwritten without confirmation; otherwise a unique name is chosen.
"""
from __future__ import annotations

import io
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence, Union
from urllib.parse import urlsplit

from docx import Document
from docx.document import Document as DocumentType
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from src.article_extractor import Completeness
from src.notes_model import (
    Chapter, ItemKind, NoteItem, NoteProvenance, NoteSection, Notes, Question, QuestionKind,
)

logger = logging.getLogger(__name__)

ORIG, DER, AI = NoteProvenance.ORIGINAL, NoteProvenance.DERIVED, NoteProvenance.AI_GENERATED

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


# =========================================================================== errors / config
class WordGenerationError(Exception):
    """The document could not be built or saved."""


@dataclass(frozen=True)
class WordConfig:
    show_source_refs: bool = True  # small grey [¶n] markers linking items to article blocks
    update_fields_on_open: bool = True  # Word refreshes the TOC and page numbers when opening
    generated_on: Optional[date] = None  # cover date; today if None (fixed in tests)
    body_font: str = "Calibri"
    code_font: str = "Consolas"
    math_font: str = "Cambria Math"
    complex_script_font: str = "Nirmala UI"  # Telugu and other Indic scripts
    author_name: str = "NoteForge AI"


@dataclass(frozen=True)
class SaveResult:
    path: Path  # where the document was written
    requested_path: Path  # the name first wanted (differs when a unique name had to be chosen)
    renamed: bool
    overwritten: bool
    sanitized_chars: int  # XML-illegal control characters removed from the text
    problems: tuple[str, ...]  # completeness problems found when the saved file was re-read (empty = complete)


# =========================================================================== palette & geometry
NAVY, BLUE, PURPLE, GREY = "1F3864", "2E74B5", "7030A0", "7F7F7F"
GOLD, GREEN, ORANGE, RED, SKY = "BF8F00", "548235", "C55A11", "C00000", "5B9BD5"
FILL_AI, FILL_DEF, FILL_TAKE, FILL_EX = "F3ECFA", "FFF2CC", "E8F3E1", "FDEBD9"
FILL_WARN, FILL_INFO, FILL_ANS, FILL_EXPL, FILL_CODE, FILL_BAND = "FDE9E7", "EAF1F8", "E2F0D9", "F2F2F2", "F5F5F5", "F2F7FC"

PAGE_W_CM, PAGE_H_CM = 21.0, 29.7  # A4
MARGIN_LR_CM, MARGIN_TOP_CM, MARGIN_BOTTOM_CM = 2.3, 2.5, 2.3
TEXT_W_CM = PAGE_W_CM - 2 * MARGIN_LR_CM  # 16.4 cm
TEXT_W_TWIPS = int(round(TEXT_W_CM * 567))

_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

# Schema child orders: Word rejects some files whose property elements are out of order.
_PPR = ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
        "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct",
        "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid", "spacing", "ind",
        "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection", "textAlignment",
        "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr", "pPrChange"]
_RPR = ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline", "shadow",
        "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing", "w", "kern", "position",
        "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang",
        "eastAsianLayout", "specVanish", "oMath"]
_TCPR = ["cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd", "noWrap", "tcMar", "textDirection",
         "tcFitText", "vAlign", "hideMark"]
_TBLPR = ["tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize", "tblStyleColBandSize", "tblW", "jc",
          "tblCellSpacing", "tblInd", "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook"]
_SECTPR = ["headerReference", "footerReference", "footnotePr", "endnotePr", "type", "pgSz", "pgMar", "paperSrc",
           "pgBorders", "lnNumType", "pgNumType", "cols", "formProt", "vAlign", "noEndnote", "titlePg", "textDirection",
           "bidi", "rtlGutter", "docGrid"]


def _local(el: Any) -> str:
    return el.tag.split("}")[1]


def _put(parent: Any, child: Any, order: Sequence[str]) -> Any:
    """Insert ``child`` into ``parent`` at the schema-correct position, replacing an existing element of the same kind."""
    name = _local(child)
    for existing in list(parent):
        if _local(existing) == name and name not in ("headerReference", "footerReference"):
            parent.remove(existing)
    idx = order.index(name)
    for existing in parent:
        if _local(existing) in order and order.index(_local(existing)) > idx:
            existing.addprevious(child)
            return child
    parent.append(child)
    return child


def _el(tag: str, **attrs: Any) -> Any:
    e = OxmlElement(f"w:{tag}")
    for k, v in attrs.items():
        e.set(qn(f"w:{k}"), str(v))
    return e


# =========================================================================== text utilities
def sanitize_text(text: Any) -> str:
    """Remove characters that are illegal in XML (they would make python-docx raise). Nothing else is changed."""
    return _XML_ILLEGAL.sub("", str(text if text is not None else ""))


_RESERVED = {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}
_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(title: str, suffix: str = "_Notes", ext: str = ".docx", max_title_chars: int = 80) -> str:
    """``Article_Title_Notes.docx``: Windows-safe, Unicode letters kept, no path components, bounded length."""
    text = unicodedata.normalize("NFC", sanitize_text(title or ""))
    text = _BAD_FILENAME.sub(" ", text)
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r"_+", "_", text).strip("._ -")
    text = text[:max_title_chars].rstrip("._ -")
    if not text:
        text = "Untitled_Article"
    if text.upper().split(".")[0] in _RESERVED:
        text = f"_{text}"
    return f"{text}{suffix}{ext}"


def _write_new(path: Path, data: bytes) -> bool:
    """Create ``path`` exclusively. False if it already exists (never overwrites)."""
    try:
        with open(path, "xb") as handle:
            handle.write(data)
        return True
    except FileExistsError:
        return False


def _format_date(d: Optional[date]) -> str:
    return d.strftime("%d %B %Y").lstrip("0") if d else "Not available"


def _refs(indices: Sequence[int], prefix: str = "") -> str:
    """Block numbers as '[¶3]', '[¶3–5]' or '[¶3, ¶7]' (1-based, like a reader would count)."""
    nums = sorted({i + 1 for i in indices if isinstance(i, int) and i >= 0})
    if not nums:
        return ""
    groups: list[tuple[int, int]] = []
    for n in nums:
        if groups and n == groups[-1][1] + 1:
            groups[-1] = (groups[-1][0], n)
        else:
            groups.append((n, n))
    parts = [f"¶{a}" if a == b else (f"¶{a}–{b}" if b > a + 1 else f"¶{a}, ¶{b}") for a, b in groups]
    shown = ", ".join(parts[:6]) + (", …" if len(parts) > 6 else "")
    return f"[{prefix}{shown}]"


# =========================================================================== item look
def look_for(item: NoteItem, chapter_no: int) -> str:
    """The paragraph style an item is drawn with (provenance first, then role)."""
    if item.kind is ItemKind.NOTICE:
        return "NF Warning" if item.level == "warning" else "NF Info"
    if item.provenance is AI:
        return "NF AI"
    if item.kind is ItemKind.DEFINITION:
        return "NF Definition"
    if item.provenance is DER:
        return "NF Derived"
    if item.kind is ItemKind.QUOTE:
        return "NF Quote"
    if chapter_no == 5:
        return "NF Takeaway"
    if chapter_no == 4:
        return "NF Example"
    return "NF Original"


def _is_formula(item: NoteItem) -> bool:
    return item.kind is ItemKind.TEXT and bool(item.label) and item.label.lower().startswith("formula")


@dataclass
class _Heading:
    level: int
    text: str
    bookmark: str


# =========================================================================== the builder
class _Builder:
    def __init__(self, notes: Notes, config: WordConfig) -> None:
        self.notes, self.cfg = notes, config
        self.doc: DocumentType = Document()
        self.sanitized = 0
        self.headings: list[_Heading] = []
        self._bm_id = 100
        self._num_id = 200
        self._bullet_num: int = 0
        self._last_heading_level = 0
        self._toc_anchor: Any = None
        self.sid: dict[str, str] = {}  # style name -> style id, filled once the styles exist

    def pstyle(self, par: Paragraph, name: str) -> None:
        par._p.style = self.sid[name]

    def new_par(self, name: str) -> Paragraph:
        p = self.doc.add_paragraph()
        p._p.style = self.sid[name]
        return p

    def cell_par(self, cell: _Cell, name: str) -> Paragraph:
        p = cell.add_paragraph()
        p._p.style = self.sid[name]
        return p

    # ------------------------------------------------------------------ small helpers
    def t(self, text: Any) -> str:
        raw = str(text if text is not None else "")
        clean = sanitize_text(raw)
        self.sanitized += len(raw) - len(clean)
        return clean

    def _set_fonts(self, rfonts: Any, name: str, *, mono: bool = False) -> None:
        for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            rfonts.attrib.pop(qn(f"w:{attr}"), None)
        cs = self.cfg.code_font if mono else self.cfg.complex_script_font
        for attr, value in (("ascii", name), ("hAnsi", name), ("eastAsia", name), ("cs", cs)):
            rfonts.set(qn(f"w:{attr}"), value)

    def _style_fonts(self, style: Any, name: str, mono: bool = False) -> None:
        rpr = style.element.get_or_add_rPr()
        rf = rpr.find(qn("w:rFonts"))
        if rf is None:
            rf = _el("rFonts")
            _put(rpr, rf, _RPR)
        self._set_fonts(rf, name, mono=mono)

    # ------------------------------------------------------------------ styles
    def define_styles(self) -> None:
        d, cfg = self.doc, self.cfg
        styles = d.styles
        # document defaults: remove theme fonts so our fonts really apply, set languages incl. Telugu (complex script)
        defaults = styles.element.find(qn("w:docDefaults")).find(qn("w:rPrDefault")).find(qn("w:rPr"))
        rf = defaults.find(qn("w:rFonts"))
        self._set_fonts(rf, cfg.body_font)
        lang = defaults.find(qn("w:lang"))
        if lang is None:
            lang = _el("lang")
            _put(defaults, lang, _RPR)
        lang.set(qn("w:val"), "en-US")
        lang.set(qn("w:bidi"), "te-IN")

        normal = styles["Normal"]
        normal.font.name, normal.font.size = cfg.body_font, Pt(10.5)
        self._style_fonts(normal, cfg.body_font)
        normal.paragraph_format.space_after = Pt(6)
        normal.paragraph_format.line_spacing = 1.12

        for name, size, color, before, after, page_break in (
                ("Heading 1", 20, NAVY, 0, 10, True), ("Heading 2", 14, BLUE, 14, 5, False), ("Heading 3", 12, "404040", 10, 4, False)):
            st = styles[name]
            st.font.name, st.font.size, st.font.bold = cfg.body_font, Pt(size), True
            st.font.color.rgb = RGBColor.from_string(color)
            self._style_fonts(st, cfg.body_font)
            st.paragraph_format.space_before, st.paragraph_format.space_after = Pt(before), Pt(after)
            st.paragraph_format.keep_with_next = True
            st.paragraph_format.page_break_before = page_break
            if name == "Heading 1":  # underline rule under chapter titles
                _put(st.element.get_or_add_pPr(), parse_xml(
                    f'<w:pBdr xmlns:w="{_W}"><w:bottom w:val="single" w:sz="12" w:space="4" w:color="{BLUE}"/></w:pBdr>'), _PPR)

        def para(name: str, *, base: str = "Normal", size: Optional[float] = None, bold: Optional[bool] = None,
                 italic: Optional[bool] = None, color: Optional[str] = None, before: float = 0, after: float = 6,
                 left_cm: float = 0.0, bar: Optional[str] = None, fill: Optional[str] = None, font: Optional[str] = None,
                 box: Optional[str] = None, align: Optional[Any] = None, keep_next: bool = False,
                 keep_lines: bool = False) -> Any:
            st = styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
            st.base_style = styles[base]
            st.quick_style = True
            if size:
                st.font.size = Pt(size)
            if bold is not None:
                st.font.bold = bold
            if italic is not None:
                st.font.italic = italic
            if color:
                st.font.color.rgb = RGBColor.from_string(color)
            if font:
                st.font.name = font
                self._style_fonts(st, font, mono=(font == cfg.code_font))
            pf = st.paragraph_format
            pf.space_before, pf.space_after = Pt(before), Pt(after)
            if left_cm:
                pf.left_indent = Cm(left_cm)
            if align is not None:
                pf.alignment = align
            pf.keep_with_next = keep_next or None
            pf.keep_together = keep_lines or None  # D-8: a boxed item is not split across two pages
            ppr = st.element.get_or_add_pPr()
            if box:
                _put(ppr, parse_xml(
                    f'<w:pBdr xmlns:w="{_W}">' + "".join(
                        f'<w:{side} w:val="single" w:sz="6" w:space="4" w:color="{box}"/>' for side in ("top", "left", "bottom", "right"))
                    + "</w:pBdr>"), _PPR)
            elif bar:
                _put(ppr, parse_xml(
                    f'<w:pBdr xmlns:w="{_W}"><w:left w:val="single" w:sz="18" w:space="8" w:color="{bar}"/></w:pBdr>'), _PPR)
            if fill:
                _put(ppr, parse_xml(f'<w:shd xmlns:w="{_W}" w:val="clear" w:color="auto" w:fill="{fill}"/>'), _PPR)
            return st

        # provenance styles: the visual vocabulary of the whole document
        para("NF Original", bar=BLUE, left_cm=0.35, after=5)
        para("NF Quote", bar=BLUE, left_cm=0.9, italic=True, after=5, keep_lines=True)
        para("NF Derived", bar="A6A6A6", left_cm=0.35, color="404040", after=4)
        para("NF AI", bar=PURPLE, fill=FILL_AI, left_cm=0.35, after=5, keep_lines=True)
        para("NF Definition", bar=GOLD, fill=FILL_DEF, left_cm=0.35, after=5, keep_lines=True)
        para("NF Takeaway", bar=GREEN, fill=FILL_TAKE, left_cm=0.35, after=5, keep_lines=True)
        para("NF Example", bar=ORANGE, fill=FILL_EX, left_cm=0.35, after=5, keep_lines=True)
        para("NF Warning", bar=RED, fill=FILL_WARN, left_cm=0.35, after=6, bold=True, color="7B1E15", keep_lines=True)
        para("NF Info", bar=SKY, fill=FILL_INFO, left_cm=0.35, after=6, color="1F3864", keep_lines=True)
        para("NF Question", bold=True, color=NAVY, before=8, after=3, keep_next=True)
        para("NF Option", left_cm=0.9, after=2, keep_next=True)
        para("NF Answer", bar=GREEN, fill=FILL_ANS, left_cm=0.35, before=3, after=3, keep_lines=True)
        para("NF Explanation", bar="A6A6A6", fill=FILL_EXPL, left_cm=0.35, italic=True, after=6, keep_lines=True)
        para("NF Tag", size=8, italic=True, color=GREY, after=2, keep_next=True)
        para("NF Label", size=8.5, bold=True, color=BLUE, after=2, keep_next=True)
        para("NF Code", size=9, font=cfg.code_font, after=0, color="1A1A1A")
        para("NF Equation", font=cfg.math_font, size=11, align=WD_ALIGN_PARAGRAPH.CENTER, before=4, after=8, bar=None)
        para("NF Cell", size=9.5, after=2)
        para("NF List", after=2)
        para("NF Small", size=8.5, color=GREY, after=2)
        para("NF Cover Kicker", size=11, color="D6DCE5", bold=True, after=6)
        para("NF Cover Title", size=30, color="FFFFFF", bold=True, after=8)
        para("NF Cover Sub", size=13, color="D6DCE5", after=4)
        para("NF TOC Title", size=20, bold=True, color=NAVY, after=12)
        for name in ("NF Header", "NF Footer"):
            st = para(name, size=9, after=0)
            st.paragraph_format.tab_stops.add_tab_stop(Cm(TEXT_W_CM), WD_TAB_ALIGNMENT.RIGHT)
        for lvl, (indent, bold) in enumerate(((0.0, True), (0.6, False), (1.2, False)), start=1):
            # D-8: compact entries (single line spacing, small gaps) so a typical table of contents of about 40
            # entries fits on one page instead of spilling a few lines onto an almost empty second page
            st = para(f"TOC {lvl}", size=10 if lvl > 1 else 10.5, bold=bold, left_cm=indent, after=1 if lvl > 1 else 2, before=3 if lvl == 1 else 0)
            st.paragraph_format.line_spacing = 1.0
            # Word reserves "TOC 1..3" for its own table-of-contents styles. python-docx marks added styles as
            # custom, so Word renamed ours ("TOC 21") and, when it rebuilt the table of contents on open, used its
            # own 5 pt spacing instead. Without the custom flag Word recognises these as the built-in styles.
            st.element.attrib.pop(qn("w:customStyle"), None)
            st.paragraph_format.tab_stops.add_tab_stop(Cm(TEXT_W_CM), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)

        for name, color, bold in (("NF Ref", GREY, False), ("NF Badge AI", PURPLE, True), ("NF Badge Orig", BLUE, True),
                                  ("NF Term", NAVY, True)):
            cs = styles.add_style(name, WD_STYLE_TYPE.CHARACTER)
            cs.font.color.rgb = RGBColor.from_string(color)
            cs.font.bold = bold
            if name == "NF Ref":
                cs.font.size = Pt(8)
            if name.startswith("NF Badge"):
                cs.font.size = Pt(8.5)
        self.sid = {st.name: st.style_id for st in styles}

    # ------------------------------------------------------------------ numbering (own definitions, so lists restart)
    def define_numbering(self) -> None:
        numbering = self.doc.part.numbering_part.element

        def abstract(aid: int, kinds: Sequence[tuple[str, str]]) -> Any:
            lvls = "".join(
                f'<w:lvl w:ilvl="{i}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/><w:lvlText w:val="{text}"/>'
                f'<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="{int((0.75 * (i + 1) + 0.35) * 567)}" w:hanging="{int(0.5 * 567)}"/></w:pPr></w:lvl>'
                for i, (fmt, text) in enumerate(kinds))
            return parse_xml(f'<w:abstractNum xmlns:w="{_W}" w:abstractNumId="{aid}"><w:multiLevelType w:val="hybridMultilevel"/>{lvls}</w:abstractNum>')

        bullets = abstract(90, [("bullet", "•"), ("bullet", "–"), ("bullet", "▪")])
        decimals = abstract(91, [("decimal", "%1."), ("lowerLetter", "%2."), ("lowerRoman", "%3.")])
        first_num = numbering.find(qn("w:num"))
        for a in (bullets, decimals):
            first_num.addprevious(a) if first_num is not None else numbering.append(a)
        self._bullet_num = self._new_num(91 - 1)  # bullets share one instance

    def _new_num(self, abstract_id: int, restart: bool = False, level: int = 0) -> int:
        self._num_id += 1
        numbering = self.doc.part.numbering_part.element
        override = (f'<w:lvlOverride w:ilvl="{level}"><w:startOverride w:val="1"/></w:lvlOverride>' if restart else "")
        numbering.append(parse_xml(
            f'<w:num xmlns:w="{_W}" w:numId="{self._num_id}"><w:abstractNumId w:val="{abstract_id}"/>{override}</w:num>'))
        return self._num_id

    # ------------------------------------------------------------------ run / paragraph primitives
    def run(self, par: Paragraph, text: str, *, bold: Optional[bool] = None, italic: Optional[bool] = None,
            color: Optional[str] = None, size: Optional[float] = None, style: Optional[str] = None,
            mono: bool = False, fill: Optional[str] = None, underline: bool = False) -> Any:
        r = par.add_run(self.t(text))
        if style:
            r._r.style = self.sid[style]
        if bold is not None:
            r.bold = bold
        if italic is not None:
            r.italic = italic
        if color:
            r.font.color.rgb = RGBColor.from_string(color)
        if size:
            r.font.size = Pt(size)
        if underline:
            r.font.underline = True
        if mono:
            r.font.name = self.cfg.code_font
            self._set_fonts(r._r.get_or_add_rPr().find(qn("w:rFonts")), self.cfg.code_font, mono=True)
        if fill:
            _put(r._r.get_or_add_rPr(), _el("shd", val="clear", color="auto", fill=fill), _RPR)
        return r

    def newline(self, par: Paragraph) -> None:
        par.add_run().add_break()

    def paragraph(self, style: str = "Normal", text: Optional[str] = None, **fmt: Any) -> Paragraph:
        p = self.new_par(style)
        if text:
            self.run(p, text, **fmt)
        return p

    def spacer(self, pts: float = 4) -> Paragraph:
        p = self.new_par("NF Small")
        p.paragraph_format.space_after = Pt(pts)
        p.paragraph_format.space_before = Pt(0)
        return p

    def shade(self, par: Paragraph, fill: str) -> None:
        _put(par._p.get_or_add_pPr(), _el("shd", val="clear", color="auto", fill=fill), _PPR)

    # ------------------------------------------------------------------ fields, bookmarks, hyperlinks
    def field(self, par: Paragraph, instr: str, cached: str = "1", **fmt: Any) -> None:
        def fld(kind: str) -> Any:
            r = par.add_run()
            c = _el("fldChar", fldCharType=kind)
            r._r.append(c)
            return r

        fld("begin")
        r = par.add_run()
        it = _el("instrText")
        it.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        it.text = f" {instr} "
        r._r.append(it)
        fld("separate")
        self.run(par, cached, **fmt)
        fld("end")

    def bookmark_heading(self, par: Paragraph, name: str) -> None:
        self._bm_id += 1
        start = _el("bookmarkStart", id=self._bm_id, name=name)
        end = _el("bookmarkEnd", id=self._bm_id)
        runs = par._p.findall(qn("w:r"))
        if runs:
            runs[0].addprevious(start)
            runs[-1].addnext(end)
        else:
            par._p.append(start)
            par._p.append(end)

    def hyperlink(self, par: Paragraph, url: str, text: Optional[str] = None) -> None:
        """A clickable link, only for http(s) URLs (anything else is written as plain text)."""
        shown = self.t(text or url)
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            self.run(par, shown)
            return
        rid = par.part.relate_to(url, RT.HYPERLINK, is_external=True)
        link = _el("hyperlink")
        link.set(qn("r:id"), rid)
        r = OxmlElement("w:r")
        rpr = OxmlElement("w:rPr")
        _put(rpr, _el("color", val="0563C1"), _RPR)
        _put(rpr, _el("u", val="single"), _RPR)
        r.append(rpr)
        t = OxmlElement("w:t")
        t.text = shown
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        r.append(t)
        link.append(r)
        par._p.append(link)

    # ------------------------------------------------------------------ headings
    def heading(self, text: str, level: int) -> Paragraph:
        level = max(1, min(3, level))
        if level > self._last_heading_level + 1 and self._last_heading_level:
            level = self._last_heading_level + 1  # never skip a level (Heading 1 -> 3)
        self._last_heading_level = level
        clean = self.t(text)
        p = self.new_par(f"Heading {level}")
        p.add_run(clean)
        name = f"_Toc{900000000 + len(self.headings) + 1}"
        self.bookmark_heading(p, name)
        self.headings.append(_Heading(level, clean, name))
        return p

    # ------------------------------------------------------------------ tables
    def table(self, rows: int, cols: int, widths_cm: Sequence[float], *, borders: Optional[str] = "BFBFBF") -> Table:
        tbl = self.doc.add_table(rows=rows, cols=cols)
        tbl.autofit = False
        tbl.alignment = WD_TABLE_ALIGNMENT.LEFT
        tblpr = tbl._tbl.tblPr
        _put(tblpr, _el("tblW", w=TEXT_W_TWIPS, type="dxa"), _TBLPR)
        if borders:
            _put(tblpr, parse_xml(
                f'<w:tblBorders xmlns:w="{_W}">' + "".join(
                    f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="{borders}"/>'
                    for s in ("top", "left", "bottom", "right", "insideH", "insideV")) + "</w:tblBorders>"), _TBLPR)
        _put(tblpr, parse_xml(
            f'<w:tblCellMar xmlns:w="{_W}"><w:top w:w="50" w:type="dxa"/><w:left w:w="100" w:type="dxa"/>'
            f'<w:bottom w:w="50" w:type="dxa"/><w:right w:w="100" w:type="dxa"/></w:tblCellMar>'), _TBLPR)
        total = sum(widths_cm) or 1.0
        scaled = [w * TEXT_W_CM / total for w in widths_cm]  # always exactly the text width: never wider than the margins
        for i, w in enumerate(scaled):
            tbl.columns[i].width = Cm(w)
            for cell in tbl.columns[i].cells:
                cell.width = Cm(w)
        return tbl

    def cell_fill(self, cell: _Cell, fill: str) -> None:
        _put(cell._tc.get_or_add_tcPr(), _el("shd", val="clear", color="auto", fill=fill), _TCPR)

    def cell_borders(self, cell: _Cell, **sides: tuple[str, int]) -> None:
        xml = f'<w:tcBorders xmlns:w="{_W}">' + "".join(
            f'<w:{side} w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>' for side, (color, sz) in sides.items()) + "</w:tcBorders>"
        _put(cell._tc.get_or_add_tcPr(), parse_xml(xml), _TCPR)

    def row_props(self, row: Any, *, header: bool = False, keep_together: bool = True) -> None:
        trpr = row._tr.get_or_add_trPr()
        if keep_together:
            trpr.append(_el("cantSplit"))
        if header:
            trpr.append(_el("tblHeader"))

    def cell_text(self, cell: _Cell, text: str, *, bold: bool = False, color: Optional[str] = None, style: str = "NF Cell",
                  align: Optional[Any] = None) -> Paragraph:
        p = cell.paragraphs[0]
        self.pstyle(p, style)
        lines = self.t(text).split("\n")
        for i, line in enumerate(lines):
            if i:
                self.newline(p)
            self.run(p, line, bold=bold or None, color=color)
        if align is not None:
            p.alignment = align
        return p

    # ------------------------------------------------------------------ blocks of content
    def data_table(self, rows: Sequence[Sequence[str]], has_header: bool) -> None:
        ncols = max((len(r) for r in rows), default=1)
        weights = [min(max(max((len(str(r[c])) if c < len(r) else 0) for r in rows), 6), 40) for c in range(ncols)]
        tbl = self.table(len(rows), ncols, weights)
        for ri, row in enumerate(rows):
            header = has_header and ri == 0
            self.row_props(tbl.rows[ri], header=header)
            for ci in range(ncols):
                cell = tbl.cell(ri, ci)
                text = row[ci] if ci < len(row) else ""
                if header:
                    self.cell_fill(cell, NAVY)
                    self.cell_text(cell, text, bold=True, color="FFFFFF")
                else:
                    if ri % 2 == (0 if has_header else 1):
                        self.cell_fill(cell, FILL_BAND)
                    self.cell_text(cell, text)
        self.spacer(6)

    def code_block(self, code: str, language: Optional[str], refs: str = "", label_text: Optional[str] = None) -> None:
        label = self.paragraph("NF Label", label_text or (f"Code ({language})" if language else "Code"))
        if refs and self.cfg.show_source_refs:
            self.run(label, "  " + refs, style="NF Ref")
        tbl = self.table(1, 1, [TEXT_W_CM], borders="BFBFBF")
        cell = tbl.cell(0, 0)
        self.cell_fill(cell, FILL_CODE)
        first = True
        for line in self.t(code).replace("\r\n", "\n").split("\n"):
            p = cell.paragraphs[0] if first else self.cell_par(cell, "NF Code")
            first = False
            self.pstyle(p, "NF Code")
            r = p.add_run(line)
            r.font.name = self.cfg.code_font
            rpr = r._r.get_or_add_rPr()
            self._set_fonts(rpr.find(qn("w:rFonts")), self.cfg.code_font, mono=True)
            _put(rpr, _el("noProof"), _RPR)
            if line != line.strip():  # keep indentation exactly
                for t in r._r.findall(qn("w:t")):
                    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        self.spacer(6)

    def equation(self, text: str, refs: str = "") -> None:
        """A display equation as a real Word math zone (OMML), so Word renders and can edit it."""
        p = self.new_par("NF Equation")
        body = self.t(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        omml = parse_xml(
            f'<m:oMathPara xmlns:m="{_M}" xmlns:w="{_W}"><m:oMath><m:r><m:rPr><m:sty m:val="p"/></m:rPr>'
            f'<w:rPr><w:rFonts w:ascii="{self.cfg.math_font}" w:hAnsi="{self.cfg.math_font}"/></w:rPr>'
            f'<m:t xml:space="preserve">{body}</m:t></m:r></m:oMath></m:oMathPara>')
        p._p.append(omml)
        if refs and self.cfg.show_source_refs:
            small = self.paragraph("NF Small", None)
            small.alignment = WD_ALIGN_PARAGRAPH.CENTER
            self.run(small, refs, style="NF Ref")

    def list_paragraphs(self, items: Sequence[str], depths: Sequence[int], numbered: bool, style: str,
                        check: bool = False, ordered: Sequence[bool] = ()) -> list[Paragraph]:
        num_id = self._new_num(91, restart=True) if numbered else self._bullet_num
        out = []
        alt_id: Optional[int] = None  # numbering of the current run of numbered items inside a bullet list
        prev_alt = False
        for i, text in enumerate(items):
            depth = max(0, min(2, depths[i] if i < len(depths) else 0))
            item_num = num_id
            is_alt = False
            if i < len(ordered) and ordered[i] != numbered:  # a sub-list of the other kind (numbered <-> bullets)
                if ordered[i]:
                    is_alt = True
                    if not prev_alt or alt_id is None:
                        alt_id = self._new_num(91, restart=True, level=depth)
                    item_num = alt_id
                else:
                    item_num = self._bullet_num
            prev_alt = is_alt
            p = self.new_par(style)
            ppr = p._p.get_or_add_pPr()
            if not check:
                _put(ppr, parse_xml(f'<w:numPr xmlns:w="{_W}"><w:ilvl w:val="{depth}"/><w:numId w:val="{item_num}"/></w:numPr>'), _PPR)
            left = int((0.75 * (depth + 1) + 0.35) * 567)
            _put(ppr, _el("ind", left=left, hanging=int(0.5 * 567)), _PPR)
            p.paragraph_format.space_after = Pt(2)
            if check:
                self.run(p, "☐  ", size=11)
            self.run(p, text)
            out.append(p)
        return out

    # ------------------------------------------------------------------ one NoteItem
    def badge(self, par: Paragraph, item: NoteItem, newline: bool = True) -> None:
        """Provenance label for AI items (always) and a role label for others."""
        label = item.label or ""
        if item.provenance is AI:
            text = label if label.lower().startswith("ai-generated") else ("AI-GENERATED" + (f" · {label}" if label else ""))
            self.run(par, "◆ " + text, style="NF Badge AI")
        elif label:
            self.run(par, label, style="NF Badge Orig" if item.provenance is ORIG else "NF Ref", bold=True)
        else:
            return
        if newline:
            self.newline(par)

    def source_ref(self, par: Paragraph, item: NoteItem) -> None:
        if not self.cfg.show_source_refs or not item.source_indices:
            return
        text = _refs(item.source_indices, prefix="based on " if item.provenance is AI else "")
        if text:
            self.run(par, " " + text, style="NF Ref")

    def render_item(self, item: NoteItem, chapter_no: int) -> None:
        style = look_for(item, chapter_no)
        kind = item.kind
        if kind is ItemKind.NOTICE:
            p = self.paragraph(style)
            self.run(p, "⚠ WARNING: " if item.level == "warning" else "ℹ ", bold=True)
            self.run(p, item.text, bold=False if item.level != "warning" else None)
            return
        if kind is ItemKind.CODE:
            self.code_block(item.text, item.language, _refs(item.source_indices), item.label)
            return
        if kind is ItemKind.TABLE:
            if item.label:
                self.paragraph("NF Label", item.label)
            elif item.is_original:
                self.paragraph("NF Label", "Table from the article" + (" " + _refs(item.source_indices) if self.cfg.show_source_refs and item.source_indices else ""))
            self.data_table(item.rows, item.has_header)
            return
        if _is_formula(item):
            if item.label:
                self.paragraph("NF Label", item.label)
            self.equation(item.text, _refs(item.source_indices))
            return
        if kind in (ItemKind.BULLETS, ItemKind.NUMBERED, ItemKind.CHECKLIST):
            if item.label or item.provenance is AI:
                head = self.paragraph(style)
                self.badge(head, item, newline=False)
                self.source_ref(head, item)
            check = kind is ItemKind.CHECKLIST
            paras = self.list_paragraphs(item.items, item.depths, kind is ItemKind.NUMBERED, style, check=check,
                                         ordered=item.ordered)
            if paras and not (item.label or item.provenance is AI):
                self.source_ref(paras[-1], item)
            return
        if kind is ItemKind.DEFINITION:
            p = self.paragraph(style)
            if item.label:
                self.badge(p, item)
            if item.term:
                self.run(p, item.term, style="NF Term")
                self.run(p, " — ")
            self.run(p, item.text)
            if item.excerpt:
                self.run(p, " […]", style="NF Ref")
            self.source_ref(p, item)
            return
        # TEXT / QUOTE
        chunks = [c for c in self.t(item.text).split("\n")] or [""]
        inline_label = bool(item.label) and item.provenance is not AI and len(item.text) <= 120 and len(chunks) == 1
        p = self.paragraph(style)
        if inline_label:
            self.run(p, f"{item.label}: ", bold=True, color=NAVY)
        elif item.label or item.provenance is AI:
            self.badge(p, item)
        for i, chunk in enumerate(chunks):
            if i:
                p = self.paragraph(style)  # keep the model's paragraph breaks
            self.run(p, chunk)
        if item.excerpt:
            self.run(p, " […]", style="NF Ref")
        self.source_ref(p, item)

    # ------------------------------------------------------------------ chapters
    def render_section_items(self, section: NoteSection, chapter_no: int) -> None:
        for item in section.items:
            self.render_item(item, chapter_no)

    def chapter_start(self, chapter: Chapter) -> None:
        self._last_heading_level = 0
        self.heading(f"Chapter {chapter.number}: {chapter.title}", 1)
        for notice in chapter.notices:
            self.render_item(notice, chapter.number)

    def render_chapter(self, chapter: Chapter) -> None:
        self.chapter_start(chapter)
        if chapter.number == 3:
            self.render_keywords(chapter)
        elif chapter.number == 7:
            self.render_questions(chapter)
        else:
            base = min((sec.level for sec in chapter.sections), default=1)
            for section in chapter.sections:
                self.heading(section.heading, 2 + max(0, section.level - base))  # shallowest sections are Heading 2
                self.render_section_items(section, chapter.number)
        if not chapter.sections and not chapter.questions and not chapter.notices:
            self.paragraph("NF Info", "This chapter has no content.")

    def render_keywords(self, chapter: Chapter) -> None:
        for section in chapter.sections:
            self.heading(section.heading, 2)
            rows, extra = [], []
            for item in section.items:
                if item.kind is ItemKind.NOTICE:
                    extra.append(item)
                    continue
                label = ("Definition" if item.kind is ItemKind.DEFINITION else (item.label or "Note"))
                if item.kind is ItemKind.DEFINITION and item.label and "possible" in item.label.lower():
                    label = "Possible definition"
                rows.append((label, item))
            if rows:
                tbl = self.table(len(rows), 2, [3.2, 13.2])
                for ri, (label, item) in enumerate(rows):
                    self.row_props(tbl.rows[ri])
                    left, right = tbl.cell(ri, 0), tbl.cell(ri, 1)
                    self.cell_fill(left, "D9E2F3")
                    self.cell_text(left, label, bold=True, color=NAVY)
                    fill = {AI: FILL_AI}.get(item.provenance, FILL_DEF if item.kind is ItemKind.DEFINITION else None)
                    if fill:
                        self.cell_fill(right, fill)
                    p = right.paragraphs[0]
                    self.pstyle(p, "NF Cell")
                    if item.provenance is AI:
                        self.run(p, "◆ AI-GENERATED", style="NF Badge AI")
                        self.newline(p)
                    elif item.kind is ItemKind.DEFINITION and item.label:
                        self.run(p, item.label, style="NF Badge Orig")
                        self.newline(p)
                    for i, line in enumerate(self.t(item.text).split("\n")):
                        if i:
                            self.newline(p)
                        self.run(p, line)
                    self.source_ref(p, item)
                self.spacer(6)
            for item in extra:
                self.render_item(item, 3)

    def render_questions(self, chapter: Chapter) -> None:
        groups = ((QuestionKind.SHORT, "Short-Answer Questions"), (QuestionKind.DESCRIPTIVE, "Descriptive Questions"),
                  (QuestionKind.MCQ, "Multiple-Choice Questions"))
        for section in chapter.sections:  # defensive: Phase 5 puts none here, but never drop content
            self.heading(section.heading, 2)
            self.render_section_items(section, 7)
        for kind, title in groups:
            qs = [q for q in chapter.questions if q.kind is kind]
            if not qs:
                continue
            self.heading(title, 2)
            key: list[str] = []
            for n, q in enumerate(qs, start=1):
                self.render_question(q, n)
                if kind is QuestionKind.MCQ:
                    key.append(f"{n} {q.correct_letter or '?'}")
            if key:
                p = self.paragraph("NF Info")
                self.run(p, "Answer key: ", bold=True)
                self.run(p, "   ".join(key))

    @staticmethod
    def _prov_text(p: NoteProvenance, what: str) -> str:
        return {AI: f"AI-generated {what.lower()}", DER: f"{what} assembled by NoteForge from the article",
                ORIG: f"{what} quoted from the article"}[p]

    def render_question(self, q: Question, n: int) -> None:
        p = self.paragraph("NF Question")
        self.run(p, f"Q{n}. ", color=BLUE)
        self.run(p, q.question)
        if self.cfg.show_source_refs and q.source_indices:
            self.run(p, " " + _refs(q.source_indices), style="NF Ref")
        self.paragraph("NF Tag", self._prov_text(q.question_provenance, "Question"))
        if q.options:
            for i, option in enumerate(q.options):
                op = self.paragraph("NF Option")
                self.run(op, f"{'ABCD'[i]})  ", bold=True)
                self.run(op, option)
            ap = self.paragraph("NF Answer")
            self.run(ap, f"Correct answer: {q.correct_letter}) " if q.correct_letter else "Correct answer: ", bold=True, color="375623")
            self.run(ap, q.answer)
            if q.answer_provenance is AI:
                self.run(ap, "   ◆ AI-generated", style="NF Badge AI")
            if q.explanation:
                ep = self.paragraph("NF Explanation")
                self.run(ep, "Explanation: ", bold=True, italic=False)
                self.run(ep, q.explanation)
                if q.explanation_provenance is AI:
                    self.run(ep, "   ◆ AI-generated", style="NF Badge AI")
        else:
            lines = self.t(q.answer).split("\n")
            style = "NF AI" if q.answer_provenance is AI else "NF Answer"
            ap = self.paragraph(style)
            if q.answer_provenance is AI:
                self.run(ap, "◆ AI-GENERATED ANSWER", style="NF Badge AI")
                self.newline(ap)
            else:
                self.run(ap, "Answer", style="NF Badge Orig")
                self.run(ap, " (quoted from the article): " if q.answer_provenance is ORIG else " (assembled by NoteForge): ", style="NF Ref")
            for i, line in enumerate(lines):
                if i:
                    ap = self.paragraph(style)
                self.run(ap, line)

    # ------------------------------------------------------------------ front matter
    def cover(self) -> None:
        n = self.notes
        today = self.cfg.generated_on or date.today()
        banner = self.table(1, 1, [TEXT_W_CM], borders=None)
        cell = banner.cell(0, 0)
        self.cell_fill(cell, NAVY)
        tcmar = parse_xml(f'<w:tcMar xmlns:w="{_W}"><w:top w:w="500" w:type="dxa"/><w:left w:w="500" w:type="dxa"/>'
                          f'<w:bottom w:w="500" w:type="dxa"/><w:right w:w="500" w:type="dxa"/></w:tcMar>')
        _put(cell._tc.get_or_add_tcPr(), tcmar, _TCPR)
        p0 = cell.paragraphs[0]
        self.pstyle(p0, "NF Cover Kicker")
        self.run(p0, "STUDY NOTES")
        p1 = self.cell_par(cell, "NF Cover Title")
        self.run(p1, n.title)
        if n.author:
            p2 = self.cell_par(cell, "NF Cover Sub")
            self.run(p2, f"by {n.author}")
        if n.published:
            p3 = self.cell_par(cell, "NF Cover Sub")
            self.run(p3, f"Published {_format_date(n.published)}")
        self.spacer(14)
        if n.completeness is Completeness.PARTIAL:
            self.render_item(NoteItem(ItemKind.NOTICE, DER, level="warning", text=(
                "PARTIAL CONTENT. These notes are based only on the part of the article that was accessible. "
                "They do not cover the rest and must not be treated as a complete study of the article.")), 0)
        elif n.completeness is Completeness.UNVERIFIED:
            self.render_item(NoteItem(ItemKind.NOTICE, DER, level="info", text=(
                "The completeness of the supplied text could not be verified; the notes cover only what was provided.")), 0)
        meta = [("Source article", n.url or "Supplied by the user (no URL)"), ("Notes generated on", _format_date(today)),
                ("Generated with", f"{n.backend.name}" + (f" ({n.backend.model})" if n.backend.model else "")
                 + (" — free, runs on this machine" if n.backend.is_local and not n.backend.is_paid else " — PAID external service" if n.backend.is_paid else "")),
                ("Level of detail", n.detail.value), ("Language", n.language.value),
                ("Source status", {Completeness.COMPLETE: "Complete article", Completeness.PARTIAL: "PARTIAL — accessible portion only",
                                   Completeness.UNVERIFIED: "Not verified (text supplied by the user)"}[n.completeness])]
        tbl = self.table(len(meta), 2, [4.0, 12.4], borders="D9D9D9")
        for i, (k, v) in enumerate(meta):
            self.cell_fill(tbl.cell(i, 0), "F2F2F2")
            self.cell_text(tbl.cell(i, 0), k, bold=True, color=NAVY)
            p = self.cell_text(tbl.cell(i, 1), "" if k == "Source article" and n.url else v)
            if k == "Source article" and n.url:
                self.hyperlink(p, n.url)
            if k == "Source status" and n.is_partial:
                for r in p.runs:
                    r.font.color.rgb = RGBColor.from_string(RED)
                    r.bold = True
        self.spacer(10)
        self.paragraph("NF Small", "Content in this document is colour-coded by origin: the article's own words, text written by "
                                   "NoteForge's rules, and text written by a language model are always visibly different. "
                                   "See “How to read these notes” on the next page.")

    def info_page(self) -> None:
        n = self.notes
        self.heading("Article Information and Source Details", 1)
        rows = [("Title", n.title), ("Author", n.author or "Not available"), ("Published", _format_date(n.published)),
                ("Source URL", n.url or "Not available (text supplied by the user)"),
                ("Source status", n.completeness.value.upper()), ("Notes backend", n.backend.display()),
                ("Backend requested", n.requested_backend + (f" (not used: {n.fallback_reason})" if n.fallback_reason else "")),
                ("Detail / language", f"{n.detail.value} / {n.language.value}")]
        tbl = self.table(len(rows), 2, [3.6, 12.8])
        for i, (k, v) in enumerate(rows):
            self.cell_fill(tbl.cell(i, 0), "D9E2F3")
            self.cell_text(tbl.cell(i, 0), k, bold=True, color=NAVY)
            p = self.cell_text(tbl.cell(i, 1), "" if k == "Source URL" and n.url else v)
            if k == "Source URL" and n.url:
                self.hyperlink(p, n.url)
        self.spacer(8)
        self.heading("Warnings and Limitations", 2)
        wrote = False
        for banner in n.banners:
            self.render_item(banner, 0)
            wrote = True
        for reason in n.completeness_reasons:
            self.paragraph("NF Warning" if n.is_partial else "NF Info", f"Source status: {reason}")
            wrote = True
        for label, items in (("Warnings from article extraction", n.source_warnings), ("Warnings from notes generation", n.generation_warnings)):
            if items:
                self.paragraph("NF Label", label)
                self.list_paragraphs([str(w) for w in items], [0] * len(items), False, "NF Derived")
                wrote = True
        if not wrote:
            self.paragraph("NF Info", "No warnings were recorded for this article.")
        self.heading("How to Read These Notes", 2)
        legend = [
            ("NF Original", "From the article", "The article's own words, quoted exactly."
             + (" [¶n] points to the nth block of the original article." if self.cfg.show_source_refs else "")),
            ("NF Derived", "Written by NoteForge", "Structure, labels, questions and summaries assembled by fixed rules. No language model was used."),
            ("NF AI", "◆ AI-GENERATED", "Written by a language model to aid understanding. Not the article's words; check it against the article."),
            ("NF Definition", "Definition", "A definition stated in the article (or marked as possible, when only inferred from the sentence)."),
            ("NF Takeaway", "Key takeaway", "A conclusion, finding or recommendation quoted from the article."),
            ("NF Example", "Example", "An example or application the article itself mentions."),
            ("NF Warning", "Warning", "Something that limits how far the notes can be trusted, such as partial content."),
        ]
        tbl = self.table(len(legend), 2, [5.2, 11.2], borders="D9D9D9")
        for i, (style, sample, meaning) in enumerate(legend):
            self.row_props(tbl.rows[i])
            c0 = tbl.cell(i, 0)
            p = c0.paragraphs[0]
            self.pstyle(p, style)
            self.run(p, sample)
            self.cell_text(tbl.cell(i, 1), meaning)
        self.spacer(6)

    def toc_anchor(self) -> None:
        p = self.new_par("NF TOC Title")
        p.paragraph_format.page_break_before = True
        self.run(p, "Table of Contents")
        self._toc_anchor = self.paragraph("NF Small", None)._p  # entries are inserted here once all headings are known

    def fill_toc(self) -> None:
        """Insert the TOC field, pre-filled with the headings (Word updates it with page numbers on open)."""
        entries = [h for h in self.headings]
        anchor = self._toc_anchor
        paragraphs: list[Paragraph] = []
        prev = anchor
        for h in entries or [_Heading(1, "(no headings)", "")]:
            p = Paragraph(OxmlElement("w:p"), self.doc._body)
            self.pstyle(p, f"TOC {h.level}")
            prev.addnext(p._p)
            prev = p._p
            paragraphs.append(p)

        def fchar(par: Paragraph, kind: str) -> None:
            r = par.add_run()
            r._r.append(_el("fldChar", fldCharType=kind))

        first = paragraphs[0]
        fchar(first, "begin")
        r = first.add_run()
        it = _el("instrText")
        it.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        it.text = ' TOC \\o "1-3" \\h \\z \\u '
        r._r.append(it)
        fchar(first, "separate")
        for p, h in zip(paragraphs, entries or [_Heading(1, "(no headings)", "")]):
            link = _el("hyperlink", anchor=h.bookmark, history=1) if h.bookmark else None
            run = OxmlElement("w:r")
            t = OxmlElement("w:t")
            t.text = h.text
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            run.append(t)
            if link is not None:
                link.append(run)
                p._p.append(link)
            else:
                p._p.append(run)
        fchar(paragraphs[-1], "end")

    def references(self) -> None:
        n = self.notes
        self.heading("Original Article and References", 1)
        p = self.paragraph("NF Label", "Original article")
        rows = [("Title", n.title), ("Author", n.author or "Not available"), ("Published", _format_date(n.published)), ("URL", n.url or "Not available")]
        tbl = self.table(len(rows), 2, [3.0, 13.4])
        for i, (k, v) in enumerate(rows):
            self.cell_fill(tbl.cell(i, 0), "D9E2F3")
            self.cell_text(tbl.cell(i, 0), k, bold=True, color=NAVY)
            cp = self.cell_text(tbl.cell(i, 1), "" if k == "URL" and n.url else v)
            if k == "URL" and n.url:
                self.hyperlink(cp, n.url)
        self.spacer(8)
        counts = {ORIG: 0, DER: 0, AI: 0}
        for item in n.all_items():
            if item.kind is not ItemKind.NOTICE:
                counts[item.provenance] += 1
        for q in (q for c in n.chapters for q in c.questions):
            counts[q.question_provenance] += 1
        self.heading("Content Provenance Summary", 2)
        tbl = self.table(4, 2, [10.4, 6.0])
        for i, (label, key) in enumerate((("Items quoted from the article (original)", ORIG), ("Items written by NoteForge's rules (derived)", DER),
                                          ("Items written by a language model (AI-generated)", AI))):
            self.cell_text(tbl.cell(i, 0), label)
            self.cell_text(tbl.cell(i, 1), str(counts[key]), bold=True)
        self.cell_text(tbl.cell(3, 0), "Generated with", bold=True)
        self.cell_text(tbl.cell(3, 1), n.backend.name + (f" ({n.backend.model})" if n.backend.model else ""), bold=True)
        self.spacer(6)
        self.paragraph("NF Small", "These notes are study aids derived from the article above. Always consult the original for authoritative "
                                   "wording. AI-generated items were not written by the article's author.")

    # ------------------------------------------------------------------ page furniture
    def furniture(self) -> None:
        d, n = self.doc, self.notes
        for section in d.sections:
            section.page_width, section.page_height = Cm(PAGE_W_CM), Cm(PAGE_H_CM)
            section.left_margin = section.right_margin = Cm(MARGIN_LR_CM)
            section.top_margin, section.bottom_margin = Cm(MARGIN_TOP_CM), Cm(MARGIN_BOTTOM_CM)
            section.header_distance, section.footer_distance = Cm(1.2), Cm(1.1)
        cover, body = d.sections[0], d.sections[1]
        body.header.is_linked_to_previous = False
        body.footer.is_linked_to_previous = False
        _put(body._sectPr, _el("pgNumType", start=1), _SECTPR)  # page 1 is the first page after the cover

        short = n.title if len(n.title) <= 70 else n.title[:67].rstrip() + "…"
        hp = body.header.paragraphs[0]
        self.pstyle(hp, "NF Header")
        self.run(hp, short, size=9, color=NAVY, bold=True)
        self.run(hp, "\t")
        if n.is_partial:
            self.run(hp, "PARTIAL CONTENT", size=9, color=RED, bold=True)
        else:
            self.run(hp, "Study notes", size=9, color=GREY)
        _put(hp._p.get_or_add_pPr(), parse_xml(
            f'<w:pBdr xmlns:w="{_W}"><w:bottom w:val="single" w:sz="6" w:space="4" w:color="{BLUE}"/></w:pBdr>'), _PPR)

        fp = body.footer.paragraphs[0]
        self.pstyle(fp, "NF Footer")
        self.run(fp, f"NoteForge AI · notes by {n.backend.name}", size=8.5, color=GREY)
        self.run(fp, "\t")
        self.run(fp, "Page ", size=9, color="404040")
        self.field(fp, "PAGE", "1", size=9, color="404040")
        self.run(fp, " of ", size=9, color="404040")
        self.field(fp, "SECTIONPAGES", "1", size=9, color="404040")
        _put(fp._p.get_or_add_pPr(), parse_xml(
            f'<w:pBdr xmlns:w="{_W}"><w:top w:val="single" w:sz="4" w:space="4" w:color="BFBFBF"/></w:pBdr>'), _PPR)

    def properties(self) -> None:
        cp, n = self.doc.core_properties, self.notes
        def prop(text: str) -> str:  # python-docx refuses document properties longer than 255 characters
            clean = self.t(text)
            return clean if len(clean) <= 250 else clean[:249].rstrip() + "…"

        cp.title = prop(f"{n.title} — Study Notes")
        cp.subject = prop(f"Study notes generated from: {n.url or n.title}")
        cp.author = self.cfg.author_name
        cp.last_modified_by = self.cfg.author_name
        cp.keywords = "study notes; NoteForge AI"
        cp.comments = prop(f"Generated with {n.backend.display()}. Source status: {n.completeness.value}.")
        cp.language = "en-US"
        if self.cfg.generated_on:
            from datetime import datetime
            cp.created = cp.modified = datetime(self.cfg.generated_on.year, self.cfg.generated_on.month, self.cfg.generated_on.day)
        # python-docx's template targets Word 2010 and Word would open the file in "Compatibility Mode".
        # Declare the current layout engine (15 = Word 2013 and later) so it opens as a normal document.
        compat = self.doc.settings.element.find(qn("w:compat"))
        if compat is not None:
            existing = [c for c in compat.findall(qn("w:compatSetting")) if c.get(qn("w:name")) == "compatibilityMode"]
            if existing:
                existing[0].set(qn("w:val"), "15")
            else:
                compat.append(_el("compatSetting", name="compatibilityMode", uri="http://schemas.microsoft.com/office/word", val="15"))
        if self.cfg.update_fields_on_open:
            settings = self.doc.settings.element
            _put_settings = _el("updateFields", val="true")
            compat = settings.find(qn("w:compat"))
            if compat is not None:
                compat.addprevious(_put_settings)
            else:
                settings.append(_put_settings)

    # ------------------------------------------------------------------ whole document
    def build(self) -> DocumentType:
        self.define_styles()
        self.define_numbering()
        d = self.doc
        # remove the template's empty first paragraph (python-docx starts with none; kept defensive)
        self.cover()
        d.add_section(WD_SECTION.NEW_PAGE)
        self.info_page()
        self.toc_anchor()
        for chapter in self.notes.chapters:
            self.render_chapter(chapter)
        self.references()
        self.fill_toc()
        self.furniture()
        self.properties()
        return d


# =========================================================================== reading a document back
def _paragraph_text(p: Any) -> str:
    out: list[str] = []
    for node in p.iter():
        tag = node.tag
        if tag in (qn("w:t"), f"{{{_M}}}t"):
            out.append(node.text or "")
        elif tag == qn("w:tab"):
            out.append("\t")
        elif tag == qn("w:br"):
            out.append("\n")
    return "".join(out)


def document_text(doc: DocumentType) -> str:
    """All text in the body (paragraphs, tables, math), one paragraph per line, whitespace normalised."""
    lines = []
    for p in doc.element.body.iter(qn("w:p")):
        ppr = p.find(qn("w:pPr"))
        pstyle = ppr.find(qn("w:pStyle")) if ppr is not None else None
        if pstyle is not None and str(pstyle.get(qn("w:val"), "")).startswith("TOC") and pstyle.get(qn("w:val")) != "TOCHeading":
            continue  # the table of contents repeats heading text; it must not satisfy the completeness check
        text = re.sub(r"\s+", " ", _paragraph_text(p)).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", sanitize_text(text)).strip()


def expected_fragments(notes: Notes) -> list[tuple[str, str]]:
    """Every piece of text the document must contain: ``(where, text)`` pairs derived from the Notes."""
    out: list[tuple[str, str]] = [("title", notes.title)]
    if notes.author:
        out.append(("author", notes.author))
    if notes.url:
        out.append(("url", notes.url))
    for b in notes.banners:
        out.append(("banner", b.text))
    for r in notes.completeness_reasons:
        out.append(("completeness reason", r))
    for w in (*notes.source_warnings, *notes.generation_warnings):
        out.append(("warning", str(w)))

    def item_fragments(where: str, item: NoteItem) -> None:
        if item.label:
            out.append((f"{where} label", item.label))
        if item.term and item.kind is ItemKind.DEFINITION:
            out.append((f"{where} term", item.term))
        if item.kind in (ItemKind.BULLETS, ItemKind.NUMBERED, ItemKind.CHECKLIST):
            out.extend((where, t) for t in item.items)
        elif item.kind is ItemKind.TABLE:
            out.extend((where, c) for row in item.rows for c in row if c)
        elif item.kind is ItemKind.CODE:
            out.extend((where, line.strip()) for line in item.text.splitlines() if line.strip())
        elif item.text:
            out.extend((where, line) for line in item.text.split("\n") if line.strip())

    for chapter in notes.chapters:
        out.append(("chapter heading", f"Chapter {chapter.number}: {chapter.title}"))
        for n in chapter.notices:
            item_fragments(f"chapter {chapter.number} notice", n)
        for section in chapter.sections:
            out.append((f"chapter {chapter.number} section", section.heading))
            for item in section.items:
                item_fragments(f"chapter {chapter.number}", item)
        for q in chapter.questions:
            out.append((f"chapter {chapter.number} question", q.question))
            out.extend((f"chapter {chapter.number} option", o) for o in q.options)
            out.extend((f"chapter {chapter.number} answer", line) for line in q.answer.split("\n") if line.strip())
            if q.explanation:
                out.append((f"chapter {chapter.number} explanation", q.explanation))
    return out


def verify_document(source: Union[str, Path, DocumentType], notes: Notes) -> list[str]:
    """Re-read a document and report anything from the Notes that is missing (empty list = complete).

    Also checks that the seven chapter headings appear once each, in order.
    """
    doc = Document(str(source)) if isinstance(source, (str, Path)) else source
    text = _norm(document_text(doc))
    problems: list[str] = []
    for where, fragment in expected_fragments(notes):
        frag = _norm(fragment)
        if frag and frag not in text:
            problems.append(f"missing from document ({where}): {frag[:80]!r}")
    headings = [(p.style.name, _norm(p.text)) for p in doc.paragraphs if p.style.name.startswith("Heading")]
    positions = []
    for chapter in notes.chapters:
        label = _norm(f"Chapter {chapter.number}: {chapter.title}")
        hits = [i for i, (s, t) in enumerate(headings) if s == "Heading 1" and t == label]
        if len(hits) != 1:
            problems.append(f"chapter heading {label!r} appears {len(hits)} times (expected once)")
        else:
            positions.append(hits[0])
    if positions != sorted(positions):
        problems.append("chapter headings are out of order")
    return problems


# =========================================================================== public API
class WordGenerator:
    """Renders :class:`Notes` to a .docx. Pure rendering: no model calls, no network."""

    def __init__(self, config: Optional[WordConfig] = None) -> None:
        self.config = config or WordConfig()

    def build(self, notes: Notes) -> DocumentType:
        """Build the document in memory (no file is written)."""
        if not isinstance(notes, Notes):
            raise WordGenerationError(f"Expected a Notes object, got {type(notes).__name__}.")
        if not notes.chapters:
            raise WordGenerationError("The notes have no chapters to write.")
        builder = _Builder(notes, self.config)
        try:
            doc = builder.build()
        except WordGenerationError:
            raise
        except Exception as exc:  # python-docx/lxml errors: report clearly instead of a bare traceback
            raise WordGenerationError(f"Could not build the Word document: {type(exc).__name__}: {exc}") from exc
        self._last_sanitized = builder.sanitized
        if builder.sanitized:
            logger.warning("Removed %d XML-illegal control character(s) from the text", builder.sanitized)
        return doc

    def to_bytes(self, notes: Notes) -> bytes:
        buf = io.BytesIO()
        self.build(notes).save(buf)
        return buf.getvalue()

    def save(
        self,
        notes: Notes,
        output_dir: Union[str, Path, None] = None,
        *,
        confirm_overwrite: Optional[Callable[[Path], bool]] = None,
        verify: bool = True,
    ) -> SaveResult:
        """Write ``<output_dir>/<Article_Title>_Notes.docx``.

        An existing file is never replaced unless ``confirm_overwrite(path)`` returns True; otherwise
        a unique name (``..._Notes_2.docx``) is used. The saved file is re-opened and checked for completeness.
        """
        directory = Path(output_dir) if output_dir is not None else DEFAULT_OUTPUT_DIR
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WordGenerationError(f"Cannot create the output folder {directory}: {exc}") from exc
        data = self.to_bytes(notes)
        sanitized = getattr(self, "_last_sanitized", 0)
        name = safe_filename(notes.title)
        requested = directory / name
        if requested.resolve().parent != directory.resolve():
            raise WordGenerationError("Refusing to write outside the output folder.")
        stem, ext = Path(name).stem, Path(name).suffix
        overwritten = renamed = False
        try:
            if _write_new(requested, data):
                final = requested
            elif confirm_overwrite is not None and confirm_overwrite(requested):
                tmp = directory / f".~{name}.tmp"
                tmp.write_bytes(data)
                os.replace(tmp, requested)  # atomic: a crash never leaves a half-written document
                final, overwritten = requested, True
            else:
                final = requested
                for counter in range(2, 10000):
                    candidate = directory / f"{stem}_{counter}{ext}"
                    if _write_new(candidate, data):
                        final, renamed = candidate, True
                        break
                else:
                    raise WordGenerationError("Could not find a free file name.")
        except PermissionError as exc:
            raise WordGenerationError(f"Cannot write {requested.name}: permission denied (is it open in Word?).") from exc
        except OSError as exc:
            raise WordGenerationError(f"Cannot write the Word document: {exc}") from exc
        problems = tuple(verify_document(final, notes)) if verify else ()
        if problems:
            logger.error("Saved document is incomplete: %s", problems[:3])
        logger.info("Saved %s (%d KB)%s", final, len(data) // 1024, " [renamed]" if renamed else "")
        return SaveResult(final, requested, renamed, overwritten, sanitized, problems)


def generate_word(notes: Notes, output_dir: Union[str, Path, None] = None, config: Optional[WordConfig] = None,
                  confirm_overwrite: Optional[Callable[[Path], bool]] = None) -> SaveResult:
    """Convenience wrapper: build and save in one call."""
    return WordGenerator(config).save(notes, output_dir, confirm_overwrite=confirm_overwrite)
