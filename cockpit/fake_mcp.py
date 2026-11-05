"""确定性假座舱 MCP server：默认 MCP 镜像来源，也是 recorder 测试的对接件。

按 docs/esa-cockpit-integration.md 的七项能力实现：

1. MCP 工具服务：``POST /eval/mcp/{sid}`` 提供 tools/list 与 tools/call；
2. 健康检查：``GET /health`` 上报 version、commit、data_version 与 data_sha256；
3. 数据版本：data_version 为 ``esa-cockpit-v1``，data_sha256 为裸 64 位十六进制；
4. 会话隔离：session_id 出现在 MCP 端点路径，也可经调用参数覆盖；
5. 初态注入：``POST /sessions`` 接收 initial_state 等字段，或 MCP initialize 参数带初态；
6. 状态读取：状态查询工具 get_vehicle_state，或 ``GET /state/{sid}``；
7. 会话删除：``DELETE /sessions/{sid}``。

状态结构与工具集合服务两个示例任务（导航、空调澄清），全部本地计算，不联网、
不使用真实时钟与随机数。用户资产就绪后以 COCKPIT_MCP_IMAGE 覆盖本镜像。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

MAX_BODY = 2 * 1024 * 1024
PROTOCOL_VERSION = "2024-11-05"
DATA_VERSION = "esa-cockpit-v1"
STATE_TOOL = "get_vehicle_state"
EARTH_RADIUS_M = 6371000
AVERAGE_SPEED_KMH = 40

TOOLS = [
    {
        "name": "get_favorite_poi",
        "description": "查询常用地点：住宅或公司，返回地点标识、名称与坐标。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "address_type": {"type": "string", "enum": ["home", "company"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["address_type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "start_navi",
        "description": "启动导航到指定地点，参数为 id:name:lon,lat 形式的地点串。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "poi_list": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["poi_list"],
            "additionalProperties": False,
        },
    },
    {
        "name": STATE_TOOL,
        "description": "读取当前座舱状态：车辆与导航的完整状态对象。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "set_ac_temperature",
        "description": "设置空调目标温度，单位为摄氏度。",
        "inputSchema": {
            "type": "object",
            "properties": {"temperature": {"type": "number", "minimum": 16, "maximum": 30}},
            "required": ["temperature"],
            "additionalProperties": False,
        },
    },
]

POIS = [
    {"id": "100001", "name": "家", "address_type": "home", "lon": 121.445, "lat": 31.21,
     "address": "上海市徐汇区汇贤居小区"},
    {"id": "200001", "name": "公司", "address_type": "company", "lon": 121.42, "lat": 31.19,
     "address": "上海市徐汇区科技园 A 座"},
]

DEFAULT_STATE = {
    "vehicle": {"gear": "P", "speed": 0, "ac_on": False, "ac_temperature": 24},
    "navigation": {"active": False, "destination": None, "distance_m": 0, "duration_s": 0},
}


def data_sha256() -> str:
    """数据指纹：工具定义与固定地点表的规范化序列化哈希，裸 64 位十六进制。"""
    payload = {"tools": TOOLS, "pois": POIS, "state_template": DEFAULT_STATE}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def merge(base, patch):
    if not isinstance(patch, dict):
        raise ValueError("initial state must be an object")
    for key, value in patch.items():
        if key not in base:
            raise ValueError("unknown state field: " + str(key))
        if isinstance(base[key], dict) and isinstance(value, dict):
            merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def distance_m(origin, target):
    lon1, lat1, lon2, lat2 = map(math.radians, [origin["lon"], origin["lat"], target["lon"], target["lat"]])
    x = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(x), math.sqrt(max(0.0, 1 - x))), 1)


def parse_poi(value):
    """解析 id:name:lon,lat 形式的地点串。"""
    if not isinstance(value, str) or value.count(":") != 2:
        raise ValueError("poi entry must use id:name:lon,lat")
    poi_id, name, coordinates = value.split(":")
    lon, _, lat = coordinates.partition(",")
    point = {"id": poi_id, "name": name, "lon": float(lon), "lat": float(lat)}
    known = next((item for item in POIS if item["id"] == poi_id), None)
    if known is not None:
        point["address"] = known["address"]
    return point


class ToolError(ValueError):
    """工具执行错误，以 isError 信封返回。"""


class FakeCockpitMcp:
    """假座舱 MCP server 的状态与工具行为，可被测试直接驱动。"""

    def __init__(self, version="1.0.0", commit=None):
        self.version = version
        self.commit = commit or os.getenv("FAKE_MCP_COMMIT", "local-build")
        self.sessions = {}
        self.lock = threading.RLock()

    # ── 能力 ──

    def health(self):
        return {"status": "ok", "version": self.version, "commit": self.commit,
                "data_version": DATA_VERSION, "data_sha256": data_sha256(),
                "tools": len(TOOLS), "sessions": len(self.sessions)}

    def manifest(self):
        return {"version": self.version, "commit": self.commit, "data_version": DATA_VERSION,
                "data_sha256": data_sha256(), "tools": copy.deepcopy(TOOLS)}

    # ── 会话 ──

    def create_session(self, fields):
        if not isinstance(fields, dict):
            raise ValueError("session fields must be an object")
        sid = fields.get("session_id") or uuid.uuid4().hex
        state = merge(copy.deepcopy(DEFAULT_STATE), fields.get("initial_state") or {})
        with self.lock:
            if sid in self.sessions:
                raise ValueError("session already exists: " + sid)
            self.sessions[sid] = {"state": state, "initial_state": copy.deepcopy(state),
                                  "tool_profile": list(fields.get("tool_profile") or [])}
        return {"session_id": sid, "state_version": 0}

    def initialize(self, fields):
        """MCP 形态的初态注入：会话不存在时按参数创建，已存在时保持幂等。"""
        if not isinstance(fields, dict):
            raise ValueError("initialize params must be an object")
        sid = fields.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("initialize params need session_id")
        with self.lock:
            if sid in self.sessions:
                return {"session_id": sid, "state_version": 0}
        return self.create_session(fields)

    def delete_session(self, sid):
        with self.lock:
            return self.sessions.pop(sid, None) is not None

    def state(self, sid):
        with self.lock:
            session = self.sessions.get(sid)
            if session is None:
                raise KeyError("session not found: " + sid)
            return copy.deepcopy(session["state"])

    # ── 工具 ──

    def list_tools(self, sid=None):
        if sid is not None:
            with self.lock:
                if sid not in self.sessions:
                    raise KeyError("session not found: " + sid)
        return {"tools": copy.deepcopy(TOOLS)}

    def call_tool(self, sid, name, arguments):
        if not isinstance(arguments, dict):
            raise ToolError("arguments must be an object")
        handler = {"get_favorite_poi": self._favorite_poi, "start_navi": self._start_navi,
                   STATE_TOOL: self._vehicle_state, "set_ac_temperature": self._set_ac_temperature}.get(name)
        if handler is None:
            raise ToolError("unknown tool: " + str(name))
        return handler(sid, arguments)

    def _favorite_poi(self, sid, arguments):
        address_type = arguments.get("address_type")
        if address_type not in ("home", "company"):
            raise ToolError("address_type must be home or company")
        limit = arguments.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ToolError("limit must be an integer between 1 and 50")
        pois = [copy.deepcopy(item) for item in POIS if item["address_type"] == address_type]
        return {"pois": pois[:limit]}

    def _start_navi(self, sid, arguments):
        poi_list = arguments.get("poi_list")
        if not isinstance(poi_list, list) or not poi_list:
            raise ToolError("poi_list must be a nonempty array")
        destination = parse_poi(poi_list[0])
        with self.lock:
            session = self.sessions.get(sid)
            if session is None:
                raise KeyError("session not found: " + sid)
            origin = {"lon": 120.200, "lat": 30.260}
            meters = distance_m(origin, destination)
            session["state"]["navigation"] = {
                "active": True, "destination": destination, "distance_m": meters,
                "duration_s": int(round(meters / 1000 / AVERAGE_SPEED_KMH * 3600)),
            }
            return copy.deepcopy(session["state"]["navigation"])

    def _vehicle_state(self, sid, arguments):
        return self.state(sid)

    def _set_ac_temperature(self, sid, arguments):
        temperature = arguments.get("temperature")
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
            raise ToolError("temperature must be a number")
        if not 16 <= temperature <= 30:
            raise ToolError("temperature must be between 16 and 30")
        with self.lock:
            session = self.sessions.get(sid)
            if session is None:
                raise KeyError("session not found: " + sid)
            session["state"]["vehicle"]["ac_temperature"] = temperature
            return {"ac_temperature": temperature}


def envelope(result, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "structuredContent": result, "isError": is_error}


def make_handler(server: FakeCockpitMcp):
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
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def do_DELETE(self):
            self.dispatch("DELETE")

        def dispatch(self, method):
            try:
                path = urlsplit(self.path).path
                if method == "GET" and path == "/health":
                    return self.send_json(200, server.health())
                if method == "GET" and path == "/manifest":
                    return self.send_json(200, server.manifest())
                if method == "POST" and path == "/sessions":
                    return self.send_json(201, server.create_session(self.body()))
                if method == "DELETE" and path.startswith("/sessions/"):
                    return self.send_json(200, {"deleted": server.delete_session(path[len("/sessions/"):])})
                if method == "GET" and path.startswith("/state/"):
                    return self.send_json(200, server.state(path[len("/state/"):]))
                if method == "POST" and path in ("/eval/mcp", "/mcp"):
                    return self.mcp(None, self.body())
                if method == "POST" and (path.startswith("/eval/mcp/") or path.startswith("/mcp/")):
                    sid = path.split("/mcp/", 1)[1]
                    return self.mcp(sid, self.body())
                return self.send_json(404, {"error": "route not found"})
            except ToolError as error:
                self.send_json(200, envelope({"error": {"code": "execution_error", "message": str(error)}}, True))
            except KeyError:
                self.send_json(404, {"error": "session not found"})
            except (ValueError, TypeError) as error:
                self.send_json(400, {"error": str(error)})
            except Exception:
                self.send_json(503, {"error": "fake mcp server unavailable"})

        def mcp(self, sid, request):
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                return self.send_json(200, {"jsonrpc": "2.0", "id": None,
                                            "error": {"code": -32600, "message": "invalid JSON-RPC request"}})
            rid = request.get("id")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(params, dict):
                return self.send_json(200, {"jsonrpc": "2.0", "id": rid,
                                            "error": {"code": -32602, "message": "params must be an object"}})
            if sid is None and isinstance(params.get("session_id"), str):
                sid = params["session_id"]
            try:
                if method == "tools/list":
                    result = server.list_tools(sid)
                elif method == "tools/call":
                    name = params.get("name")
                    arguments = params.get("arguments", {})
                    if sid is None:
                        raise ToolError("tools/call needs a session")
                    result = envelope(server.call_tool(sid, name, arguments))
                elif method == "initialize":
                    result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                              "serverInfo": {"name": "esa-cockpit-fake-mcp", "version": server.version}}
                    fields = {key: value for key, value in params.items()
                              if key in ("initial_state", "context", "tool_profile", "seed", "dataset_version")}
                    if fields.get("initial_state") is not None:
                        fields["session_id"] = sid
                        server.initialize(fields)
                elif method == "notifications/initialized":
                    return self.send_json(200, {})
                else:
                    return self.send_json(200, {"jsonrpc": "2.0", "id": rid,
                                                "error": {"code": -32601, "message": "method not found"}})
            except ToolError as error:
                return self.send_json(200, envelope({"error": {"code": "execution_error", "message": str(error)}}, True))
            except KeyError:
                return self.send_json(200, {"jsonrpc": "2.0", "id": rid,
                                            "error": {"code": -32000, "message": "session not found"}})
            return self.send_json(200, {"jsonrpc": "2.0", "id": rid, "result": result})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="确定性假座舱 MCP server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(FakeCockpitMcp()))
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
