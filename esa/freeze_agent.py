#!/usr/bin/env python3
"""从 agent-runtime 的 configs/config.toml 生成评测用 agent 定义。

指令与模型参数原样复制，工具清单换成 mock-system 评测模式实际暴露的集合。
agent-runtime 在建立会话时要求每个声明的工具都存在，声明了未注册的工具会直接拒绝。

用法：
    python3 freeze_agent.py --config /path/to/config.toml --output agent.json
"""

import argparse
import copy
import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 及更早版本
    import tomli as tomllib

# mock-system 评测模式暴露的工具（62 个注册，移除 17 个后剩下 45 个）。
EXPOSED = {
    "a2a_invoke", "activate_autopilot", "add_memory", "add_waypoint", "after_sale_agent",
    "control_map_zoom", "delete_favorite_poi", "delete_waypoint", "digital_service_agent",
    "display_candidate_cards", "end_chat", "estimate_trip_info", "execute_vehicle_control",
    "exit_navigation", "get_all_waypoint", "get_favorite_poi", "get_home_or_company_address",
    "get_joke", "get_navi_info", "get_story", "get_vehicle_status", "multi_task_agent",
    "music_search", "open_or_close_map", "open_special_map", "optimize_navi_plan",
    "overview_navi_route", "plan_navi_route", "play_playlist", "query_adas_status",
    "query_traffic_ahead", "reroute", "scheduled_task", "search_along_way_poi",
    "search_charging_station", "search_memory", "search_vehicle_knowledge",
    "set_display_focus", "set_favorite_poi", "set_map_auto_scale", "set_map_view",
    "set_navi_route_preference", "set_navi_voice_mode", "share_route", "start_navi",
}

# 用例需要、但默认 assistant 定义没有声明的工具。媒体播放类工具不在默认声明里，
# 评测用例要求模型能选到音乐检索与播放，因此补齐。
EXTRA_TOOLS = [
    "display_candidate_cards",
    "end_chat",
    "execute_vehicle_control",
    "get_vehicle_status",
    "music_search",
    "overview_navi_route",
    "play_playlist",
]

BASE_AGENT = "assistant"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--agent", default=BASE_AGENT)
    parser.add_argument("--agent-id", default="esa_eval")
    parser.add_argument("--model", default=None, help="覆盖模型名，缺省沿用配置里的 model_id")
    args = parser.parse_args()

    with open(args.config, "rb") as handle:
        cfg = tomllib.load(handle)

    base = cfg["agents"]["definitions"][args.agent]
    react = copy.deepcopy(base["react"])

    if args.model:
        model = dict(react.get("model", {}))
        model["model_id"] = args.model
        react["model"] = model

    # 上游网关按响应编号续接上下文时不回带历史，模型只能看到工具结果的文本，
    # 会把结果当成用户新发来的消息。改为每次请求回放完整历史。
    react["context_continuity"] = "replay"

    declared = sorted(react.get("tools", {}))
    dropped = [name for name in declared if name not in EXPOSED]
    kept = {name: react["tools"][name] for name in declared if name in EXPOSED}

    missing = [name for name in EXTRA_TOOLS if name not in EXPOSED]
    if missing:
        raise SystemExit(f"补齐的工具不在暴露集合里：{missing}")
    for name in EXTRA_TOOLS:
        kept.setdefault(name, {})

    react["tools"] = kept

    payload = {
        "id": args.agent_id,
        "display_name": base.get("display_name", args.agent),
        "description": base.get("description", ""),
        "engine": "react",
        "capabilities": base.get("capabilities", []),
        "react": react,
    }

    Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf8")
    print(json.dumps({
        "output": args.output,
        "declared": len(declared),
        "kept": len(kept),
        "dropped": dropped,
        "kept_tools": sorted(kept),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
