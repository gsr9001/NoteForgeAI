# NoteForge AI - Phase 8: Quality Assurance and Real-World Validation Report

Date: 2026-11-06 | Version under test: 0.1.0 | Environment: Windows 11, Python 3.14 venv, Microsoft Word 16.0, Ollama `qwen3.5:4b`

Classification key: PASS, PASS WITH LIMITATIONS, NEEDS IMPROVEMENT, FAIL, NOT TESTED.

## 1. Executive summary

| Area | Result |
|---|---|
| Regression suite | **PASS** - 1,455 passed, 1 expected xfail (1,444 baseline + 11 new); `pip check` clean |
| Word rendering (Microsoft Word 16.0) | **PASS WITH LIMITATIONS** - all 3 inspected documents open without errors; only cosmetic layout defects |
| Real-world Substack extraction | **PASS WITH LIMITATIONS** - 9 public URLs, 0 crashes, paywalls detected correctly; no live code/table article found; longest article 1,550 words |
| Content quality - Local backend | **PASS WITH LIMITATIONS** - faithful and traceable, but question variety and definition detection need improvement |
| Content quality - Ollama (AI) backend | **PASS WITH LIMITATIONS** - labelled correctly; some AI statements go beyond the source and are flagged for human review |
| Provenance and traceability | **PASS** |
| CLI and error recovery | **PASS** (real runs for 10 scenarios; mocked tests for the rest) |
| Performance and memory | **PASS WITH LIMITATIONS** - Ollama is slow and uses about 3.7 GB; one live run only |

**Release readiness: suitable for a controlled demonstration; not yet for unsupervised release** (see section 13).
No source code was modified. No paid API was used, `OPENAI_API_KEY` was never accessed, no model was downloaded, no paywall was bypassed, and no existing document was changed.

## 2. Baseline inspection

- Project: `src/` (article_extractor, content_processor, notes_model, notes_generator, word_generator, main, `backends/`), `tests/` (10 files), `config/`, `input/`, `output/`. `docs/` did not exist and was created for this report.
- `README.md` still says "Phase 2 (project setup)"; usage documentation is planned for Phase 10 (recorded as a limitation, not changed).
- `output/` before testing: five documents `Understanding_Retrieval-Augmented_Generation_Notes.docx` and `_2` to `_5`, plus `noteforge.log` (17 lines; contains extraction/generation timings only, no secrets, no keys).
- SHA-256 of every file in `output/` taken before and after all Phase 8 activity: **no document changed** (5 of 5 identical).
- System at start: 4.79 GB free RAM (threshold for any live Ollama run is 4 GB), no Word instance, Ollama server idle.

## 3. Word document visual inspection

Method: each document was opened **read-only** in Microsoft Word 16.0 by a separate watched process (timeout enforced), fields/TOC updated in memory, and exported to PDF in the scratchpad. PDF pages were then read visually. Originals were not modified (hash verified).

| Document | Opens | Compat mode | Pages (PDF) | Pages inspected visually |
|---|---|---|---|---|
| `..._Notes_4.docx` (Local, with questions) | Yes | 15 | 17 (16 numbered) | 12-17 (Q&A, references); structure/TOC/header/footer via Word object model |
| `..._Notes_5.docx` (Ollama, no questions) | Yes | 15 | 14 (13 numbered) | all 14 |
| Real partial article `Demystifying_Deep_Learning_Notes.docx` (scratchpad) | Yes | 15 | 21 (20 numbered) | 1, 2, 5, 6 |

Checklist results (observed in Word):

| Item | Result |
|---|---|
| Opens without errors | PASS |
| Cover page | PASS (title band, metadata table, partial warning when applicable) |
| Table of contents | PASS WITH LIMITATIONS - correct entries and page numbers; overflows onto a nearly empty page (D-8) |
| Heading hierarchy | PASS WITH LIMITATIONS - consistent levels; one over-long paragraph styled as a heading (D-9) |
| Page numbering | PASS - cover unnumbered, "Page n of N" restarts at 1 |
| Headers/footers | PASS - title + "Study notes" (red "PARTIAL CONTENT" for partial articles); footer shows backend |
| Tables and widths | PASS - article table with repeating header row fits the margins |
| Lists and numbering | PASS - bullets, numbered and lettered nested lists correct |
| Code formatting | PASS - monospace shaded box with language label |
| AI-generated labels | PASS - purple "AI-GENERATED" boxes, "WARNING: not from the article" banner in Chapter 4 |
| Original-content labels / legend | PASS - "How to Read These Notes" legend; origin colour coding |
| Source references `[¶n]` | PASS |
| Partial-content warnings | PASS - cover banner, red header on every page, banner at the top of each chapter, three stated reasons |
| Questions and answers | PASS - answer key and explanations present (Local doc) |
| Page breaks / blank pages | PASS WITH LIMITATIONS - see D-8 |

## 4. Real-world extraction results

Nine public URLs, one request each, 3 s apart, no login, cookies or bypass. Local backend, Intermediate detail, output to scratch (not `output/`). Three of the pages were fetched a second time (one extra request each) to save the HTML for diagnosing D-1 and the paywall reasons.

| Case | URL (host/path) | Title as extracted | Words | Status | Warnings | Processing / notes | Result |
|---|---|---|---|---|---|---|---|
| Short | on.substack.com/p/grow-5 | "Subscription platform for online creators - Substack" (see D-1) | 1,550 | COMPLETE | none | OK, 25 questions, 0.04 s | PASS WITH LIMITATIONS |
| Medium | on.substack.com/p/what-you-get-when-you-start-a-substack | What you get when you start a substack | 832 | COMPLETE | none | OK, 22 q | PASS |
| Lists | on.substack.com/p/new-on-substack-subscriber-perks | New on Substack: Subscriber Perks, ... | 837 | COMPLETE | none | OK, 23 q | PASS |
| Headings | on.substack.com/p/product-news-for-writers-audio-paywall | Product news for writers: ... | 659 | COMPLETE | none | OK, 19 q | PASS |
| Technical 1 | aimaker.substack.com/p/he-built-google-for-newsletters-... | He Built Google for Newsletters... | 1,279 | COMPLETE | last paragraph lacks final punctuation | OK, 25 q | PASS |
| Technical 2 | awesomeneuron.substack.com/p/how-rag-enhances-llms-a-step-by-step | How RAG Enhances LLMs: A Step-by-Step Exploration | 1,176 | COMPLETE | same warning | OK, 25 q | PASS |
| Lists/headings | codecompass00.substack.com/p/welcome-to-the-code-compass-start-here | Welcome to The Code Compass! | 907 | COMPLETE | none | OK, 25 q (15 from one block, D-4) | PASS WITH LIMITATIONS |
| Paywalled 1 | quycompute.substack.com/p/retrieval-augmented-generation | Retrieval-Augmented Generation | 56 | **PARTIAL** | 3 reasons (paywall component, metadata not free, preview only) | OK, 2 q | PASS |
| Paywalled 2 | onepagecode.substack.com/p/demystifying-deep-learning | Demystifying Deep Learning | 772 | **PARTIAL** | same 3 reasons | OK, 25 q | PASS |

- Extraction errors: **none**. Fetch plus extract took 0.6 to 1.1 s per article. `validate_notes` and `verify_provenance` returned no problems for all 9; the Word re-read check (`verify_document`) found no missing content in all 9.
- **NOT TESTED live:** an article with a real table or code block (none found among public candidates; both are covered by fixtures and were seen rendering correctly in Word), articles above about 1,600 words, and non-English articles.

## 5. Content quality assessment

Automated checks on all 9 real notes: MCQ answer keys valid (correct index points at the answer, no duplicate options) in all; exact-duplicate questions: 0; numbers in derived/AI text absent from the source: only word counts and dates (structural, not claims); fabricated figures: none found.

Manual comparison against the source (RAG fixture article, Local and Ollama documents):

| Chapter | Local backend | Ollama backend |
|---|---|---|
| 1 Overview | Faithful quotes with references; "objective not stated" fallback is honest | Same |
| 2 Concepts | Faithful, quoted, with structure map; "Concepts that appear together" list is repetitive and low-value | AI explanations are mostly accurate. **Needs human review:** "instead of complex vector databases" and "rather than being sent to external servers" are not in the source |
| 3 Keywords | Weak terms such as "AI", "retrieve", and a heading ("Common failure modes") appear as key terms (D-6); "Chunk size is a trade-off." is labelled a definition (D-5) | AI "explanation" of Chunk size just restates the sentence (no added value) |
| 4 Analogies | None generated (by design) | 12 analogies/applications, all labelled and under a warning. Plausible, but the generic "AI" analogy is not tied to the article. All AI content **needs human review**; correctness cannot be established from the source |
| 5 Takeaways | Conclusions quoted correctly; "Main findings" and "Practical lessons" empty although the source states a practical lesson (¶18) (D-10) | Same |
| 6 Revision | Accurate; truncated definition marked with an ellipsis | Same |
| 7 Questions | See D-3 and D-4 below | Not requested in the Ollama run (not tested live) |

Question-set findings (Local, RAG fixture): answer keys correct; but the same source sentence feeds up to four questions (short Q3, descriptive Q4, MCQ Q2 and Q3), "List the 4 items" contradicts the article's "three main benefits" because a nested item is flattened (D-3), and some MCQ distractors are headings rather than terms. On the real article "Code Compass", 15 of 25 questions come from one list block (D-4). On the "RAG" real article the answer to "what is RAG?" is a sentence that is not a definition (D-5).

## 6. Provenance and traceability assessment: **PASS**

- All 9 real-world notes: every `source_indices` value is within the article's block range (0 invalid references).
- Every non-heading article block is cited by at least one note item (headings appear as section titles); the only "original" items without a reference are the two article-metadata items, as designed.
- ORIGINAL / DERIVED / AI_GENERATED counts shown in the document match the notes (RAG sample: 29 / 9 / 19 for Ollama; 33 / 34 / 0 for Local; Ollama document contains no unlabelled AI text).
- Removed content: only one real-world article removed anything (1 item, reason "boilerplate"); nothing important was lost (headings, lists, tables and code verified present by the Word re-read check).
- Partial-extraction warnings remain visible in the cover, header, every chapter and the legend.

## 7. CLI and error-recovery results

| Scenario | Evidence | Result |
|---|---|---|
| Invalid URL / private (localhost) URL | real CLI session | PASS |
| Invalid source file (missing, binary) | real CLI | PASS |
| Empty article | real CLI | PASS |
| Network failure | mocked (test_main) | PASS |
| Paywall detection | real article + real CLI + tests | PASS |
| Partial article confirm (decline / accept) | real CLI | PASS |
| Missing Ollama server | real CLI (port with nothing listening): clear message, Local fallback only on confirmation (verified in a clean folder) | PASS |
| Missing Ollama model | real server, selection only (no generation): message names `ollama pull` | PASS |
| Ollama timeout, invalid model response | mocked | PASS (mock only) |
| Word generation failure (+JSON backup) | mocked | PASS (mock only) |
| Existing filename (never overwritten without confirmation) | real CLI + tests | PASS |
| Ctrl+C | mocked at 6 stages | PASS (mock only; real keyboard interrupt not exercised) |
| Switching Local and Ollama; returning to menu after each error | real CLI + tests | PASS |

Observation D-2: the documented `OLLAMA_MODEL` environment variable has no effect in the CLI, because the model chosen in the menu (default `qwen3.5:4b`) is always passed explicitly.

## 8. Performance and memory observations

| Measure | Value | Source |
|---|---|---|
| Local backend (9 real articles, 659-1,550 words) | 0.02 to 0.08 s for notes; 47-51 KB documents | measured |
| Fetch + extract | 0.6 to 1.1 s | measured |
| Ollama, 513-word article, Basic, no questions | 4m 20s total; 4 requests, 39 to 129 s each, about 6-7 tokens/s; prompts 312-892 tokens, outputs 162-704 tokens | existing `noteforge.log` evidence (no new live generation was run) |
| Free RAM, same run | start 4.68 GB, lowest 0.94 GB (about 3.7 GB used), above the 0.8 GB abort line | existing run evidence |
| Model unload | `ollama ps` shows no resident model after the run | verified now |
| Free RAM now / Word running | 5.72 GB / none | measured |
| Word open + PDF export | 2 to 3 s per document | measured |

Not measured: Local-backend peak memory, and any live Ollama generation on a real-world article. Per the Phase 8 rules, no unnecessary live Ollama generation and no Advanced-detail benchmark was run. Estimates for Intermediate/Advanced with questions (10 to 40 min in the CLI) remain unmeasured.

## 9. Regression test results

- Full suite: **1,455 passed, 1 xfailed, 0 failed** (1,444 baseline + 11 new in `tests/test_phase8_validation.py`).
- `pip check`: no broken requirements.
- No existing test was changed or weakened. The one xfail documents defect D-3 (strict=False).
- A guard in `tests/test_main.py` proves the test run does not modify the real `output/`.

## 10. Known limitations

1. Ollama generation is slow (minutes per article) and memory-heavy; only Basic/no-questions was validated live.
2. AI-generated text (analogies, explanations) cannot be verified from the source automatically; it is labelled and requires human review.
3. No live test of a Substack article containing real tables/code, long (over 2,000 words) or non-English articles.
4. Question generation reuses the same source sentences heavily on short articles (D-4).
5. Only one real-world partial document and two sample documents were inspected visually in Word; the other eight real-world documents were validated structurally (Word re-read check) but not rendered.
6. README is still at Phase 2; user documentation is planned for Phase 10.
7. OpenAI is intentionally unreachable; the paid path is untested by design.

## 11. Defects discovered (none fixed; no source changed)

| ID | Severity | Description | Where |
|---|---|---|---|
| D-1 | Low | Title taken from JSON-LD `headline`, which on some older pages is the generic site tagline ("Subscription platform for online creators - Substack"); `og:title` held "Substack Grow: Launching paid subscriptions". Wrong document title and file name. | `article_extractor.py:405-406`; on.substack.com/p/grow-5 |
| D-2 | Low | `OLLAMA_MODEL` environment variable ignored by the CLI. | `main.py:443, 561` |
| D-3 | Medium | Nested list items are flattened, so "List the 4 items" contradicts the article's "three main benefits". | question builder; RAG fixture (xfail test) |
| D-4 | Medium | Low question diversity: the same sentence supports up to 4 questions; 15 of 25 questions from one block on one real article. | Local question generator |
| D-5 | Medium | Definition heuristic accepts non-definitions ("Chunk size is a trade-off."; "RAG systems are rapidly gaining traction ... including:"), which then become answer keys. | content processor / questions |
| D-6 | Low | Weak key terms ("AI", "retrieve", a heading). | keyword extraction |
| D-7 | Medium | Ollama text adds claims not in the source (e.g., "instead of complex vector databases", "rather than being sent to external servers"); restating "explanations". | `qwen3.5:4b` output; needs human review |
| D-8 | Low | Layout: TOC overflows to a page with 3 lines (`_5` page 3); a one-line overflow page (real partial document page 5); mostly empty pages caused by chapter page breaks; AI box splits across pages. | `word_generator.py` |
| D-9 | Low | A 304-character paragraph classified and styled as a heading (partial document page 6). | extraction |
| D-10 | Low | Chapter 5 "Main findings" and "Practical lessons" can be empty while the source contains such statements. | content processor |

## 12. Prioritized recommendations

1. (Medium) Count and quote nested list items correctly and cap per-sentence question reuse (D-3, D-4).
2. (Medium) Require definition-like structure with a minimum content check before using a sentence as a definition or answer key (D-5).
3. (Medium) For Ollama output, add a stricter "no claims beyond the cited paragraphs" check or visible "review needed" marker (D-7).
4. (Low) Prefer `og:title` when JSON-LD `headline` equals the page `<title>`/site tagline (D-1); honour `OLLAMA_MODEL` when set (D-2).
5. (Low) Layout polish: keep TOC compact, avoid one-line pages (D-8, D-9).
6. (Low) Test with a live article containing code/tables and a long article; update the README (Phase 10).

## 13. Overall release-readiness assessment

**PASS WITH LIMITATIONS - ready for a controlled, supervised demonstration; NEEDS IMPROVEMENT before unsupervised release.**
Strengths: no crashes on 9 real articles, honest paywall handling, strong provenance and labelling, safe CLI error recovery, byte-identical preservation of existing outputs, clean regression suite. Main gaps are content-quality heuristics (D-3 to D-5, D-7) and untested live scenarios listed in section 10.

## Files

- Created: `docs/PHASE_8_QUALITY_ASSURANCE_REPORT.md`, `tests/test_phase8_validation.py`.
- Modified: none (no source or existing test files changed).
- Scratch evidence (not part of the project): `...\scratchpad\phase8\` (PDF exports, real-world JSON, scripts).
