# NoteForge AI - Troubleshooting Guide

Messages below are quoted from the program. Text in `<angle brackets>` varies. Back to the [README](../README.md) | [Installation Guide](INSTALLATION_GUIDE.md) | [User Guide](USER_GUIDE.md)

Unexpected errors never close the program: it prints `[!] ...` and returns to the menu, and writes details to `noteforge.log` in the output folder (section 13).

## 1. Python installation issues

| Symptom | Fix |
|---|---|
| `python : The term 'python' is not recognized...` | Install Python from python.org and tick "Add python.exe to PATH", then open a **new** PowerShell window |
| `python` opens the Microsoft Store | Windows Settings > Apps > Advanced app settings > App execution aliases: turn off the `python.exe` alias; or use the launcher: `py --version`, `py -m venv .venv` |
| Wrong Python version | `python --version`. All testing used Python 3.14.7; other versions are untested |

## 2. Virtual environment activation

| Symptom | Fix |
|---|---|
| `.\.venv\Scripts\Activate.ps1 cannot be loaded because running scripts is disabled on this system` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate again (affects only the current window) |
| `.venv\Scripts\Activate.ps1 : The term ... is not recognized` or the path is not found | You are not in the project folder, or `.venv` does not exist: `cd` to the folder that contains `src`, then `python -m venv .venv` |
| Prompt has no `(.venv)` | Activation did not run; check with `(Get-Command python).Source` (it should end in `.venv\Scripts\python.exe`) |
| A copied project folder behaves oddly | A virtual environment is tied to its location: delete `.venv` in the copy and create it again |

## 3. Dependency installation

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'docx'` (or `requests`, `trafilatura`, `bs4`, `lxml`, `regex`) | The environment is not active, or packages are not installed: activate `.venv`, then `pip install -r requirements.txt` |
| `pip` cannot connect / SSL errors | Check network, VPN or proxy; retry |
| A package fails to build | The project was tested only with Python 3.14.7 and the package versions listed in the [Installation Guide](INSTALLATION_GUIDE.md); use that Python version |
| Is the installation consistent? | `pip check` should print `No broken requirements found.` |

## 4. CLI launch errors

| Symptom | Fix |
|---|---|
| `python: can't open file '...\src\main.py'` | Run from the project folder, or give the full path to `main.py` |
| `ModuleNotFoundError: No module named 'src'` (when using `python -m src.main`) | `python -m src.main` must be started from the project folder; `python src/main.py` works from the project folder, and with a full path from elsewhere |
| `error: unrecognized arguments: ...` | Only `--output-dir <folder>` and `--version` exist |
| The menu repeats `'<text>' is not a menu option. Please enter a number from 1 to 7.` | Enter a number from the menu; see also section 12 |
| `No more input. Goodbye.` | Standard input was closed (for example an empty pipe); run the program in a normal terminal |

## 5. Invalid Substack URLs

| Message | Meaning and fix |
|---|---|
| `[!] That input is not usable: URL must not contain whitespace.` | Remove spaces; paste only the address |
| `... Unsupported URL scheme 'http'; use https.` | Use `https://...` |
| `... URL does not look like a Substack post (expected a path like /p/<article-slug>).` | Use the address of a single post, not the home page or archive |
| `... Host 'localhost' is not a public site.` / `... is not a public address.` | Local and private-network addresses are refused on purpose |
| `... URLs with embedded credentials are not accepted.` | Remove `user:password@` from the address |
| `Too many invalid attempts. Back to the main menu.` | Three failures end the prompt; start again from the menu |

Only Substack posts can be downloaded. For another site, save the page as HTML (menu option 2) or copy the text into a file (option 3); results are "NOT VERIFIED".

## 6. Network failures

The message is `Could not download the article: <reason> Check your internet connection and the URL, then try again.` Reasons include:

| Reason | What to do |
|---|---|
| `Could not resolve host '<host>'.` | Check the spelling and your connection |
| `Network error while fetching <url>: ...` | Check the connection, VPN or firewall; retry |
| `Article not found (HTTP 404).` (or 410) | The post was moved or deleted: check the address |
| `Rate limited by the site (HTTP 429); try again later.` | Wait a while before retrying |
| `Server returned HTTP <code>.` | The site has a problem; retry later |
| `Expected an HTML page but received '<type>'.` | The address is not a web page |
| `Too many redirects (more than 5).` | The address redirects in a loop |
| `Response larger than <n> bytes; refusing to read.` | The page exceeds the 5 MB limit |

The request waits up to 20 seconds. If the browser can read the article but NoteForge cannot, use option 2 or 3.

## 7. Paywall detection

`This article is behind a paywall and no public text could be read. NoteForge AI never bypasses paywalls...` (or `The article is paywalled and no public content could be extracted.`). Nothing is created. If you are entitled to read the article, save the page from your browser as HTML and use menu option 2, or copy the text to a `.txt` file and use option 3.

`The article is not publicly accessible: ... Access denied (HTTP 401/403)`, `The site redirected to a sign-in page` and `The page appears to require signing in` mean the page needs a login: same remedy.

## 8. Partial article extraction

`Extracted : PARTIAL ...` with `[!] PARTIAL CONTENT: the notes will cover only the accessible part, and say so on every page.` The reasons are listed under it (for example a paywall component or a preview only). You are asked `Only part of the article is available. Continue with the accessible part? [Y/n]`. Continue only if the partial notes are useful to you; the document carries a red "PARTIAL CONTENT" warning everywhere. To get complete notes, supply the full text (section 7).

`No article content could be extracted from the page.` or `No article content could be found: ...` means the page had fewer than 40 usable words or an unusual layout: use option 2 or 3 with a saved copy.

## 9. Ollama problems

First check the basics in PowerShell:

```powershell
ollama --version
ollama list
```

| Message | Fix |
|---|---|
| `Ollama is not running at http://localhost:11434. Start the Ollama app (or run `ollama serve`) and try again.` (also `Ollama is not reachable at ...: it does not appear to be running.`) | Start the Ollama app, or run `ollama serve` in another window; then choose menu option `4` > `2` again |
| `Ollama is running but has no models installed (install one with `ollama pull qwen3.5:4b`).` | Run `ollama pull qwen3.5:4b` (about 3.3 GB). NoteForge AI never downloads models itself |
| `Model '<name>' is not installed in Ollama (installed: ...). Install it with `ollama pull <name>`.` | Install it, or pick an installed model with menu option `5` |
| `Ollama is not reachable ... as an Ollama server (...); something else may be using that address.` | Another program uses port 11434: stop it or fix `OLLAMA_BASE_URL` |
| `Ollama did not finish within 900s. On a CPU-only machine generation runs at only a few tokens per second; lower OLLAMA_MAX_OUTPUT_TOKENS or raise OLLAMA_TIMEOUT.` | `$env:OLLAMA_TIMEOUT = "1800"` before starting the program, use a lower detail level, turn questions off, or use Local |
| `The model's answer was cut off after <n> tokens (OLLAMA_MAX_OUTPUT_TOKENS=1500) ...` | Raise `OLLAMA_MAX_OUTPUT_TOKENS` (it must stay under half of `OLLAMA_NUM_CTX`) |
| `The prompt is too large for the model's context window ... Raise OLLAMA_NUM_CTX or use a smaller article/detail level.` | `$env:OLLAMA_NUM_CTX = "16384"` (2048-32768 allowed; larger uses more memory) or lower the detail level |
| `The model returned an empty answer (it spent its output on thinking; keep OLLAMA_THINK=false).` | Leave `OLLAMA_THINK` at `false` |
| `OLLAMA_THINK must be true, false or default, got ...` / `num_ctx must be an integer between 2048 and 32768` / `OLLAMA_TIMEOUT must be a number` | Fix the environment variable's value (see the README table) |
| `Ollama host '<host>' is not this machine. Article text would be sent over the network ...` | Only a loopback address is accepted; use `http://localhost:11434` |
| `[!] Ollama cannot be used: <reason>` then `Continue with the Local backend instead? [Y/n]` | Answer `y` for Local notes now, or `n` to go back and fix Ollama |

Environment variables last only for the current PowerShell window; set them **before** running `python src/main.py`. Remove one with `Remove-Item Env:OLLAMA_TIMEOUT`.

**Memory limitations.** The program does not measure memory. The validated model used about 3.7 GB while running. If the PC becomes very slow or Ollama stops answering: close other large programs, wait for the model to unload (`ollama ps` shows what is loaded), lower `OLLAMA_NUM_CTX`, or use the Local backend. Ollama's own error text may be shown as `Ollama returned HTTP <code>: <message>` or `Ollama request failed: <type>: <message>`.

## 10. Word generation errors

| Message | Fix |
|---|---|
| `[!] The Word document could not be created: Cannot write <name>: permission denied (is it open in Word?).` | Close the document in Word and try again |
| `... Cannot create the output folder <path>: ...` | Check the `--output-dir` path and your permissions; choose another folder |
| `... Cannot write the Word document: <reason>` | Check free disk space and that the folder is writable |
| `... Refusing to write outside the output folder.` | Internal safety check; report it |
| `Save the generated notes as a JSON file so the work is not lost? [Y/n]` | Answer `y`: the notes are saved as `<Title>_Notes.json` in the output folder |
| `[!] N problem(s) found in the generated notes:` + `Create the Word document anyway? [y/N]` | The self-check failed; the default is not to create the file. Report the problem |
| `[!] N completeness problem(s) found when re-reading the file` | The saved document was re-read and something may be missing; report it |

If Word says the file is damaged or asks to update fields, accept the field update (it refreshes the table of contents and page numbers).

## 11. Existing output filenames

Existing documents are never deleted or silently replaced. If `<Title>_Notes.docx` exists:

```
      A document named <Title>_Notes.docx already exists.
      Overwrite it? (No saves under a new name instead) [y/N]:
```

Press Enter (No) and the new file is saved as `<Title>_Notes_2.docx` (then `_3`, ...), with the line `(saved under a new name; ... already existed and was not touched)`. Answer `y` only if you really want to replace the old file.

## 12. Unicode and PowerShell input issues

| Symptom | Explanation and fix |
|---|---|
| Piped input is rejected, e.g. `'<invisible character>2' is not a menu option` | Windows PowerShell 5.1 can put a UTF-8 byte-order mark in front of piped text. This was fixed for V1.0 (the program removes a leading mark from input). If you still see it, you are running an older copy: update the project |
| Non-ASCII characters in an article title look wrong in the console | Console fonts vary; the program replaces unprintable characters instead of crashing. The Word document keeps the real characters |
| `File not found: ...` for a path with spaces | Use quotes or "Copy as path" from File Explorer; the program accepts quoted paths and `~` |
| `The file is empty` / `That is a folder, not a file` / `The file is too large (... MB; the limit is 10 MB)` / `<name> looks like a binary file, not HTML or text.` | Choose a text or HTML file under 10 MB |
| Text file shows odd characters | UTF-8, UTF-16 and Windows-1252 files are read; if neither fits, characters are replaced and the program says so |
| Scripted use | Example: `'7' \| python src/main.py --output-dir "D:\Notes"` |

## 13. Getting more information

- `noteforge.log` in the output folder lists each run (extraction, processing, generation, Ollama request timings, saved file paths).
- `pip check` and `python src/main.py --version` confirm the installation.
- `python -m pytest -o addopts="" -q` runs the test suite (about two minutes).
- Known limitations: [README, section 16](../README.md#16-known-limitations).
