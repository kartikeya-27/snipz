"""A scripted Anthropic HTTP API for driving the *real* SDK client offline.

``FakeAnthropicAPI.client()`` returns a genuine ``anthropic.AsyncAnthropic``
whose transport is an ``httpx2.MockTransport``: requests go through the
SDK's real request building, SSE parsing, stream accumulation, and error
mapping — only the network is replaced. ``outcome`` scripts each streamed
request from its body; the API records what it served so tests can reconcile the ledger.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic
import httpx2

# How a streamed request ends:
#   complete         — every event through message_stop
#   error_event      — message_start and a text delta, then an SSE error event
#   connection_drop  — message_start and a text delta, then the connection resets
#   status_error     — HTTP 529 before any event
StreamOutcome = Literal["complete", "error_event", "connection_drop", "status_error"]


_TEXT_BLOCK_START = {
    "type": "content_block_start",
    "index": 0,
    "content_block": {"type": "text", "text": ""},
}
_TEXT_DELTA = {
    "type": "content_block_delta",
    "index": 0,
    "delta": {"type": "text_delta", "text": "hi"},
}
_OVERLOADED = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@dataclass
class FakeAnthropicAPI:
    #: What ``count_tokens`` reports (the reservation's prompt size).
    counted_tokens: int = 1_000
    #: Prompt usage reported in ``message_start``.
    input_tokens: int = 800
    #: Output usage reported in the final ``message_delta``.
    output_tokens: int = 500
    text_chunks: int = 3
    #: Picks how each streamed request ends, given its JSON body.
    outcome: Callable[[dict[str, Any]], StreamOutcome] = lambda _: "complete"
    streams_served: Counter[StreamOutcome] = field(default_factory=Counter)
    stream_requests: list[dict[str, Any]] = field(default_factory=list)

    def client(self) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            api_key="test",
            max_retries=0,  # outcomes are scripted per request; no SDK retries
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle)),
        )

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/count_tokens"):
            return httpx2.Response(200, json={"input_tokens": self.counted_tokens})
        body = json.loads(request.content)
        if not body.get("stream"):
            raise AssertionError("FakeAnthropicAPI only serves streamed messages")
        index = len(self.stream_requests)
        self.stream_requests.append(body)
        outcome = self.outcome(body)
        self.streams_served[outcome] += 1
        if outcome == "status_error":
            return httpx2.Response(529, json=_OVERLOADED)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=self._events(index, body["model"], outcome),
        )

    async def _events(self, index: int, model: str, outcome: StreamOutcome) -> AsyncIterator[bytes]:
        yield _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": f"msg_{index}",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": self.input_tokens,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
            },
        )
        yield _sse("content_block_start", _TEXT_BLOCK_START)
        for _ in range(self.text_chunks):
            yield _sse("content_block_delta", _TEXT_DELTA)
            if outcome == "error_event":
                yield _sse("error", _OVERLOADED)
                return
            if outcome == "connection_drop":
                raise httpx2.ReadError("connection reset by peer")
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": self.output_tokens},
            },
        )
        yield _sse("message_stop", {"type": "message_stop"})
