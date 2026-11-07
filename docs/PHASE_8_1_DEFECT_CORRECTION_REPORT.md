# NoteForge AI - Phase 8.1: Educational Quality Defect Corrections

Date: 2026-11-06 | Scope: D-3, D-4, D-5, D-7 from `docs/PHASE_8_QUALITY_ASSURANCE_REPORT.md` (nothing else was fixed)

Result classification per defect: FIXED / PARTIALLY FIXED / NOT FIXED / NOT VERIFIED.

| Defect | Result |
|---|---|
| D-3 Nested list items flattened | **FIXED** |
| D-4 Question diversity | **PARTIALLY FIXED** (Local backend fixed; AI-generated questions get no diversity control) |
| D-5 Non-definitions accepted as definitions / answer keys | **FIXED** (heuristic, English patterns; see limitations) |
| D-7 Unsupported claims from Ollama | **PARTIALLY FIXED**: deterministic checks implemented and verified with mocked responses and replay of stored real output; **live run (2026-11-07): PARTIALLY VERIFIED** - supported/partial/Word paths confirmed with the real model, but no unsupported sentence occurred, so live removal was not observed (see the last section) |

Test status: **1,575 passed, 0 failed, 0 xfail** (baseline 1,455 passed + 1 xfail = 1,456 tests; +119 new tests). `pip check`: clean. No live Ollama generation, no network, no OpenAI, no paid API. All five existing output documents and `noteforge.log` are byte-identical (SHA-256).

## 1. Defect diagnosis and root cause

| ID | Where the defect actually was | Root cause |
|---|---|---|
| D-3 | `LocalQuestionBuilder._lists` (`src/backends/local_questions.py`); JSON form `Notes.to_dict` (`src/notes_model.py`) | Extraction, the notes model and Word already carried per-item depth. The question builder counted *every* item (main and nested) as "the items", so "List the 4 items" contradicted the article's "three main benefits", and answers printed sub-points as peers. `to_dict` (used for the JSON backup) silently dropped depth. The list kind (numbered/bullet) was kept only for the top-level list. |
| D-4 | `LocalQuestionBuilder` rotation | Generators were round-robined but each re-used the same sentence/block; only identical *question text* was de-duplicated, never the *fact* (definition, sentence, table cell, list). One block could feed up to 15 of 25 questions. |
| D-5 | `content_processor._definitions_from_sentence`, `_colon_definition`, and `local_questions` | `"X is a Y"` was trusted whatever `Y` was ("Chunk size is a trade-off."); an acronym introduction in any sentence made the whole sentence an answer key ("RAG systems are rapidly gaining traction ... including:"); glossary-style `Label: text` accepted calls to action and questions ("Read more: Can I customize ...?"). |
| D-7 | `src/backends/llm.py` | The only grounding check was for numbers, URLs, `et al.`-style citations and equations. Plain-language claims, comparisons ("instead of complex vector databases") and attributions passed unchecked. |

## 2. Files modified

Source (7): `src/article_extractor.py`, `src/content_processor.py`, `src/notes_model.py`, `src/notes_generator.py`, `src/word_generator.py`, `src/backends/local_questions.py`, `src/backends/llm.py`.
Tests modified (7, see section 5): `tests/test_backends.py`, `test_notes_generator.py`, `test_llm_quality.py`, `test_ollama_integration.py`, `test_word_generator.py`, `test_main.py`, `test_phase8_validation.py`.
Created: `tests/test_phase8_1_corrections.py` (119 tests), this report.
Public interfaces: unchanged except additive optional fields (`ListItem.ordered`, `NoteItem.ordered`, extra `depths`/`ordered` keys in the JSON form only when non-trivial), a new public function `is_definitional`, and an optional `ordered` argument on `WordGenerator.list_paragraphs`.

## 3. Corrections implemented

### D-3 - nested lists
- `ListItem.ordered` (optional, default `None`) records a sub-list whose kind differs from the top-level list (numbered inside bullets or the reverse), set by all three extraction paths (trafilatura XML, HTML fallback, markdown text). Ordinary lists add nothing, so existing data and equality are unchanged.
- Question builder: only outermost items are counted ("List the **3** items ..., including their sub-points"); answers show each sub-point indented under its parent with its own marker; sub-points are never offered as main items in choice questions or distractor pools.
- `NoteItem.ordered` carries per-item kind to Word; Word picks bullet vs numbered per item, restarting numbered sub-runs under each parent.
- `Notes.to_dict()` now keeps `depths` (and `ordered`) so the JSON backup no longer flattens lists.

### D-4 - question diversity (Local backend)
- Each question carries an internal *fact* identity (definition, sentence, table cell, list, code, section...). A fact is never asked twice, whatever the question type.
- Per-block budget: at most 2 questions per source block (strict pass for all three kinds); a second pass raises it to 4 only to reach the requested number. The extra pass still never repeats a fact.
- No padding: when material runs out the existing shortfall note is kept and now adds "Questions that would repeat a fact already asked, or rely again on the same passage, were left out on purpose."
- Latent bug fixed on the way: an MCQ distractor could appear inside the stem in inflected form ("answer" vs "answers").

### D-5 - definitions
- `is_a` sentences: forecasts/opinions and list lead-ins (ending ":" or "?") are rejected; predicates that only evaluate or position the term ("is a trade-off", "is a key part", "is the first step", "is an example of"), fewer than 4 words, hedged ("may", "often", "such as"...) or over-long sentences become **LOW** (kept as a key-term cue, shown as "Possible definition", excluded from answer keys and revision definitions).
- Glossary lines `Term: text` are rejected when the term is a label ("Example", "Note"...) or the text is an instruction or a question.
- New `is_definitional(d)`: only explicit patterns (`shortened_to`, `refers_to`, `is_a`, `called`, `term_colon`, `table_row`) at non-LOW confidence count. Acronym introductions and appositives are kept for "What does X stand for?" and key terms but are never definitions/answer keys. Used by the question builder, Chapter 3 labels and Chapter 6.
- Accepted definitions keep exact source wording, block index and provenance (unchanged).

### D-7 - Ollama / OpenAI shared validation (`LLMBackend`)
- Stronger rules: only the supplied text, no background knowledge, no number words/studies/experts/versions, no outside comparisons, "leave it out rather than guess"; keyword and section prompts require every sentence to be supported.
- `check_grounding` extended: number words of ten and above and multiplicative words ("twice", "dozens"; one to nine are ignored like single digits), fabricated attributions ("studies show", "according to a survey") unless the article uses that noun, version/specification tokens ("GPT-4"), long quotations not in the article.
- `assess_support`: sentence-by-sentence vocabulary check against the article (hyphenated words split, light stemming, meta words ignored). A comparison cue ("instead of", "rather than", "unlike", "... than") followed by words the article never uses is always unsupported; otherwise supported >= 60% of key words, partial 40-60%, unsupported < 40%.
- Policy: unsupported sentences are **removed** (logged; the removed claim is not repeated in the document's warnings); partial sentences are **kept and reported** as "Review needed ..." in the notes' warnings; an explanation with nothing left is omitted. AI question answers/explanations are checked the same way (unsupported rejects the question, partial is flagged). AI labels are unchanged. No second model or external service is used.

## 4. Tests added (`tests/test_phase8_1_corrections.py`, 119 tests)

- **D-3 (32):** extraction depth for HTML, markdown and the real trafilatura path; mixed kinds; counting for single/two/three levels; outline for ordered, unordered, mixed, technical terms; "three main benefits with four details" never reported as 4 or 7; list between paragraphs; JSON keeps depth; Word indentation at levels 0/1/2 and numbering format (decimal/bullet/letter) per item.
- **D-4 (16):** per-block cap on three fixtures; no repeated fact; coverage of several question families; honest shortfall note and provenance; many sections, very few sections, limited content, single block, repeated concept, long article; determinism; distractors never inside the stem.
- **D-5 (31):** positive ("An embedding is a numerical representation of text in a vector space.") and negative cases (ordinary statements, evaluative, example, forecast, hedged, lead-in, labels, instructions, questions, acronym introductions); answer keys, "stands for" questions, Chapter 3/6 labelling.
- **D-7 (40):** supported, partially supported, unsupported technical claim, invented number, number word, unsupported comparison, fabricated citation/study/specification, quotation, mixed supported + unsupported; keyword, section and question paths; removed text absent from notes and the Word file; prompts; shared by Ollama/OpenAI.
- Mutation check: 20 deliberate breakages of the fixes; 19 caught at once, the survivor exposed a test gap (fixed with a new test, now caught).

## 5. Existing tests changed (listed for your review)

15 existing tests were touched (11 count expectations, 3 scripted replies, 1 `xfail` marker). None was deleted or weakened; each change follows from the intended behaviour:
- **Count expectations (D-4)**: the RAG fixture now yields 20 questions (10 short, 5 descriptive, **5** MCQ) instead of 25 (10 MCQ), because 5 MCQs could only be built by repeating facts. Updated in `test_backends` (1), `test_notes_generator` (3, plus a new assertion that the shortfall notice exists), `test_llm_quality` (1), `test_ollama_integration` (1), `test_word_generator` (4: numbering, options, answers, explanations), `test_main` (1, renamed and now also asserts the shortfall notice).
- **Scripted model text (D-7)**: `test_long_input_is_batched...` and `test_long_ai_notes` used generic filler explanations over random-vocabulary articles; they now return text drawn from the section. `test_the_real_failure_not_discussed_is_dropped_silently` still requires no warning for the dropped filler but allows the new "Review needed" notice.
- `test_phase8_validation.py`: the D-3 `xfail` became a normal passing test.

## 6. Before and after

| Case | Before (Phase 8) | After |
|---|---|---|
| RAG fixture list question | "List the 4 items given under 'Why retrieval matters'" (article says three benefits) | "List the 3 items ..., including their sub-points", answer shows the nested item under its parent |
| RAG fixture "what is Chunk size?" | answer "Chunk size is a trade-off." | no such question; term kept as a flagged key term |
| Real article, acronym sentence | "what is Retrieval Augmented Generation?" answered with "... systems are rapidly gaining traction ..., including:" | not a definition; "What does RAG stand for?" kept |
| Real pages, `Label: question?` lines | "what is Knowing your readers?" etc. | rejected |
| RAG fixture, questions | 25; most-used block 8 questions on real page "grow 5" | 20 (shortfall explained); most-used block 4 (fixture) / 2 (grow 5) |
| Real page "Demystifying Deep Learning" (partial) | 25 questions, up to 11 from one block | 18 questions, at most 4 per block |
| Ollama text stored in Phase 8 document `_5` (replay, no live run) | "... plain lists instead of complex vector databases" and "... rather than being sent to external servers" shown as AI explanation | both sentences flagged unsupported and would be removed; 20 of 34 sentences supported, 5 partial (review), 9 unsupported (analogy/application items, which this filter does not apply to) |
| JSON backup of a nested list | flat | depth preserved |

Word check in Microsoft Word 16.0 (scratch document, not in `output/`): nested bullets with dash sub-points, `1. / a. / i.` three levels, numbered parents with bullet children, bullet parents with lettered children restarting under each parent.

## 7. Remaining limitations

1. D-7 is a vocabulary check, not a fact check: a wrong claim phrased in the article's own words passes; legitimate paraphrases may be removed or flagged for review (some noise is expected). Chapter 4 analogies/applications are not filtered (they are labelled as not from the article).
2. D-7 live evidence is one supervised run on one article (the removal path was not triggered by the model in that run); other real-output evidence is a replay of text stored in the Phase 8 document.
3. D-4 does not touch AI-written questions (only the support check applies), and the Local builder cannot invent application/comparison questions, so those types appear only when the article's structure offers them.
4. Question counts are lower on articles with little distinct material (by design). Descriptive questions can be 0 on articles without conclusions, recommendations or multi-sentence sections.
5. D-5 is pattern-based English only; some medium-confidence `is a` sentences that are not definitions may remain. Terms introduced only by an acronym now show "Possible definition" in Chapter 3.
6. Word renders at most three list levels (existing behaviour); mixed list kinds are recorded only where a sub-list differs from its top-level list.
7. Not addressed by instruction: D-1, D-2, D-6, D-8 to D-10.

## 8. Output preservation

SHA-256 recorded before and compared after all work; `Understanding_Retrieval-Augmented_Generation_Notes.docx` and `_2` to `_5`, and `noteforge.log`, are identical. New sample files were written only to temporary or scratch folders. No live Ollama generation occurred; Word was opened once on a scratch document and closed.

## 9. Overall assessment

The four defects are corrected at their root causes with the smallest changes that hold, all 1,456 earlier tests still pass (after the 15 documented test updates), and the new behaviour is covered by 119 tests that catch deliberate regressions. The application remains suitable for controlled demonstrations; D-7 should be confirmed once with a supervised live Ollama run before relying on it.

## Supervised Live Ollama Validation - D-7

Run on 2026-11-07 with your explicit approval for **one** generation. No source code was changed; instrumentation (recording each raw model reply) lived only in a scratch script. Output went to the scratchpad, not to `output/`.

**Classification: D-7 PARTIALLY VERIFIED.** The live run confirmed the safeguard works end to end with the real model on the *supported* and *partially supported* paths and in the Word document, but this run did **not** reproduce an unsupported claim, so the *removal* path was not exercised live (it remains verified by mocked tests and by replay of the stored Phase 8 output).

### Setup
- Article: `tests/fixtures/substack_free.html` ("Understanding Retrieval-Augmented Generation", 513 words, 21 blocks), extracted offline.
- Backend/model: Ollama `qwen3.5:4b` (already installed; nothing downloaded). Settings: num_ctx 8192, think off, temperature 0.2, keep_alive 10m, max output 1500 tokens, loopback only. Detail Basic, questions disabled.
- Pre-run checks passed: server running, model installed, no resident model/generation, 5.43 GB free (minimum 2 GB), D-7 code present, output hashes equal to the Phase 8.1 baseline. Safeguards not lowered (abort at 0.8 GB free).
- OpenAI: not used, `OPENAI_API_KEY` not read, no paid service.

### Run
- **4 model requests, strictly sequential, one generation session:** keyword explanations 37.6 s, section explanations 92.1 s, analogies 41.6 s and 29.9 s; total **201 s (3.4 min)**; 2,492 prompt tokens, 1,002 output tokens (about 6.7 tokens/s).
- Memory: start 5.43 GB free, **lowest 1.68 GB** (about 3.7 GB used; 0.9 GB above the abort line), 5.95 GB after unload, 6.03 GB at the end of validation.
- Model unload: `ollama ps` shows no resident model after the run. No Word or Ollama process left running.

### D-7 results on the live output
Sentences were classified offline with the same `assess_support` and `check_grounding` the safeguard uses, then compared with what reached the notes. This is a vocabulary check; I also read each explanation against the article by hand. Neither proves factual correctness.

| Group | Sentences | Supported | Partially supported | Unsupported | Removed by D-7 |
|---|---|---|---|---|---|
| Keyword and section explanations (in the filter's scope) | 17 | 16 | 1 | 0 | 0 |
| Analogies/applications (labelled "not from the article"; outside the filter by design) | 8 | 0 | 2 | 6 | 0 (kept, labelled, under a warning banner) |

- The one partial sentence ("The code is deliberately simple and not intended for production use.") was **kept and flagged**: "Review needed ..." appears in the notes' warnings and in the Word document ("Warnings from notes generation"). Manual check: the article says "deliberately simple and is not meant for production use", so this was a conservative flag on a paraphrase, not a real problem.
- One keyword sentence ("It represents a trade-off.") was dropped, but by the existing short-text rule, not by D-7.
- Specific checks: **no** invented figures (every number or count, "three", "four", 100/300 words, 2020, is in the article), **no** version numbers, **no** citations, **no** "studies show" statements, **no** claims about external servers or systems, **no** unsupported comparison inside an explanation, in this run. The two Phase 8 examples ("instead of complex vector databases", "rather than being sent to external servers") did **not** recur (the model is not deterministic), so they were not caught live; they remain caught by replay of the Phase 8 text and by the mocked tests.
- Manual read of the 16 kept explanation sentences: all consistent with the source (for example "which also facilitates checking for mistakes" matches "makes it easier to check for mistakes").
- Gap seen live: one analogy contains a comparison cue ("Think of ... as a librarian ... rather than ..."). Analogies are exempt from the support filter by design, so it is not checked; it is labelled AI-generated under the red "not from the article" banner.

### Word document validation
- Opened in Microsoft Word 16.0 without errors (compatibility mode 15, 14 PDF pages, 13 numbered); not modified.
- AI-generated labels present (14 AI-styled paragraphs, 24 "AI-generated" labels); original article text remains distinguishable (blue-bar quotes versus purple AI boxes); the legend explains both.
- Review warning visible on page 2; source references `[¶n]` all valid (35 references, highest 21 of 21 blocks); `validate_notes`, `verify_provenance` and the Word re-read check found no problems; Chapter 3 and 4 labelling unchanged.

### Regression and preservation (after the run)
- Focused D-7 and LLM tests: 305 passed. **Full suite: 1,575 passed, 0 failed, 0 xfail** (unchanged baseline). `pip check`: no broken requirements.
- SHA-256 of the five existing documents and `noteforge.log`: identical to before (6 of 6). `output/` still holds exactly those six files.

### What this does and does not show
Shown: with the real model the safeguard accepts faithful explanations, flags a partly matched sentence for review, keeps labels and review warnings in Word, and runs within memory limits. Not shown: removal of an unsupported sentence by the real model (no such sentence was produced in this one run), behaviour on other articles or detail levels, and any guarantee of factual correctness. A further live run would be needed to observe a live removal; none was run.
