"""An in-process stand-in for ``anthropic.AsyncAnthropic``.

Returns real SDK objects (``Message``, ``MessageTokensCount``) and raises
real SDK exceptions, so the guarded client is exercised against the same
types it sees in production — without network access. ``outcome`` lets a
test script each call's result; the fake records what it "billed" so a
test can reconcile the ledger against it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic
import httpx2
from anthropic.types import Message, MessageTokensCount

_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

Outcome = Literal["ok", "status_error", "connection_error"]


@dataclass
class Usage:
    """The usage a successful fake call reports."""

    input_tokens: int = 1_000
    output_tokens: int = 500
    cache_read_input_tokens: int = 0
    cache_creation_5m: int = 0
    cache_creation_1h: int = 0

    def as_api_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_5m + self.cache_creation_1h,
            "cache_creation": {
                "ephemeral_5m_input_tokens": self.cache_creation_5m,
                "ephemeral_1h_input_tokens": self.cache_creation_1h,
            },
        }


@dataclass
class FakeMessages:
    prompt_tokens: int
    usage: Usage
    latency: float
    outcome: Callable[[int], Outcome]
    create_calls: list[dict[str, Any]] = field(default_factory=list)
    count_calls: list[dict[str, Any]] = field(default_factory=list)
    succeeded: int = 0
    # Set once the first create() call has started.
    called: asyncio.Event = field(default_factory=asyncio.Event)

    async def count_tokens(self, **params: Any) -> MessageTokensCount:
        self.count_calls.append(params)
        return MessageTokensCount(input_tokens=self.prompt_tokens)

    async def create(self, **params: Any) -> Message:
        call_index = len(self.create_calls)
        self.create_calls.append(params)
        self.called.set()
        if self.latency:
            await asyncio.sleep(self.latency)
        result = self.outcome(call_index)
        if result == "status_error":
            raise anthropic.InternalServerError(
                "overloaded", response=httpx2.Response(529, request=_REQUEST), body=None
            )
        if result == "connection_error":
            raise anthropic.APITimeoutError(request=_REQUEST)
        self.succeeded += 1
        return Message.model_validate(
            {
                "id": f"msg_{call_index}",
                "type": "message",
                "role": "assistant",
                "model": params["model"],
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": self.usage.as_api_dict(),
            }
        )


class FakeAsyncAnthropic:
    """Duck-types the parts of ``AsyncAnthropic`` the guarded client uses."""

    def __init__(
        self,
        *,
        prompt_tokens: int = 1_000,
        usage: Usage | None = None,
        latency: float = 0.0,
        outcome: Callable[[int], Outcome] = lambda _: "ok",
    ) -> None:
        self.messages = FakeMessages(
            prompt_tokens=prompt_tokens,
            usage=usage if usage is not None else Usage(),
            latency=latency,
            outcome=outcome,
        )
