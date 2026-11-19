"""Make structured LLM calls through independently configured HTTP clients."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from traceback import format_exception
from typing import Any, Generic, Protocol, TypeVar

import httpx
from langchain_core.messages import BaseMessage, convert_to_messages
from langchain_core.messages.utils import MessageLikeRepresentation
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
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


class LlmBudgetExhaustedError(LlmGatewayError):
    pass


@dataclass(slots=True)
class _RequestBudget:
    acquire_sync: Callable[[], bool]
    acquire_async: Callable[[], Awaitable[bool]]
    initial_slots: int
    lock: Lock = field(default_factory=Lock)

    def consume_initial_slot(self) -> bool:
        with self.lock:
            if self.initial_slots == 0:
                return False
            self.initial_slots -= 1
            return True


_REQUEST_BUDGET: ContextVar[_RequestBudget | None] = ContextVar("llm_request_budget", default=None)


@contextmanager
def llm_request_budget(
    acquire_sync: Callable[[], bool],
    acquire_async: Callable[[], Awaitable[bool]],
    *,
    initial_slots: int = 0,
) -> Iterator[None]:
    """Charge each HTTP attempt against a caller's budget, including SDK retries."""
    if initial_slots < 0:
        raise ValueError("initial_slots must be non-negative")
    token = _REQUEST_BUDGET.set(_RequestBudget(acquire_sync, acquire_async, initial_slots))
    try:
        yield
    finally:
        _REQUEST_BUDGET.reset(token)


def _charge_request(_request: httpx.Request) -> None:
    budget = _REQUEST_BUDGET.get()
    if budget and not budget.consume_initial_slot() and not budget.acquire_sync():
        raise LlmBudgetExhaustedError("已用完 LLM HTTP 请求额度")


async def _charge_async_request(_request: httpx.Request) -> None:
    budget = _REQUEST_BUDGET.get()
    if budget and not budget.consume_initial_slot() and not await budget.acquire_async():
        raise LlmBudgetExhaustedError("已用完 LLM HTTP 请求额度")


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


class OpenAICompatibleLlmClient:
    def __init__(
        self,
        settings: AppSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        credentials = settings.resolve_validated_llm_credentials()
        if credentials is None:
            raise LlmNotConfiguredError("当前 LLM 端点未配置有效地址或可用密钥")
        endpoint, api_key = credentials
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
            http_client = httpx.Client(
                **http_options, transport=transport, event_hooks={"request": [_charge_request]}
            )
            async_client = httpx.AsyncClient(
                **http_options,
                transport=async_transport,
                event_hooks={"request": [_charge_async_request]},
            )
            chat_options = dict(
                model=settings.llm_model,
                base_url=endpoint,
                api_key=api_key,
                timeout=settings.llm_timeout_seconds,
                openai_proxy=None,
                http_socket_options=(),
                http_client=http_client,
                http_async_client=async_client,
            )
            chat = ChatOpenAI(**chat_options, max_retries=0)
            budgeted_chat = (
                ChatOpenAI(**chat_options, max_retries=settings.llm_max_retries)
                if settings.llm_max_retries
                else chat
            )
        except Exception as exc:
            if http_client is not None:
                http_client.close()
            error = self._gateway_error("初始化 LLM 客户端失败", exc)
        else:
            self._http_client = http_client
            self._http_async_client = async_client
            self._chat = chat
            self._budgeted_chat = budgeted_chat
            return
        raise error from None

    def _redact_keys(self, text: str) -> str:
        for key in self._api_keys:
            text = text.replace(key, "[REDACTED]")
        return text

    def _redact_response_text(self, text: str) -> str:
        return redact_url_queries_in_text(self._redact_keys(text))

    def _gateway_error(self, context: str, cause: Exception) -> LlmGatewayError:
        current: BaseException | None = cause
        seen = set()
        while current is not None and id(current) not in seen:
            if isinstance(current, LlmBudgetExhaustedError):
                return current
            seen.add(id(current))
            current = current.__cause__ or current.__context__
        details = self._redact_response_text(str(cause))
        error = LlmGatewayError(f"{context}（{type(cause).__name__}）：{details}")
        # Retain diagnostics without exposing SDK exceptions that hold request credentials.
        _LOGGER.warning("%s", error)
        return error

    def _structured_response(self, result: dict[str, Any]) -> StructuredLlmResponse[Any]:
        raw = result["raw"]
        original_text = str(raw.text)
        raw_text = self._redact_response_text(original_text)
        parsing_error = result["parsing_error"]
        if parsing_error is not None:
            trace = "".join(format_exception(parsing_error))
            if raw_text != original_text or self._redact_response_text(trace) != trace:
                details = self._redact_response_text(str(parsing_error))
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
            with tracing_context(enabled=False):
                # A caller with its own reservation but no HTTP budget gets one attempt.
                chat = self._budgeted_chat if _REQUEST_BUDGET.get() is not None else self._chat
                model = chat.with_structured_output(schema, method="json_mode", include_raw=True)
                result = model.invoke(_sanitize_messages(messages))
            return self._structured_response(result)
        except Exception as exc:
            error = self._gateway_error("LLM 请求失败", exc)
        raise error from None

    async def ainvoke_structured(
        self, messages: LlmMessages, schema: type[_SchemaT]
    ) -> StructuredLlmResponse[_SchemaT]:
        try:
            with tracing_context(enabled=False):
                chat = self._budgeted_chat if _REQUEST_BUDGET.get() is not None else self._chat
                model = chat.with_structured_output(schema, method="json_mode", include_raw=True)
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
