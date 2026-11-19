"""Make structured LLM calls through independently configured HTTP clients."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from traceback import format_exception
from typing import Any, Generic, Protocol, TypeVar

import httpx
from langchain_core.messages import BaseMessage, convert_to_messages
from langchain_core.messages.utils import MessageLikeRepresentation
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from shipping_billing_agent.config.settings import AppSettings
from shipping_billing_agent.gateways.text_sanitizer import sanitize_for_llm
from shipping_billing_agent.gateways.url_guard import redact_url_queries_in_text

_LOGGER = logging.getLogger(__name__)
_SchemaT = TypeVar("_SchemaT", bound=BaseModel)
LlmMessages = str | Sequence[MessageLikeRepresentation]
_OUTBOUND_MESSAGE_FIELDS = {"content", "name", "additional_kwargs", "tool_calls"}


class LlmNotConfiguredError(RuntimeError):
    pass


class LlmGatewayError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StructuredLlmResponse(Generic[_SchemaT]):  # noqa: UP046 - Python 3.11 is supported.
    parsed: _SchemaT | None
    raw_text: str
    parsing_error: Exception | None
    model_name: str
    usage: dict[str, Any] = field(default_factory=dict)


class StructuredLlmClient(Protocol):
    def invoke_structured(
        self, messages: LlmMessages, schema: type[_SchemaT]
    ) -> StructuredLlmResponse[_SchemaT]: ...

    async def ainvoke_structured(
        self, messages: LlmMessages, schema: type[_SchemaT]
    ) -> StructuredLlmResponse[_SchemaT]: ...


def _sanitize_message_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_for_llm(value)
    if isinstance(value, list):
        return [_sanitize_message_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_message_value(item) for key, item in value.items()}
    return value


def _sanitize_messages(messages: LlmMessages) -> list[BaseMessage]:
    sanitized = []
    for message in convert_to_messages([messages] if isinstance(messages, str) else messages):
        fields = {
            name: _sanitize_message_value(value)
            for name, value in message
            if name in _OUTBOUND_MESSAGE_FIELDS
        }
        sanitized.append(message.model_copy(update=fields))
    return sanitized


def _resolve_endpoint(settings: AppSettings) -> tuple[httpx.URL, str]:
    base_url, api_key = settings.resolve_llm_credentials()
    if not api_key:
        raise LlmNotConfiguredError("当前 LLM 端点未配置可用密钥")
    try:
        endpoint = httpx.URL(base_url)
    except (httpx.InvalidURL, ValueError):
        raise LlmNotConfiguredError("LLM 端点 URL 格式无效") from None
    if endpoint.scheme not in {"http", "https"} or not endpoint.host or endpoint.userinfo:
        raise LlmNotConfiguredError("LLM 端点必须使用无用户信息的 HTTP 或 HTTPS URL")
    if api_key == settings.deepseek_api_key.strip() and (
        endpoint.scheme != "https" or endpoint.host != "api.deepseek.com"
    ):
        raise LlmNotConfiguredError("DeepSeek 密钥仅可用于 https://api.deepseek.com")
    return endpoint, api_key


class OpenAICompatibleLlmClient:
    def __init__(self, settings: AppSettings) -> None:
        endpoint, api_key = _resolve_endpoint(settings)
        self._model_name = settings.llm_model
        self._api_keys = sorted(
            {api_key, settings.deepseek_api_key.strip()} - {""}, key=len, reverse=True
        )
        http_options = {
            "trust_env": settings.llm_trust_env,
            "proxy": settings.llm_proxy_url or None,
            "timeout": settings.llm_timeout_seconds,
            "follow_redirects": False,
        }
        http_client = None
        try:
            http_client = httpx.Client(**http_options)
            async_client = httpx.AsyncClient(**http_options)
            chat = ChatOpenAI(
                model=settings.llm_model,
                base_url=str(endpoint),
                api_key=api_key,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
                openai_proxy=None,
                http_socket_options=(),
                http_client=http_client,
                http_async_client=async_client,
            )
        except Exception as exc:
            if http_client is not None:
                http_client.close()
            error = self._gateway_error("初始化 LLM 客户端失败", exc)
        else:
            self._http_client = http_client
            self._http_async_client = async_client
            self._chat = chat
            return
        raise error from None

    def _redact_keys(self, text: str) -> str:
        for key in self._api_keys:
            text = text.replace(key, "[REDACTED]")
        return text

    def _gateway_error(self, context: str, cause: Exception) -> LlmGatewayError:
        details = redact_url_queries_in_text(self._redact_keys(str(cause)))
        error = LlmGatewayError(f"{context}（{type(cause).__name__}）：{details}")
        # Retain diagnostics without exposing SDK exceptions that hold request credentials.
        _LOGGER.warning("%s", error)
        return error

    def _structured_response(self, result: dict[str, Any]) -> StructuredLlmResponse[Any]:
        raw = result["raw"]
        original_text = str(raw.text)
        raw_text = self._redact_keys(original_text)
        parsing_error = result["parsing_error"]
        if parsing_error is not None:
            trace = "".join(format_exception(parsing_error))
            if raw_text != original_text or self._redact_keys(trace) != trace:
                details = self._redact_keys(str(parsing_error))
                parsing_error = ValueError(f"{type(parsing_error).__name__}: {details}")
        return StructuredLlmResponse(
            parsed=result["parsed"],
            raw_text=raw_text,
            parsing_error=parsing_error,
            model_name=raw.response_metadata.get("model_name") or self._model_name,
            usage=dict(raw.usage_metadata or {}),
        )

    def invoke_structured(
        self, messages: LlmMessages, schema: type[_SchemaT]
    ) -> StructuredLlmResponse[_SchemaT]:
        try:
            model = self._chat.with_structured_output(schema, method="json_mode", include_raw=True)
            result = model.invoke(_sanitize_messages(messages))
            return self._structured_response(result)
        except Exception as exc:
            error = self._gateway_error("LLM 请求失败", exc)
        raise error from None

    async def ainvoke_structured(
        self, messages: LlmMessages, schema: type[_SchemaT]
    ) -> StructuredLlmResponse[_SchemaT]:
        try:
            model = self._chat.with_structured_output(schema, method="json_mode", include_raw=True)
            result = await model.ainvoke(_sanitize_messages(messages))
            return self._structured_response(result)
        except Exception as exc:
            error = self._gateway_error("LLM 请求失败", exc)
        raise error from None

    def close(self) -> None:
        """Release the synchronous client after sync-only use."""
        self._http_client.close()

    async def aclose(self) -> None:
        """Release both clients after asynchronous or mixed use."""
        try:
            self.close()
        finally:
            await self._http_async_client.aclose()


class FakeStructuredLlmClient:
    def __init__(self, response: StructuredLlmResponse[Any]) -> None:
        self._response = response
        self.sync_calls: list[tuple[LlmMessages, type[BaseModel]]] = []
        self.async_calls: list[tuple[LlmMessages, type[BaseModel]]] = []

    def invoke_structured(
        self, messages: LlmMessages, schema: type[_SchemaT]
    ) -> StructuredLlmResponse[_SchemaT]:
        self.sync_calls.append((messages, schema))
        return self._response

    async def ainvoke_structured(
        self, messages: LlmMessages, schema: type[_SchemaT]
    ) -> StructuredLlmResponse[_SchemaT]:
        self.async_calls.append((messages, schema))
        return self._response
