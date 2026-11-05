"""确定性假座舱 agent：默认 agent 镜像来源，按任务规则驱动工具调用。

实现 docs/esa-cockpit-integration.md 的 agent 接入契约：

- ``GET /v1/health`` 上报 status 与 model_id（取自 COCKPIT_AGENT_MODEL_ID）；
- ``POST /v1/turn`` 接收 session_id、message 与 history，返回 reply、status 与 usage；
- 从 ``ESA_MOCK_URL`` 与会话标识拼出 ``/eval/mcp/{session_id}``，每轮先
  ``tools/list`` 再调用，与真实座舱 agent 走同一条 recorder 链路。

策略是确定性的规则：导航类消息先查公司地点再启动导航；空调类消息在拿到目标
温度后设置温度，否则先询问。用户资产就绪后以 COCKPIT_AGENT_IMAGE 覆盖本镜像。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

MAX_BODY = 2 * 1024 * 1024
DEFAULT_MOCK_URL = "http://mock-system:8003"
DEFAULT_MODEL_ID = "esa-cockpit-fake-agent"
TEMPERATURE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:度|℃)")
NAVIGATION_HINT = re.compile(r"导航|带我去|前往")
CLIMATE_HINT = re.compile(r"空调|温度|冷暖|制冷|制热")


class AgentError(RuntimeError):
    """agent 执行失败，以 status=failed 返回给驱动。"""


def _post_json(url, body, timeout):
    data = json.dumps(body, ensure_ascii=False).encode()
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise AgentError("无法访问 " + url + "：" + type(error).__name__) from None
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise AgentError(url + " 返回的不是 JSON") from None


def _temperature_of(text):
    match = TEMPERATURE_PATTERN.search(text or "")
    return float(match.group(1)) if match else None


class FakeCockpitAgent:
    """确定性座舱 agent：按会话缓存工具清单，按消息与历史决定动作。"""

    def __init__(self, mock_url=None, timeout=60.0):
        self.mock_url = (mock_url or os.getenv("ESA_MOCK_URL", DEFAULT_MOCK_URL)).rstrip("/")
        self.timeout = float(timeout)
        self.sessions = {}

    # ── MCP 访问 ──

    def _mcp(self, session_id, method, params):
        payload = {"jsonrpc": "2.0", "id": method, "method": method, "params": params}
        response = _post_json(self.mock_url + "/eval/mcp/" + session_id, payload, self.timeout)
        if response.get("jsonrpc") != "2.0" or "result" not in response:
            error = response.get("error") or {}
            raise AgentError("MCP " + method + " 失败：" + str(error.get("message") or "invalid envelope"))
        return response["result"]

    def _tools(self, session_id):
        state = self.sessions.setdefault(session_id, {})
        if "tools" not in state:
            self._mcp(session_id, "initialize", {})
            listed = self._mcp(session_id, "tools/list", {})
            state["tools"] = [tool["name"] for tool in listed.get("tools") or []]
        return state["tools"]

    def _call(self, session_id, name, arguments):
        result = self._mcp(session_id, "tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            structured = result.get("structuredContent") or {}
            raise AgentError(str(name) + " 返回错误：" + str(structured.get("error") or result))
        return result.get("structuredContent")

    # ── 回合策略 ──

    def turn(self, session_id, message, history):
        if not isinstance(session_id, str) or not session_id:
            raise AgentError("session_id 不能为空")
        if not isinstance(message, str) or not message.strip():
            raise AgentError("message 不能为空")
        tools = self._tools(session_id)
        transcript = "\n".join(str(item.get("content") or "") for item in history or [])
        if NAVIGATION_HINT.search(message) or (NAVIGATION_HINT.search(transcript) and not CLIMATE_HINT.search(message)):
            return self._navigate(session_id, tools, transcript)
        if CLIMATE_HINT.search(message) or CLIMATE_HINT.search(transcript):
            return self._climate(session_id, tools, message, transcript)
        return self._fallback(session_id, tools)

    def _navigate(self, session_id, tools, transcript):
        if "get_favorite_poi" not in tools or "start_navi" not in tools:
            raise AgentError("会话没有导航工具")
        pois = self._call(session_id, "get_favorite_poi", {"address_type": "company", "limit": 10})
        entries = pois.get("pois") or []
        if not entries:
            raise AgentError("没有查询到公司地点")
        target = entries[0]
        point = "{id}:{name}:{lon},{lat}".format(**{key: target[key] for key in ("id", "name", "lon", "lat")})
        navigation = self._call(session_id, "start_navi", {"poi_list": [point]})
        reply = "已开始导航前往{name}，距离{distance:.1f}公里，预计{duration:.0f}分钟。".format(
            name=target["name"], distance=float(navigation.get("distance_m") or 0) / 1000,
            duration=float(navigation.get("duration_s") or 0) / 60)
        return reply

    def _climate(self, session_id, tools, message, transcript):
        temperature = _temperature_of(message)
        if temperature is None:
            temperature = _temperature_of(transcript)
        if temperature is None:
            return "空调温度需要设置为多少度？"
        if "set_ac_temperature" not in tools:
            raise AgentError("会话没有空调控制工具")
        result = self._call(session_id, "set_ac_temperature", {"temperature": temperature})
        value = result.get("ac_temperature", temperature)
        return "已把空调温度调到{value:g}度。".format(value=float(value))

    def _fallback(self, session_id, tools):
        if "get_vehicle_state" in tools:
            state = self._call(session_id, "get_vehicle_state", {})
            vehicle = state.get("vehicle") or {}
            return "当前挡位{gear}，空调{state}，目标温度{temperature:g}度。".format(
                gear=vehicle.get("gear", "P"),
                state="开启" if vehicle.get("ac_on") else "关闭",
                temperature=float(vehicle.get("ac_temperature") or 0))
        return "我可以帮你查询地点、启动导航或调整空调温度。"


def make_handler(agent: FakeCockpitAgent, model_id=None):
    resolved_model = model_id or os.getenv("COCKPIT_AGENT_MODEL_ID") or DEFAULT_MODEL_ID

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def send_json(self, status, value):
            data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self):
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("chunked requests unsupported")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("request body missing or too large")
            return json.loads(self.rfile.read(length),
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON number")))

        def do_GET(self):
            if urlsplit(self.path).path == "/v1/health":
                return self.send_json(200, {"status": "ok", "model_id": resolved_model, "version": "1.0.0"})
            return self.send_json(404, {"error": "route not found"})

        def do_POST(self):
            try:
                if urlsplit(self.path).path != "/v1/turn":
                    return self.send_json(404, {"error": "route not found"})
                payload = self.body()
                if not isinstance(payload, dict):
                    raise ValueError("turn request must be an object")
                session_id = payload.get("session_id")
                message = payload.get("message")
                history = payload.get("history") or []
                if not isinstance(history, list):
                    raise ValueError("history must be an array")
                reply = agent.turn(session_id, message, history)
            except AgentError as error:
                return self.send_json(200, {"reply": "", "status": "failed", "error": str(error)})
            except (ValueError, TypeError) as error:
                return self.send_json(400, {"error": str(error)})
            usage = {"input_tokens": _tokens(payload.get("history") or [], message),
                     "output_tokens": max(1, len(reply) // 2)}
            return self.send_json(200, {"reply": reply, "status": "completed", "usage": usage})

    return Handler


def _tokens(history, message):
    total = len(message or "")
    for item in history:
        if isinstance(item, dict):
            total += len(str(item.get("content") or ""))
    return max(1, total // 2)


def main():
    parser = argparse.ArgumentParser(description="确定性假座舱 agent")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--mock-url", default=os.getenv("ESA_MOCK_URL", DEFAULT_MOCK_URL))
    parser.add_argument("--model-id", default=None)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(FakeCockpitAgent(args.mock_url), args.model_id))
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
