# NoteForge AI - Phase 8.2: Remaining Defect Corrections

Date: 2026-11-07 | Scope: D-1, D-2, D-4 (AI part), D-6, D-7 (analogy part), D-8, D-9, D-10 from `docs/PHASE_8_QUALITY_ASSURANCE_REPORT.md` and `docs/PHASE_8_1_DEFECT_CORRECTION_REPORT.md`.

Classification per defect: FIXED / PARTIALLY FIXED / NOT FIXED / NOT VERIFIED.

| Defect | Result |
|---|---|
| D-1 Wrong title from JSON-LD `headline` | **FIXED** (heuristic; tested with the Phase 8 page shape, not re-run against the live page) |
| D-2 `OLLAMA_MODEL` ignored by the CLI | **FIXED** |
| D-4 AI-written question diversity | **FIXED** (verified with scripted model replies only; no live model run) |
| D-6 Weak key terms | **PARTIALLY FIXED** (undefined two-letter acronyms removed; heading-only and code-only terms are demoted, not removed; see section 7) |
| D-7 Unsupported claims in Ollama text | **PARTIALLY VERIFIED** (unchanged classification). Analogy "Review needed" marker added and tested with mocks. The removal path has still not been observed with the real model |
| D-8 Word layout | **PARTIALLY FIXED**: TOC overflow and split boxes fixed and confirmed in Microsoft Word; content-dependent sparse trailing pages remain |
| D-9 Long paragraph styled as a heading | **FIXED** (heuristic; tested with synthetic input, the original page was not re-fetched) |
| D-10 Chapter 5 lessons and findings missing | **FIXED** |
| D-3, D-5 | unchanged (FIXED in Phase 8.1) |

Test status: **1,650 passed, 0 failed** (baseline 1,575 + 75 new in `tests/test_phase8_2_corrections.py`). `pip check`: no broken requirements. No live Ollama generation, no network, no OpenAI, no paid API, no model download. `OPENAI_API_KEY` was never read. Local remains the default backend.

## 1. Baseline

- Before any change: 1,575 tests passed when run from the Bash tool. Existing `output/` hashes were recorded (7 files).
- One test, `tests/test_main.py::TestEntryPoint::test_works_from_windows_powershell`, **fails when pytest is launched from the PowerShell tool** and passes from Bash. Cause (verified): in that environment Windows PowerShell 5.1 pipes text to the program with a UTF-8 byte-order mark, so the first menu answer arrives as `"﻿2"` and is rejected. This is environmental and pre-existing, not caused by Phase 8.2, and I did not change it. All results here are from the Bash tool. The program does not strip a BOM from standard input; that is a small robustness gap worth a future fix (not done: out of scope).

## 2. Root causes and corrections (one defect at a time, in the approved order)

### D-2 - `OLLAMA_MODEL` (src/main.py)
Root cause: `Settings.ollama_model` always started as `qwen3.5:4b` and was always passed explicitly to the resolver, which overrode the environment value that `OllamaSettings.from_env` reads.
Correction: `App.__init__` starts `ollama_model` from `OLLAMA_MODEL` when set (read through the existing `_ollama_env`, so only documented `OLLAMA_*` keys are touched). A model chosen in the menu still wins. Unset or blank keeps `qwen3.5:4b`.

### D-1 - title (src/article_extractor.py)
Root cause: `extract_metadata` always trusted JSON-LD `headline`. On some older pages it is the site tagline while `og:title` holds the real title.
Correction: new `_headline_is_site_tagline`. The headline is distrusted only when `og:title` exists and differs and the headline equals the page `<title>`, equals the site name, or carries the site name as a suffix that `og:title` does not. Otherwise behaviour is unchanged (a normal headline still beats a different `og:title`; no `og:title` keeps the headline). A blank headline now falls back to `og:title`.
Side effect to know: for the rare page whose headline is "Title - SiteName" and `og:title` is the clean "Title", the title becomes the clean one.

### D-9 - long "headings" (src/article_extractor.py)
Root cause: both the Trafilatura XML path and the HTML fallback turned any `head`/`h1-h6` into a heading, with no length check.
Correction: new `_heading_block`. Text over 200 characters, or over 120 characters with at least two sentences, becomes a paragraph. The text is kept word for word, so nothing is lost; it just stops appearing in the table of contents. Explicit Markdown `#` headings are not touched.

### D-6 - weak key terms (src/content_processor.py)
Root cause: any undefined acronym, any 1-3 word heading and any code identifier counted as a strong term regardless of whether the prose used it.
Correction: new `_weak_keyword`. An undefined two-letter acronym ("AI", "UI") is dropped. A heading-only term the running text never uses, and a plain lower-case word seen only in code, keep a rank penalty (2.5) so they sit below terms the article actually uses. Anything with a definition or a defined alias is never weak.
Constraint discovered: existing tests deliberately require that heading-only terms such as "Chunk size" and code identifiers such as `retrieve` remain available as key terms, so demotion rather than removal was the only change that respects them. A bug found and fixed during this step: acronym keys are case-sensitive, so a defined alias like "ML" had to be compared in normalised form.

### D-10 - Chapter 5 (src/content_processor.py, src/notes_generator.py)
Root causes (confirmed on the RAG fixture): (1) no cue recognised an instruction ("Measure retrieval quality before you tune the prompt.") or "In practice"; (2) every sentence in a conclusion section was forced to the Conclusion kind, hiding facts and recommendations; (3) one global top-N cut could push a lone lesson out.
Corrections: extra recommendation cues (`in practice`, `lesson`, `tip`, `rule of thumb`, `start with`...); a closed-list imperative cue that is never applied to list items (procedure steps are not recommendations); a conclusion-section sentence keeps a more specific recommendation or fact kind; takeaway selection first keeps the best statement of each kind, then fills the rest by score, never exceeding the existing per-detail limits (6 / 10 / 16); when statements of a kind are cut, a visible notice says how many and why. Duplicate control uses the existing ledger: takeaways are reserved so Chapters 4 and 6 do not repeat them.
Observed effect: "Main findings" and "Practical lessons" are populated on the fixture. The same sentence now appears once as a Chapter 5 takeaway instead of also as a Chapter 4 "example" (Chapter 2 still reproduces the article section by section, as designed).

### D-4 - AI question diversity (src/backends/llm.py)
Root cause: the fact and block budget existed only in the Local builder; model-written questions had no source references and no budget.
Correction: `locate_sources` finds the best-matching article block(s) from the answer's content words (needs 3 or more content words and a 35% match; otherwise **no reference is claimed**, never a guess). `select_diverse_questions` applies, across all three kinds: no repeated fact (near-identical answers), at most `STRICT_BLOCK_CAP` (2) questions per block, relaxing to `RELAXED_BLOCK_CAP` (4) only to get closer to the requested number. Nothing is padded. Shortfalls are reported in Chapter 7 ("N short questions from the model were left out on purpose ... No question was added only to reach the requested number."); the existing "Only X of N ... passed validation" message is kept. Question wording duplicates remain the generator's existing near-duplicate rule, with its own warning. Source references now reach Word (shown as `[¶n]`, 1-based). AI labels and provenance are unchanged. The Local builder is untouched.

### D-7 - analogy review warnings (src/backends/base.py, llm.py, notes_generator.py)
Correction: `ExampleNote.review_needed` (optional). An accepted analogy/application is marked when the existing `assess_support` finds a contrast cue ("rather than", "instead of", "unlike"...) followed by words the article never uses. The item is kept, unchanged and still labelled AI-generated; its label ends with "Review needed" and a "Review needed: ..." warning is added to the notes' warnings (shown in Word).
**Filter not weakened:** acceptance rules are untouched (numbers, copies of the article, missing comparison are still rejected - tested), and the explanation-removal policy is unchanged.
**No claim of live verification:** the live removal path has still never been observed with the real model, and no additional live generation was run. D-7 stays PARTIALLY VERIFIED.

### D-8 - Word layout (src/word_generator.py)
Root cause found by inspecting the document in Word: python-docx marks added styles as custom, and Word reserves "TOC 1-3" for its own table-of-contents styles. Word therefore renamed ours ("TOC 21" ...) and, when it rebuilt the table of contents on opening, used its own built-in spacing. Our TOC formatting had never applied.
Correction: the custom flag is removed from the three TOC styles, TOC spacing is compact (single line spacing, 10-10.5 pt, 1-3 pt gaps), and boxed item styles (AI, definition, takeaway, example, warning, info, quote, answer, explanation) are kept on one page. Chapters still start on a new page (design kept).
Word 16 check on scratch copies (never in `output/`; same notes with old versus new styling; read-only; fields updated in memory):

| Document | Pages before | Pages after | TOC pages before | TOC pages after |
|---|---|---|---|---|
| Local, 41 TOC entries | 16 | 15 | 2 (8 entries on page 2) | **1** |
| AI-style, 43 TOC entries | 18 | 17 | 2 (10 entries on page 2) | **1** |

## 3. Files changed

Source (7): `src/main.py`, `src/article_extractor.py`, `src/content_processor.py`, `src/notes_generator.py`, `src/backends/base.py`, `src/backends/llm.py`, `src/word_generator.py`.
Tests: created `tests/test_phase8_2_corrections.py`; modified 13 existing tests (section 5). Created: this report. No existing document, log, or config was changed. Public interfaces: additive only (`ExampleNote.review_needed`, new helper functions).

## 4. Tests added (75 in `tests/test_phase8_2_corrections.py`)

D-2 (6), D-1 (9), D-9 (10), D-6 (8), D-10 (12), D-4 (14), D-7 (9), D-8 (7) = 75; each test class names its defect. They cover positive and negative cases, edge cases (blank values, no `og:title`, tiny answers, no located source, crowded conclusions, all detail levels), determinism, provenance/labels, source references, the Word output, and "nothing weakened" checks.
Mutation check: each fix was deliberately disabled in turn (files restored afterwards). First round: 16 breakages, 15 caught; the survivor (D-9's single-long-sentence rule) exposed a test gap. A test was added, the D-9 mutation was split into its two conditions, and in the final round all 17 breakages were caught.

## 5. Existing tests modified (13; none deleted; listed for your review)

Why they changed: recognising the practical lessons (D-10) gives the Local question builder one more distinct fact, so the RAG fixture now yields 21 questions (10 short, 5 descriptive, **6** MCQ) instead of 20 (5 MCQ), and the shortfall notice reads "Only 6 of 10". Two Word tests assumed a sentence has one style only, but it now also appears once as a Chapter 5 takeaway and as an answer.

| File | Test | Change |
|---|---|---|
| test_backends.py | `test_works_offline_and_without_any_keys` | MCQ count 5 -> 6 |
| test_llm_quality.py | `test_local_template_questions_are_never_filtered_by_this_rule` | 20 -> 21 questions |
| test_main.py | `test_default_requests_every_question_and_reports_any_shortfall` | 20 -> 21; "Only 5 of 10" -> "Only 6 of 10" |
| test_notes_generator.py | `test_chapter7_requested_question_counts` | MCQ 5 -> 6; notice text |
| test_notes_generator.py | `test_ollama_unavailable_still_produces_complete_local_notes` | 20 -> 21 |
| test_notes_generator.py | `test_to_dict_is_json_serialisable_and_complete` | 20 -> 21 |
| test_ollama_integration.py | `test_server_dies_mid_run_so_the_rest_falls_back_without_retrying` | 20 -> 21 |
| test_word_generator.py | `test_all_questions_are_numbered_per_group` | 20 -> 21; MCQ numbering to 6 |
| test_word_generator.py | `test_mcq_options_are_lettered_a_to_d` | 5 -> 6 groups |
| test_word_generator.py | `test_each_mcq_states_its_correct_answer_and_explanation` | 5 -> 6 (two assertions) |
| test_word_generator.py | `test_takeaways_use_the_takeaway_style_and_examples_the_example_style` | the "In practice" sentence is now asserted as `NF Takeaway` (it moved from Chapter 4 to Chapter 5 and is not repeated) |
| test_word_generator.py | `test_quotes_are_italic_and_indented` | first appearance must be `NF Quote`, exactly one `NF Quote`; the same sentence is also a takeaway and an answer |
| test_word_generator.py | `test_expected_fragments_cover_all_content_kinds` | size floor `> 300` -> `> 290` (D-6: the undefined "AI" keyword no longer produces fragments; actual 299) |

Note: the "example style" intent of the fourth Word test is no longer exercised by the RAG fixture, because its one example became a takeaway. `look_for` still maps Chapter 4 to `NF Example`, and Word styling is covered by the D-8 tests.

## 6. Before and after

| Case | Before | After |
|---|---|---|
| Page with tagline `headline` | "Subscription platform for online creators - Substack" | `og:title` ("Substack Grow: Launching paid subscriptions") |
| `OLLAMA_MODEL=llama3:8b` | ignored | CLI starts with llama3:8b; menu still overrides |
| 304-character "heading" | styled Heading, in the TOC | paragraph, text identical |
| RAG fixture keywords | "AI" listed; "Common failure modes" ranked 5th | "AI" gone; "Common failure modes" last |
| RAG fixture, Chapter 5 | Findings and Lessons empty; the 2020 fact filed as a conclusion | Findings: the 2020 fact. Lessons: "Measure retrieval quality..." and "In practice, teams add a re-ranking step..." |
| AI questions | no source references, no budget | references `[¶n]`; at most 2 (relaxed 4) per block; honest shortfall note |
| Analogy with "rather than <unknown>" | kept silently | kept, "Review needed" label and warning |
| Word TOC (Word 16) | spilled 8-10 lines onto a second page | one page |

## 7. Remaining limitations

1. **D-6**: heading-only and code-only terms still exist; they are only ranked lower, because existing tests require them as key terms. A function name that the prose also mentions (such as `retrieve` in the fixture) stays ranked normally. English-only heuristics.
2. **D-10**: the imperative verb list is English and closed; a tutorial written entirely as instructions can yield many recommendations (still capped by the Chapter 5 limits, and the cut is now reported). Chapter 5 and the question builder share statements, which is why question counts moved.
3. **D-9 / D-1**: thresholds are heuristics (200 / 120 characters; tagline detection needs an `og:title`). Neither was re-run against the original live pages; no network was used in this phase.
4. **D-4**: verified with scripted model replies only. Source location is a vocabulary match and says where to check, not that the answer is right. Questions whose answer has fewer than 3 content words get no reference and are not limited by the block cap.
5. **D-7**: not a fact check; the removal path was not observed live; analogies remain outside the support filter by design and are only marked, not removed. No further live Ollama run was made.
6. **D-8**: chapters still start on new pages, so a chapter's last line can spill onto an otherwise empty page (seen once in the AI-style scratch document: one line of the low-value "Concepts that appear together" list). This is content-dependent. Only the RAG-fixture scratch documents were rendered in Word; the nine real-world documents were not re-rendered.
7. **Environment**: the PowerShell-launched BOM failure in section 1, and the program's lack of BOM stripping on standard input.
8. README is still at the Phase 2 wording (planned for Phase 10).

## 8. Output preservation and safety

- SHA-256 of `output/` recorded before and compared after all work: **7 of 7 identical** (the five `Understanding_Retrieval-Augmented_Generation_Notes*.docx` documents, `noteforge.log`, `.gitkeep`). `output/` holds exactly those files.
- Word was used only on scratch copies in the session scratchpad, opened read-only, and closed; no Word process was left running.
- No live Ollama generation, no model download, no network call, no paid API; `OPENAI_API_KEY` not accessed (the only environment variable the new code reads is `OLLAMA_MODEL`, through the existing allow-list).
- Stopped after Phase 8.2. Phase 9 and Phase 10 have not been started.

## 9. Overall assessment

All eight remaining defects were addressed at their root causes with small, targeted changes, each with focused tests, a full regression run after each correction, and a final full run (1,650 passed). The most valuable finding of this phase was the D-8 root cause: the Word table-of-contents styling had never taken effect. The content-quality defects (D-6, D-7) are improved but not closed, and are stated as such. NoteForge AI remains suitable for controlled demonstrations; before an unsupervised V1.0, the open items are the remaining D-6 and D-7 limits, a live re-check of D-1/D-9 on real pages, and the README (Phase 10).
