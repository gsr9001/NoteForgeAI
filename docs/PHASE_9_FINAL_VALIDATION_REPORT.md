# NoteForge AI - Phase 9: Final Product Validation and V1.0 Release Readiness

Date: 2026-11-07 | Version under test: 0.1.0 | Environment: Windows 11, Python 3.14 venv, Windows PowerShell 5.1, Microsoft Word 16.0, Bash (Git) tool, Ollama `qwen3.5:4b` (not used in this phase)

Classification key: PASS, PASS WITH LIMITATIONS, NEEDS IMPROVEMENT, FAIL, NOT TESTED. Every result below is marked **[real]** (live public pages or the real program in real Windows PowerShell / Word) or **[fixture]** (saved HTML, synthetic input or scripted model replies).

## 1. Executive summary

| Area | Result |
|---|---|
| Windows PowerShell compatibility | **PASS** - the known failure was reproduced, fixed with a two-line input correction, and now passes in real PowerShell [real] |
| D-1 title extraction | **PASS** - verified on the real page that exhibits the defect (one public request) [real] and on all saved fixtures |
| D-9 heading classification | **PASS WITH LIMITATIONS** - verified through the whole pipeline [fixture]; not re-run on the original real page |
| D-6 keyword quality | **PASS WITH LIMITATIONS** - good on technical articles; one small improvement made after real-article evaluation; residual noise on marketing-style articles |
| D-7 grounding | **PASS WITH LIMITATIONS** - behaves as designed with scripted replies [fixture]; vocabulary check only; the live removal path remains unobserved |
| D-8 Word layout | **PASS WITH LIMITATIONS** - 6 documents inspected in Microsoft Word [real Word, generated notes]; one cosmetic issue remains |
| End-to-end (real CLI, 2 live articles) | **PASS** [real] |
| Regression | **PASS** - 1,712 passed, 0 failed, in both Bash and Windows PowerShell; `pip check` clean |
| Output preservation | **PASS** - 7 of 7 files identical |

**Recommendation: READY FOR CONTROLLED V1.0 RELEASE** (section 13). Phase 9 changed two source files and added one test file; no existing test was changed, no feature or format was added, Phase 10 was not started.

## 2. Windows PowerShell compatibility - PASS [real]

- **Reproduced** in real Windows PowerShell 5.1: running `test_works_from_windows_powershell` (and piping text to the program) failed. Output showed `'<U+FEFF>2' is not a menu option`.
- **Root cause (verified):** in that environment PowerShell encodes piped text as UTF-8 **with** a byte-order mark. Python decodes it as U+FEFF at the start of the first line, so the first menu answer ("2") was never recognised.
- **Input-handling location:** `App.__init__` in `src/main.py`. Every prompt (`ask`, `confirm`, the main menu) goes through `self.input_fn`, so one wrapper covers all of them.
- **Correction (smallest safe change):** new `strip_bom()`; `self.input_fn` now wraps the injected input function with it. It removes only *leading* byte-order marks (U+FEFF, or the same three bytes mis-decoded by a legacy code page). Marks inside text, whitespace, non-ASCII and Unicode text are untouched. Source stays ASCII (an existing test requires that, and caught my first attempt).
- **Tests added (33, `tests/test_phase9_validation.py`):** BOM-prefixed, double-BOM, mis-decoded BOM, normal UTF-8, empty, whitespace, Telugu/accented text, BOM in the middle of text (kept); app-level: BOM menu choice, full workflow with a BOM on the first line, empty lines, several inputs in a row, invalid choices (`0`, `8`, `abc`, `-1`, `1.5`) still rejected with the user's text intact, BOM inside a prompt answer, `b`/`back`/BOM+`b`, Ctrl+C at the menu (exit 130) and inside an action, closed input; real process: raw BOM bytes on a real pipe, a full session, and a real Windows PowerShell session with the BOM forced on.
- **Mutation check:** with the fix disabled, 15 of those 33 tests fail; with it, all pass.
- **Run in actual Windows PowerShell:** the previously failing test now passes; the full suite passes in PowerShell (1,712).

## 3. D-1 title extraction - PASS

| Source | JSON-LD headline | og:title | Final title | Kind |
|---|---|---|---|---|
| `on.substack.com/p/grow-5` (the Phase 8 defect page; one public request, saved to scratch) | "Subscription platform for online creators - Substack" (also the page `<title>`) | "Substack Grow: Launching paid subscriptions" | **"Substack Grow: Launching paid subscriptions"** (tagline detected, og:title used) | **real** |
| `on.substack.com/p/what-you-get-when-you-start-a-substack` | "What you get when you start a substack" + trailing no-break space | "What you get when you start a substack" | "What you get when you start a substack" (normal path, whitespace cleaned) | real |
| `substack_free.html` | "Understanding Retrieval-Augmented Generation" | same | same | fixture |
| `substack_paywalled.html` | "The Hidden Costs of Vector Databases" | same | same | fixture |
| `substack_custom_domain.html` | "Why Small Models Win at the Edge" | same | same | fixture |
| `short_post.html` | none | "Quick note on embeddings" | same | fixture |
| `login_wall.html` | none | none | "Sign in to continue" (heading fallback) | fixture |

og:title is preferred only when it exists and differs, and the headline equals the page `<title>`, equals the site name, or has a site-name suffix that og:title lacks. A normal headline still wins over a different og:title; no og:title keeps the headline. All nine Phase 8.2 D-1 tests pass.

## 4. D-9 heading classification - PASS WITH LIMITATIONS [fixture]

Tested through the full pipeline (`extract_from_html` -> `process_article` -> notes) on a synthetic article, since the original real page was not kept (10 tests, 4 of them parametrised block-converter cases from Phase 8.2):

| Case | Result |
|---|---|
| Normal heading | stays a heading |
| Long single-sentence "heading" (over 200 characters) | paragraph, text identical |
| Multi-sentence "heading" (over 120 characters) | paragraph, text identical |
| Long single-sentence paragraph / multi-sentence paragraph | unchanged paragraphs |
| Bold paragraph | paragraph (a short bold label cannot be told from body text) |
| Nested headings h2 > h3 > h4 | levels 2, 3, 4 preserved; sections nest correctly |
| Text loss | none; source indices valid; `verify_provenance` returns no problems; the demoted text is not in the section list or table of contents |
| Markdown `#` headings | never demoted (explicit by the author) |

Limitation: thresholds (200 / 120 characters) are heuristics; not re-run on the original real page.

## 5. D-6 keyword quality - PASS WITH LIMITATIONS

Evaluated on the five fixtures and two real articles (a 832-word and a 1,550-word product article).

- **Technical articles (RAG fixture, user notes, paywalled, custom-domain): good.** Defined acronyms kept (RAG, RAM), meaningful multiword concepts kept (Learning rate, Loss function, Gradient descent, Chunk size, Retrieval-Augmented Generation), "AI" gone, the heading-only "Common failure modes" ranked last, `retrieve` kept because the prose uses it, compound code identifiers outrank plain function names, ranking deterministic.
- **Real product articles: noisy.** Bare frequency words such as "post" and "example" ranked high.
- **Change made (one):** `_GENERIC_WORDS` in `src/content_processor.py` - a short list of everyday words (post, example, thing, time, way, day, week, month, year, people and plurals) that are dropped when they are found *only* by frequency. Words with a definition or other evidence are never dropped; technical-sounding words (list, page, type) are deliberately not on the list. Effect on the two real articles: "post" and "example(s)" disappear; the fixtures are unchanged.
- **Residual noise (not changed):** all-caps shouting matched as an acronym ("HEATED"), capitalised names ("Month", "Front Month", "Donna Tartt"), numbered heading labels ("Cost two: re-embedding"). These need a dictionary or a language model to separate reliably; Chapter 3 is capped by detail level, so the impact is limited.
- 9 new tests; mutation check: disabling the generic-word rule fails a test.

## 6. D-7 grounding - PASS WITH LIMITATIONS [fixture, scripted model; no live generation]

Verified with a deliberately mixed scripted reply (supported + partly supported + unsupported-comparison + invented-figure text) through the real generator and Word writer (10 tests):

| Check | Result |
|---|---|
| Supported statement | kept |
| Unsupported comparison ("instead of complex vector databases...") | removed; the removed text is not repeated in the warnings |
| Invented number / "Studies show" | the whole significance text rejected, with a warning |
| Partly supported statement | kept with "Review needed: ... only partly supported" in the warnings and the Word document |
| Comparison-based analogy ("... rather than ...") | kept, label ends "Review needed", warning added |
| Labelling | every AI item carries "AI-generated"; Chapter 4 keeps the red banner "The items below are NOT from the article..." (also visible in the Word render) |
| Source references | valid (`verify_provenance` clean; every index within the article's block range) |

**Not claimed:** the check is vocabulary-based (`assess_support` documents that it "proves nothing about truth"); a wrong claim in the article's own words passes, and a faithful paraphrase may be flagged. The filter was not changed. **No live Ollama generation was run in Phase 9** (not necessary, not requested). The live removal path was observed in no live run (Phase 8.1: PARTIALLY VERIFIED, unchanged).

## 7. D-8 Word layout - PASS WITH LIMITATIONS [real Word 16, generated documents]

Six scratch documents (never in `output/`): short (Basic), medium (real 832-word article), long (real 1,550-word article, Advanced), partial (paywalled fixture), RAG fixture Local, RAG fixture AI-style with review warnings. Opened read-only in Word, fields updated; PDF pages rendered and inspected visually.

| Document | Pages | TOC | Header | Sparse pages (<= 3 paragraphs) |
|---|---|---|---|---|
| short (Basic) | 11 | 1 page, 29 entries | "...title / PARTIAL CONTENT" (that fixture is partial) | p6 (3) |
| medium | 18 | 2 pages, 63 entries | title / Study notes | p6 (3 list lines) |
| long (Advanced) | 21 | 2 pages, 53 entries | title / Study notes | p10 (3) |
| partial | 13 | 1 page, 34 entries | red "PARTIAL CONTENT" | none |
| RAG Local | 15 | 1 page, 41 entries | title / Study notes | none |
| RAG AI-style | 18 | 1 page, 43 entries | title / Study notes | p8 (**1 line**) |

Checklist: cover (title band, metadata table, partial warning when relevant) PASS; table of contents (real field, correct entries) PASS; heading hierarchy PASS; page numbering PASS (cover unnumbered, "Page n of N"); headers and footers PASS; tables (repeating header row) PASS; nested lists (bullet with dash sub-point, numbered, lettered) PASS; code block with language label PASS; AI content boxes with labels and "based on [¶n]" PASS; review warnings (warnings page, "Review needed" label) PASS; source references PASS; Chapter 4 banner PASS; no completely empty pages.

- **The remaining one-line overflow page still occurs.** It is always the tail (1-3 lines) of a list - in these documents the last items of Chapter 2's "Concepts that appear together" list or the "Structure of the article" list - spilling onto a page before the forced chapter break. Reproducible, content-dependent, cosmetic (it wastes a page but loses nothing). It does not justify changing the chapter page breaks, which I left untouched.
- A TOC longer than about 45 entries (many key terms) runs to a second page; that is expected.
- Tooling note (not a product issue): Word's PDF export hung when launched from a nested PowerShell script and worked when launched inline; one stuck Word instance was cleaned up and none remained afterwards.

## 8. End-to-end workflow - PASS [real]

Real program, real Windows PowerShell, from the project root, two live public pages (one request each, 3 s apart, no login, no paywall bypass): `python src/main.py --output-dir <scratch>`, input `4`,`1` (Local backend), `1` (Substack URL), URL, detail, Enter (questions), `7`.
Note: the program appends to `noteforge.log` in its output directory, so these runs used a scratch directory (identical code path) to keep `output/noteforge.log` byte-identical, as required.

| Article | Detail | Extraction | Notes | Checks | Word |
|---|---|---|---|---|---|
| `on.substack.com/p/product-news-for-writers-audio-paywall...` | Basic | COMPLETE, 659 words, 32 blocks | 1,121 words, 15 questions | "OK: every quoted passage is from the article and the structure is valid" | opens in Word 16, 15 pages, 14 tables |
| `on.substack.com/p/what-you-get-when-you-start-a-substack` | Intermediate | COMPLETE, 832 words, 51 blocks | 1,377 words, 17 questions | same | opens in Word 16, 18 pages, 17 tables |

All six pipeline stages ran; completeness is shown; nothing was overwritten (unique file names); exit code 0; no Ollama was used.

## 9. Regression test results - PASS

| Run | Result |
|---|---|
| Full suite, Bash | **1,712 passed** (baseline 1,650 + 62 new) |
| Full suite, Windows PowerShell | **1,712 passed** (previously 1 failure in PowerShell) |
| `pip check` | no broken requirements |
| `python src/main.py` (root) | exit 0, "Goodbye.", BOM input accepted |
| `python -m src.main` (root) | exit 0, same |
| `python <absolute path>/src/main.py` from another working directory | exit 0, same |

Test-count change: +62, all in the new `tests/test_phase9_validation.py` (33 BOM/input + 10 heading pipeline + 9 keyword + 10 grounding). No existing test was modified, deleted or weakened. (Note: `python -m src.main` from a non-root directory is not supported by design: the module needs the project root on the path; the existing tests cover only the root.)

## 10. Output preservation - PASS

SHA-256 of all seven files in `output/` (five `Understanding_Retrieval-Augmented_Generation_Notes*.docx`, `noteforge.log`, `.gitkeep`) recorded before and after: **7 of 7 identical**. Nothing new was written to `output/`. All new documents, PDFs, page images and saved HTML are in the session scratch folder.

## 11. Known limitations

1. Ollama was not exercised in Phase 9; its behaviour rests on Phase 8/8.1 live runs (Basic, no questions, one article) and scripted tests. It is slow (minutes) and memory-heavy (about 3.7 GB).
2. AI-generated text (explanations, analogies, AI questions) is labelled and partly checked by vocabulary, never fact-checked; human review is required.
3. Not tested live: an article containing a real table or code block, articles over about 2,000 words, non-English articles; a real keyboard Ctrl+C (mocked only).
4. Word rendering was inspected for six generated documents, not for every real-world document; the Phase 8 real-world documents were not re-rendered.
5. Heuristics (D-1, D-9, D-6, D-10 recommendation verbs) are English-only and threshold-based.
6. The one-line / few-line overflow page before a chapter break (D-8 residual).
7. README still describes Phase 2; user documentation is Phase 10.
8. `noteforge.log` is written into the chosen output directory, so normal use grows the log in `output/`.

## 12. Remaining defects

| ID | Status | Note |
|---|---|---|
| D-8 residual | open, Low (cosmetic) | list-tail overflow page before a chapter break |
| D-6 residual | open, Low | all-caps shouts, capitalised names and numbered heading labels can still appear as terms on marketing-style articles |
| D-7 | PARTIALLY VERIFIED | live removal never observed; vocabulary check only |
| D-4 (AI part) | verified with scripted replies only | no live AI-question run |
| D-9, D-1 | closed | D-1 verified live; D-9 fixture only |
| PowerShell BOM | **closed in Phase 9** | |

No Medium or High defect is open.

## 13. V1.0 release-readiness recommendation

**READY FOR CONTROLLED V1.0 RELEASE.**

Reasons: the default Local path is fast, deterministic and traceable, completes the whole real workflow on live public articles under real Windows PowerShell, produces Word documents that open and render correctly, and has 1,712 passing tests with mutation-verified fixes. Provenance, labelling, paywall honesty, safe error recovery and preservation of existing outputs were all confirmed. The open items are cosmetic or limited to the optional AI path and documentation.

Conditions for the controlled release: (1) describe Ollama notes as experimental and requiring human review; (2) ship with the supervised-use caveat for non-English, very long and code/table-heavy articles; (3) Phase 10 must deliver the README/user documentation before any wider release; (4) keep OpenAI unreachable as designed.

## Source and test modifications in Phase 9

| File | Change | Why |
|---|---|---|
| `src/main.py` | added `strip_bom()` and `_BOM_PREFIXES`; `App.input_fn` wraps the injected input with it | PowerShell BOM failure |
| `src/content_processor.py` | added `_GENERIC_WORDS` and one rule in `_weak_keyword` | meaningless frequency keywords found on real articles |
| `tests/test_phase9_validation.py` | **new**, 62 tests | regression coverage for the above and for D-9, D-6, D-7 |
| `docs/PHASE_9_FINAL_VALIDATION_REPORT.md` | this report | |

No other source, test or document was modified.
