# NoteForge AI V1.0 - Release Notes (controlled release candidate)

Validation date: **2026-11-07** (Phase 9). Program version string: `0.1.0` (see the [release checklist](V1.0_RELEASE_CHECKLIST.md)). Back to the [README](../README.md).

## 1. Overview

NoteForge AI converts a public **Substack** article into a seven-chapter, professionally formatted **Word study-notes document** in which every item shows its origin: the article's own words, text assembled by NoteForge's rules, or (optionally) text written by a language model. The Local backend is the default and is free, offline and deterministic; the Ollama backend is an optional, experimental add-on.

**Recommendation: controlled release.** The Phase 9 validation concluded "READY FOR CONTROLLED V1.0 RELEASE" with conditions (section 8). This is a recommendation based on testing, not a guarantee of defect-free operation.

## 2. Supported capabilities

- Input: public Substack post URL; saved HTML page; plain-text file. **Substack is the only supported article source.** PDF, Medium, YouTube and research-paper extraction are not supported.
- Extraction completeness reporting (COMPLETE / PARTIAL / NOT VERIFIED); paywalls are detected and never bypassed.
- Seven chapters: Article Overview, Detailed Concept Explanation, Important Technical Keywords, Real-Life Analogies and Applications, Key Takeaways, Quick Revision Notes, Questions and Answers (10 short-answer, 5 descriptive, 10 multiple-choice requested; fewer when the article lacks distinct material).
- Detail levels Basic, Intermediate (default), Advanced.
- Word output: cover page, table of contents, headers and footers, page numbering, tables, nested lists, code blocks, source references `[¶n]`, colour-coded origin labels, partial-content warnings.
- Built-in checks: structure, provenance (quoted text really occurs in the article), and a re-read of the saved file.
- Interactive CLI for Windows PowerShell; never overwrites an existing document without confirmation.

## 3. Main components

`src/main.py` (CLI) -> `article_extractor.py` -> `content_processor.py` -> `notes_generator.py` (+ backends: Local, Ollama) -> `word_generator.py`. See [ARCHITECTURE.md](ARCHITECTURE.md).

## 4. Quality improvements since the first end-to-end build

Phase 8 (real-world QA) found ten defects; Phases 8.1, 8.2 and 9 addressed them:

- Nested list items are counted and quoted correctly (D-3).
- Sentences that are not real definitions are no longer used as answer keys (D-5).
- Question diversity: a fact is never asked twice and one passage cannot supply most of the questions; AI questions carry source references (D-4).
- AI-written text is checked against the article's wording: unsupported statements are removed, partly supported ones carry "Review needed", comparison analogies are marked (D-7).
- The `OLLAMA_MODEL` environment variable is honoured (D-2).
- Correct title when a page's structured data holds the site tagline (D-1); long paragraphs are no longer styled as headings (D-9).
- Weaker key terms are removed or ranked lower (D-6); Chapter 5 now shows practical lessons and findings (D-10).
- Word layout: the table of contents formatting now takes effect (it fits one page for a typical document), and boxed items do not split across pages (D-8).
- Windows PowerShell compatibility: input prefixed with a UTF-8 byte-order mark is now accepted (Phase 9).

## 5. Validation evidence (2026-11-07)

| Evidence | Result |
|---|---|
| Automated tests | **1,712 passed, 0 failed**, in both Bash and Windows PowerShell; `pip check` clean |
| Windows PowerShell | The CLI works from real Windows PowerShell, including piped input |
| Real end-to-end tests | **Two real public Substack articles** (659 and 832 words) ran through the real CLI with the Local backend: extraction COMPLETE, notes self-check OK, Word documents opened in Microsoft Word 16 |
| Word inspection | Six generated documents (short, medium, long, partial, Local, AI-style) inspected in Microsoft Word |
| Installation | A clean virtual environment was built from `requirements-dev.txt` on 2026-11-07 (Phase 10): install OK, `pip check` clean, 84 tests passed there |

These results show the tested paths worked on the tested date. They do **not** prove the absence of defects.

## 6. Known limitations

- Substack only; English-oriented heuristics.
- Local notes are extractive: no new explanations or analogies; question counts can be below 10/5/10.
- Ollama notes: experimental, slow (minutes; measured only for Basic, no questions), memory-heavy (about 3.7 GB), not repeatable, and **not guaranteed factually correct**. The support check is vocabulary-based and the live removal of unsupported sentences was never observed with the real model.
- Layout: chapters start on new pages; the last lines of a list can spill onto an almost empty page; a table of contents of more than about 45 entries uses two pages.
- Keyword noise can remain on marketing-style articles (all-caps words, capitalised names, numbered heading labels).
- Not tested live: articles over about 2,000 words with real tables/code, non-English articles, a real keyboard Ctrl+C. Word rendering was inspected for six generated documents only.
- Configuration: the program does not load `.env` files and has no settings file. `.env.example` is only a reference list of the nine supported `OLLAMA_*` environment variables; only real environment variables (set in PowerShell), `--output-dir` and menu choices have any effect.
- The program reports version `0.1.0`. No Git repository tag exists.

## 7. Installation requirements

Windows 10/11, Python (3.14.7 used for all testing; other versions untested), packages from `requirements.txt` (requests, beautifulsoup4, lxml, trafilatura, readability-lxml, python-docx, python-dotenv, regex), internet access for URL input. Optional: Ollama and the `qwen3.5:4b` model (about 3.3 GB). Microsoft Word is needed only to view the output. Steps: [INSTALLATION_GUIDE.md](INSTALLATION_GUIDE.md).

## 8. Backend information and controlled-release conditions

| Backend | Status in V1.0 |
|---|---|
| Local | Default; supported |
| Ollama | Optional; **experimental**; label notes as requiring human review |
| OpenAI | Not reachable from the program; never reads `OPENAI_API_KEY`; no paid calls |

Conditions: (1) present Ollama notes as experimental and requiring review; (2) state the supervised-use caveat for very long, non-English, and table/code-heavy articles; (3) keep the documentation set with the release; (4) resolve the open checklist items (for example the version string) before any wider distribution. Guides: [Backend Guide](BACKEND_GUIDE.md), [User Guide](USER_GUIDE.md), [Troubleshooting](TROUBLESHOOTING.md).

## 9. Reports

[Phase 8 QA](PHASE_8_QUALITY_ASSURANCE_REPORT.md) · [Phase 8.1](PHASE_8_1_DEFECT_CORRECTION_REPORT.md) · [Phase 8.2](PHASE_8_2_DEFECT_CORRECTION_REPORT.md) · [Phase 9 validation](PHASE_9_FINAL_VALIDATION_REPORT.md) · [Phase 10 preparation](PHASE_10_RELEASE_PREPARATION_REPORT.md)
