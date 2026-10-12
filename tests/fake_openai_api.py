"""A scripted OpenAI HTTP API for driving the *real* SDK client offline.

``FakeOpenAIAPI.client()`` returns a genuine ``openai.AsyncOpenAI`` whose
transport is an ``httpx2.MockTransport``: requests go through the SDK's
real request building, SSE parsing, stream state, and error mapping —
only the network is replaced. ``outcome`` scripts each request from its
body; the API records what it served so tests can reconcile the ledger.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx2
import openai

# How a request ends:
#   completed        — response.completed with usage
#   incomplete       — response.incomplete with usage (e.g. max_output_tokens hit)
#   failed           — response.failed with usage
#   error_event      — an SSE error event, no usage (streams only)
#   connection_drop  — the connection resets (mid-stream for streams)
#   status_error     — HTTP 500 before any event
Outcome = Literal[
    "completed", "incomplete", "failed", "error_event", "connection_drop", "status_error"
]

_ITEM = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "status": "in_progress",
    "content": [],
}
_PART = {"type": "output_text", "text": "", "annotations": []}
_SERVER_ERROR = {"error": {"message": "server error", "type": "server_error"}}


def _sse(data: dict[str, Any]) -> bytes:
    return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n".encode()


@dataclass
class FakeOpenAIAPI:
    #: What ``responses.input_tokens.count`` reports (the reservation's prompt size).
    counted_tokens: int = 1_000
    #: Usage on the final response.
    input_tokens: int = 800
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 500
    text_chunks: int = 3
    outcome: Callable[[dict[str, Any]], Outcome] = lambda _: "completed"
    served: Counter[Outcome] = field(default_factory=Counter)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def client(self) -> openai.AsyncOpenAI:
        return openai.AsyncOpenAI(
            api_key="test",
            max_retries=0,  # outcomes are scripted per request; no SDK retries
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle)),
        )

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/responses/input_tokens"):
            return httpx2.Response(
                200, json={"object": "response.input_tokens", "input_tokens": self.counted_tokens}
            )
        body = json.loads(request.content)
        index = len(self.requests)
        self.requests.append(body)
        outcome = self.outcome(body)
        self.served[outcome] += 1
        if outcome == "status_error":
            return httpx2.Response(500, json=_SERVER_ERROR)
        if not body.get("stream"):
            if outcome == "connection_drop":
                raise httpx2.ReadError("connection reset by peer")
            return httpx2.Response(200, json=self._response(index, body["model"], outcome))
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=self._events(index, body["model"], outcome),
        )

    def _response(
        self, index: int, model: str, status: str, *, usage: bool = True
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "id": f"resp_{index}",
            "object": "response",
            "created_at": 1,
            "model": model,
            "status": status,
            "output": [],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
        }
        if usage:
            response["usage"] = {
                "input_tokens": self.input_tokens,
                "input_tokens_details": {
                    "cached_tokens": self.cached_tokens,
                    "cache_write_tokens": self.cache_write_tokens,
                },
                "output_tokens": self.output_tokens,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": self.input_tokens + self.output_tokens,
            }
        return response

    async def _events(self, index: int, model: str, outcome: Outcome) -> AsyncIterator[bytes]:
        seq: Iterator[int] = iter(range(1_000))

        def event(kind: str, **fields: Any) -> bytes:
            return _sse({"type": kind, "sequence_number": next(seq), **fields})

        text_position = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
        yield event(
            "response.created", response=self._response(index, model, "in_progress", usage=False)
        )
        yield event("response.output_item.added", output_index=0, item=_ITEM)
        yield event("response.content_part.added", part=_PART, **text_position)
        for _ in range(self.text_chunks):
            yield event("response.output_text.delta", delta="hi", logprobs=[], **text_position)
            if outcome == "error_event":
                yield event("error", code="server_error", message="server error", param=None)
                return
            if outcome == "connection_drop":
                raise httpx2.ReadError("connection reset by peer")
        yield event(f"response.{outcome}", response=self._response(index, model, outcome))
