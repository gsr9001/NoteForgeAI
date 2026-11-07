"""Ollama backend: a local language model, free to run, nothing leaves this machine.

Tuned and verified against a real server running ``qwen3.5:4b`` on a 16 GB CPU-only PC.
Behaviour that was confirmed against the live API (not assumed):

* Qwen 3.5 **thinks by default** (34 s for "17 + 25" vs 2.8 s with thinking off). Thinking is
  switched off with the TOP-LEVEL ``think: false`` field. ``options.think`` is silently ignored.
* ``format`` accepts a JSON schema and works together with ``think: false``.
* Ollama's default context window is 4096 tokens. Always send ``num_ctx`` explicitly.
* A prompt longer than ``num_ctx`` is **silently truncated** (HTTP 200, no error), so prompt size
  is checked before sending.
* An unknown model gives HTTP 404 ``{"error": "model '...' not found"}``.

Configuration (environment variables, all optional; see ``OllamaSettings.from_env``):
``OLLAMA_BASE_URL`` (preferred) or ``OLLAMA_HOST``, ``OLLAMA_MODEL``, ``OLLAMA_NUM_CTX``,
``OLLAMA_TEMPERATURE``, ``OLLAMA_THINK``, ``OLLAMA_KEEP_ALIVE``, ``OLLAMA_MAX_OUTPUT_TOKENS``,
``OLLAMA_TIMEOUT``.
"""
from __future__ import annotations

import dataclasses
import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

import requests

from src.backends.base import Availability, BackendError, BackendResponseError, BackendUnavailableError
from src.backends.llm import LLMBackend
from src.notes_model import BackendInfo, BackendKind

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_HOST = DEFAULT_BASE_URL  # backwards-compatible name from Phase 5
DEFAULT_PORT = 11434

#: One generation at a time per process. A CPU-only machine gains nothing from parallel requests
#: (they just compete for the same cores and RAM) and the server may be shared by several backends.
_GENERATION_LOCK = threading.Lock()

_UNSET: Any = object()
_KEEP_ALIVE = re.compile(r"^(?:-1|0|\d+(?:\.\d+)?(?:ms|s|m|h)?)$")


# =========================================================================== helpers
def _is_loopback(host: str) -> bool:
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_base_url(raw: Optional[str]) -> str:
    """Turn what users type into a usable base URL.

    Ollama's own ``OLLAMA_HOST`` convention allows ``127.0.0.1:11434``, ``0.0.0.0`` and a bare host,
    so those are accepted: a missing scheme becomes http, a missing port becomes 11434, and the bind
    addresses ``0.0.0.0`` / ``::`` are mapped to loopback (they are not valid destinations).
    """
    text = (raw or "").strip()
    if not text:
        return DEFAULT_BASE_URL
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BackendError(f"Invalid Ollama URL {raw!r}; expected something like {DEFAULT_BASE_URL}.")
    host = parts.hostname
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    host_part = f"[{host}]" if ":" in host else host
    try:
        port = parts.port
    except ValueError as exc:
        raise BackendError(f"Invalid port in Ollama URL {raw!r}.") from exc
    netloc = f"{host_part}:{port or DEFAULT_PORT}"
    path = parts.path.rstrip("/")
    if path.endswith("/api"):
        path = path[: -len("/api")]
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def estimate_tokens(text: str) -> int:
    """Deliberately pessimistic token estimate: 1 token per 3 ASCII characters, 1 per other character.

    Latin text averages about 4 characters per token, so this over-counts English a little; Telugu and
    other scripts are far denser, which the per-character rule covers. Over-counting is the safe direction.
    """
    ascii_chars = len(text.encode("ascii", "ignore"))
    return ascii_chars // 3 + (len(text) - ascii_chars)


def _error_text(resp: Any) -> str:
    """The server's own error message if it sent one."""
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
    except Exception:  # noqa: BLE001 - any failure to parse just means "no message"
        pass
    return str(getattr(resp, "text", "") or "")[:200]


# =========================================================================== settings
@dataclass(frozen=True)
class OllamaSettings:
    """Validated Ollama configuration. Defaults suit a 16 GB, CPU-only machine running qwen3.5:4b."""

    base_url: str = DEFAULT_BASE_URL
    model: Optional[str] = None  # None: use the first installed model
    num_ctx: int = 8192  # ~3.1 GB for this model; leaves headroom on 16 GB
    temperature: float = 0.2  # low: notes should follow the article, not improvise
    think: Optional[bool] = False  # False = routine generation; None = leave to the model's default
    keep_alive: str = "10m"  # keep the model in RAM between the calls of one notes run
    max_output_tokens: int = 1500  # ~4 min at the ~6 tokens/s measured on this CPU
    timeout: float = 900.0
    allow_remote: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))
        if not isinstance(self.num_ctx, int) or isinstance(self.num_ctx, bool) or not 2048 <= self.num_ctx <= 32768:
            raise BackendError(f"num_ctx must be an integer between 2048 and 32768, got {self.num_ctx!r}.")
        if not isinstance(self.temperature, (int, float)) or isinstance(self.temperature, bool) \
                or not 0.0 <= self.temperature <= 0.3:
            raise BackendError(f"temperature must be between 0 and 0.3 (higher invites invented details in "
                               f"notes), got {self.temperature!r}.")
        if not isinstance(self.max_output_tokens, int) or isinstance(self.max_output_tokens, bool) \
                or not 64 <= self.max_output_tokens <= 4096 or self.max_output_tokens >= self.num_ctx // 2:
            raise BackendError(f"max_output_tokens must be 64-4096 and less than half of num_ctx, "
                               f"got {self.max_output_tokens!r}.")
        if not isinstance(self.keep_alive, str) or not _KEEP_ALIVE.match(self.keep_alive.strip()):
            raise BackendError(f"keep_alive must look like '10m', '30s', '1h', '0' or '-1', got {self.keep_alive!r}.")
        if not isinstance(self.timeout, (int, float)) or isinstance(self.timeout, bool) or self.timeout <= 0:
            raise BackendError(f"timeout must be a positive number of seconds, got {self.timeout!r}.")
        if self.think is not None and not isinstance(self.think, bool):
            raise BackendError(f"think must be True, False or None, got {self.think!r}.")

    @classmethod
    def from_env(cls, env: Mapping[str, str], **overrides: Any) -> "OllamaSettings":
        """Build settings from environment variables, with explicit keyword overrides taking priority.

        ``OLLAMA_BASE_URL`` is preferred; ``OLLAMA_HOST`` (the name Ollama itself and earlier NoteForge
        versions use) is accepted as a fallback. If both are set and differ, BASE_URL wins.
        """
        def get(name: str) -> Optional[str]:
            value = env.get(name)
            return value.strip() if isinstance(value, str) and value.strip() else None

        base, legacy = get("OLLAMA_BASE_URL"), get("OLLAMA_HOST")
        if base and legacy and normalize_base_url(base) != normalize_base_url(legacy):
            logger.warning("Both OLLAMA_BASE_URL and OLLAMA_HOST are set and differ; using OLLAMA_BASE_URL.")
        values: dict[str, Any] = {"base_url": base or legacy or DEFAULT_BASE_URL, "model": get("OLLAMA_MODEL")}

        def number(name: str, kind: type) -> Any:
            raw = get(name)
            if raw is None:
                return _UNSET
            try:
                return kind(raw)
            except ValueError as exc:
                raise BackendError(f"{name} must be a number, got {raw!r}.") from exc

        for key, var, kind in (("num_ctx", "OLLAMA_NUM_CTX", int), ("temperature", "OLLAMA_TEMPERATURE", float),
                               ("max_output_tokens", "OLLAMA_MAX_OUTPUT_TOKENS", int), ("timeout", "OLLAMA_TIMEOUT", float)):
            parsed = number(var, kind)
            if parsed is not _UNSET:
                values[key] = parsed
        if get("OLLAMA_KEEP_ALIVE"):
            values["keep_alive"] = get("OLLAMA_KEEP_ALIVE")
        think = get("OLLAMA_THINK")
        if think is not None:
            lowered = think.lower()
            if lowered in ("true", "1", "yes", "on"):
                values["think"] = True
            elif lowered in ("false", "0", "no", "off"):
                values["think"] = False
            elif lowered in ("default", "auto", "model"):
                values["think"] = None
            else:
                raise BackendError(f"OLLAMA_THINK must be true, false or default, got {think!r}.")
        values.update({k: v for k, v in overrides.items() if v is not None and v is not _UNSET})
        return cls(**values)


@dataclass
class RequestStats:
    """Timing of one generation request, for reporting and tuning."""

    purpose: str
    wall_s: float
    wait_s: float  # time spent waiting for the one-at-a-time lock
    load_s: float
    prompt_tokens: int
    output_tokens: int
    tokens_per_s: float
    done_reason: str


# =========================================================================== backend
class OllamaBackend(LLMBackend):
    """Notes backend using a local Ollama server.

    Remote servers are refused unless ``allow_remote=True``, because article text would otherwise
    leave this machine. Generation requests are serialised with a process-wide lock.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        host: Optional[str] = None,
        timeout: Optional[float] = None,
        allow_remote: Optional[bool] = None,
        session: Optional[requests.Session] = None,
        *,
        settings: Optional[OllamaSettings] = None,
        base_url: Optional[str] = None,
        num_ctx: Optional[int] = None,
        temperature: Optional[float] = None,
        think: Any = _UNSET,
        keep_alive: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
    ) -> None:
        super().__init__()
        changes: dict[str, Any] = {k: v for k, v in {
            "model": model, "base_url": base_url or host, "timeout": timeout, "allow_remote": allow_remote,
            "num_ctx": num_ctx, "temperature": temperature, "keep_alive": keep_alive,
            "max_output_tokens": max_output_tokens}.items() if v is not None}
        if think is not _UNSET:
            changes["think"] = think
        self.settings = dataclasses.replace(settings or OllamaSettings(), **changes)
        parts = urlsplit(self.settings.base_url)
        if not self.settings.allow_remote and not _is_loopback((parts.hostname or "").lower()):
            raise BackendError(
                f"Ollama host {parts.hostname!r} is not this machine. Article text would be sent over the "
                "network; pass allow_remote=True only if you trust that server."
            )
        self.host = self.settings.base_url
        self.model = self.settings.model
        self.timeout = self.settings.timeout
        self.allow_remote = self.settings.allow_remote
        self._session = session or requests.Session()
        self._availability: Optional[Availability] = None
        self._think_supported = True
        self.stats: list[RequestStats] = []
        # Keep every prompt inside the context window (Ollama truncates silently): leave room for the
        # answer and for the fixed instructions, and use the pessimistic 3 characters per token.
        room = self.settings.num_ctx - self.settings.max_output_tokens - 700
        self.max_prompt_chars = max(1000, min(LLMBackend.max_prompt_chars, room * 3))

    @property
    def info(self) -> BackendInfo:
        return BackendInfo(
            name="Ollama", kind=BackendKind.OLLAMA, model=self.model,
            is_local=_is_loopback((urlsplit(self.host).hostname or "").lower()),
            is_paid=False, description="local language model via Ollama; AI-generated text is labelled",
        )

    # ------------------------------------------------------------------ availability
    def check_available(self) -> Availability:
        """Is the server running and the model installed? Free and local; only success is cached."""
        if self._availability is not None:
            return self._availability
        result = self._probe()
        if result.available:
            self._availability = result
        return result

    def _probe(self) -> Availability:
        try:
            resp = self._session.get(f"{self.host}/api/tags", timeout=3)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("models", []), list):
                raise ValueError("unexpected response shape")  # something else is listening on this port
            names = [m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)]
        except requests.ConnectionError:
            return Availability(False, f"Ollama is not reachable at {self.host}: it does not appear to be running. "
                                       "Start the Ollama app (or run `ollama serve`) and try again.")
        except requests.Timeout:
            return Availability(False, f"Ollama at {self.host} is not reachable: the server did not respond within 3 seconds.")
        except (requests.RequestException, ValueError) as exc:
            return Availability(False, f"Ollama is not reachable at {self.host} as an Ollama server "
                                       f"({type(exc).__name__}); something else may be using that address.")
        if not names:
            return Availability(False, "Ollama is running but has no models installed "
                                       "(install one with `ollama pull qwen3.5:4b`).")
        if self.model is None:
            self.model = names[0]
            logger.info("No OLLAMA_MODEL set; using the first installed model: %s", self.model)
            return Availability(True, f"using installed model {self.model}")
        if any(n == self.model or n == f"{self.model}:latest" or (":" not in self.model and n.split(":")[0] == self.model)
               for n in names):
            return Availability(True)
        return Availability(False, f"Model {self.model!r} is not installed in Ollama (installed: {', '.join(names)}). "
                                   f"Install it with `ollama pull {self.model}`.")

    # ------------------------------------------------------------------ generation
    def _complete(self, system: str, user: str, purpose: str) -> str:
        return self._complete_json(system, user, purpose, None)

    def _complete_json(self, system: str, user: str, purpose: str, schema: Optional[dict[str, Any]]) -> str:
        s = self.settings
        if not self.model:
            raise BackendUnavailableError("No Ollama model selected. Set OLLAMA_MODEL (for example qwen3.5:4b).")
        estimated = estimate_tokens(system) + estimate_tokens(user)
        if estimated + s.max_output_tokens > s.num_ctx:
            raise BackendResponseError(
                f"The prompt is too large for the model's context window: about {estimated} tokens plus up to "
                f"{s.max_output_tokens} for the answer exceeds num_ctx={s.num_ctx}. Ollama would silently cut "
                "the prompt, so nothing was sent. Raise OLLAMA_NUM_CTX or use a smaller article/detail level."
            )
        body: dict[str, Any] = {
            "model": self.model, "stream": False, "keep_alive": s.keep_alive,
            "format": schema if schema is not None else "json",
            "options": {"num_ctx": s.num_ctx, "temperature": s.temperature, "num_predict": s.max_output_tokens},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if s.think is not None and self._think_supported:
            body["think"] = s.think  # top-level: options.think is silently ignored by Ollama

        queued = time.perf_counter()
        with _GENERATION_LOCK:  # one generation at a time
            started = time.perf_counter()
            data = self._send(body, purpose)
            wall = time.perf_counter() - started
        wait = started - queued
        if wait > 0.5:
            logger.info("Waited %.1fs for another Ollama request to finish", wait)
        return self._read(data, purpose, wall, wait)

    def _send(self, body: dict[str, Any], purpose: str) -> dict[str, Any]:
        logger.info("Ollama request (%s) to %s, model %s, num_ctx %s", purpose, self.host, self.model,
                    body["options"]["num_ctx"])
        for attempt in (1, 2):
            try:
                resp = self._session.post(f"{self.host}/api/chat", json=body, timeout=self.timeout)
            except requests.ConnectionError as exc:
                raise BackendUnavailableError(
                    f"Ollama is not running at {self.host}. Start the Ollama app (or run `ollama serve`) and try again."
                ) from exc
            except requests.Timeout as exc:
                raise BackendError(
                    f"Ollama did not finish within {self.timeout:.0f}s. On a CPU-only machine generation runs at "
                    "only a few tokens per second; lower OLLAMA_MAX_OUTPUT_TOKENS or raise OLLAMA_TIMEOUT."
                ) from exc
            except requests.RequestException as exc:
                raise BackendError(f"Ollama request failed: {type(exc).__name__}: {exc}") from exc
            status = getattr(resp, "status_code", 200)
            if status < 400:
                break
            message = _error_text(resp)
            if status == 404 and "model" in message.lower() and "not found" in message.lower():
                raise BackendUnavailableError(
                    f"Model {self.model!r} is not installed in Ollama. Install it with `ollama pull {self.model}`.")
            if status == 400 and "think" in message.lower() and "think" in body and attempt == 1:
                logger.warning("Model does not accept 'think' (%s); retrying without it.", message)
                self._think_supported = False
                body.pop("think")
                self._warn(f"The model {self.model!r} does not support the 'think' option; it was ignored.")
                continue
            raise BackendError(f"Ollama returned HTTP {status}: {message or 'no error message'}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise BackendError("Ollama returned a reply that is not JSON.") from exc
        if not isinstance(data, dict):
            raise BackendError("Ollama returned an unexpected reply shape.")
        return data

    def _read(self, data: dict[str, Any], purpose: str, wall: float, wait: float) -> str:
        message = data.get("message")
        if not isinstance(message, dict) or "content" not in message:
            raise BackendError("Ollama's reply had no message content.")
        content = str(message.get("content") or "")
        eval_s = (data.get("eval_duration") or 0) / 1e9
        out_tokens = int(data.get("eval_count") or 0)
        stats = RequestStats(
            purpose=purpose, wall_s=wall, wait_s=wait, load_s=(data.get("load_duration") or 0) / 1e9,
            prompt_tokens=int(data.get("prompt_eval_count") or 0), output_tokens=out_tokens,
            tokens_per_s=(out_tokens / eval_s) if eval_s else 0.0, done_reason=str(data.get("done_reason") or ""),
        )
        self.stats.append(stats)
        logger.info("Ollama %s: %.1fs, %d prompt + %d output tokens (%.1f tok/s)", purpose, wall,
                    stats.prompt_tokens, out_tokens, stats.tokens_per_s)
        if stats.done_reason == "length":
            raise BackendResponseError(
                f"The model's answer was cut off after {out_tokens} tokens (OLLAMA_MAX_OUTPUT_TOKENS="
                f"{self.settings.max_output_tokens}), so the JSON is incomplete. Raise the limit or ask for less.")
        if not content.strip():
            hint = " (it spent its output on thinking; keep OLLAMA_THINK=false)" if message.get("thinking") else ""
            raise BackendResponseError(f"The model returned an empty answer{hint}.")
        return content
