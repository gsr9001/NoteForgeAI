"""Notes backends: local (default), ollama (optional, local LLM), openai (optional, paid).

Selection rules (see :func:`resolve_backend`):

* the default is ``local``; environment variables never change the default
* ``OPENAI_API_KEY`` existing in the environment does NOT activate OpenAI: the caller must ask
  for ``"openai"`` by name AND supply a confirmation handler AND a model
* an unavailable Ollama falls back to local, with the reason recorded
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from src.backends.base import (
    Availability, BackendError, BackendNotAuthorizedError, BackendResponseError, BackendUnavailableError,
    ExampleNote, GeneratedText, GenerationParams, NotesBackend, QuestionResult, QuestionSpec,
    SectionPacket, SourceSentence, TermNote, TermPacket, UnknownBackendError,
)
from src.backends.local import LocalNotesBackend
from src.backends.ollama import (
    DEFAULT_BASE_URL, DEFAULT_HOST, OllamaBackend, OllamaSettings, RequestStats, normalize_base_url,
)
from src.backends.openai_backend import ConfirmationRequest, OpenAIBackend

logger = logging.getLogger(__name__)

BACKEND_NAMES = ("local", "ollama", "openai")


@dataclass(frozen=True)
class BackendSelection:
    """The backend that will actually be used, and how it was reached."""

    backend: NotesBackend
    requested: str
    fallback_reason: Optional[str] = None

    @property
    def fell_back(self) -> bool:
        return self.fallback_reason is not None

    def describe(self) -> str:
        line = f"Active backend: {self.backend.info.display()}"
        if self.fell_back:
            line += f"\n  (requested '{self.requested}' but using local instead: {self.fallback_reason})"
        return line


def resolve_backend(
    name: str = "local",
    *,
    env: Optional[Mapping[str, str]] = None,
    confirm: Optional[Callable[[ConfirmationRequest], bool]] = None,
    ollama_model: Optional[str] = None,
    ollama_base_url: Optional[str] = None,
    ollama_host: Optional[str] = None,  # older name for ollama_base_url
    ollama_settings: Optional[OllamaSettings] = None,
    openai_model: Optional[str] = None,
) -> BackendSelection:
    """Create the requested backend, falling back to local (never to a paid service) on failure.

    ``env`` is only consulted for the backend that was explicitly requested.
    """
    requested = (name or "local").strip().lower()
    if requested not in BACKEND_NAMES:
        raise UnknownBackendError(f"Unknown backend {name!r}; choose one of: {', '.join(BACKEND_NAMES)}.")
    env = os.environ if env is None else env

    if requested == "local":
        return BackendSelection(LocalNotesBackend(), "local")

    if requested == "ollama":
        try:
            settings = ollama_settings or OllamaSettings.from_env(
                env, model=ollama_model, base_url=ollama_base_url or ollama_host)
            backend = OllamaBackend(settings=settings)
        except BackendError as exc:  # invalid configuration, or a remote server that was not allowed
            return BackendSelection(LocalNotesBackend(), "ollama", f"Invalid Ollama configuration: {exc}")
        availability = backend.check_available()
        if not availability.available:
            logger.warning("Ollama unavailable, using local backend: %s", availability.reason)
            return BackendSelection(LocalNotesBackend(), "ollama", availability.reason)
        return BackendSelection(backend, "ollama")

    # openai: explicit request only, and every precondition must hold
    model = openai_model or env.get("OPENAI_MODEL") or None
    key = env.get("OPENAI_API_KEY") or None  # read only because OpenAI was explicitly requested
    backend = OpenAIBackend(api_key=key, model=model, confirm=confirm, enabled=True)
    availability = backend.check_available()
    if not availability.available:
        return BackendSelection(LocalNotesBackend(), "openai", availability.reason)
    return BackendSelection(backend, "openai")


__all__ = [
    "Availability", "BACKEND_NAMES", "BackendError", "BackendNotAuthorizedError", "BackendResponseError",
    "BackendSelection", "BackendUnavailableError", "ConfirmationRequest", "ExampleNote", "GeneratedText",
    "DEFAULT_BASE_URL", "DEFAULT_HOST", "GenerationParams", "LocalNotesBackend", "NotesBackend", "OllamaBackend",
    "OllamaSettings", "OpenAIBackend", "RequestStats", "normalize_base_url",
    "QuestionResult", "QuestionSpec", "SectionPacket", "SourceSentence", "TermNote", "TermPacket",
    "UnknownBackendError", "resolve_backend",
]
