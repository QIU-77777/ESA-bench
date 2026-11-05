#!/usr/bin/env python3
"""本地全链路自检：假 MCP server + recorder + 假 agent + driver.mjs 座舱分支。

用法：.venv/bin/python scripts/check-cockpit-driver.py [任务 initial_state.json ...]
不传参数时按内置的导航与空调两个场景各跑一遍。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cockpit.fake_agent import FakeCockpitAgent, make_handler as make_agent_handler
from cockpit.fake_mcp import DATA_VERSION, FakeCockpitMcp, make_handler as make_mcp_handler
from cockpit.recorder import CockpitRecorder, CockpitServer, make_handler as make_recorder_handler
from environment.store import MemoryStore

NAVI = {
    "case_id": "esa-cockpit-navi",
    "instruction": "导航去公司。",
    "initialContext": {},
    "initialState": {"vehicle": {"gear": "D"}, "navigation": {"active": False}},
    "toolProfile": ["get_favorite_poi", "start_navi"],
    "userMessages": ["导航去公司。"],
    "datasetVersion": DATA_VERSION,
    "seed": 0,
}
CLIMATE = {
    "case_id": "esa-cockpit-climate",
    "instruction": "帮我调整空调温度。温度还没有确定，请先问我。",
    "initialContext": {},
    "initialState": {"vehicle": {"ac_on": True, "ac_temperature": 24}},
    "toolProfile": ["get_vehicle_state", "set_ac_temperature"],
    "userMessages": ["帮我调整空调温度。温度还没有确定，请先问我。"],
    "userScript": [{"reply_pattern": "温度|多少度|几度|度数", "user_reply": "调到22度。"}],
    "datasetVersion": DATA_VERSION,
    "seed": 0,
}


def serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:" + str(httpd.server_port)


def run_case(name, task, agent_url, recorder_url):
    with tempfile.TemporaryDirectory(prefix="cockpit-driver-") as tmp:
        output = Path(tmp) / "driver-result.json"
        config = dict(task)
        config.update({"mockSystemUrl": recorder_url, "agentApiUrl": agent_url,
                       "outputPath": str(output), "evalPrefix": "/eval", "agentProtocol": "turn",
                       "turnTimeoutSec": 60})
        config_path = Path(tmp) / "config.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False), "utf-8")
        result = subprocess.run(["node", str(ROOT / "esa" / "driver.mjs"), "--config", str(config_path)],
                                cwd=ROOT, text=True, capture_output=True)
        print("[" + name + "] node exit", result.returncode)
        if result.stderr.strip():
            print(result.stderr.strip()[-2000:])
        if not output.is_file():
            print("[" + name + "] 驱动没有产出结果文件")
            return 1
        payload = json.loads(output.read_text("utf-8"))
        print("[" + name + "] status:", payload["turnStatus"], "errors:", payload["errors"])
        print("[" + name + "] reply:", payload["reply"])
        print("[" + name + "] turns:", len(payload["turns"]), "calls:",
              [(call["name"], call["turn_index"], call["status"]) for call in payload["toolCalls"]])
        print("[" + name + "] dialogue turn_index:",
              [(item["role"], item["turn_index"]) for item in payload["dialogue"]])
        print("[" + name + "] final state:", json.dumps(payload["finalSnapshot"]["state"], ensure_ascii=False))
        events = payload["eventEvidence"]["events"]
        print("[" + name + "] events:", [(event["sequence"], event["tool"], event["status"],
                                          [change["path"] for change in event["changes"]]) for event in events])
        print("[" + name + "] manifest:", json.dumps(payload["manifest"], ensure_ascii=False)[:400])
        return 0 if payload["turnStatus"] == "completed" and not payload["errors"] else 1


def main():
    mcp_http, mcp_url = serve(make_mcp_handler(FakeCockpitMcp()))
    server = CockpitServer(mcp_url)
    recorder = CockpitRecorder(MemoryStore(test_only=True), server)
    recorder_http, recorder_url = serve(make_recorder_handler(recorder))
    agent = FakeCockpitAgent(recorder_url)
    agent_http, agent_url = serve(make_agent_handler(agent))
    status = 0
    try:
        status |= run_case("navi", NAVI, agent_url, recorder_url)
        status |= run_case("climate", CLIMATE, agent_url, recorder_url)
    finally:
        for httpd in (agent_http, recorder_http, mcp_http):
            httpd.shutdown()
            httpd.server_close()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
