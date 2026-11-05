"""由离线能力快照得出每个模型的输出预算和推理设置；取不到来源的最大输出时用 DEFAULT_MAX_TOKENS。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # configuration 导入了本模块，运行时反过来导入会循环。
    from auction_game.configuration import ModelEntry

DEFAULT_MAX_TOKENS = 64000


def read_snapshot(path: Path) -> dict:
    """读取 sync_models 写出的快照；文件不存在时视为空快照。"""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def effective_capabilities(model: ModelEntry, snapshot: dict) -> dict:
    """快照条目的来源与 models.yaml 不一致时整条作废，等重新同步。

    输出预算只取来源的最大输出，context 和 input 不参与计算。
    """
    entry = snapshot.get("models", {}).get(model.key, {})
    if entry.get("source") != model.models_dev:
        entry, reason = {}, "能力快照缺失或来源已变更；请同步"
    elif entry["status"] != "matched":
        reason = entry["reason"]
    else:
        reason = None
    limit = entry.get("limit", {})
    output = limit.get("output")
    options = entry.get("reasoning_options", [])
    has_medium = any(option.get("type") == "effort" and "medium" in option.get("values", [])
                     for option in options)
    # 比赛一轮常超过 5 分钟。消息级缓存点是兼容网关用的写法，不依赖请求顶层的 cache_control。
    settings = {"anthropic_cache_messages": "1h"} if model.protocol == "anthropic" else {}
    if model.reasoning_effort is not None:
        status, detail = model.reasoning_effort, "models.yaml 指定"
    elif has_medium:
        status, detail = "medium", "来源列出 medium"
    else:
        status, detail = "default", "沿用渠道默认"
    if status != "default" and model.protocol == "anthropic":
        # hub 的 claude-opus-5-5 收到 effort 时要求同时显式声明 adaptive thinking。
        settings.update(anthropic_thinking={"type": "adaptive"}, anthropic_effort=status)
    elif status != "default":
        settings["openai_reasoning_effort"] = status
    return {
        "max_tokens": output or DEFAULT_MAX_TOKENS,
        "model_settings": settings,
        "capabilities": {
            "source": model.models_dev, "retrieved_from": snapshot.get("retrieved_from"),
            "fetched_at": snapshot.get("fetched_at"), "status": entry.get("status", "unknown"),
            "matched_id": entry.get("matched_id"), "limit": limit,
            "reasoning": entry.get("reasoning"), "reasoning_options": options,
            "output_fallback_reason": reason or (None if output else "来源缺少有效最大输出"),
            "reasoning_status": status, "reasoning_detail": detail,
        },
    }
