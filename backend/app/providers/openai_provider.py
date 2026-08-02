"""Adapter: wraps langchain-openai's ChatOpenAI behind ChatProvider. Same
retry/timeout/error-translation shape as google_provider.py — the adapters
are intentionally structured identically so a reviewer can diff them and see
the pattern, not two ad-hoc implementations.

An optional `base_url` lets this same adapter target **any** OpenAI-compatible
endpoint — real OpenAI, or Groq / OpenRouter / a local server — so switching
the judge to a different model family is a config change (base_url + key +
model), not a new class. When `OPENAI_API_KEY` is set, the factory routes the
judge here for a cross-family check (DESIGN_DECISIONS.md #10)."""

from __future__ import annotations

import asyncio
from typing import Any

import openai
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.core.errors import (
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.base import ChatProvider, ChatResult, Message, T


def _to_lc_message(message: Message) -> HumanMessage | AIMessage:
    if message.role == "assistant":
        return AIMessage(content=message.content)
    return HumanMessage(content=message.content)


class OpenAIChatProvider(ChatProvider):
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._model_name = model
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._max_retries = max_retries

    def _build_runnable(self, *, max_tokens: int, response_model: type[Any] | None) -> Any:
        # See google_provider._build_runnable: structured output binds on the
        # base model FIRST, then retry wraps it. base_url is None for real
        # OpenAI; set it to a compatible endpoint (Groq/OpenRouter/...) to run
        # the judge on a different model family.
        model = ChatOpenAI(
            model=self._model_name,
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._timeout,
            max_retries=0,
            max_tokens=max_tokens,
        )
        base: Any = model
        if response_model is not None:
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
                runnable.ainvoke(lc_messages),
                timeout=self._timeout * 2,
            )
        except TimeoutError as exc:
            raise ProviderTimeoutError(
                f"OpenAI call exceeded {self._timeout * 2}s", provider="openai", cause=exc
            ) from exc
        except openai.RateLimitError as exc:
            raise ProviderRateLimitError(str(exc), provider="openai", cause=exc) from exc
        except openai.APITimeoutError as exc:
            raise ProviderTimeoutError(str(exc), provider="openai", cause=exc) from exc
        except (openai.APIConnectionError, openai.InternalServerError, openai.APIStatusError) as exc:
            raise ProviderUnavailableError(str(exc), provider="openai", cause=exc) from exc

        if response_model is not None:
            parsing_error = result.get("parsing_error")
            if parsing_error is not None:
                raise ProviderResponseError(
                    f"structured output failed validation: {parsing_error}",
                    provider="openai",
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
