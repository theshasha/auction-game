"""同步 models.dev 模型能力；默认只预览，--write 保存到本地，不调用模型。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import difflib
import json
from pathlib import Path
from urllib.request import Request, urlopen

from auction_game.configuration import MODELS_PATH, ModelEntry, load_model_entries
from auction_game.model_capabilities import effective_capabilities, read_snapshot
from auction_game.paths import CAPABILITIES_PATH

API_URL = "https://models.dev/api.json"


def positive_integer(value):
    return value if type(value) is int and value > 0 else None


def build_snapshot(catalog: dict[str, ModelEntry], data: dict, retrieved_from: str) -> dict:
    """按 models.yaml 的精确来源取条目；数值上限规范成正整数或 None。"""
    entries = {}
    for key, model in catalog.items():
        source = model.models_dev
        item = data.get(source["provider"], {}).get("models", {}).get(source["model"])
        entry = {"source": source}
        if item is None:
            entry.update(status="unknown", reason="未找到配置指定的精确来源")
        else:
            limit = item.get("limit", {})
            entry.update(status="matched", matched_id=item["id"],
                         limit={field: positive_integer(limit.get(field))
                                for field in ("context", "input", "output")},
                         reasoning=item.get("reasoning"),
                         reasoning_options=item.get("reasoning_options") or [])
        entries[key] = entry
    return {"schema_version": 1, "retrieved_from": retrieved_from,
            "fetched_at": datetime.now(timezone.utc).isoformat(), "models": entries}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=MODELS_PATH)
    parser.add_argument("--input", type=Path, help="从本地 models.dev JSON 导入")
    parser.add_argument("--output", type=Path, help="默认 data/model_capabilities.json")
    parser.add_argument("--write", action="store_true", help="保存模型能力；省略时只预览差异")
    args = parser.parse_args(argv)
    output = args.output or CAPABILITIES_PATH
    try:
        catalog = load_model_entries(args.models)
        if args.input:
            text = args.input.read_text(encoding="utf-8")
        else:
            request = Request(API_URL, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urlopen(request, timeout=60) as response:
                text = response.read().decode("utf-8")
        snapshot = build_snapshot(catalog, json.loads(text), str(args.input.resolve()) if args.input else API_URL)
        old = read_snapshot(output)
        # 获取时间不算能力差异；能力和来源都没变时不重写。
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
            effective = effective_capabilities(model, snapshot)
            cap = effective["capabilities"]
            print(f"{key}: max_tokens={effective['max_tokens']} / reasoning={cap['reasoning_status']} / "
                  f"{cap['reasoning_detail']}")
            if cap["output_fallback_reason"]:
                print(f"  暂用默认输出预算：{cap['output_fallback_reason']}")
        if args.write and changed:
            output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"已更新 {output}")
        elif changed:
            print("仅预览；使用 --write 保存模型能力。")
    except (OSError, ValueError) as exc:
        print(f"同步失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
