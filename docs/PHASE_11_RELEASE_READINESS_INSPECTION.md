# NoteForge AI - Phase 11: Release Readiness Inspection

Date: 2026-11-07 | Type: **inspection and reporting only.** The only file created is this report. No source, test, configuration, documentation, output or log file was modified; Git was not initialised; nothing was staged, committed, tagged, published or distributed; no network request, download, dependency installation or paid call was made; no secret (including `OPENAI_API_KEY`) was read, printed or accessed.

Evidence base: Phase 10 report, release checklist, README, the files listed in section 10, read-only searches of `src/`, `tests/`, `docs/`, the installed package metadata (offline), and one run of the existing test suite.

## 1. Executive summary

NoteForge AI is technically in good shape for a first release: 1,712 tests pass (re-run today, unchanged), `pip check` is clean, the default Local backend is free and offline, and no real secret or personal identifier exists in the files a first commit would contain. What is **not** ready is the surrounding release hygiene:

| Area | Finding |
|---|---|
| Configuration | The program uses only the 9 `OLLAMA_*` environment variables, `--output-dir` and menu choices. **`.env` files are never loaded**; `.env.example` and `config/settings.example.json` contain **14 names/keys the program ignores** (5 in `.env.example`, 9 in the JSON file) and give a misleading impression (including about OpenAI). Smallest safe fix needs **no code or test change** |
| OpenAI / paid usage | A paid backend exists in the library but is **not reachable from the CLI**; nothing in the configuration can trigger a paid call. Residual risk is only a developer calling the library directly, and even then four independent gates apply |
| Versioning | `0.1.0` in **1 source line + 1 test assertion**; 12 lines across 5 current documents; the User-Agent string `NoteForgeAI/0.1` is a separate decision. Generated Word documents embed no version |
| Git | **Not a repository.** `.gitignore` is good for the essentials but has gaps. A first commit would contain 63 files, about 1.2 MB, 3 small `.docx` examples, no secrets |
| Licensing / ownership | **No licence, no terms, no ownership statement exist.** This is the main blocker to any distribution |
| Recommendation | A **staged private pilot** first, public/commercial decisions later (section 5) |

**Release blockers (details in section 7):** B1 no licence/ownership decision; B2 misleading example configuration; B3 version still 0.1.0 and no version control history (blocks a *tagged V1.0*, not a pilot).

## 2. Configuration audit

### 2.1 Settings that are actually used

| Setting | Where | Effect | Default |
|---|---|---|---|
| `OLLAMA_BASE_URL` (fallback `OLLAMA_HOST`) | `backends/ollama.py` `OllamaSettings.from_env`; read by `main.py` via an allow-list | Ollama server address; loopback only | `http://localhost:11434` |
| `OLLAMA_MODEL` | same (CLI uses it as the starting model since Phase 8.2) | Model name | `qwen3.5:4b` (CLI) |
| `OLLAMA_NUM_CTX` | same | context window, 2048-32768 | 8192 |
| `OLLAMA_TEMPERATURE` | same | 0-0.3 | 0.2 |
| `OLLAMA_THINK` | same | true / false / default | false |
| `OLLAMA_KEEP_ALIVE` | same | model residency | 10m |
| `OLLAMA_MAX_OUTPUT_TOKENS` | same | 64-4096, under half of context | 1500 |
| `OLLAMA_TIMEOUT` | same | seconds per reply | 900 |
| `--output-dir`, `--version` | `main.py` argparse | output folder; version text | project `output/` |
| Menu choices | `main.py` `Settings` | backend (Local/Ollama), Ollama model, detail level, questions | Local, Intermediate, yes |

Hard-coded (not configurable): fetch timeout 20 s, 5 MB response limit, 5 redirects, User-Agent `NoteForgeAI/0.1 (personal study-notes tool)`, 10 MB input-file limit, question counts 10/5/10.

### 2.2 Unused or misleading settings

**`.env.example`** (header says "Copy to `.env` and edit"):

| Entry | Status |
|---|---|
| `NOTEFORGE_BACKEND` (comment: "local \| ollama \| openai") | **Unused** (0 references in `src/`). Misleading: suggests the backend can be set outside the menu and that `openai` is selectable |
| `NOTEFORGE_LANGUAGE`, `NOTEFORGE_DETAIL_LEVEL` | **Unused** (0 references) |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | **Not read by the CLI.** Read only by the library function `resolve_backend("openai")` when called programmatically. The comment "NoteForge AI asks for confirmation before every API call" is true of the library, but no CLI path exists |
| The 9 `OLLAMA_*` lines | Names, values and ranges are **accurate**, but only take effect as real environment variables (`$env:NAME = ...`). Putting them in a `.env` file does nothing |

**`config/settings.example.json`:** all 9 keys (`backend`, `language`, `detail_level`, `include_questions`, `request_timeout_seconds`, `user_agent`, `mcq_count`, `short_question_count`, `descriptive_question_count`) are **unused**: the file is not opened anywhere in `src/` or `tests/`. `user_agent` merely duplicates a constant; the counts duplicate defaults in `NotesConfig`.

**`requirements.txt`:** `python-dotenv` is installed but **never imported by `src/`**. The only reference in the project is `tests/test_setup.py` (`DEPENDENCIES` list, test `test_dependency_importable[dotenv]`), so removing it from the requirements would make that test fail on a fresh install. (Not a risk today; a candidate for later clean-up together with that test.)

### 2.3 The five questions

1. **Settings actually used:** section 2.1.
2. **Unused or misleading:** section 2.2.
3. **Are `.env` files loaded automatically?** **No.** There is no `load_dotenv`/`dotenv` call anywhere in `src/`; the CLI reads `os.environ` for the nine `OLLAMA_*` keys only.
4. **OpenAI status:**
   - *Available in the library:* `src/backends/openai_backend.py`, a `BACKEND_NAMES` entry, and `resolve_backend("openai")`.
   - *Optional:* the `openai` package is not in `requirements.txt` (only `requirements-openai.txt`), is **not installed** in the project environment, and is imported lazily only immediately before a confirmed call.
   - *Exposed in the CLI:* **No.** `main.py` calls the resolver only with `"local"` and `"ollama"`; the menu says "Paid cloud services are not available from this menu"; `main.py` contains no `openai` string in executable code.
5. **Can any configuration accidentally trigger paid usage? No.** For a paid request *all* of the following must hold: the caller asks for `"openai"` by name; `enabled=True`; a model is supplied; an API key is supplied; a confirmation handler is supplied **and returns True for every single request**; and fewer than `max_calls` (12) requests have been made. Merely having `OPENAI_API_KEY` in the environment activates nothing (code docstring and canary tests, e.g. `tests/test_main.py::TestOpenAIIsNeverInvolved`). The residual scenario is a developer writing their own script that calls `resolve_backend("openai", ...)` *and* approves each confirmation. Low risk; worth one sentence in a developer note.

### 2.4 Recommended smallest safe configuration clean-up (for a later phase, with approval)

| Option | Change | Code/test impact | Verdict |
|---|---|---|---|
| **A (recommended)** | Edit `.env.example`: delete the `NOTEFORGE_*` and `OPENAI_*` lines; replace the header with "Not loaded automatically. Set these as real environment variables in PowerShell, for example `$env:OLLAMA_MODEL = 'qwen3.5:4b'`"; keep the 9 accurate `OLLAMA_*` lines. Delete `config/settings.example.json` (or move it to a clearly labelled `docs/` note) | None. No test references either file (verified by search) | Removes every misleading statement; 2 files touched |
| B | Implement `.env` loading and the unused settings | Source change; new feature; new tests | Out of scope for release preparation |
| C | Delete both example files | None | Acceptable; loses the documented OLLAMA variable list (it already exists in the README) |

Leave `python-dotenv` in `requirements.txt` for now (a test depends on it).

## 3. Versioning audit

Current version: `0.1.0` (kept unchanged).

**Locations that must change for a 1.0.0 release**

| # | Location | What |
|---|---|---|
| 1 | `src/__init__.py:3` | `__version__ = "0.1.0"` (the only definition; the banner `v0.1.0` and `--version` output derive from it automatically) |
| 2 | `tests/test_main.py:228` | `assert ... stdout.strip() == "NoteForge AI 0.1.0"` (a test edit; the only test reference) |
| 3 | `README.md` lines 5, 81, 218 | status line, `--version` comment, "Program version string" |
| 4 | `docs/INSTALLATION_GUIDE.md` lines 3, 90, 125 | expected `--version` output and banner text |
| 5 | `docs/USER_GUIDE.md` line 25 | menu banner example |
| 6 | `docs/RELEASE_NOTES_V1.0.md` lines 3, 60, 74 | version-string wording |
| 7 | `docs/V1.0_RELEASE_CHECKLIST.md` lines 37, 38 (and the version-tagging items) | |
| 8 | `docs/PHASE_10_RELEASE_PREPARATION_REPORT.md` | version-string defect entries (could remain as a historical record) |

**Not changed by a version bump / separate decisions**

- `src/article_extractor.py:38` `DEFAULT_USER_AGENT = "NoteForgeAI/0.1 (personal study-notes tool)"`, repeated in `config/settings.example.json:7` and `README.md:190`. It is a network identifier, not the program version; no test pins it. Decide whether it should read `NoteForgeAI/1.0`, and whether "personal study-notes tool" still describes the intended distribution (section 6).
- Phase 8, 8.1, 8.2 and 9 reports mention `0.1.0` as history: leave them unchanged.
- **Generated Word documents embed no version number** (checked inside the examples and an output document), so no example needs regenerating after a bump.
- There is no `pyproject.toml`, `setup.py` or `CHANGELOG`, so no other version field exists today.

## 4. Git readiness audit

**Status:** `fatal: not a git repository (or any of the parent directories)`. No `.git` folder at the project or any parent. (A global Git identity is configured on this machine; it was not displayed. It would be written into every commit, see 4.4.) Nothing was initialised or staged.

### 4.1 `.gitignore` coverage

Current contents: `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.env`, `output/*` with `!output/.gitkeep`, `input/*` with `!input/.gitkeep`, `*.log`, `.idea/`, `.vscode/`.

| Needed exclusion | Covered? |
|---|---|
| Virtual environment (92 MB) | Yes |
| `.env` | Yes (exact name only) |
| Generated documents and logs (`output/*`, `*.log`) | Yes |
| Python caches, pytest cache | Yes |
| Editor folders | Yes (`.idea/`, `.vscode/`) |
| `.env.local`, `.env.production` and similar variants | **No** (would need `.env.*` plus `!.env.example`) |
| Word lock/temp files `~$*.docx` (appear when a document is open in Word, including those in `docs/examples/`) | **No** |
| Build/package output (`build/`, `dist/`, `*.egg-info/`) | **No** (not needed today; useful before packaging) |
| Tool caches (`.mypy_cache/`, `.ruff_cache/`, `.coverage`, `htmlcov/`) | **No** |
| OS files (`Thumbs.db`, `desktop.ini`, `.DS_Store`) | **No** |
| `.claude/` (assistant session folder, if one appears) | **No** (none exists now) |
| Line-ending policy (`.gitattributes`) | **None**; all current files use LF, and Git for Windows can convert on checkout |

### 4.2 What a first commit would contain (simulated by applying the rules by hand)

63 files, about 1.2 MB: `src/` 15, `tests/` 22 (including 6 synthetic HTML/text fixtures), `docs/` 16 (reports, guides, 3 example `.docx`, examples README), 7 root files, `config/` 1, `.gitkeep` files. Largest items are source/test files (under 100 KB each); the only binaries are the three example `.docx` (about 45 KB each). Ignored and therefore not committed: the five generated documents and `noteforge.log` in `output/`, caches, and `.venv`.

### 4.3 Secrets and personal information

- **No real secret** in any of the 57 text files scanned (patterns for key prefixes, tokens, private keys, password assignments).
- **5 fake "canary" strings** exist in tests (`sk-test-SECRET-DO-NOT-LEAK-123456` in `tests/test_backends.py:30`; `sk-secret-never-read` in `tests/test_main.py` and `tests/test_ollama_integration.py`; `sk-should-never-be-used` in `tests/test_notes_generator.py`). They are deliberate test values. Automated secret scanners (for example GitHub push protection) could flag them as false positives on a public push; a short note in a developer README would pre-empt questions.
- **No personal identifiers** (username, e-mail address, absolute user paths) in any text file or inside the three example `.docx` files (checked their XML); document properties show author "NoteForge AI".
- `output/noteforge.log` and the five documents in `output/` **do** contain absolute local paths (shown here as `<LOCAL_PROJECT_PATH>`; the real user-folder path was redacted from this report in Phase 13B), but they are ignored by `.gitignore` and would not be committed.
- Fixtures and tests use the domain `appliedai.substack.com` (8 test references, 1 fixture URL). It is described as fictional; I could not verify without network access that no such real publication exists. Using a clearly reserved placeholder would remove any doubt.

### 4.4 Other points

- **Commit identity:** every commit records the configured author name and e-mail. For a public repository decide whether to use a project-specific or "noreply" identity.
- **Should the example `.docx` files go into a private distribution?** Yes, with caveats: all three come from synthetic fixtures and contain no personal data. The Ollama example was produced **before** the Phase 8.1/8.2 safeguards and still contains two unsupported AI sentences; it is correctly labelled as such in `docs/examples/README.md`. It is fine for a private pilot that is told to expect human review, but I would **omit it from any public or commercial package** until it is regenerated with the current build (that needs one live Ollama run and your approval).
- The five documents in `output/` are test artefacts of earlier builds and should not be shipped.

## 5. Distribution recommendation

| | 1. Private distribution to a small group | 2. Public open-source GitHub release | 3. Commercial distribution |
|---|---|---|---|
| Feedback value | High and controllable | Broad but uncontrolled | Depends on first customers |
| Reversibility | **High**: access can be withdrawn, terms changed | **Low**: once published under an open licence, released versions stay open | Medium |
| Preserves commercial options | **Yes** | Weakened (earlier versions remain open under the chosen licence) | Yes |
| Legal exposure | Low (known users, terms agreed) | Higher (anyone, any use; third-party content questions) | Highest (warranties, support, content rights, taxes) |
| Fit with today's product | **Good**: Windows/PowerShell users comfortable installing Python | Possible, but needs packaging, support capacity, licence decision | Premature: no installer, Python 3.14.7 only tested, Ollama experimental |
| Work needed | Smallest | Moderate | Largest |

**Recommended practical path (staged; every stage is reversible until the last):**

1. **Resolve the ownership and licensing questions (section 6) first.** No distribution of any kind before that.
2. **Clean up and prepare a private 1.0.0 release candidate:** configuration clean-up (option A), `.gitignore` additions, version bump, then `git init` and a first commit under an identity you choose (each needs your separate approval).
3. **Private pilot with 5-10 trusted users** from a *private* repository or a zip, under a short written terms notice (all rights reserved, pilot use only, feedback welcome, Ollama output experimental). Ask them for the two real inputs the validation did not cover: long articles and non-English or table/code-heavy posts. Collect feedback through issues or a simple form. The program collects no telemetry and sends nothing anywhere except the article fetch.
4. **Decide the long-term model from the feedback:** keep proprietary/commercial; make the source public later; or dual approach. Because the pilot is private and unlicensed-by-default, **all three options stay open**.

I recommend against going straight to a public release: it is the least reversible step and it removes the commercial option you want to keep.

## 6. Licensing and ownership considerations (questions to resolve; no licence selected or applied)

The repository has **no `LICENSE`, `NOTICE`, copyright header, terms or contribution policy**.

1. **Code ownership.** Who owns the code (you personally, an employer, a client)? Any employment or contract IP clauses?
2. **AI-assisted authorship.** The code, tests, fixtures and documentation were developed with an AI coding assistant. Confirm the assistant provider's terms on ownership/use of outputs, and consider whether your jurisdiction affects copyright in AI-assisted material.
3. **Choice of licence model** (your decision): all rights reserved / proprietary; source-available; permissive open source; copyleft open source. Each has different consequences for commercialisation, contributions and the ability to relicense later (open contributions typically need a contributor agreement if you want to keep relicensing freedom).
4. **Name and branding.** Check availability and trademark conflicts for "NoteForge" / "NoteForge AI". The docs refer to Substack, Ollama, Qwen, OpenAI and Microsoft Word: use those names only descriptively and avoid implying endorsement.
5. **Third-party content.** The tool downloads articles and quotes them in the notes. Questions: the articles' copyright and fair-use/personal-use position in your jurisdictions; Substack's terms of service for automated access; **no `robots.txt` handling and no rate limiting** exist in the fetcher (one user-initiated request per run); the User-Agent calls it a "personal study-notes tool", which may not describe a commercial product; whether distributing the generated notes (rather than using them privately) is permitted.
6. **Dependency licences** (read from installed metadata): requests (Apache), beautifulsoup4 (MIT), lxml (BSD-3), trafilatura 2.3.1 (Apache-2.0), readability-lxml (Apache), python-docx (MIT), python-dotenv (BSD-3), regex (Apache-2.0 AND CNRI-Python), pytest (MIT). Transitive packages to review: **`chardet` 5.2.0 (LGPL v2+)**, `certifi` (MPL-2.0), `tld` (MPL-1.1 / GPL-2.0 / LGPL-2.1 choice). They are installed by pip rather than bundled, which is the simple case; **bundling them into a single installer or executable would raise additional LGPL/MPL obligations.** Dependencies are unpinned (`>=`), so future versions may change licences.
7. **AI model terms.** The validated model `qwen3.5:4b` is not distributed by the project, but its licence and any use restrictions (especially for commercial use or for using its output) should be checked on the model's own page. The same applies to Ollama itself.
8. **Liability and warranty.** Notes can contain errors, particularly the experimental Ollama content. A disclaimer ("provided as is", not guaranteed factually correct, review before relying) belongs in any terms.
9. **Privacy.** The program collects no data and stores logs locally; a short privacy statement (what is fetched, what stays on the PC) is advisable for external users.
10. **Examples and fixtures.** Confirm you are comfortable owning/publishing the synthetic fixtures and example documents, and replace the possibly real-sounding `appliedai.substack.com` with a placeholder.

## 7. Release risks and limitations

### 7.1 Critical release blockers

| # | Blocker | Blocks | Smallest remedy |
|---|---|---|---|
| B1 | No licence, terms or ownership decision | **Any distribution** (even a pilot should have written terms) | Decide section 6 items 1-3; write terms (not selected here) |
| B2 | `.env.example` and `settings.example.json` describe options that do not exist, including OpenAI/`NOTEFORGE_*`, contradicting the README | A tagged V1.0 and any external user | Option A in section 2.4 (2 files, no code/test change) |
| B3 | Version still `0.1.0`; folder is not under version control, so no tag, history or reproducible release exists | A **tagged V1.0** (not an informal pilot) | Version bump (2 code/test edits + docs), `git init` and first commit, by you |

### 7.2 Important but non-blocking issues

1. `.gitignore` gaps (section 4.1) and no `.gitattributes`.
2. `python-dotenv` is an unused runtime dependency (a test still imports it).
3. No installer or packaging metadata; users must manage Python and PowerShell themselves. Dependencies are unpinned; only Python 3.14.7 and Windows 11 were tested (Windows 10 and other Python versions untested).
4. Ollama example predates the safeguards (omit from public packages).
5. Real-looking fixture domain (`appliedai.substack.com`); fake key canaries in tests may trip secret scanners.
6. User-Agent string says "personal study-notes tool"; no robots.txt handling or rate limiting.
7. No continuous-integration or automated release check; the 1,712 tests take 2-4 minutes locally.
8. Minor documentation nuance: the Installation Guide lists `regex 2026.11.7` (from the clean-environment test) while the project environment has 2026.9.29. Harmless, but pin or restate if you publish exact versions.

### 7.3 Known limitations to disclose to users

- Substack only (plus user-supplied HTML/text). No PDF, Medium, YouTube or research-paper support.
- Local notes are extractive: no new explanations or analogies; question counts can be below 10/5/10.
- **Ollama notes are experimental, slow (minutes; measured only for Basic detail without questions), memory-heavy (about 3.7 GB), not repeatable, and are not guaranteed to be factually correct.** The built-in support check compares vocabulary only; the live removal of unsupported sentences was never observed with the real model. All AI-generated text needs human review.
- English-oriented heuristics; keyword noise can remain on marketing-style articles.
- Layout: each chapter starts on a new page, so a few trailing lines of a list can occupy an almost empty page; a table of contents over about 45 entries uses two pages.
- Not tested live: very long (over about 2,000 words) or non-English articles, real table/code-heavy posts, a real keyboard Ctrl+C.
- Paywalled articles yield partial notes (clearly marked) and are never bypassed.
- The example configuration files are not read (until clean-up is done).

## 8. Prioritised next steps (each needs your approval before it is done)

1. **Decide ownership and licensing questions** (section 6); write pilot terms. *(B1)*
2. **Approve the configuration clean-up, option A** (edit `.env.example`, remove `config/settings.example.json`). *(B2)*
3. **Decide the version number** and approve the `src/__init__.py` + `tests/test_main.py` + documentation updates. *(B3)*
4. **Approve `.gitignore` additions and a `.gitattributes`**, then `git init`, a first commit under an identity you choose, and (only on your instruction) a tag. *(B3)*
5. **Prepare the private pilot package** (private repository or zip: README, guides, examples without or with the Ollama caveat) and a feedback channel. Choose 5-10 pilot users; ask them to test long, non-English and code/table-heavy articles.
6. **Optional hardening:** pin or lock dependencies; test Windows 10 and one other Python version; replace the fixture domain with a placeholder; remove or justify `python-dotenv` together with its test; consider one regenerated Ollama example (needs approval for one live run).
7. **Before any public or commercial step:** legal review of third-party content, Substack terms, name/trademark, dependency and model licences (section 6), plus a privacy and disclaimer text.

## 9. Test status (existing suite, run unchanged)

`python -m pytest -o addopts="" -q`: **1,712 passed, 0 failed** (3 min 48 s). `pip check`: No broken requirements found. No test was added, changed or deleted.

## 10. Files inspected

Read in full or searched read-only: `README.md`, `docs/PHASE_10_RELEASE_PREPARATION_REPORT.md`, `docs/V1.0_RELEASE_CHECKLIST.md`, `.env.example`, `config/settings.example.json`, `requirements.txt`, `requirements-dev.txt`, `requirements-openai.txt`, `pytest.ini`, `.gitignore`, `src/__init__.py`, `src/main.py`, `src/backends/__init__.py`, `src/backends/openai_backend.py`, `src/backends/ollama.py`, `src/article_extractor.py` (version/User-Agent/fetch), `tests/test_setup.py`, `tests/test_main.py` (version assertion and canaries), `tests/test_backends.py`, `tests/test_notes_generator.py`, `tests/test_ollama_integration.py` (canary strings), all Markdown files in `docs/`, `docs/examples/*.docx` (XML scanned), `output/*.docx` (one scanned for a version string), installed-package metadata for 9 direct and all transitive dependencies, and the project directory tree.

## 11. Confirmation of no modification

Before the inspection I recorded SHA-256 hashes of 61 project files (`src/`, `tests/`, `config/`, `docs/` as they existed, `README.md`, `requirements*.txt`, `.env.example`, `.gitignore`, `pytest.ini`) and of the 6 visible files in `output/` (five documents and `noteforge.log`; the empty `.gitkeep` was not part of the baseline). After the inspection and the test run, **all 61 files and all 6 output files were byte-identical.** **No source file and no existing output document or log was modified.** The only new file is this report. `.venv`, caches and `__pycache__` are untracked build artefacts regenerated by the normal test run. Git was not initialised, nothing was staged or committed, and no tag, release or publication was made.
