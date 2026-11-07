"""OpenAI backend: PAID, optional, and only ever used on explicit opt-in (Phase 5).

Safeguards, all enforced here and covered by tests:

* never chosen automatically: constructing it requires ``enabled=True`` and an explicit model
* the API key is passed in by the caller; this module never reads environment variables
* ``confirm`` is called before EVERY request and must return True, otherwise nothing is sent
* a hard cap on requests per instance (``max_calls``)
* the ``openai`` package is imported lazily, only when a confirmed call is about to be made
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from src.backends.base import Availability, BackendError, BackendNotAuthorizedError, BackendUnavailableError
from src.backends.llm import LLMBackend
from src.notes_model import BackendInfo, BackendKind

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfirmationRequest:
    """What the user is asked to approve before a paid request is sent."""

    model: str
    purpose: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    call_number: int
    max_calls: int

    def describe(self) -> str:
        return (f"OpenAI request #{self.call_number} of at most {self.max_calls}: {self.purpose} "
                f"(model {self.model}; ~{self.estimated_input_tokens} input tokens, up to "
                f"~{self.estimated_output_tokens} output tokens). This is a PAID API call and sends "
                "article text to OpenAI.")


class OpenAIBackend(LLMBackend):
    def __init__(
        self,
        api_key: Optional[str],
        model: Optional[str],
        confirm: Optional[Callable[[ConfirmationRequest], bool]],
        enabled: bool = False,
        client: Any = None,
        max_calls: int = 12,
        estimated_output_tokens: int = 2000,
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self.model = model
        self._confirm = confirm
        self.enabled = enabled
        self._client = client
        self.max_calls = max_calls
        self.estimated_output_tokens = estimated_output_tokens
        self.calls_made = 0

    def __repr__(self) -> str:  # never reveal the key
        return f"OpenAIBackend(model={self.model!r}, enabled={self.enabled}, calls_made={self.calls_made})"

    @property
    def info(self) -> BackendInfo:
        return BackendInfo(
            name="OpenAI", kind=BackendKind.OPENAI, model=self.model, is_local=False, is_paid=True,
            description="paid API; article text is sent to OpenAI after each confirmation; output is labelled AI-generated",
        )

    def check_available(self) -> Availability:
        """Checks configuration only. Makes no network request and costs nothing."""
        if not self.enabled:
            return Availability(False, "The OpenAI backend was not explicitly enabled.")
        if not self.model:
            return Availability(False, "No OpenAI model was specified.")
        if not (self._api_key or self._client):
            return Availability(False, "No OpenAI API key was provided.")
        if self._confirm is None:
            return Availability(False, "No confirmation handler was provided; paid calls need explicit approval.")
        return Availability(True)

    def _complete(self, system: str, user: str, purpose: str) -> str:
        availability = self.check_available()
        if not availability.available:
            raise BackendNotAuthorizedError(availability.reason)
        if self.calls_made >= self.max_calls:
            raise BackendNotAuthorizedError(f"Call limit reached ({self.max_calls} requests); no further requests sent.")
        request = ConfirmationRequest(
            model=str(self.model), purpose=purpose,
            estimated_input_tokens=(len(system) + len(user)) // 4,
            estimated_output_tokens=self.estimated_output_tokens,
            call_number=self.calls_made + 1, max_calls=self.max_calls,
        )
        assert self._confirm is not None
        if not self._confirm(request):
            raise BackendNotAuthorizedError("The OpenAI request was declined; nothing was sent.")
        client = self._get_client()
        self.calls_made += 1
        logger.info("OpenAI request %d (%s), model %s", self.calls_made, purpose, self.model)
        try:
            resp = client.chat.completions.create(
                model=self.model, temperature=0,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
            content = resp.choices[0].message.content
        except BackendError:
            raise
        except Exception as exc:  # SDK errors vary widely by version
            raise BackendError(f"OpenAI request failed: {type(exc).__name__}") from exc
        if not content:
            raise BackendError("OpenAI returned an empty response.")
        return str(content)

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI  # imported only once a confirmed call is about to happen
            except ImportError as exc:
                raise BackendUnavailableError(
                    "The 'openai' package is not installed (pip install -r requirements-openai.txt)."
                ) from exc
            self._client = OpenAI(api_key=self._api_key)
        return self._client
