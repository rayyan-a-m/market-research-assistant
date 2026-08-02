"""Adapter: wraps langchain-google-genai (Gemini) behind ChatProvider and
EmbeddingProvider. Same retry/timeout/error-translation shape as the other
adapters — a reviewer can diff this against openai_provider.py and see the
identical pattern, just a different SDK and a different exception taxonomy
mapped to the same normalized errors.

Gemini (via the Google AI Studio API key) is the summarizer, the discovery
ranking model, and the embedding provider. The judge deliberately runs on a
different model family (GitHub Models, via the OpenAI-compatible adapter) so
verification never grades the summarizer's own lineage — see
ProviderFactory.judge() and DESIGN_DECISIONS.md #10.
"""

from __future__ import annotations

import asyncio
from typing import Any

from google.api_core import exceptions as gexc
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

from app.core.errors import (
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.base import ChatProvider, ChatResult, EmbeddingProvider, Message, T


def _to_lc_message(message: Message) -> HumanMessage | AIMessage:
    if message.role == "assistant":
        return AIMessage(content=message.content)
    return HumanMessage(content=message.content)


class GoogleChatProvider(ChatProvider):
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._model_name = model
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._max_retries = max_retries

    def _build_runnable(self, *, max_tokens: int, response_model: type[Any] | None) -> Any:
        # Order matters: `.with_structured_output()` lives on the chat model,
        # NOT on the RunnableRetry/RunnableBinding wrappers — so structured
        # output must be bound on the base model FIRST, then wrapped with retry.
        # max_output_tokens is set at construction because generation kwargs
        # don't reliably forward through those wrappers to the model.
        model = ChatGoogleGenerativeAI(
            model=self._model_name,
            google_api_key=self._api_key,
            timeout=self._timeout,
            max_retries=0,  # retry is owned by .with_retry() below
            max_output_tokens=max_tokens,
        )
        base: Any = model
        if response_model is not None:
            # include_raw=True → returns a raw/parsed/parsing_error dict.
            base = model.with_structured_output(response_model, include_raw=True)
        return base.with_retry(stop_after_attempt=self._max_retries, wait_exponential_jitter=True)

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        response_model: type[T] | None = None,
        max_tokens: int = 4096,
    ) -> ChatResult[T]:
        lc_messages = [SystemMessage(content=system), *(_to_lc_message(m) for m in messages)]
        runnable = self._build_runnable(max_tokens=max_tokens, response_model=response_model)
        try:
            result = await asyncio.wait_for(
                runnable.ainvoke(lc_messages), timeout=self._timeout * 2
            )
        except TimeoutError as exc:
            raise ProviderTimeoutError(
                f"Gemini call exceeded {self._timeout * 2}s", provider="google", cause=exc
            ) from exc
        except gexc.ResourceExhausted as exc:
            raise ProviderRateLimitError(str(exc), provider="google", cause=exc) from exc
        except gexc.DeadlineExceeded as exc:
            raise ProviderTimeoutError(str(exc), provider="google", cause=exc) from exc
        except (
            gexc.ServiceUnavailable,
            gexc.InternalServerError,
            gexc.GoogleAPICallError,
        ) as exc:
            raise ProviderUnavailableError(str(exc), provider="google", cause=exc) from exc

        if response_model is not None:
            parsing_error = result.get("parsing_error")
            if parsing_error is not None:
                raise ProviderResponseError(
                    f"structured output failed validation: {parsing_error}",
                    provider="google",
                    cause=parsing_error if isinstance(parsing_error, Exception) else None,
                )
            parsed = result["parsed"]
            raw = result["raw"]
            return ChatResult(
                text=raw.content if isinstance(raw.content, str) else str(raw.content),
                parsed=parsed,
                model=self._model_name,
                input_tokens=(raw.usage_metadata or {}).get("input_tokens", 0),
                output_tokens=(raw.usage_metadata or {}).get("output_tokens", 0),
            )

        text = result.content if isinstance(result.content, str) else str(result.content)
        return ChatResult(
            text=text,
            parsed=None,
            model=self._model_name,
            input_tokens=(result.usage_metadata or {}).get("input_tokens", 0),
            output_tokens=(result.usage_metadata or {}).get("output_tokens", 0),
        )


class GoogleEmbeddingProvider(EmbeddingProvider):
    def __init__(self, *, model: str, api_key: str, timeout_seconds: float = 30.0) -> None:
        self._client = GoogleGenerativeAIEmbeddings(model=model, google_api_key=api_key)
        self._timeout = timeout_seconds

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            return await asyncio.wait_for(
                self._client.aembed_documents(texts), timeout=self._timeout * 2
            )
        except TimeoutError as exc:
            raise ProviderTimeoutError(
                f"Gemini embedding call exceeded {self._timeout * 2}s",
                provider="google",
                cause=exc,
            ) from exc
        except gexc.ResourceExhausted as exc:
            raise ProviderRateLimitError(str(exc), provider="google", cause=exc) from exc
        except (
            gexc.ServiceUnavailable,
            gexc.InternalServerError,
            gexc.GoogleAPICallError,
        ) as exc:
            raise ProviderUnavailableError(str(exc), provider="google", cause=exc) from exc
