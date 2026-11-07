# NoteForge AI - Backend Guide: Local versus Ollama

A *backend* is the part of NoteForge AI that writes the notes' content once the article has been extracted and analysed. You choose it with menu option `4`. Two backends are available from the program:

| | **Local** (default) | **Ollama** (optional, experimental) |
|---|---|---|
| Needs an AI model | No | Yes: a model served by Ollama on your own PC (validated: `qwen3.5:4b`) |
| Needs internet | Only to download the article | Only to download the article; the model runs on your PC |
| Cost | Free | Free (runs on your hardware) |
| Speed | A few seconds | Minutes (see section 3) |
| Memory | Small | About 3.7 GB in testing, on a 16 GB PC |
| Writes new explanations/analogies | **No** | Yes, labelled "AI-generated" |
| Content comes from | The article's own sentences, re-organised by fixed rules | The article **plus** text written by the model |
| Repeatable | Same input, same notes | Not repeatable: model output varies |
| Needs human review | Light (it only quotes) | **Yes, always** |
| Status | Supported default for V1.0 | Experimental |

Back to the [README](../README.md) | [User Guide](USER_GUIDE.md) | [Troubleshooting](TROUBLESHOOTING.md)

## 1. Local backend (the default)

- **Always the starting choice.** Every session starts on Local; nothing in the environment changes that.
- **Runs without any external LLM or API.** It does not call any model, paid or free.
- **Fast.** Notes for the articles in the Phase 9 tests (659 and 832 words) were written in about a second (the program reported 0 s for the notes step, and 1 s in total including the download).
- **Organises and extracts.** It finds definitions, key terms, conclusions, recommendations and facts in the article and re-presents them in the seven chapters. Questions and answers are built from the article's own sentences, with source references.
- **Does not write new explanations or analogies.** Chapter 4 therefore shows the notice that the local backend cannot create new examples without inventing content, and Chapter 3 shows the article's definitions rather than a model's explanation. This is deliberate: nothing in a Local document is invented.
- **Limits:** it works only with what the article states. If the article never defines a term, the notes say so rather than guess. Question counts may be below 10/5/10 when the article has little distinct material.

Choose Local when you want reliable, quick, fully traceable notes.

## 2. Ollama backend (optional, experimental)

- **What it is:** Ollama is a separate free program that runs a language model on your PC. NoteForge AI talks to it only at your own machine's address (`http://localhost:11434` by default). A non-local address is refused so the article text cannot leave your computer.
- **Which model:** the validated model is `qwen3.5:4b`. The program uses the model you select (menu option `5`, or the `OLLAMA_MODEL` environment variable as the starting choice). It never downloads a model itself; install one with `ollama pull qwen3.5:4b`.
- **What it adds:** AI-written explanations of key terms and of sections, analogies and applications (Chapter 4), and AI-written questions. These items are marked "AI-GENERATED" (purple boxes) and point to the article paragraphs they were based on. The article's own quoted content and the rule-built content remain in the notes and keep their own labels.
- **What it checks:** a built-in wording check compares the AI's sentences with the article. Unsupported statements (for example an invented number, a "studies show" claim, or a comparison with something the article never mentions) are removed; partly supported statements are kept with a "Review needed" warning; AI analogies that contrast the idea with something outside the article are kept but marked "Review needed". AI questions are limited to a few per source passage and carry source references.
- **What it does not do:** the check compares *vocabulary*. It cannot tell whether a statement is true. A wrong statement phrased in the article's own words can pass, and a correct paraphrase can be flagged.

### Content from Ollama is experimental and not guaranteed to be factually correct

Treat every "AI-GENERATED" item as a draft written by a small model. Check it against the article (the `[¶n]` references tell you where) before you rely on it. The Phase 8 test run produced two sentences that were not in the source ("... instead of complex vector databases", "... rather than being sent to external servers"); the safeguards added afterwards would remove them, but the filter cannot catch every case.

### Time and memory (measured, small sample)

All figures are from one CPU-only 16 GB PC with `qwen3.5:4b`, one 513-word article, **Basic detail, no questions**:

| Run | Result |
|---|---|
| Phase 8 | 4 requests, about 4 min 20 s total, about 6-7 tokens per second |
| Phase 8.1 | 4 requests, 201 s (3.4 min) |
| Memory | about 3.7 GB used by the model while running; released after the run |

Intermediate and Advanced detail, and generation with questions, were **not measured**. The program prints its own estimate before starting (for example "may take roughly 10-30+ minutes"); treat it as guidance only. Requests run one at a time; a "still generating" line appears about every minute; Ctrl+C cancels.

### Requirements for a good run

1. Ollama is installed and running ([Installation Guide, section 8](INSTALLATION_GUIDE.md#8-optional-ollama-experimental-ai-backend)).
2. The model is installed (`ollama list` shows it).
3. Enough free memory: close large programs first. The program does not check memory itself; the validated model used about 3.7 GB, and during testing a live run was only started with at least 4 GB free as a safety margin.
4. Time. The default reply timeout is 900 seconds per request (`OLLAMA_TIMEOUT`).

### If Ollama cannot be used

The program tells you why and asks `Continue with the Local backend instead? [Y/n]`. It never switches silently. Typical messages and fixes: [Troubleshooting, section 9](TROUBLESHOOTING.md#9-ollama-problems).

## 3. Choosing between them

| If you want... | Use |
|---|---|
| Fast, repeatable, fully traceable notes | Local |
| Plain study notes and questions taken from the article | Local |
| Extra explanations and analogies, and you will review them | Ollama |
| To run unattended or on a low-memory PC | Local |

A sensible routine: make Local notes first; make Ollama notes only if you want the extra explanations, then read the "Warnings and Limitations" page and every "Review needed" item.

## 4. Paid services

No paid cloud service can be reached from NoteForge AI's menu, and the program never reads `OPENAI_API_KEY`. The source tree contains an OpenAI backend module that is **not reachable** from the command-line program and was never tested against the real service.
