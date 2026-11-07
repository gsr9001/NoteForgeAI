# NoteForge AI - User Guide

This guide explains NoteForge AI from a user's point of view. No programming knowledge is needed. If you have not installed the program yet, start with the [Installation Guide](INSTALLATION_GUIDE.md).

Back to the [README](../README.md) | [Backend Guide](BACKEND_GUIDE.md) | [Troubleshooting](TROUBLESHOOTING.md)

## 1. What NoteForge AI does

You give it an article. It gives you a Word document of study notes. In the notes, **every item shows where it came from**: the article's own words, text NoteForge assembled by fixed rules, or (only if you choose the optional Ollama backend) text written by a language model. Only **Substack** articles can be downloaded from a web address; you can also supply a saved HTML page or a plain-text file.

## 2. Starting the program

1. Open Windows PowerShell and go to the project folder:

```powershell
cd "C:\Users\<you>\Desktop\ClaudeCode\NoteForge AI"
.\.venv\Scripts\Activate.ps1
python src/main.py
```

2. You will see the banner and the main menu:

```
============================================================
  NOTEFORGE AI   v0.1.0
  Turn articles into professional Word study notes
============================================================

Backend: Local (free, offline) | Detail: Intermediate | Output: C:\...\NoteForge AI\output

  1. Generate notes from Substack URL.
  2. Generate notes from HTML file.
  3. Generate notes from text file.
  4. Select AI backend.
  5. View available models.
  6. View generated documents.
  7. Exit.

Choose an option (1-7):
```

The status line shows the current backend, detail level and where documents will be saved. Type a number and press Enter. (Typing `exit` or `quit` also leaves the program.)

**Going back:** at any question you can type `b`, `back`, `cancel` or `q` and press Enter to return to the menu. **Ctrl+C** cancels the current action and returns to the menu without creating a document; pressing it at the menu itself exits the program.

## 3. Your first notes, step by step

### Step 1 - choose the source (option 1)

Type `1`. Enter the address of a public Substack post, for example:

```
Article URL (for example https://name.substack.com/p/article-title): https://on.substack.com/p/what-you-get-when-you-start-a-substack
```

The address must start with `https`, and the path must look like `/p/<article-title>`. If it is not accepted you see a message such as:

```
[!] That input is not usable: URL does not look like a Substack post (expected a path like /p/<article-slug>).
```

You get three attempts, then the program returns to the menu.

### Step 2 - choose the detail level

```
Notes detail level:
  1. Basic
  2. Intermediate  <- current
  3. Advanced
Detail (1-3 or name) [Intermediate]:
```

Press Enter to keep the level in brackets, or type `1`, `2`, `3` or the name.

| Level | Meaning |
|---|---|
| Basic | Shortest notes (up to 8 key terms, 6 takeaways) |
| Intermediate | Balanced; the default (up to 15 key terms, 10 takeaways) |
| Advanced | Longest notes (up to 25 key terms, 16 takeaways) |

### Step 3 - questions

```
Include questions (10 short-answer, 5 descriptive, 10 multiple-choice)? [Y/n]:
```

Press Enter for yes. The numbers are what the program *tries* to create. If the article does not contain enough different facts, you get fewer, and Chapter 7 says so. The program never repeats a fact just to reach a number.

### Step 4 - the backend

The default is **Local** (free, offline, fast). To switch, go back to the menu and use option `4` before generating (see section 5).

### Step 5 - watch the progress

```
Backend : Local - free, runs on this machine
Detail  : Intermediate   Questions: yes   Source: URL https://...
The local backend is fast; this should take a few seconds.
[1/6] Reading and extracting the article ...
      Title     : What you get when you start a substack
      Author    : On Substack   Published: 2019-04-12
      Extracted : COMPLETE - 832 words in 51 blocks (trafilatura)
[2/6] Processing the content ...
      20 sections, 2 definitions, 40 key terms found.
[3/6] Writing the notes with Local ...
      Notes written in 0s: 1,377 words, 17 questions.
[4/6] Checking the notes (structure and provenance) ...
      OK: every quoted passage is from the article and the structure is valid.
[5/6] Creating the Word document ...
[6/6] Done.
```

Step 4 checks that every quoted passage really occurs in the article.

### Step 6 - find your document

```
Your document is ready:
  C:\Users\<you>\Desktop\ClaudeCode\NoteForge AI\output\What_you_get_when_you_start_a_substack_Notes.docx
  File name : What_you_get_when_you_start_a_substack_Notes.docx   Size: 47 KB
  Made with : Local | Source status: COMPLETE
```

Documents are saved in the `output` folder of the project (or the folder you gave with `--output-dir`). Back at the menu, option `6` lists all documents there, newest first, and lets you type a number to open one in Word.

## 4. Understanding extraction completeness

After reading the article the program prints one of three states:

| State | Meaning | What to do |
|---|---|---|
| **COMPLETE** | The whole public article was read | Nothing |
| **PARTIAL** | Only part was available (for example a paid post whose rest is behind a paywall) | Read the warning below |
| **NOT VERIFIED** | You supplied the text yourself (HTML or text file), so completeness cannot be checked | Make sure your file contains the whole article |

### Paywalls and partial articles

NoteForge AI **never bypasses a paywall** and never logs in. For a partly available article you see:

```
      Extracted : PARTIAL - 772 words in ...
      [!] PARTIAL CONTENT: the notes will cover only the accessible part, and say so on every page.
          - <reason>
    Only part of the article is available. Continue with the accessible part? [Y/n]:
```

Answer `y` (Enter) to continue; answer `n` to cancel. A document made from a partial article carries a red "PARTIAL CONTENT" banner on its cover, in every page header and at the top of each chapter. It must not be treated as a complete study of the article.

If **no** public text could be read you see:

```
[!] This article is behind a paywall and no public text could be read. NoteForge AI never bypasses paywalls. If you have legitimate access, save the page as an HTML file (menu option 2) or copy the text into a .txt file (menu option 3).
```

Do exactly that: open the article in your browser (signed in, if you are entitled to read it), use **Save page as...** to save the HTML, then choose option `2` and enter the file path. Option `3` takes a plain-text file; you can type a title, author and date (YYYY-MM-DD), all optional.

## 5. Choosing Local or Ollama

- **Local** (default): fast, free, offline. It quotes and reorganises the article. It does not write new explanations or analogies.
- **Ollama** (optional, experimental): a language model on your own PC adds explanations, analogies and questions. It takes minutes, needs memory, and its text must be reviewed.

Menu option `4`:

```
Select the notes backend  (Enter keeps the current choice, 'b' goes back)

  1. Local   - free, offline, no AI model. Quotes and reorganises the article.  <- current
  2. Ollama  - a language model running on this PC (model: qwen3.5:4b). Adds explanations,
               analogies and AI-written questions, always labelled. Slower.

  (Paid cloud services are not available from this menu.)
Backend (1-2):
```

Selecting Ollama checks that it is running and the model is installed. Option `5` ("View available models") lists installed Ollama models and lets you pick one by number. The selection lasts for the current session only (the program always starts on Local). Before generating, Ollama shows a time estimate and prints a "still generating" line every minute; **Ctrl+C cancels**. If Ollama is not usable you are asked:

```
[!] Ollama cannot be used: <reason>
    Continue with the Local backend instead? [Y/n]:
```

Nothing falls back to Local without asking you. More in the [Backend Guide](BACKEND_GUIDE.md).

## 6. Reading the notes: labels and warnings

Open the document in Word. The first pages are a cover page, "Article Information and Source Details", "Warnings and Limitations" and "How to Read These Notes", then a table of contents and seven chapters:

1. Article Overview  2. Detailed Concept Explanation  3. Important Technical Keywords  4. Real-Life Analogies and Applications  5. Key Takeaways  6. Quick Revision Notes  7. Questions and Answers (with an answer key and explanations). The document ends with the original-article references and a provenance summary.

### Colour and label legend

| You see | It is |
|---|---|
| Blue bar, plain text | The article's own words (quoted) |
| Grey bar | Text NoteForge assembled by rules (for example "Also written as", structure maps) |
| Purple box labelled **"AI-GENERATED"** | Written by a language model (Ollama backend only) |
| Red box "WARNING: The items below are NOT from the article" | The start of Chapter 4's AI analogies and applications |
| `[¶7]`, `[¶3-5]` | The article paragraph numbers the item relies on. AI items say "based on ¶..." |

### Review warnings

- **"Review needed"** (in the "Warnings and Limitations" page, and in an analogy's label) means a person should check that item against the article before relying on it. It appears when an AI sentence is only partly supported by the article's wording, or when an AI analogy compares the idea with something the article never mentions.
- Sentences the program judges unsupported, and invented numbers or "studies show" claims, are **removed** and a notice says so.
- This is a wording check only. **It does not prove a statement is true.** Treat all AI-generated text as a draft.

## 7. Example workflow

*Goal: study notes from a public article, Local backend.*

1. `python src/main.py` -> type `1`.
2. Paste the article address, press Enter. Press Enter for Intermediate and Enter for questions.
3. Wait a few seconds; note `Extracted : COMPLETE`.
4. Copy the path printed after "Your document is ready:".
5. Open it in Word. Read the legend page first, then the table of contents.
6. If Word shows "Update field?" or the page numbers look wrong, right-click the table of contents and choose **Update Field**.

*Partial article example:* a paywalled post prints `PARTIAL`, you accept, and the document opens with a red warning on the cover. A ready-made demonstration is in [docs/examples](examples/README.md).

## 8. Errors and cancellation

| What happens | What you see | What to do |
|---|---|---|
| Invalid address | `[!] That input is not usable: ...` | Enter a `https://.../p/<title>` address, or `b` to go back |
| No internet | `Could not download the article: ... Check your internet connection and the URL, then try again.` | Check the connection and retry |
| Sign-in required | `The article is not publicly accessible: ...` | Save the page as HTML (option 2) or copy the text (option 3) |
| File already exists | `A document named ... already exists.` then `Overwrite it? (No saves under a new name instead) [y/N]:` | Enter keeps the old file and saves a new one as `_2`, `_3`... |
| File open in Word | `Cannot write ...: permission denied (is it open in Word?).` | Close the document and try again |
| Word step failed | `Save the generated notes as a JSON file so the work is not lost? [Y/n]` | Answer `y` to keep your work |
| Notes failed the self-check | `[!] N problem(s) found in the generated notes:` then `Create the Word document anyway? [y/N]` | Normally answer `n` and report the problem |
| Ctrl+C | `Cancelled by Ctrl+C. No document was created. Back to the main menu.` | Start again when ready |

An unexpected error never closes the program: it prints `[!] ...` and returns to the menu. Details go to `noteforge.log` in the output folder. More: [Troubleshooting](TROUBLESHOOTING.md).

## 9. Good to know

- Existing documents are never deleted or silently overwritten.
- The same article can be run again at another detail level; each run saves a new file.
- Use the notes for personal study and respect the article author's rights.
