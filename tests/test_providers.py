from __future__ import annotations

import json
import socket
import unittest
import urllib.error
from unittest.mock import patch

from trainingos.providers import (
    ChatMessage,
    ChatRequest,
    EmbeddingRequest,
    FakeChatProvider,
    FakeEmbeddingProvider,
    OllamaChatProvider,
    OllamaEmbeddingProvider,
    ProviderError,
    ProviderErrorCategory,
    check_ollama_health,
)


class ProviderContractTests(unittest.TestCase):
    def test_fake_chat_provider_is_deterministic_and_records_requests(self) -> None:
        provider = FakeChatProvider("local answer")
        request = ChatRequest(
            messages=(ChatMessage(role="user", content="How was the week?"),)
        )

        response = provider.complete(request)

        self.assertEqual("local answer", response.message.content)
        self.assertEqual("fake", response.metadata.provider)
        self.assertEqual("fake-chat", response.metadata.model)
        self.assertEqual("stop", response.finish_reason)
        self.assertEqual([request], provider.requests)
        self.assertGreater(response.usage.input_tokens or 0, 0)

    def test_fake_embedding_provider_returns_stable_dimensions(self) -> None:
        provider = FakeEmbeddingProvider(dimensions=3)

        response = provider.embed(EmbeddingRequest(inputs=("easy run", "long run")))

        self.assertEqual(2, len(response.vectors))
        self.assertEqual((3, 3), tuple(len(vector) for vector in response.vectors))
        self.assertEqual("fake-embedding", response.metadata.model)

    def test_ollama_chat_provider_maps_non_streaming_response(self) -> None:
        payload = {
            "model": "llama3.2",
            "message": {"role": "assistant", "content": "Use local evidence."},
            "done_reason": "stop",
            "created_at": "2026-06-17T12:00:00Z",
            "prompt_eval_count": 12,
            "eval_count": 4,
        }
        with patch(
            "urllib.request.urlopen",
            return_value=_Response(json.dumps(payload)),
        ) as urlopen:
            provider = OllamaChatProvider(model="llama3.2", timeout_seconds=5)

            response = provider.complete(
                ChatRequest(
                    messages=(
                        ChatMessage(role="system", content="Use evidence only."),
                        ChatMessage(role="user", content="Am I on track?"),
                    )
                )
            )

        self.assertEqual("Use local evidence.", response.message.content)
        self.assertEqual("ollama", response.metadata.provider)
        self.assertEqual("llama3.2", response.metadata.model)
        self.assertEqual("2026-06-17T12:00:00Z", response.metadata.correlation_id)
        self.assertEqual(12, response.usage.input_tokens)
        self.assertEqual(4, response.usage.output_tokens)
        request = urlopen.call_args.args[0]
        request_payload = json.loads(request.data.decode("utf-8"))
        self.assertFalse(request_payload["stream"])
        self.assertEqual("llama3.2", request_payload["model"])

    def test_ollama_embedding_provider_maps_response(self) -> None:
        payload = {
            "model": "nomic-embed-text",
            "embeddings": [[1, 2.5], [3, 4]],
            "prompt_eval_count": 5,
        }
        with patch(
            "urllib.request.urlopen",
            return_value=_Response(json.dumps(payload)),
        ):
            provider = OllamaEmbeddingProvider(
                model="nomic-embed-text",
                timeout_seconds=5,
            )

            response = provider.embed(EmbeddingRequest(inputs=("a", "b")))

        self.assertEqual(((1.0, 2.5), (3.0, 4.0)), response.vectors)
        self.assertEqual("ollama", response.metadata.provider)
        self.assertEqual(5, response.usage.input_tokens)

    def test_ollama_timeout_maps_to_sanitized_timeout_error(self) -> None:
        with patch("urllib.request.urlopen", side_effect=socket.timeout("secret prompt")):
            provider = OllamaChatProvider(model="llama3.2", timeout_seconds=1)

            with self.assertRaises(ProviderError) as raised:
                provider.complete(
                    ChatRequest(messages=(ChatMessage(role="user", content="private"),))
                )

        self.assertEqual(ProviderErrorCategory.TIMEOUT, raised.exception.category)
        self.assertNotIn("private", str(raised.exception))
        self.assertNotIn("secret prompt", str(raised.exception))

    def test_ollama_connection_error_maps_to_unavailable(self) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            provider = OllamaChatProvider(model="llama3.2", max_retries=0)

            with self.assertRaises(ProviderError) as raised:
                provider.complete(
                    ChatRequest(messages=(ChatMessage(role="user", content="private"),))
                )

        self.assertEqual(
            ProviderErrorCategory.PROVIDER_UNAVAILABLE,
            raised.exception.category,
        )
        self.assertTrue(raised.exception.retryable)

    def test_ollama_http_errors_are_normalized(self) -> None:
        cases = (
            (401, ProviderErrorCategory.AUTHENTICATION),
            (403, ProviderErrorCategory.AUTHENTICATION),
            (429, ProviderErrorCategory.RATE_LIMIT),
            (500, ProviderErrorCategory.TRANSIENT),
            (400, ProviderErrorCategory.UNSUPPORTED_CAPABILITY),
        )
        for status_code, category in cases:
            with self.subTest(status_code=status_code):
                with patch(
                    "urllib.request.urlopen",
                    side_effect=urllib.error.HTTPError(
                        url="http://localhost:11434/api/chat",
                        code=status_code,
                        msg="raw provider message",
                        hdrs=None,
                        fp=None,
                    ),
                ):
                    provider = OllamaChatProvider(model="llama3.2", max_retries=0)

                    with self.assertRaises(ProviderError) as raised:
                        provider.complete(
                            ChatRequest(
                                messages=(ChatMessage(role="user", content="private"),)
                            )
                        )

                self.assertEqual(category, raised.exception.category)
                self.assertEqual(status_code, raised.exception.status_code)
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn("raw provider message", str(raised.exception))

    def test_ollama_malformed_json_maps_to_malformed_response(self) -> None:
        with patch("urllib.request.urlopen", return_value=_Response("not-json")):
            provider = OllamaChatProvider(model="llama3.2")

            with self.assertRaises(ProviderError) as raised:
                provider.complete(
                    ChatRequest(messages=(ChatMessage(role="user", content="private"),))
                )

        self.assertEqual(
            ProviderErrorCategory.MALFORMED_RESPONSE,
            raised.exception.category,
        )

    def test_ollama_health_reports_available_model(self) -> None:
        payload = {
            "models": [
                {"name": "llama3.2:latest"},
                {"model": "nomic-embed-text:latest"},
            ]
        }
        with patch("urllib.request.urlopen", return_value=_Response(json.dumps(payload))):
            health = check_ollama_health(chat_model="llama3.2:latest")

        self.assertTrue(health.available)
        self.assertEqual("http://localhost:11434", health.base_url)
        self.assertEqual("llama3.2:latest", health.chat_model)
        self.assertIn("llama3.2:latest", health.available_models)
        self.assertIsNone(health.error)

    def test_ollama_health_reports_missing_service_and_model(self) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            unavailable = check_ollama_health(chat_model="llama3.2")

        self.assertFalse(unavailable.available)
        self.assertIn("ollama serve", unavailable.error or "")

        with patch("urllib.request.urlopen", return_value=_Response('{"models": []}')):
            missing_model = check_ollama_health(chat_model="llama3.2")

        self.assertFalse(missing_model.available)
        self.assertIn("ollama pull llama3.2", missing_model.error or "")


class _Response:
    def __init__(self, body: str) -> None:
        self.body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body.encode("utf-8")


if __name__ == "__main__":
    unittest.main()
