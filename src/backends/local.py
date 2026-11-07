"""Local backend: free, offline, deterministic. The default. Uses no AI model (Phase 5)."""
from __future__ import annotations

import logging
from typing import Sequence

from src.backends.base import (
    ExampleNote, GeneratedText, GenerationParams, NotesBackend, QuestionResult, QuestionSpec,
    SectionPacket, TermNote, TermPacket,
)
from src.backends.local_questions import LocalQuestionBuilder
from src.content_processor import ProcessedContent
from src.notes_model import BackendInfo, BackendKind, NoteProvenance

logger = logging.getLogger(__name__)


class LocalNotesBackend(NotesBackend):
    """Reorganises and quotes the article. It cannot write new prose, so it never pretends to.

    * keyword explanations are the article's own sentences about the term (ORIGINAL)
    * section explanations and analogies are not produced
    * questions are assembled from the article's definitions, statements, tables and lists
    """

    supports_translation = False

    @property
    def info(self) -> BackendInfo:
        return BackendInfo(
            name="Local", kind=BackendKind.LOCAL, model=None, is_local=True, is_paid=False,
            description="deterministic and offline; quotes and reorganises the article, no AI model used",
        )

    def explain_keywords(
        self, content: ProcessedContent, packets: Sequence[TermPacket], params: GenerationParams
    ) -> dict[str, TermNote]:
        out: dict[str, TermNote] = {}
        for p in packets:
            explanation = tuple(
                GeneratedText(s.text, NoteProvenance.ORIGINAL, (s.source_index,))
                for s in p.contexts[: params.contexts_per_term]
            )
            significance = tuple(
                GeneratedText(s.text, NoteProvenance.ORIGINAL, (s.source_index,))
                for s in p.significance_contexts[:2]
            )
            out[p.term] = TermNote(explanation, significance)
        return out

    def explain_sections(
        self, content: ProcessedContent, packets: Sequence[SectionPacket], params: GenerationParams
    ) -> dict[str, tuple[GeneratedText, ...]]:
        return {}

    def create_analogies(
        self, content: ProcessedContent, packets: Sequence[TermPacket], params: GenerationParams
    ) -> list[ExampleNote]:
        logger.info("Local backend does not generate analogies (would require inventing content)")
        return []

    def create_questions(
        self, content: ProcessedContent, spec: QuestionSpec, params: GenerationParams
    ) -> QuestionResult:
        return LocalQuestionBuilder(content, spec).build()
