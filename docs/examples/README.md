# Example documents

Three Word documents showing what NoteForge AI produces. They are **demonstrations only**.

| File | What it shows | How it was made |
|---|---|---|
| `EXAMPLE_Local_Intermediate_RAG.docx` | **Local backend** (the default), Intermediate detail, complete article, with questions | Generated on 2026-11-07 with the V1.0 candidate code from the repository's test article `tests/fixtures/substack_free.html`, through the same notes and Word generators the program uses. No AI model involved |
| `EXAMPLE_Ollama_Basic_RAG_Phase8_build.docx` | **Ollama backend** (`qwen3.5:4b`), Basic detail, no questions: purple "AI-GENERATED" boxes and the Chapter 4 warning "NOT from the article" | A byte-for-byte **copy** of `output/Understanding_Retrieval-Augmented_Generation_Notes_5.docx`, a real Ollama run from Phase 8 (2026-11-06). It was made **before** the Phase 8.1 / 8.2 safeguards, so it still contains two AI sentences that later checks would remove ("...instead of complex vector databases", "...rather than being sent to external servers"). It illustrates why AI content needs human review |
| `EXAMPLE_Partial_Local_Paywalled.docx` | **Partial article** handling: red "PARTIAL CONTENT" banner on the cover, in the header and in each chapter | Generated on 2026-11-07 (Local backend, Intermediate) from the repository's test article `tests/fixtures/substack_paywalled.html` |

## About the content

- All three articles are **synthetic test fixtures** written for this project (fictional authors and publications). They are not real Substack posts, contain no private or personal information, and no API keys. The document properties show the author "NoteForge AI".
- The source URL in the Ollama example (`appliedai.substack.com/p/understanding-rag`) is a fictional address used by the project's tests.
- The Local and Partial examples were generated, not copied. The Ollama example is an untouched copy; the original in `output/` was not modified (SHA-256 identical).
- Nothing here was made from a real third-party article, so the files are safe to show and share as demonstrations.

Open them in Microsoft Word; if Word offers to update fields, accept (it refreshes the table of contents and page numbers). Back to the [README](../../README.md) | [User Guide](../USER_GUIDE.md#6-reading-the-notes-labels-and-warnings).
