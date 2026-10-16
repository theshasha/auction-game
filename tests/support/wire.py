"""Offline production Agent / SDK wire contracts; see docs/pydantic-ai-integration.md.

No dotenv, provider discovery or real HTTP: every SDK gets a fake key and MockTransport.
Successful-stream checking and SDK construction use the production model factory.
"""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import pytest

import httpx2 as http

from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ToolCallPart, ToolReturnPart, ModelRequest, SystemPromptPart

from tests.support.factories import CHANNELS, TEST_GAME
from auction_game.configuration import channel_model_profile
from auction_game.ai.llm_client import ModelFactory, RequestCapture
from auction_game.reporting.replay import capture_protocol_definition, protocol_tools
from auction_game.ai.agent_tasks import AuctionDeps, BidOutput as Bid, make_agent as production_agent


PROTOCOLS = ("chat", "responses", "anthropic")
MODEL = "relay-custom-gemini-alias"
SYSTEM = "private system, only once"
GOOD = {"strategy": "keep private reasoning", "bid": 2, "super_bid": None}


def make_agent(model):
    return production_agent(model, "bid", settings={"timeout": 3.0, "max_tokens": 123})


def sse(protocol, data=GOOD, *, ending="ok", tool=True, tool_name="bid", tool_id="call_1", text="no tool"):
    """Small wire fixtures, split JSON arguments across genuine SSE deltas."""
    args = json.dumps(data)
    halves = (args[:len(args) // 2], args[len(args) // 2:])
    events = []
    if protocol == "chat":
        def chunk(delta, finish=None):
            return {"id": "chat_1", "object": "chat.completion.chunk", "created": 1,
                    "model": MODEL, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if tool:
            events.append(chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": tool_id,
                           "type": "function", "function": {"name": tool_name, "arguments": halves[0]}}]}))
            events.append(chunk({"tool_calls": [{"index": 0, "function": {"arguments": halves[1]}}]}))
        else:
            events.append(chunk({"role": "assistant", "content": text}))
        if ending != "missing":
            events.append(chunk({}, "length" if ending == "limit" else "tool_calls" if tool else "stop"))
        return [("data: " + json.dumps(e) + "\n\n").encode() for e in events] + [b"data: [DONE]\n\n"]
    if protocol == "responses":
        response = {"id": "resp_1", "object": "response", "created_at": 1, "model": MODEL,
                    "status": "in_progress", "output": [], "error": None, "incomplete_details": None,
                    "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}}
        events.append({"type": "response.created", "response": response})
        item = {"type": "function_call", "id": "fc_1", "call_id": tool_id, "name": tool_name,
                "arguments": "", "status": "in_progress"}
        if tool:
            events.append({"type": "response.output_item.added", "output_index": 0, "item": item})
            for part in halves:
                events.append({"type": "response.function_call_arguments.delta", "item_id": "fc_1",
                               "output_index": 0, "delta": part})
            events.append({"type": "response.function_call_arguments.done", "item_id": "fc_1",
                           "output_index": 0, "arguments": args})
            item = dict(item, arguments=args, status="completed")
            events.append({"type": "response.output_item.done", "output_index": 0, "item": item})
        else:
            part = {"type": "output_text", "text": text, "annotations": []}
            item = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                    "content": [part]}
            events.append({"type": "response.output_item.added", "output_index": 0,
                           "item": dict(item, content=[], status="in_progress")})
            events.append({"type": "response.content_part.added", "item_id": "msg_1", "output_index": 0,
                           "content_index": 0, "part": dict(part, text="")})
            events.append({"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0,
                           "content_index": 0, "delta": text})
            events.append({"type": "response.output_item.done", "output_index": 0, "item": item})
        if ending != "missing":
            status = "incomplete" if ending == "limit" else "completed"
            final = dict(response, status=status, output=[item],
                         incomplete_details={"reason": "max_output_tokens"} if ending == "limit" else None)
            events.append({"type": "response." + status, "response": final})
    else:
        events.append({"type": "message_start", "message": {"id": "msg_1", "type": "message",
                       "role": "assistant", "model": MODEL, "content": [], "stop_reason": None,
                       "stop_sequence": None, "usage": {"input_tokens": 2, "output_tokens": 0}}})
        if tool:
            events.append({"type": "content_block_start", "index": 0, "content_block": {
                           "type": "tool_use", "id": tool_id, "name": tool_name, "input": {}}})
            for part in halves:
                events.append({"type": "content_block_delta", "index": 0,
                               "delta": {"type": "input_json_delta", "partial_json": part}})
            events.append({"type": "content_block_stop", "index": 0})
        else:
            events.append({"type": "content_block_start", "index": 0,
                           "content_block": {"type": "text", "text": ""}})
            events.append({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "text_delta", "text": text}})
            events.append({"type": "content_block_stop", "index": 0})
        if ending != "missing":
            events.append({"type": "message_delta", "delta": {
                           "stop_reason": "max_tokens" if ending == "limit" else "tool_use" if tool else "end_turn",
                           "stop_sequence": None}, "usage": {"output_tokens": 3}})
            events.append({"type": "message_stop"})
    return [("event: " + e["type"] + "\ndata: " + json.dumps(dict(e, sequence_number=i)) + "\n\n").encode()
            for i, e in enumerate(events)]


class Wire(http.AsyncByteStream):
    def __init__(self, chunks, broken=False):
        self.chunks, self.broken, self.closed = chunks, broken, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.broken:
            raise http.ReadError("simulated interruption after complete arguments")

    async def aclose(self):
        self.closed = True


class Harness:
    def __init__(self, protocol, script):
        self.protocol, self.script = protocol, list(script)
        self.requests, self.streams = [], []
        self.loops = []
        self.closes = 0

        harness = self

        class Transport(http.MockTransport):
            async def aclose(self):
                harness.closes += 1
                await super().aclose()

        self.factory = ModelFactory(self.protocol, MODEL,
                                    "https://offline.invalid/anthropic" if self.protocol == "anthropic"
                                    else "https://offline.invalid/v1", "fake-key", 3.0, 123,
                                    Transport(self.handle))

    async def handle(self, request):
        assert request.url.host == "offline.invalid"
        self.loops.append(asyncio.get_running_loop())
        self.requests.append((request, json.loads(request.content)))
        item = self.script.pop(0)
        if item == "connect":
            raise http.ConnectError("offline", request=request)
        if isinstance(item, int):
            return http.Response(item, json={"error": {"type": "invalid_request_error", "message": "bad config"}})
        data, options = item
        options = dict(options)
        broken = options.pop("broken", False)
        body = self.requests[-1][1]
        tools = body.get("tools", [])
        tool_name = tools[0].get("function", tools[0])["name"] if tools else "bid"
        chunks = sse(self.protocol, data, tool_name=tool_name,
                     tool_id=f"call_{len(self.requests)}", **options)
        if broken and self.protocol == "chat":
            # [DONE] is a successful SDK terminator; interrupt before it, not after it.
            chunks = chunks[:-1]
        wire = Wire(chunks, broken)
        self.streams.append(wire)
        return http.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    async def __aenter__(self):
        self.context = self.factory.open()
        model = await self.context.__aenter__()
        self.sdk = model.wrapped.client
        self.model = model
        return self

    async def __aexit__(self, *exc):
        await self.context.__aexit__(*exc)
        assert self.sdk.is_closed()
        assert all(stream.closed for stream in self.streams)


def reply(data=None, **options):
    return (GOOD if data is None else data, options)


async def run(agent, prompt="today", history=(), coins=7):
    # event_stream_handler makes Agent.run stream every model node, including correction.
    # Unlike run_stream(), it drives the whole graph and output retry machinery.
    async def drain(ctx, events):
        async for _ in events:
            pass
    history = list(history) or [ModelRequest(parts=[SystemPromptPart(SYSTEM)])]
    return await agent.run(prompt, message_history=deepcopy(history), deps=AuctionDeps(TEST_GAME, coins),
                           event_stream_handler=drain)
