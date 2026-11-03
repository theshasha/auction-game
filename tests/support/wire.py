"""三种协议的 SSE 夹具和 MockTransport：用生产模型工厂构造真实 SDK，只替换 HTTP 层。"""

import asyncio
from copy import deepcopy
import json

import httpx2 as http

from pydantic_ai.messages import ModelRequest, SystemPromptPart

from tests.support.factories import TEST_GAME, text_reply
from auction_game.llm.client import ModelFactory
from auction_game.agent.tasks import AuctionDeps, TASKS, make_agent as production_agent


PROTOCOLS = ("chat", "responses", "anthropic")
MODEL = "relay-custom-gemini-alias"
SYSTEM = "private system, only once"
GOOD = {"strategy": "keep private reasoning", "bid": 2, "super_bid": None}


def make_agent(model):
    return production_agent(model, TASKS["bid"])


def sse(protocol, data=GOOD, *, ending="ok", text=None, thinking=None):
    """真实 SSE 文本夹具。"""
    if text is None:
        text = text_reply(data)
    events = []
    if protocol == "chat":
        def chunk(delta, finish=None):
            return {"id": "chat_1", "object": "chat.completion.chunk", "created": 1,
                    "model": MODEL, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if thinking:
            events.append(chunk({"role": "assistant", "reasoning_content": thinking}))
        events.append(chunk({"role": "assistant", "content": text}))
        if ending != "missing":
            events.append(chunk({}, "length" if ending == "limit" else "stop"))
        return [("data: " + json.dumps(e) + "\n\n").encode() for e in events] + [b"data: [DONE]\n\n"]
    if protocol == "responses":
        response = {"id": "resp_1", "object": "response", "created_at": 1, "model": MODEL,
                    "status": "in_progress", "output": [], "error": None, "incomplete_details": None,
                    "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}}
        events.append({"type": "response.created", "response": response})
        reasoning = {"type": "reasoning", "id": "rs_old", "summary": [{"type": "summary_text", "text": thinking}],
                     "encrypted_content": "encrypted_old_reasoning"}
        if thinking:
            events.append({"type": "response.output_item.added", "output_index": 1,
                           "item": dict(reasoning, summary=[], encrypted_content=None)})
            events.append({"type": "response.reasoning_summary_text.delta", "item_id": "rs_old",
                           "output_index": 1, "summary_index": 0, "delta": thinking})
            events.append({"type": "response.output_item.done", "output_index": 1, "item": reasoning})
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
            final = dict(response, status=status, output=([reasoning] if thinking else []) + [item],
                         incomplete_details={"reason": "max_output_tokens"} if ending == "limit" else None)
            events.append({"type": "response." + status, "response": final})
    else:
        events.append({"type": "message_start", "message": {"id": "msg_1", "type": "message",
                       "role": "assistant", "model": MODEL, "content": [], "stop_reason": None,
                       "stop_sequence": None, "usage": {"input_tokens": 2, "output_tokens": 0}}})
        if thinking:
            events.extend([
                {"type": "content_block_start", "index": 1, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "thinking_delta", "thinking": thinking}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "signature_delta", "signature": "signed_old_reasoning"}},
                {"type": "content_block_stop", "index": 1},
                {"type": "content_block_start", "index": 2, "content_block": {"type": "redacted_thinking", "data": "redacted_old_reasoning"}},
                {"type": "content_block_stop", "index": 2},
            ])
        events.append({"type": "content_block_start", "index": 0,
                       "content_block": {"type": "text", "text": ""}})
        events.append({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": text}})
        events.append({"type": "content_block_stop", "index": 0})
        if ending != "missing":
            events.append({"type": "message_delta", "delta": {
                           "stop_reason": "max_tokens" if ending == "limit" else "end_turn",
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
                                    Transport(self.handle), retry_delay=0)

    async def handle(self, request):
        assert request.url.host == "offline.invalid"
        self.loops.append(asyncio.get_running_loop())
        self.requests.append((request, json.loads(request.content)))
        item = self.script.pop(0)
        if item == "connect":
            raise http.ConnectError("offline", request=request)
        if item == "hang":  # 模拟进行中的请求，只能被取消
            await asyncio.Event().wait()
        if isinstance(item, int):
            return http.Response(item, json={"error": {"type": "invalid_request_error", "message": "bad config"}})
        if isinstance(item, list):  # 原始 SSE 块
            item = (None, {"chunks": item})
        data, options = item
        options = dict(options)
        broken = options.pop("broken", False)
        chunks = options.pop("chunks", None) or sse(self.protocol, data, **options)
        if broken and self.protocol == "chat":
            # SDK 把 [DONE] 当作正常结束，要在它之前中断。
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
    # 传 event_stream_handler 后 Agent.run 对每次请求（含纠错）都走流式，并完整执行输出重试。
    async def drain(ctx, events):
        async for _ in events:
            pass
    history = list(history) or [ModelRequest(parts=[SystemPromptPart(SYSTEM)])]
    return await agent.run(prompt, message_history=deepcopy(history), deps=AuctionDeps(TEST_GAME, coins),
                           model_settings={"timeout": 3.0, "max_tokens": 123}, event_stream_handler=drain)
