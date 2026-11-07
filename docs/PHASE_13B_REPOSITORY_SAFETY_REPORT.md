# NoteForge AI - Phase 13B: Repository Safety Cleanup Report

Date: 2026-11-07 | Repository: local only, branch `main`, no commits | Target remote (not added): https://github.com/gsr9001/NoteForgeAI (private)

**Nothing was staged, committed, tagged, pushed, pulled or published, and no remote was added.** No application source, test, output document or log was modified; the version is still `0.1.0`. No network request was made, no authentication token or credential was read or shown, and `OPENAI_API_KEY` was never accessed.

## 1. Summary

| Task | Result |
|---|---|
| 1. Repository-level GitHub noreply e-mail | **Not configured: the exact address could not be verified.** Repository e-mail left unchanged; **your input is needed** (section 3) |
| 2. `.gitignore` | **Improved** (12 lines to a documented 58-line file; all original rules kept) |
| 3. `.gitattributes` | **Created** (2 rules) |
| 4. Personal path redaction | **Done** in both reports; no username path remains in any file that would be committed |
| 5. Fake test canary strings | **Documented**; tests untouched |
| 6. Verification | All checks pass: **1,712 tests passed**, `pip check` clean, 0 staged files |

## 2. Files modified and created

| File | Action | What |
|---|---|---|
| `.gitignore` | **Modified** | New exclusions (section 4) |
| `.gitattributes` | **Created** | Binary `.docx` and LF normalisation (section 5) |
| `docs/PHASE_11_RELEASE_READINESS_INSPECTION.md` | **Modified** | One personal path redacted (section 6) |
| `docs/PHASE_13A_GIT_INITIALIZATION_REPORT.md` | **Modified** | Placeholder unified to `<LOCAL_PROJECT_PATH>` (section 6) |
| `docs/PHASE_13B_REPOSITORY_SAFETY_REPORT.md` | **Created** | This report |

Not modified (SHA-256 identical to the baseline taken at the start of the phase): all 15 files in `src/`, all 22 in `tests/`, `.env.example`, the three `requirements*.txt`, `pytest.ini`, `README.md`, every other document, and all of `output/` (five documents, `noteforge.log`, `.gitkeep`). Baseline: 72 files; 69 identical; the 3 that differ are exactly `.gitignore` and the two reports above.

## 3. GitHub noreply e-mail: verification and status

| Check | Result |
|---|---|
| Global identity | name set; e-mail set; the e-mail's domain is `gmail.com`; it is **not** a GitHub noreply address |
| Repository-level identity | none set (no local `user.email`, no local `user.name`); commits would currently use the global values |
| GitHub CLI (`gh`) | not installed; no `gh` configuration folder |
| GitHub Desktop | not installed; no configuration folder |
| `GITHUB_*` / `GH_*` environment variables | none |
| Other reliable local source of the exact address | none found |

A GitHub noreply address normally has the form `<numeric id>+<username>@users.noreply.github.com` (older accounts may use `<username>@users.noreply.github.com`). The numeric id is not stored anywhere on this computer, so **the address cannot be derived and I did not guess it.** The repository e-mail was therefore **left unchanged**, the global e-mail was **not changed** (`~/.gitconfig` was not modified during this phase), and the author name was not changed.

**What I need from you:** open GitHub > Settings > Emails, copy the line shown under "Keep my email addresses private" (it ends in `@users.noreply.github.com`), and give it to me. I will then run `git config --local user.email "<that address>"` and verify it. Until then, do not commit: a commit made now would record your Gmail address, and GitHub's "Block command line pushes that expose my email" setting (if enabled) would reject the push.

## 4. `.gitignore` improvements

All 12 original rules are preserved (`.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.env`, `output/*` with `!output/.gitkeep`, `input/*` with `!input/.gitkeep`, `*.log`, `.idea/`, `.vscode/`). The complete new file:

```gitignore
# --- Virtual environments ---
.venv/
venv/
env/
ENV/

# --- Python bytecode and caches ---
__pycache__/
*.pyc
*.pyo
*.pyd
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
.coverage.*
htmlcov/
.tox/
.nox/
.hypothesis/

# --- Secrets and environment-specific files (.env.example stays tracked) ---
.env
.env.*
!.env.example
*.pem
*.key
*.pfx
*.p12
id_rsa*
id_ed25519*
id_ecdsa*

# --- Packaging and build output ---
build/
dist/
*.egg-info/
*.egg

# --- Generated output and input (the .gitkeep placeholders stay tracked) ---
output/*
!output/.gitkeep
input/*
!input/.gitkeep
*.log

# --- Word temporary/lock files (created while a document is open in Word) ---
~$*

# --- Editors, assistant local state and operating-system files ---
.idea/
.vscode/
# Only the personal, machine-local assistant settings are ignored; shared project
# instructions (for example CLAUDE.md or .claude/settings.json) may be committed.
.claude/settings.local.json
Thumbs.db
Desktop.ini
.DS_Store
```

**What was added:** other virtual environments; extra Python and tool caches; `.env.*` with `!.env.example` after it (order matters); private-key and certificate-store files; build and packaging output; Word lock files (`~$*`, which also covers `docs/examples/`); OS files; the machine-local assistant settings file.

**Decision on `.claude/`:** inspected first. The project contains **no** `.claude/` folder, no `CLAUDE.md` and no other assistant configuration. Because shared project instructions or settings (for example `CLAUDE.md`, `.claude/settings.json`, agent definitions) would be useful to version-control if they are added later, the whole folder is **not** ignored; only the personal, machine-local `.claude/settings.local.json` is.

**Deliberately not ignored:** `.env.example`, source files, tests, documentation, `docs/examples/*.docx`, the two `.gitkeep` placeholders.

## 5. `.gitattributes`

```gitattributes
# Normalise text files to LF in the repository and in working copies (the project uses LF throughout).
* text=auto eol=lf

# Word documents are binary: never convert line endings or attempt text diffs.
*.docx binary
```

Only two rules: the project contains only text files and `.docx` binaries (no images, PDFs or archives), so no other binary types were listed. `git check-attr` confirms: `.docx` files resolve to `binary` (text/diff/merge unset; the inherited `eol` setting has no effect on a non-text file); `README.md`, `src/main.py` and `.gitignore` resolve to `text: auto`, `eol: lf`. All existing files already use LF, so staging will not trigger line-ending warnings even though `core.autocrlf=true` is configured on this machine.

## 6. Personal path redaction

| File | Before | After |
|---|---|---|
| `docs/PHASE_11_RELEASE_READINESS_INSPECTION.md` (line 133) | the output log and documents "contain the absolute path" with the real Windows user-folder path | "contain absolute local paths (shown here as `<LOCAL_PROJECT_PATH>`; the real user-folder path was redacted from this report in Phase 13B)"; the rest of the sentence (that they are ignored by `.gitignore` and not committed) is unchanged |
| `docs/PHASE_13A_GIT_INITIALIZATION_REPORT.md` (2 places) | the placeholder `<project root>` (the real path had already been replaced when that report was finalised) | `<LOCAL_PROJECT_PATH>`; one sentence now says the earlier personal-path finding was "redacted in Phase 13B" |

Historical meaning and all other facts in both reports are unchanged. **Search result:** across every file that would be committed (everything except `.git`, `.venv`, caches and the ignored `output/` files) there is now **no** occurrence of the Windows user-folder path, the bare username, or the e-mail address. (The ignored `output/` documents and `noteforge.log` still contain local paths; they are excluded by `.gitignore`.)

## 7. Fake test canary strings (intentional)

Five test files contain deliberately fake, key-shaped strings (they begin `sk-`, and their text contains words such as "test", "secret" or "never"): `tests/test_backends.py` (line 30, the constant `SECRET`), `tests/test_main.py` (lines 831 and 840), `tests/test_notes_generator.py` (line 492) and `tests/test_ollama_integration.py` (line 660). Three of them are also quoted in section 4.3 of `docs/PHASE_11_RELEASE_READINESS_INSPECTION.md`.

They are **not credentials.** They are canaries used to prove, by test, that the program never reads or leaks an API key: for example, a fake `OPENAI_API_KEY` is placed in a recording environment and the tests assert that it is never accessed, printed or sent. Removing or changing them would weaken those safety tests, so **no test was altered.**

If GitHub secret scanning or push protection flags them: they appear only at the locations above, are not valid keys, and any alert can be dismissed as a test value **only after checking that the flagged file and line are one of those locations.** An alert on any other file, or on a string that does not match that description, must be treated as real and investigated, not dismissed. (No real secret was found anywhere in the Phase 13A scan.)

## 8. Verification

| # | Check | Result |
|---|---|---|
| 1 | Complete `.gitignore` inspected | Printed in full in section 4 |
| 2 | `.env.example` remains trackable | `git check-ignore` reports it **not ignored** |
| 3 | `.env`, `.env.local`, `.env.production` (and `.env.staging`) ignored | **Ignored** |
| 4 | `.venv`, `venv`, `env`, `ENV`, `build`, `dist`, `*.egg-info`, tool caches, `__pycache__`, `.pytest_cache`, Word lock files (`~$anything.docx`, `output/~$lock.docx`, `docs/examples/~$EXAMPLE_...docx`) | **All ignored** (38 representative paths checked, none missed) |
| 4b | Keys and OS files: `*.pem`, `*.key`, `*.pfx`, `*.p12`, `id_rsa`, `id_rsa.pub`, `id_ed25519`, `Thumbs.db`, `.DS_Store`, `Desktop.ini`, `.claude/settings.local.json` | **Ignored** |
| 4c | Must stay trackable: `.env.example`, `.gitignore`, `.gitattributes`, `README.md`, `src/main.py`, a test, a doc, the three example `.docx`, both `.gitkeep`, a fixture, `requirements.txt`, `pytest.ini`, and hypothetical `CLAUDE.md`, `.claude/settings.json`, `.claude/agents/x.md` | **All trackable** (18 checked) |
| 5 | `.docx` binary handling | `git check-attr`: `binary: set`, `text/diff/merge: unset` |
| 6 | Personal path in both reports | **None** (searched all to-be-committed files) |
| 7 | Source and test files unchanged | 15 of 15 in `src/`, 22 of 22 in `tests/` identical |
| 8 | Output documents and log unchanged | 7 of 7 identical (five documents, `noteforge.log`, `.gitkeep`) |
| 9 | Full pytest suite | **1,712 passed, 0 failed** (1 min 46 s) |
| 10 | `pip check` | No broken requirements found |
| 11 | Git e-mail | Global e-mail unchanged (`~/.gitconfig` not modified; still the same Gmail-domain address); repository-level e-mail **not set** (see section 3) |

Ignored-but-present files (`git status --ignored`): `.pytest_cache/`, `.venv/`, the five `output/Understanding_Retrieval-Augmented_Generation_Notes*.docx` documents, `output/noteforge.log`, and the four `__pycache__/` folders: unchanged from Phase 13A.

## 9. Git status

- Branch: `main`; commits: **none** (no `HEAD`); tags: **0**; remotes: **0**.
- Staged files: **0** (`git diff --cached --name-only`); tracked files: **0**.
- `git status --short` (all untracked, `??`): `.env.example`, `.gitattributes`, `.gitignore`, `README.md`, `docs/`, `input/`, `output/`, `pytest.ini`, `requirements-dev.txt`, `requirements-openai.txt`, `requirements.txt`, `src/`, `tests/`.
- Candidates for staging now: **68 files** = the 65 files in the Phase 13A list + `.gitattributes` + the Phase 13A report + this report. No file was removed from the candidate set.

## 10. Confirmation

No `git add`, no staging, no commit, no remote, no push or pull, no tag, no release or publication, no change to application source, tests or version, no change to output documents or logs, no authentication token or credential exposed, no paid API call, no model download.

## 11. Stop

Waiting for explicit approval. Before staging or committing I need **(a)** the exact noreply address from GitHub Settings > Emails (section 3), and **(b)** your approval to stage and commit the 68 candidate files. Adding the remote and pushing remain separate approvals.
