"""离线能力快照与有效调用策略；这里唯一维护未知输出上限的回退值。"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_MAX_TOKENS = 64000


def positive_integer(value):
    return value if type(value) is int and value > 0 else None


def read_snapshot(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or not isinstance(data.get("models"), dict)):
        raise ValueError(f"{path}：能力快照结构无效，请运行 sync_models.py")
    return data


def effective_capabilities(key: str, model: dict, snapshot: dict) -> dict:
    """来源变更后不沿用旧能力；context 不冒充独立 input，也不缩小输出预算。"""
    entry = snapshot.get("models", {}).get(key)
    reason = None
    if not isinstance(entry, dict) or entry.get("source") != model["models_dev"]:
        entry = {}
        reason = "能力快照缺失或来源已变更；请同步"
    elif entry.get("status") != "matched":
        reason = entry.get("reason", "来源未匹配")
    limit = entry.get("limit", {})
    if not isinstance(limit, dict):
        raise ValueError(f"{key}：能力 limit 结构无效")
    output = positive_integer(limit.get("output"))
    fallback = reason or (None if output else "来源缺少有效最大输出")
    options = entry.get("reasoning_options")
    if not isinstance(options, list):
        options = []
    has_medium = any(isinstance(option, dict) and option.get("type") == "effort"
                   and isinstance(option.get("values"), list) and "medium" in option["values"]
                   for option in options)
    settings = {}
    if reason:
        status, detail = "unknown", reason
    elif has_medium and model["protocol"] in ("chat", "responses"):
        status = "medium"
        detail = "来源明确列出 medium；按协议设置 openai_reasoning_effort，渠道兼容待连通验证"
        settings["openai_reasoning_effort"] = "medium"
    elif entry.get("reasoning") is False:
        status, detail = "unsupported", "来源明确标记不支持推理"
    elif has_medium:
        status, detail = "unknown", "来源列出 medium，当前协议未配置对应的公开设置"
    elif options:
        status, detail = "no_medium", "来源未列出 medium；沿用渠道默认，不换算数字预算或擅自开启"
    else:
        status, detail = "unknown", reason or "来源未提供明确推理档位，待核实"
    if "reasoning_effort" in model:
        # 显式调用策略不改写公共能力事实；仍由真实渠道验证兼容性。
        status = model["reasoning_effort"]
        settings["openai_reasoning_effort"] = status
        detail = f"模型配置显式指定 {status}；渠道兼容待连通验证"
    return {
        "max_tokens": output or DEFAULT_MAX_TOKENS,
        "model_settings": settings,
        "capabilities": {
            "source": model["models_dev"], "retrieved_from": snapshot.get("retrieved_from"),
            "fetched_at": snapshot.get("fetched_at"), "status": entry.get("status", "unknown"),
            "matched_id": entry.get("matched_id"), "canonical_model_id": entry.get("canonical_model_id"),
            "limit": {field: positive_integer(limit.get(field)) for field in ("context", "input", "output")},
            "reasoning": entry.get("reasoning"), "reasoning_options": options,
            "output_fallback_reason": fallback,
            "reasoning_status": status, "reasoning_detail": detail,
        },
    }
