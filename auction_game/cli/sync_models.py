"""同步 models.dev 公共能力；默认仅预览，--write 原子更新离线快照，不调用模型。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import difflib
import json
import os
from pathlib import Path
import tempfile
from urllib.request import Request, urlopen

from auction_game.configuration import MODELS_PATH, load_model_catalog
from auction_game.model_capabilities import effective_capabilities, positive_integer, read_snapshot
from auction_game.paths import CAPABILITIES_PATH

API_URL = "https://models.dev/api.json"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 键重复，来源有歧义：{key}")
        result[key] = value
    return result


def build_snapshot(catalog: dict, data: dict, retrieved_from: str) -> dict:
    if (not isinstance(data, dict) or not data or any(
        not isinstance(provider, dict) or not isinstance(provider.get("models"), dict)
        for provider in data.values()
    )):
        raise ValueError("models.dev 响应结构无效：需要 provider → models mapping")
    entries = {}
    for key, model in catalog.items():
        source = model["models_dev"]
        provider = data.get(source["provider"], {})
        item = provider.get("models", {}).get(source["model"])
        entry = {"source": source}
        if item is None:
            entry.update(status="unknown", reason="精确来源未找到；未进行模糊匹配")
        else:
            if (not isinstance(item, dict) or item.get("id") != source["model"]
                    or not isinstance(item.get("limit"), dict)):
                raise ValueError(f"{key}：匹配条目结构异常或 id 不一致")
            for flag in ("reasoning", "tool_call"):
                if flag in item and type(item[flag]) is not bool:
                    raise ValueError(f"{key}：{flag} 必须是布尔值")
            options = item.get("reasoning_options")
            if options is not None and (not isinstance(options, list) or any(
                not isinstance(option, dict) or not isinstance(option.get("type"), str)
                or (option["type"] == "effort" and (
                    not isinstance(option.get("values"), list)
                    or any(not isinstance(value, str) for value in option["values"])))
                for option in options
            )):
                raise ValueError(f"{key}：reasoning_options 结构异常")
            canonical = source.get("canonical_model_id")
            if canonical and item.get("canonical_model_id") != canonical:
                raise ValueError(f"{key}：canonical_model_id 已变更，请核对来源映射")
            entry.update(status="matched", matched_id=item["id"],
                         canonical_model_id=item.get("canonical_model_id"),
                         limit={field: positive_integer(item["limit"].get(field))
                                for field in ("context", "input", "output")},
                         reasoning=item.get("reasoning"),
                         reasoning_options=item.get("reasoning_options"), tool_call=item.get("tool_call"))
        entries[key] = entry
    return {"schema_version": 1, "retrieved_from": retrieved_from,
            "fetched_at": datetime.now(timezone.utc).isoformat(), "models": entries}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=MODELS_PATH)
    parser.add_argument("--input", type=Path, help="离线导入原始 models.dev JSON")
    parser.add_argument("--output", type=Path, help="默认 data/model_capabilities.json")
    parser.add_argument("--write", action="store_true", help="写入快照；省略时只预览差异")
    args = parser.parse_args(argv)
    output = args.output or CAPABILITIES_PATH
    try:
        catalog = load_model_catalog(args.models, resolve_capabilities=False)
        if args.input:
            text = args.input.read_text(encoding="utf-8")
        else:
            request = Request(API_URL, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urlopen(request, timeout=60) as response:
                text = response.read().decode("utf-8")
        data = json.loads(text, object_pairs_hook=unique_object)
        snapshot = build_snapshot(catalog, data, str(args.input.resolve()) if args.input else API_URL)
        old = read_snapshot(output)
        # 获取时间不产生无意义的能力差异；数据与来源未变时不重写。
        comparison = {key: value for key, value in snapshot.items() if key != "fetched_at"}
        previous = {key: value for key, value in old.items() if key != "fetched_at"}
        changed = comparison != previous
        if changed:
            before = json.dumps(previous, ensure_ascii=False, indent=2).splitlines()
            after = json.dumps(comparison, ensure_ascii=False, indent=2).splitlines()
            print("\n".join(difflib.unified_diff(before, after, fromfile=str(output), tofile="models.dev")))
        else:
            print("能力无变化。")
        for key, model in catalog.items():
            effective = effective_capabilities(key, model, snapshot)
            cap = effective["capabilities"]
            print(f"{key}: max_tokens={effective['max_tokens']} / reasoning={cap['reasoning_status']} / "
                  f"{cap['reasoning_detail']}")
            if cap["output_fallback_reason"]:
                print(f"  预算回退：{cap['output_fallback_reason']}")
        if args.write and changed:
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                             prefix=output.name + ".", suffix=".tmp", delete=False) as handle:
                handle.write(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
                temporary = handle.name
            os.replace(temporary, output)
            print(f"已更新 {output}")
        elif changed:
            print("仅预览；使用 --write 更新本地快照。")
    except (OSError, ValueError) as exc:
        print(f"同步失败，已有快照保持不变：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
