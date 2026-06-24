"""Replaceable local chat and embedding provider adapters."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from urllib.parse import urljoin


class ProviderErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    MALFORMED_RESPONSE = "malformed_response"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    TRANSIENT = "transient"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"


class ProviderError(RuntimeError):
    """Sanitized provider failure that does not expose prompts or payloads."""

    def __init__(
        self,
        category: ProviderErrorCategory,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    provider: str
    model: str
    model_revision: str | None = None
    latency_seconds: float | None = None
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class OllamaHealth:
    base_url: str
    chat_model: str
    available: bool
    available_models: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AnthropicHealth:
    chat_model: str
    available: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError(f"unsupported chat role: {self.role}")
        _require_text(self.content, "content")


@dataclass(frozen=True, slots=True)
class ChatRequest:
    messages: tuple[ChatMessage, ...]
    temperature: float = 0.2

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("messages must not be empty")
        if self.temperature < 0:
            raise ValueError("temperature must not be negative")


@dataclass(frozen=True, slots=True)
class ChatResponse:
    message: ChatMessage
    metadata: ProviderMetadata
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    inputs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.inputs:
            raise ValueError("inputs must not be empty")
        for input_text in self.inputs:
            _require_text(input_text, "input")


@dataclass(frozen=True, slots=True)
class EmbeddingResponse:
    vectors: tuple[tuple[float, ...], ...]
    metadata: ProviderMetadata
    usage: ProviderUsage = field(default_factory=ProviderUsage)

    def __post_init__(self) -> None:
        if not self.vectors:
            raise ValueError("vectors must not be empty")
        expected_dimensions = len(self.vectors[0])
        if expected_dimensions == 0:
            raise ValueError("embedding vectors must not be empty")
        for vector in self.vectors:
            if len(vector) != expected_dimensions:
                raise ValueError("embedding vectors must have stable dimensions")


class ChatProvider(Protocol):
    def complete(self, request: ChatRequest) -> ChatResponse:
        """Return a chat completion for the provider-neutral request."""


class EmbeddingProvider(Protocol):
    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Return embeddings for the provider-neutral request."""


class FakeChatProvider:
    """Deterministic chat provider for offline conformance and coach tests."""

    def __init__(self, response_text: str = "fake local coach response") -> None:
        _require_text(response_text, "response_text")
        self.response_text = response_text
        self.requests: list[ChatRequest] = []

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(
            message=ChatMessage(role="assistant", content=self.response_text),
            metadata=ProviderMetadata(
                provider="fake",
                model="fake-chat",
                latency_seconds=0.0,
            ),
            usage=ProviderUsage(
                input_tokens=sum(len(message.content.split()) for message in request.messages),
                output_tokens=len(self.response_text.split()),
            ),
            finish_reason="stop",
        )


class FakeEmbeddingProvider:
    """Deterministic embedding provider for offline conformance tests."""

    def __init__(self, dimensions: int = 4) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions
        self.requests: list[EmbeddingRequest] = []

    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        self.requests.append(request)
        vectors = tuple(
            tuple(float((sum(input_text.encode("utf-8")) + offset) % 17) for offset in range(self.dimensions))
            for input_text in request.inputs
        )
        return EmbeddingResponse(
            vectors=vectors,
            metadata=ProviderMetadata(
                provider="fake",
                model="fake-embedding",
                latency_seconds=0.0,
            ),
            usage=ProviderUsage(input_tokens=sum(len(text.split()) for text in request.inputs)),
        )


class OllamaChatProvider:
    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        model: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 1,
    ) -> None:
        self.base_url = _base_url(base_url)
        self.model = _require_text(model, "model")
        self.timeout_seconds = _timeout(timeout_seconds)
        self.max_retries = _max_retries(max_retries)

    def complete(self, request: ChatRequest) -> ChatResponse:
        payload = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "options": {"temperature": request.temperature},
            "stream": False,
        }
        started_at = time.monotonic()
        response = self._post_json("api/chat", payload)
        latency = time.monotonic() - started_at
        message = response.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise _malformed("Ollama chat response did not include message content")
        role = message.get("role", "assistant")
        if role != "assistant":
            role = "assistant"
        return ChatResponse(
            message=ChatMessage(role=role, content=message["content"]),
            metadata=ProviderMetadata(
                provider="ollama",
                model=str(response.get("model") or self.model),
                latency_seconds=latency,
                correlation_id=_optional_text(response.get("created_at")),
            ),
            usage=ProviderUsage(
                input_tokens=_optional_int(response.get("prompt_eval_count")),
                output_tokens=_optional_int(response.get("eval_count")),
            ),
            finish_reason=_optional_text(response.get("done_reason")),
        )

    def _post_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        return _post_json(
            urljoin(f"{self.base_url}/", path),
            payload,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
        )


class OllamaEmbeddingProvider:
    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        model: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 1,
    ) -> None:
        self.base_url = _base_url(base_url)
        self.model = _require_text(model, "model")
        self.timeout_seconds = _timeout(timeout_seconds)
        self.max_retries = _max_retries(max_retries)

    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        payload = {
            "model": self.model,
            "input": list(request.inputs),
        }
        started_at = time.monotonic()
        response = self._post_json("api/embed", payload)
        latency = time.monotonic() - started_at
        raw_vectors = response.get("embeddings")
        if not isinstance(raw_vectors, list):
            raise _malformed("Ollama embedding response did not include embeddings")
        vectors: list[tuple[float, ...]] = []
        for raw_vector in raw_vectors:
            if not isinstance(raw_vector, list):
                raise _malformed("Ollama embedding response included an invalid vector")
            try:
                vectors.append(tuple(float(value) for value in raw_vector))
            except (TypeError, ValueError) as error:
                raise _malformed("Ollama embedding response included a non-numeric vector") from error
        return EmbeddingResponse(
            vectors=tuple(vectors),
            metadata=ProviderMetadata(
                provider="ollama",
                model=str(response.get("model") or self.model),
                latency_seconds=latency,
            ),
            usage=ProviderUsage(input_tokens=_optional_int(response.get("prompt_eval_count"))),
        )

    def _post_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        return _post_json(
            urljoin(f"{self.base_url}/", path),
            payload,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
        )


def check_ollama_health(
    *,
    base_url: str = "http://localhost:11434",
    chat_model: str,
    timeout_seconds: float = 5.0,
) -> OllamaHealth:
    normalized_base_url = _base_url(base_url)
    model = _require_text(chat_model, "chat_model")
    request = urllib.request.Request(
        urljoin(f"{normalized_base_url}/", "api/tags"),
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_timeout(timeout_seconds)) as response:
            decoded = response.read().decode("utf-8")
    except (TimeoutError, socket.timeout):
        return OllamaHealth(
            base_url=normalized_base_url,
            chat_model=model,
            available=False,
            error="local Ollama health check timed out",
        )
    except urllib.error.HTTPError as error:
        return OllamaHealth(
            base_url=normalized_base_url,
            chat_model=model,
            available=False,
            error=f"local Ollama /api/tags returned HTTP {error.code}",
        )
    except urllib.error.URLError:
        return OllamaHealth(
            base_url=normalized_base_url,
            chat_model=model,
            available=False,
            error="local Ollama service is not reachable; start it with `ollama serve`",
        )
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError:
        return OllamaHealth(
            base_url=normalized_base_url,
            chat_model=model,
            available=False,
            error="local Ollama health check returned malformed JSON",
        )
    if not isinstance(payload, dict):
        return OllamaHealth(
            base_url=normalized_base_url,
            chat_model=model,
            available=False,
            error="local Ollama health check returned an unexpected response shape",
        )
    models = _ollama_model_names(payload)
    if not _ollama_model_available(model, models):
        return OllamaHealth(
            base_url=normalized_base_url,
            chat_model=model,
            available=False,
            available_models=models,
            error=(
                f"local Ollama is running but model {model!r} is not installed; "
                f"run `ollama pull {model}` or set TRAININGOS_OLLAMA_CHAT_MODEL"
            ),
        )
    return OllamaHealth(
        base_url=normalized_base_url,
        chat_model=model,
        available=True,
        available_models=models,
    )


class AnthropicChatProvider:
    _BASE_URL = "https://api.anthropic.com"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_key = _require_text(api_key, "api_key")
        self.model = _require_text(model, "model")
        self.timeout_seconds = _timeout(timeout_seconds)

    def complete(self, request: ChatRequest) -> ChatResponse:
        system_blocks = [
            {"type": "text", "text": m.content, "cache_control": {"type": "ephemeral"}}
            for m in request.messages
            if m.role == "system"
        ]
        conversation = [
            {"role": m.role, "content": m.content}
            for m in request.messages
            if m.role != "system"
        ]
        payload: dict[str, object] = {
            "model": self.model,
            "max_tokens": 8192,
            "messages": conversation,
            "stream": False,
        }
        if system_blocks:
            payload["system"] = system_blocks
        started_at = time.monotonic()
        response = _anthropic_post_json(
            f"{self._BASE_URL}/v1/messages",
            payload,
            api_key=self._api_key,
            timeout_seconds=self.timeout_seconds,
        )
        latency = time.monotonic() - started_at
        content = response.get("content")
        if not isinstance(content, list) or not content:
            raise _anthropic_malformed("Anthropic response did not include content")
        first = content[0]
        if not isinstance(first, dict) or not isinstance(first.get("text"), str):
            raise _anthropic_malformed("Anthropic response content block missing text")
        usage = response.get("usage") or {}
        return ChatResponse(
            message=ChatMessage(role="assistant", content=first["text"]),
            metadata=ProviderMetadata(
                provider="anthropic",
                model=str(response.get("model") or self.model),
                latency_seconds=latency,
                correlation_id=_optional_text(response.get("id")),
            ),
            usage=ProviderUsage(
                input_tokens=_optional_int(usage.get("input_tokens")),
                output_tokens=_optional_int(usage.get("output_tokens")),
            ),
            finish_reason=_optional_text(response.get("stop_reason")),
        )


def check_anthropic_health(
    *,
    api_key: str,
    chat_model: str,
    timeout_seconds: float = 5.0,
) -> AnthropicHealth:
    model = _require_text(chat_model, "chat_model")
    url = f"https://api.anthropic.com/v1/models/{model}"
    headers = _anthropic_headers(api_key)
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_timeout(timeout_seconds)):
            pass
        return AnthropicHealth(chat_model=model, available=True)
    except (TimeoutError, socket.timeout):
        return AnthropicHealth(
            chat_model=model,
            available=False,
            error="Anthropic health check timed out",
        )
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return AnthropicHealth(
                chat_model=model,
                available=False,
                error=f"model {model!r} not found on Anthropic API",
            )
        if error.code == 401:
            return AnthropicHealth(
                chat_model=model,
                available=False,
                error="Anthropic API authentication failed; check TRAININGOS_ANTHROPIC_API_KEY",
            )
        return AnthropicHealth(
            chat_model=model,
            available=False,
            error=f"Anthropic API returned HTTP {error.code}",
        )
    except urllib.error.URLError:
        return AnthropicHealth(
            chat_model=model,
            available=False,
            error="Anthropic API is not reachable",
        )


def _anthropic_post_json(
    url: str,
    payload: dict[str, object],
    *,
    api_key: str,
    timeout_seconds: float,
) -> dict[str, object]:
    body = json.dumps(payload).encode("utf-8")
    headers = _anthropic_headers(api_key)
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            decoded = response.read().decode("utf-8")
    except (TimeoutError, socket.timeout) as error:
        raise ProviderError(
            ProviderErrorCategory.TIMEOUT,
            "Anthropic request timed out",
            provider="anthropic",
            retryable=True,
        ) from error
    except urllib.error.HTTPError as error:
        raise _anthropic_http_error(error) from error
    except urllib.error.URLError as error:
        raise ProviderError(
            ProviderErrorCategory.PROVIDER_UNAVAILABLE,
            "Anthropic API is unavailable",
            provider="anthropic",
            retryable=True,
        ) from error
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError as error:
        raise _anthropic_malformed("Anthropic returned malformed JSON") from error
    if not isinstance(parsed, dict):
        raise _anthropic_malformed("Anthropic returned an unexpected response shape")
    return parsed


def _anthropic_headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "prompt-caching-2024-07-31",
    }


def _anthropic_http_error(error: urllib.error.HTTPError) -> ProviderError:
    if error.code == 401:
        return ProviderError(
            ProviderErrorCategory.AUTHENTICATION,
            "Anthropic authentication failed; check TRAININGOS_ANTHROPIC_API_KEY",
            provider="anthropic",
            status_code=error.code,
        )
    if error.code == 429:
        return ProviderError(
            ProviderErrorCategory.RATE_LIMIT,
            "Anthropic rate limit was reached",
            provider="anthropic",
            status_code=error.code,
            retryable=True,
        )
    if error.code == 529 or error.code >= 500:
        return ProviderError(
            ProviderErrorCategory.TRANSIENT,
            "Anthropic returned a transient server error",
            provider="anthropic",
            status_code=error.code,
            retryable=True,
        )
    return ProviderError(
        ProviderErrorCategory.UNSUPPORTED_CAPABILITY,
        "Anthropic request was rejected",
        provider="anthropic",
        status_code=error.code,
    )


def _anthropic_malformed(message: str) -> ProviderError:
    return ProviderError(
        ProviderErrorCategory.MALFORMED_RESPONSE,
        message,
        provider="anthropic",
    )


def _post_json(
    url: str,
    payload: dict[str, object],
    *,
    timeout_seconds: float,
    max_retries: int,
) -> dict[str, object]:
    body = json.dumps(payload).encode("utf-8")
    attempts = max_retries + 1
    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                decoded = response.read().decode("utf-8")
        except TimeoutError as error:
            raise ProviderError(
                ProviderErrorCategory.TIMEOUT,
                "local Ollama request timed out",
                provider="ollama",
                retryable=True,
            ) from error
        except socket.timeout as error:
            raise ProviderError(
                ProviderErrorCategory.TIMEOUT,
                "local Ollama request timed out",
                provider="ollama",
                retryable=True,
            ) from error
        except urllib.error.HTTPError as error:
            provider_error = _http_error(error)
            if provider_error.retryable and attempt + 1 < attempts:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise provider_error from error
        except urllib.error.URLError as error:
            provider_error = ProviderError(
                ProviderErrorCategory.PROVIDER_UNAVAILABLE,
                "local Ollama service is unavailable",
                provider="ollama",
                retryable=True,
            )
            if attempt + 1 < attempts:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise provider_error from error
        try:
            parsed = json.loads(decoded)
        except json.JSONDecodeError as error:
            raise _malformed("Ollama returned malformed JSON") from error
        if not isinstance(parsed, dict):
            raise _malformed("Ollama returned an unexpected response shape")
        return parsed
    raise ProviderError(
        ProviderErrorCategory.PROVIDER_UNAVAILABLE,
        "local Ollama service is unavailable",
        provider="ollama",
        retryable=True,
    )


def _http_error(error: urllib.error.HTTPError) -> ProviderError:
    if error.code in {401, 403}:
        return ProviderError(
            ProviderErrorCategory.AUTHENTICATION,
            "local Ollama authentication or access failed",
            provider="ollama",
            status_code=error.code,
        )
    if error.code == 429:
        return ProviderError(
            ProviderErrorCategory.RATE_LIMIT,
            "local Ollama rate limit was reached",
            provider="ollama",
            status_code=error.code,
            retryable=True,
        )
    if error.code >= 500:
        return ProviderError(
            ProviderErrorCategory.TRANSIENT,
            "local Ollama returned a transient server error",
            provider="ollama",
            status_code=error.code,
            retryable=True,
        )
    return ProviderError(
        ProviderErrorCategory.UNSUPPORTED_CAPABILITY,
        "local Ollama request was rejected",
        provider="ollama",
        status_code=error.code,
    )


def _malformed(message: str) -> ProviderError:
    return ProviderError(
        ProviderErrorCategory.MALFORMED_RESPONSE,
        message,
        provider="ollama",
    )


def _base_url(value: str) -> str:
    normalized = _require_text(value, "base_url").rstrip("/")
    return normalized


def _timeout(value: float) -> float:
    if value <= 0:
        raise ValueError("timeout_seconds must be positive")
    return float(value)


def _max_retries(value: int) -> int:
    if value < 0:
        raise ValueError("max_retries must not be negative")
    return value


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _require_text(value: str, name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value.strip()


def _ollama_model_names(payload: dict[str, object]) -> tuple[str, ...]:
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        return ()
    names: list[str] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            continue
        name = raw_model.get("name") or raw_model.get("model")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return tuple(sorted(dict.fromkeys(names)))


def _ollama_model_available(model: str, available_models: tuple[str, ...]) -> bool:
    return model in available_models or f"{model}:latest" in available_models
