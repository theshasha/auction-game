"""Offline production model / future Agent contract; see docs/pydantic-ai-integration.md.

No dotenv, provider discovery or real HTTP: every SDK gets a fake key and MockTransport.
Successful-stream checking and SDK construction use the production model factory.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
import json

import pytest

import httpx2 as http

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolCallPart, ToolReturnPart
from pydantic_ai.models.wrapper import WrapperModel

from conftest import TEST_GAME
from game_rules import validate_submission
from llm_client import IncompleteStream, ModelFactory


PROTOCOLS = ("chat", "responses", "anthropic")
MODEL = "relay-custom-gemini-alias"
SYSTEM = "private system, only once"
GOOD = {"strategy": "keep private reasoning", "bid": 2, "super_bid": None}


class Bid(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)
    strategy: str = Field(min_length=1)
    bid: int
    super_bid: int | None


class Plan(Bid):
    """Planning returns strategy plus the cached first-day bid in one result."""


class Speech(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)
    strategy: str = Field(min_length=1)
    speech: str = Field(min_length=1)


class CandidateRetry(WrapperModel):
    """Step-three feasibility only; production step two deliberately has no retry."""

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        for attempt in range(2):
            try:
                async with self.wrapped.request_stream(
                    messages, model_settings, model_request_parameters, run_context
                ) as stream:
                    yield stream
                return
            except (http.TransportError, ModelAPIError) as exc:
                # HTTP status/config errors are not transport retries.
                if isinstance(exc, ModelAPIError) and not isinstance(exc.__cause__, http.TransportError):
                    cause = exc.__cause__
                    if cause is None or not isinstance(cause.__cause__, http.TransportError):
                        raise
                if attempt:
                    raise


def make_agent(model, output=Bid):
    agent = Agent(model, output_type=ToolOutput(output, name=output.__name__.lower()),
                  system_prompt=SYSTEM, deps_type=int, retries=1,
                  model_settings={"timeout": 3.0, "max_tokens": 123})
    if issubclass(output, Bid):
        @agent.output_validator
        def validate(ctx: RunContext[int], result):
            reason = validate_submission(result.bid, result.super_bid, ctx.deps, TEST_GAME)
            if reason:
                raise ModelRetry(reason)
            return result
    return agent


def sse(protocol, data=GOOD, *, ending="ok", tool=True, tool_name="bid", text="no tool"):
    """Small wire fixtures, split JSON arguments across genuine SSE deltas."""
    args = json.dumps(data)
    halves = (args[:len(args) // 2], args[len(args) // 2:])
    events = []
    if protocol == "chat":
        def chunk(delta, finish=None):
            return {"id": "chat_1", "object": "chat.completion.chunk", "created": 1,
                    "model": MODEL, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if tool:
            events.append(chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1",
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
        item = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": tool_name,
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
                           "type": "tool_use", "id": "call_1", "name": tool_name, "input": {}}})
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
    def __init__(self, protocol, script, *, buffered=True, retry=False):
        self.protocol, self.script, self.buffered = protocol, list(script), buffered
        self.retry = retry
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
        broken = options.pop("broken", False)
        body = self.requests[-1][1]
        tools = body.get("tools", [])
        tool_name = tools[0].get("function", tools[0])["name"] if tools else "bid"
        chunks = sse(self.protocol, data, tool_name=tool_name, **options)
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
        self.model = model if self.buffered else model.wrapped
        if self.retry:
            self.model = CandidateRetry(self.model)
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
    return await agent.run(prompt, message_history=deepcopy(list(history)), deps=coins,
                           event_stream_handler=drain)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_real_sdk_stream_contract(protocol):
    async def scenario():
        async with Harness(protocol, [reply()]) as h:
            result = await run(make_agent(h.model))
            assert result.output == Bid(**GOOD)
            assert len(h.requests) == 1
            request, body = h.requests[0]
            assert request.url.path == {"chat": "/v1/chat/completions", "responses": "/v1/responses",
                                        "anthropic": "/anthropic/v1/messages"}[protocol]
            assert body["model"] == MODEL and body["stream"] is True
            field = "max_output_tokens" if protocol == "responses" else "max_tokens"
            assert body[field] == 123 and "max_completion_tokens" not in body
            assert request.extensions["timeout"]["read"] == 3.0
            assert request.headers["x-api-key" if protocol == "anthropic" else "authorization"] == (
                "fake-key" if protocol == "anthropic" else "Bearer fake-key")
            assert json.dumps(body).count(SYSTEM) == 1
            assert json.dumps(body).count('"today"') == 1
            assert body["tool_choice"] == ({"type": "any"} if protocol == "anthropic" else "required")
            tool = body["tools"][0]
            assert tool.get("function", tool)["name"] == "bid"
            # Unknown aliases retain the provider's default profile, not guessed model-brand routing.
            assert h.model.profile["supports_tools"] is True
            parts = [p for m in result.all_messages() for p in m.parts]
            call = next(p for p in parts if isinstance(p, ToolCallPart))
            returned = next(p for p in parts if isinstance(p, ToolReturnPart))
            assert call.tool_call_id == returned.tool_call_id
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_legal_super_bid_uses_shared_rules(protocol):
    async def scenario():
        async with Harness(protocol, [reply(dict(GOOD, super_bid=0))]) as h:
            result = await run(make_agent(h.model), coins=4)
            assert result.output.super_bid == 0 and len(h.requests) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("invalid", [
    {"strategy": "s", "super_bid": None},
    dict(GOOD, bid="2"), dict(GOOD, bid=True), dict(GOOD, bid=8),
    dict(GOOD, bid=6), dict(GOOD, super_bid=2), dict(GOOD, bid=4, super_bid=0),
    dict(GOOD, super_bid=True),
])
def test_one_shared_correction_budget(protocol, invalid):
    async def scenario():
        async with Harness(protocol, [reply(invalid), reply()]) as h:
            result = await run(make_agent(h.model), coins=5)
            assert result.output.bid == 2 and len(h.requests) == 2
        async with Harness(protocol, [reply(invalid), reply(dict(GOOD, bid="2"))]) as h:
            with pytest.raises(UnexpectedModelBehavior, match="retries"):
                await run(make_agent(h.model), coins=5)
            assert len(h.requests) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_unwrapped_agent_accepts_full_json_even_when_truncated(protocol):
    """Pin why checking finish status at the model boundary is necessary."""
    async def scenario():
        async with Harness(protocol, [reply(ending="limit")], buffered=False) as h:
            result = await run(make_agent(h.model))
            assert result.output.bid == 2 and len(h.requests) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("ending", ["missing", "limit"])
def test_full_json_still_requires_successful_finish(protocol, ending):
    async def scenario():
        async with Harness(protocol, [reply(ending=ending)]) as h:
            with pytest.raises((IncompleteStream, ModelAPIError)):
                await run(make_agent(h.model))
            assert len(h.requests) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_no_tool_exhausts_correction(protocol):
    async def scenario():
        async with Harness(protocol, [reply(tool=False), reply(tool=False)]) as h:
            with pytest.raises(UnexpectedModelBehavior):
                await run(make_agent(h.model))
            assert len(h.requests) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("fault", ["connect", "stream"])
def test_network_retries_only_current_request(protocol, fault):
    async def scenario():
        initial_failure = "connect" if fault == "connect" else reply(broken=True, ending="missing")
        async with Harness(protocol, [initial_failure, reply()], retry=True) as h:
            result = await run(make_agent(h.model))
            assert result.output.bid == 2 and len(h.requests) == 2
            assert h.requests[0][1] == h.requests[1][1]
            assert len([p for m in result.all_messages() for p in m.parts if isinstance(p, ToolCallPart)]) == 1
        failure = "connect" if fault == "connect" else reply(broken=True, ending="missing")
        async with Harness(protocol, [reply(dict(GOOD, bid="2")), failure, reply()], retry=True) as h:
            result = await run(make_agent(h.model))
            assert result.output.bid == 2 and len(h.requests) == 3
            assert h.requests[1][1] == h.requests[2][1]
            assert len([p for m in result.all_messages() for p in m.parts if isinstance(p, ToolCallPart)]) == 2
        failure = "connect" if fault == "connect" else reply(broken=True, ending="missing")
        again = "connect" if fault == "connect" else reply(broken=True, ending="missing")
        async with Harness(protocol, [failure, again], retry=True) as h:
            with pytest.raises((http.TransportError, ModelAPIError)):
                await run(make_agent(h.model))
            assert len(h.requests) == 2
        failure = "connect" if fault == "connect" else reply(broken=True, ending="missing")
        async with Harness(protocol, [reply(dict(GOOD, bid="2")), failure, reply(dict(GOOD, bid=99))], retry=True) as h:
            with pytest.raises(UnexpectedModelBehavior, match="retries"):
                await run(make_agent(h.model))
            assert len(h.requests) == 3
        first_fault = "connect" if fault == "connect" else reply(broken=True, ending="missing")
        correction_fault = "connect" if fault == "connect" else reply(broken=True, ending="missing")
        async with Harness(protocol, [first_fault, reply(dict(GOOD, bid="2")), correction_fault, reply()], retry=True) as h:
            result = await run(make_agent(h.model))
            assert result.output.bid == 2 and len(h.requests) == 4
            assert h.requests[0][1] == h.requests[1][1]
            assert h.requests[2][1] == h.requests[3][1]
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_bad_request_is_not_a_network_retry(protocol):
    async def scenario():
        async with Harness(protocol, [400]) as h:
            with pytest.raises(ModelAPIError):
                await run(make_agent(h.model))
            assert len(h.requests) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("output,data", [
    (Plan, dict(GOOD, strategy=True)),
    (Speech, {"strategy": "private", "speech": 123}),
    (Speech, {"strategy": "private", "speech": "   "}),
])
def test_planning_and_speech_are_typed(output, data):
    async def scenario():
        async with Harness("chat", [reply(data), reply(data)]) as h:
            with pytest.raises(UnexpectedModelBehavior, match="retries"):
                await run(make_agent(h.model, output))
            assert len(h.requests) == 2
    asyncio.run(scenario())


def test_simple_summary_uses_independent_text_context():
    async def scenario():
        async with Harness("chat", [reply(), reply(tool=False)]) as h:
            first = await run(make_agent(h.model))
            history = first.all_messages()
            before = deepcopy(history)
            summary_agent = Agent(h.model, system_prompt="audience context", output_type=str)
            summary = await run(summary_agent, "summary for audience")
            assert summary.output == "no tool" and history == before
            body = json.dumps(h.requests[1][1])
            assert SYSTEM not in body and "call_1" not in body
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_history_across_task_types_and_transaction(protocol):
    async def scenario():
        history, pending = [], (1, None)
        async with Harness(protocol, [reply(), reply({"strategy": "private speech", "speech": "public"}),
                                     reply(broken=True), reply(broken=True)]) as h:
            plan = await run(make_agent(h.model, Plan), "plan", history)
            history.extend(plan.new_messages())
            pending = (plan.output.bid, plan.output.super_bid)
            speech = await run(make_agent(h.model, Speech), "speak", history)
            history.extend(speech.new_messages())
            assert speech.output.speech == "public" and speech.output.strategy == "private speech"
            encoded = ModelMessagesTypeAdapter.dump_json(history)
            assert ModelMessagesTypeAdapter.dump_json(ModelMessagesTypeAdapter.validate_json(encoded)) == encoded
            body = h.requests[1][1]
            assert json.dumps(body).count(SYSTEM) == 1
            assert json.dumps(body).index('"plan"') < json.dumps(body).index('"speak"')
            # Previous output call and its acknowledgement survive output-schema changes.
            serialized = json.dumps(body)
            assert serialized.count("call_1") >= 2
            assert "plan" in serialized and "speech" in serialized
            before = deepcopy(history), pending
            with pytest.raises((http.TransportError, ModelAPIError)):
                await run(make_agent(h.model), "fails", history)
            assert (history, pending) == before
    asyncio.run(scenario())


def test_controller_style_threads_have_local_loops_and_clients():
    def worker(index):
        async def task(prompt, history=()):
            async with Harness("chat", [reply(dict(GOOD, bid=index))]) as h:
                return await run(make_agent(h.model), prompt, history)
        first = asyncio.run(task(f"player-{index}"))
        second = asyncio.run(task(f"next-{index}", first.all_messages()))
        return second.output.bid, ModelMessagesTypeAdapter.dump_json(second.all_messages())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, range(4)))
    for index, (bid, messages) in enumerate(results):
        assert bid == index and f"player-{index}".encode() in messages
        assert all(f"player-{other}".encode() not in messages for other in range(4) if other != index)
