# NoteForge AI - Windows Installation Guide

This guide installs NoteForge AI on Windows 10/11 using Windows PowerShell. The commands in sections 3-6 were run on 2026-11-07 in a brand-new virtual environment (Windows 11, Windows PowerShell 5.1, Python 3.14.7): the install finished in about 100 seconds, `pip check` reported no problems, `python src/main.py --version` printed `NoteForge AI 0.1.0`, and 84 of the project's tests passed in that fresh environment.

Back to the [README](../README.md) | Next: [User Guide](USER_GUIDE.md) | Problems: [Troubleshooting](TROUBLESHOOTING.md)

## 1. Prerequisites

| Need | Details |
|---|---|
| Windows | Windows 10 or 11 (all testing was on Windows 11) |
| Python | The project does not pin a version. **Python 3.14.7 was used for all development and testing**; other versions are untested. Install from <https://www.python.org/downloads/windows/> and tick **"Add python.exe to PATH"** |
| Internet | Needed to install packages and to read a Substack URL. HTML-file and text-file input work offline once installed |
| Disk space | Well under 1 GB for the program and packages; the optional Ollama model `qwen3.5:4b` is about 3.3 GB |
| Microsoft Word | Only needed to *open* the generated `.docx` files. NoteForge AI does not need Word to create them |
| RAM | The Local backend needs little. Ollama with `qwen3.5:4b` used about 3.7 GB in testing (validated on a 16 GB PC) |

Dependencies (from `requirements.txt`): `requests`, `beautifulsoup4`, `lxml`, `trafilatura`, `readability-lxml`, `python-docx`, `python-dotenv`, `regex`. Versions installed in the validated environment: requests 2.34.2, beautifulsoup4 4.15.0, lxml 6.1.3, trafilatura 2.3.1, python-docx 1.2.0, python-dotenv 1.2.4, regex 2026.11.7, readability-lxml 0.9.

## 2. Check Python

Open **Windows PowerShell** and run:

```powershell
python --version
```

Expected: `Python 3.14.7` (or the version you installed). If the command is not found, see [Troubleshooting, section 1](TROUBLESHOOTING.md#1-python-installation-issues).

## 3. Get the project and open it in VS Code

1. Place the project folder somewhere you can write to, for example `C:\Users\<you>\Desktop\ClaudeCode\NoteForge AI`.
2. In PowerShell:

```powershell
cd "C:\Users\<you>\Desktop\ClaudeCode\NoteForge AI"
code .
```

(`code .` opens the folder in VS Code if its command-line launcher is installed; otherwise use **File > Open Folder** in VS Code. VS Code is optional: everything below works in a plain PowerShell window.) In VS Code, open a terminal with **Terminal > New Terminal**; it starts in the project folder.

## 4. Create and activate the virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

After activation the prompt starts with `(.venv)`. To confirm:

```powershell
(Get-Command python).Source        # should end with ...\NoteForge AI\.venv\Scripts\python.exe
```

If you see "running scripts is disabled on this system", run once, for the current window only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

(On the validated machine the policy was already `RemoteSigned` for the current user and activation worked without changes.) To leave the environment later, run `deactivate`. If `.venv` already exists in the project folder, skip `python -m venv` and only activate it.

## 5. Install dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

To also run the test suite (recommended once), install the development requirements instead, which include everything above plus `pytest`:

```powershell
pip install -r requirements-dev.txt
```

`requirements-openai.txt` installs the optional `openai` package. **You do not need it**: no paid service can be reached from the command-line program.

## 6. Verify the installation

```powershell
pip check
python src/main.py --version
```

Expected output:

```
No broken requirements found.
NoteForge AI 0.1.0
```

Optional, with the development requirements installed:

```powershell
python -m pytest -o addopts="" -q tests/test_setup.py
```

Full suite (about two minutes; 1,712 tests passed in the Phase 9 validation):

```powershell
python -m pytest -o addopts="" -q
```

## 7. Run NoteForge AI

From the project folder, with the environment active:

```powershell
python src/main.py
```

or

```powershell
python -m src.main
```

`python src/main.py` also works when started from another folder if you give the full path to `main.py`. (`python -m src.main` must be started from the project folder.) To save documents somewhere else:

```powershell
python src/main.py --output-dir "D:\MyNotes"
```

You should see the banner `NOTEFORGE AI   v0.1.0` and the seven-item menu. Choose `7` to exit. A first real run is described in the [User Guide](USER_GUIDE.md#3-your-first-notes-step-by-step).

## 8. Optional: Ollama (experimental AI backend)

Skip this section if you only want the default Local backend.

1. Install Ollama from <https://ollama.com/download> (Windows installer). After installation the Ollama app runs in the background and listens on `http://localhost:11434`.
2. Check it is available:

```powershell
ollama --version        # validated with: ollama version is 0.40.15
ollama list             # lists installed models
```

3. Install the validated model once (about 3.3 GB download):

```powershell
ollama pull qwen3.5:4b
```

4. In NoteForge AI choose menu option `5` ("View available models") to see what the program finds, then option `4` and `2` to select Ollama.
5. Optional settings are environment variables; for the current PowerShell window only:

```powershell
$env:OLLAMA_MODEL = "qwen3.5:4b"
$env:OLLAMA_TIMEOUT = "1200"
```

The complete list of supported variables is in the [README, section 14](../README.md#14-configuration-options). Only a server on this PC is accepted.

Ollama notes are slow, memory-heavy and experimental: read the [Backend Guide](BACKEND_GUIDE.md) first.

## 9. A note on configuration files

The project contains one example file, `.env.example`. It is a **reference list of the nine supported Ollama environment variables** (`OLLAMA_BASE_URL`, `OLLAMA_HOST`, `OLLAMA_MODEL`, `OLLAMA_NUM_CTX`, `OLLAMA_TEMPERATURE`, `OLLAMA_THINK`, `OLLAMA_KEEP_ALIVE`, `OLLAMA_MAX_OUTPUT_TOKENS`, `OLLAMA_TIMEOUT`) with their ranges and defaults. **The program does not load `.env` files automatically**, and there is no settings file, so copying `.env.example` to `.env` and editing it does **not** configure NoteForge AI. To use one of these variables, set it in PowerShell before starting the program (it lasts for that window only), for example `$env:OLLAMA_MODEL = "qwen3.5:4b"`. Everything else is chosen in the menu or with `--output-dir`. All of these settings are optional; the Local backend needs none of them.

## 10. Common setup errors

| Symptom | Fix |
|---|---|
| `python` is not recognised | Reinstall Python with "Add python.exe to PATH", or use `py -m venv .venv` |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` and activate again |
| `ModuleNotFoundError: No module named 'docx'` (or `trafilatura`, `requests`...) | The environment is not active, or dependencies are not installed: activate `.venv`, then `pip install -r requirements.txt` |
| `pip` installs nothing / SSL or proxy errors | Check your network or proxy settings, then retry `pip install -r requirements.txt` |
| `python -m src.main` fails with `ModuleNotFoundError: No module named 'src'` | Run it from the project folder (the one containing `src`) |

More cases: [Troubleshooting](TROUBLESHOOTING.md).
