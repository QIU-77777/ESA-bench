"""MCP 记录代理：对外实现 ESA 环境协议，向内转发到用户 MCP server。

座舱评测里，工具由用户设计的本地 MCP server 提供，座舱状态由该 server 维护。
本模块在座舱模式中替代 environment 服务：

- 对外路由与 ``environment/server.py`` 完全一致（会话、快照、事件、round、
  reset、``/eval/mcp/{sid}``、``/eval/api/v1/manifest``）；
- 对内把 ``tools/list``、``tools/call`` 与会话创建转发给用户 MCP server，
  状态读取也指向该 server；
- 工具权限由本代理按会话 tool_profile 执行，故障注入（reject、
  missing_field、response_lost）在转发层执行，语义与现服务一致；
- 每次转发的 ``tools/call`` 前后各读一次座舱状态，产出逐事件 JSON 差异。

复用 ``environment/store.py`` 的会话存储与 ``environment/service.py`` 的
``changes()`` 差异函数，其余为平行实现：座舱状态结构由用户 MCP server 决定，
不套用 ESA 十域模板。接入契约见 docs/esa-cockpit-integration.md。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import pathlib
import re
import threading
import time
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from environment.compat import point
from environment.service import changes
from environment.store import Conflict, MemoryStore, MissingSession, RedisStore

MAX_BODY = 2 * 1024 * 1024
RECORDER_VERSION = "1.0.0"
ENVIRONMENT_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"
MCP_ENDPOINT = "/eval/mcp"
SESSIONS_ENDPOINT = "/sessions"
DEFAULT_STATE_TOOL = "get_vehicle_state"
_HEX64 = set("0123456789abcdef")

_SESSIONS_RE = re.compile(r"/(?:api/v1/rl|eval/api/v1)/sessions/([a-f0-9]+)/(?P<action>snapshot|events|reset|round)")
_DELETE_RE = re.compile(r"/eval/api/v1/sessions/([a-f0-9]+)")
_MCP_RE = re.compile(r"/eval/mcp/([a-f0-9]+)")


class ResponseLost(Exception):
    """注入的响应丢失：调用已提交，连接在返回响应前断开。"""


class ServerError(RuntimeError):
    """用户 MCP server 不可用或返回了无法使用的响应。"""


def _request(url, method="GET", body=None, timeout=60.0):
    data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raise ServerError(method + " " + url + " -> HTTP " + str(error.code)) from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ServerError(method + " " + url + " failed (" + type(error).__name__ + ")") from None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ServerError(method + " " + url + " returned invalid JSON") from None


def _text_of(envelope) -> str:
    """从 MCP 信封里取出文本内容，用于错误消息。"""
    if not isinstance(envelope, dict):
        return "MCP server returned an unusable tool result"
    parts = []
    for item in envelope.get("content") or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif isinstance(item, str):
            parts.append(item)
    structured = envelope.get("structuredContent")
    if not parts and isinstance(structured, dict):
        parts.append(json.dumps(structured, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts) or "MCP server returned an unusable tool result"


def _normalized_tools(tools):
    """工具定义的规范化形式：按 name 排序，供 source_sha256 与 manifest 使用。"""
    normalized = [
        {"name": str(tool.get("name") or ""),
         "description": str(tool.get("description") or ""),
         "inputSchema": tool.get("inputSchema") or {}}
        for tool in tools
    ]
    normalized.sort(key=lambda tool: tool["name"])
    return normalized


class CockpitServer:
    """用户 MCP server 客户端：健康检查、工具清单、会话与状态读取。"""

    def __init__(self, base_url, *, session_mode="path", inject_mode="http",
                 state_tool=None, state_url=None, timeout=60.0):
        self.base_url = base_url.rstrip("/")
        if session_mode not in ("path", "param"):
            raise ValueError("session_mode must be path or param")
        if inject_mode not in ("http", "mcp"):
            raise ValueError("inject_mode must be http or mcp")
        self.session_mode = session_mode
        self.inject_mode = inject_mode
        self.state_tool = state_tool or DEFAULT_STATE_TOOL
        self.state_url = (state_url or "").rstrip("/") or None
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self.version, self.commit, self.data_version, self.data_sha256 = self._capabilities()
        self.tools = _normalized_tools(self._tool_list())
        names = [tool["name"] for tool in self.tools]
        if not names or len(set(names)) != len(names) or any(not name for name in names):
            raise ServerError("MCP server returned an empty or duplicate tool list")
        self.definitions = {tool["name"]: tool for tool in self.tools}

    # ── 启动期能力读取 ──

    def _capabilities(self):
        health = _request(self.base_url + "/health", timeout=self.timeout)
        if not isinstance(health, dict):
            raise ServerError("MCP server /health did not return an object")
        data_version = health.get("data_version")
        data_sha256 = health.get("data_sha256")
        if not isinstance(data_version, str) or not data_version.strip():
            manifest = _request(self.base_url + "/manifest", timeout=self.timeout)
            data_version = manifest.get("data_version") if isinstance(manifest, dict) else None
            data_sha256 = manifest.get("data_sha256") if isinstance(manifest, dict) else None
        if not isinstance(data_version, str) or not data_version.strip():
            raise ServerError("MCP server did not report data_version")
        if not isinstance(data_sha256, str) or len(data_sha256) != 64 or set(data_sha256) - _HEX64:
            raise ServerError("MCP server did not report a bare 64 hex digit data_sha256")
        return (str(health.get("version") or ""), str(health.get("commit") or ""),
                data_version, data_sha256)

    def _tool_list(self):
        response = self._rpc(None, "tools/list", {})
        tools = response.get("tools") if isinstance(response, dict) else None
        if not isinstance(tools, list):
            raise ServerError("MCP server tools/list did not return a tools array")
        return tools

    # ── 运行期转发 ──

    def _mcp_url(self, sid):
        if sid is not None and self.session_mode == "path":
            return self.base_url + MCP_ENDPOINT + "/" + sid
        return self.base_url + MCP_ENDPOINT

    def _rpc(self, sid, method, params):
        payload = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": dict(params)}
        if sid is not None and self.session_mode == "param":
            payload["params"]["session_id"] = sid
        response = _request(self._mcp_url(sid), method="POST", body=payload, timeout=self.timeout)
        if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
            raise ServerError("MCP server returned an invalid JSON-RPC envelope")
        if response.get("id") != payload["id"]:
            raise ServerError("MCP server returned a mismatched JSON-RPC id")
        if "error" in response or "result" not in response:
            error = response.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else None
            raise ServerError("MCP server rejected " + method + (": " + str(message) if message else ""))
        return response["result"]

    def create_session(self, sid, fields):
        """HTTP 形态的初态注入：POST /sessions 接收 initial_state 等字段。"""
        body = dict(fields)
        body["session_id"] = sid
        response = _request(self.base_url + SESSIONS_ENDPOINT, method="POST", body=body, timeout=self.timeout)
        if not isinstance(response, dict):
            raise ServerError("MCP server session creation did not return an object")
        return response

    def inject_initial_state(self, sid, fields):
        """按接入形态转发初态：HTTP /sessions 或 MCP initialize 参数。"""
        if self.inject_mode == "mcp":
            params = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                      "clientInfo": {"name": "esa-cockpit-recorder", "version": RECORDER_VERSION}}
            params.update(fields)
            self._rpc(sid, "initialize", params)
            return {}
        return self.create_session(sid, fields)

    def delete_session(self, sid):
        """按 session_id 删除 server 侧会话。"""
        response = _request(self.base_url + SESSIONS_ENDPOINT + "/" + sid, method="DELETE", timeout=self.timeout)
        if isinstance(response, dict):
            return bool(response.get("deleted", True))
        return True

    def read_state(self, sid):
        """读取座舱状态：HTTP 状态端点或状态查询工具，二者之一。"""
        if self.state_url:
            if "{session_id}" in self.state_url:
                url = self.state_url.replace("{session_id}", sid)
            else:
                url = self.state_url + "/" + sid
            state = _request(url, timeout=self.timeout)
        else:
            result = self._rpc(sid, "tools/call", {"name": self.state_tool, "arguments": {}})
            state = result.get("structuredContent") if isinstance(result, dict) else None
        if not isinstance(state, dict):
            raise ServerError("cockpit state read did not return a state object")
        return state

    def call_tool(self, sid, name, arguments):
        return self._rpc(sid, "tools/call", {"name": name, "arguments": arguments})


DEFAULT_CONTEXT = {
    "time": "2026-10-20T09:00:00+08:00",
    "location": {"id": "poi_home", "name": "当前位置", "lon": 120.200, "lat": 30.260},
    "vehicle_model": "local-electric-v1",
    "timezone": "Asia/Shanghai",
}
DYNAMIC_FIELDS = {"vehicle", "navigation", "user_addresses", "addresses", "memories", "memory",
                  "navi_history", "media", "display", "map", "schedules", "service_tasks", "chat", "gear"}
FORBIDDEN_FIELDS = {"expected", "expected_state", "tool_overrides", "forbidden_changes",
                    "process_constraints", "reply", "judge"}


def build_context(request):
    """固定上下文，语义与 environment 服务一致；座舱数据归用户 MCP server。"""
    context = copy.deepcopy(DEFAULT_CONTEXT)
    supplied = request.get("initial_context", request.get("context", {})) or {}
    if not isinstance(supplied, dict):
        raise ValueError("context must be an object")
    context.update({key: copy.deepcopy(value) for key, value in supplied.items() if key not in DYNAMIC_FIELDS})
    if isinstance(supplied.get("datetime"), dict):
        clock = supplied["datetime"]
        context["time"] = clock.get("date", "2026-10-20") + "T" + clock.get("time", "09:00") + ":00+08:00"
    if isinstance(context.get("location"), dict):
        context["location"] = point(context["location"])
    if "current_time" in supplied:
        context["time"] = supplied["current_time"]
    if "current_location" in supplied:
        context["location"] = copy.deepcopy(supplied["current_location"])
    if not isinstance(context["time"], str) or len(context["time"]) < 10:
        raise ValueError("invalid context time")
    location = context["location"]
    if not isinstance(location, dict) or not all(isinstance(location.get(key), (int, float)) for key in ("lon", "lat")):
        raise ValueError("location needs lon and lat")
    return context


def implementation_sha256():
    """recorder 实现哈希：本模块自身的字节内容。"""
    return hashlib.sha256(pathlib.Path(__file__).resolve().read_bytes()).hexdigest()


class CockpitRecorder:
    """座舱模式的会话生命周期、工具权限、故障注入与逐事件差异。"""

    def __init__(self, store, server, *, event_limit=10000):
        self.store = store
        self.server = server
        self.event_limit = event_limit
        if event_limit < 1:
            raise ValueError("event_limit must be positive")
        self.implementation_sha256 = implementation_sha256()
        self.source_sha256 = hashlib.sha256(json.dumps(
            self.server.tools, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        self.call_counts = {}
        self.counts_lock = threading.Lock()
        # Redis 的 WATCH/MULTI 在冲突后重跑整个 fn，而座舱的 fn 里含远程调用，
        # 重放会在用户座舱上重复执行工具。按会话串行化，避免重放。
        self.session_locks = {}
        self.session_locks_guard = threading.Lock()

    # ── 会话 ──

    def validate_faults(self, faults):
        if not isinstance(faults, list):
            raise ValueError("faults must be a list")
        for fault in faults:
            if not isinstance(fault, dict) or set(fault) - {"tool", "arguments", "times", "kind", "field", "message"}:
                raise ValueError("invalid fault fields")
            if fault.get("tool") not in self.server.definitions or fault.get("kind") not in ("reject", "missing_field", "response_lost"):
                raise ValueError("invalid fault tool/kind")
            if not isinstance(fault.get("arguments", {}), dict) or not isinstance(fault.get("times", 1), int) or isinstance(fault.get("times", 1), bool) or fault.get("times", 1) < 1:
                raise ValueError("invalid fault matcher/count")
            if fault["kind"] == "missing_field" and not isinstance(fault.get("field"), str):
                raise ValueError("missing_field requires field")

    def create(self, request):
        if not isinstance(request, dict):
            raise ValueError("session request must be an object")
        if set(request) & FORBIDDEN_FIELDS:
            raise ValueError("grading/override fields forbidden in environment session")
        if request.get("type", "car") != "car":
            raise ValueError("session type must be car")
        version = request.get("dataset_version", self.server.data_version)
        if version != self.server.data_version:
            raise ValueError("unknown dataset version")
        seed = request.get("seed", 0)
        if not isinstance(seed, (int, str)) or isinstance(seed, bool):
            raise ValueError("seed must be an integer or string")
        profile = request.get("tool_profile")
        if profile is None:
            profile = [tool["name"] for tool in self.server.tools]
        if (not isinstance(profile, list) or any(not isinstance(name, str) for name in profile)
                or len(set(profile)) != len(profile) or set(profile) - set(self.server.definitions)):
            raise ValueError("tool_profile must be unique tool names provided by the MCP server")
        faults = request.get("faults", []) or []
        self.validate_faults(faults)
        initial_state = request.get("initial_state", {}) or {}
        if not isinstance(initial_state, dict):
            raise ValueError("initial_state must be an object")
        context = build_context(request)
        sid = uuid.uuid4().hex
        self.server.inject_initial_state(sid, {
            "type": "car", "context": context, "initial_state": initial_state,
            "data_overlays": request.get("data_overlays", {}) or {}, "tool_profile": list(profile),
            "seed": seed, "dataset_version": version,
        })
        self.server.read_state(sid)  # 注入立即生效，否则会话创建失败，不留孤儿会话
        record = {"session_id": sid, "context": context, "initial_state": copy.deepcopy(initial_state),
                  "dataset_version": version, "seed": seed, "tool_profile": list(profile),
                  "data_overlays": copy.deepcopy(request.get("data_overlays", {}) or {}),
                  "state_version": 0, "faults": copy.deepcopy(faults), "fault_counts": {},
                  "events": [], "event_sequence": 0, "events_truncated": False, "idempotency": {}}
        self.store.create(sid, record)
        return {"id": sid, "session_id": sid, "dataset_version": version, "seed": seed,
                "tool_profile": list(profile), "state_version": 0, "mcp_url": MCP_ENDPOINT + "/" + sid}

    def snapshot(self, sid):
        record = self.store.read(sid)
        state = self.server.read_state(sid)
        return {**{key: record[key] for key in ("session_id", "context", "initial_state", "dataset_version",
                                                "seed", "tool_profile", "state_version")},
                "state": state, "environment_version": ENVIRONMENT_VERSION,
                "implementation_sha256": self.implementation_sha256, "source_sha256": self.source_sha256,
                "seed_sha256": self.server.data_sha256, "manifest": self.manifest()}

    def events(self, sid, after=0, call_id=None):
        record = self.store.read(sid)
        selected = [event for event in record["events"]
                    if event["sequence"] > after and (call_id is None or event["call_id"] == call_id)]
        return {"events": selected, "truncated": record["events_truncated"],
                "total_events": record["event_sequence"], "state_version": record["state_version"]}

    def list_tools(self, sid):
        record = self.store.read(sid)
        return {"tools": [self.server.definitions[name] for name in record["tool_profile"]]}

    def manifest(self):
        """静态字段，与 environment 服务 manifest() 同构；调用统计只进 /eval/api/v1/manifest。"""
        return {"environment_version": ENVIRONMENT_VERSION,
                "implementation_sha256": self.implementation_sha256,
                "dataset_version": self.server.data_version, "seed_sha256": self.server.data_sha256,
                "source_sha256": self.source_sha256,
                "server_version": self.server.version, "server_commit": self.server.commit,
                "tools": [{"name": tool["name"], "description": tool["description"], "enabled": True}
                          for tool in self.server.tools]}

    def manifest_route(self):
        """能力清单：静态 manifest 加进程期调用统计。"""
        with self.counts_lock:
            counts = dict(self.call_counts)
        payload = self.manifest()
        payload["call_counts"] = counts
        payload["total_calls"] = sum(counts.values())
        return payload

    # ── 生命周期 ──

    def begin_round(self, sid):
        def transaction(record):
            record["event_sequence"] += 1
            result = {"round": True, "state_version": record["state_version"]}
            record["events"].append({"sequence": record["event_sequence"], "call_id": None,
                                     "tool": "session/round", "arguments": {}, "result": result,
                                     "status": "ok", "before_version": record["state_version"],
                                     "after_version": record["state_version"], "changes": []})
            self.trim(record)
            return result
        return self.store.transact(sid, transaction)

    def reset(self, sid):
        """删除 server 侧会话并用同一份初态重建，清空幂等缓存与故障计数。"""
        record = self.store.read(sid)
        before = self.server.read_state(sid)
        self.server.delete_session(sid)
        self.server.inject_initial_state(sid, {
            "type": "car", "context": record["context"], "initial_state": record["initial_state"],
            "data_overlays": copy.deepcopy(record.get("data_overlays") or {}),
            "tool_profile": list(record["tool_profile"]), "seed": record["seed"],
            "dataset_version": record["dataset_version"],
        })
        after = self.server.read_state(sid)

        def transaction(current):
            delta = changes(before, after)
            before_version = current["state_version"]
            if delta:
                current["state_version"] += 1
            current["event_sequence"] += 1
            result = {"reset": True, "state_version": current["state_version"]}
            current["events"].append({"sequence": current["event_sequence"], "call_id": None,
                                      "tool": "session/reset", "arguments": {}, "result": result,
                                      "status": "ok", "before_version": before_version,
                                      "after_version": current["state_version"], "changes": delta})
            current["idempotency"] = {}
            current["fault_counts"] = {}
            self.trim(current)
            return result
        with self.session_lock(sid):
            return self.store.transact(sid, transaction)

    def delete(self, sid):
        self.server.delete_session(sid)
        return {"deleted": self.store.delete(sid)}

    # ── 工具执行 ──

    def trim(self, record):
        if len(record["events"]) > self.event_limit:
            record["events"] = record["events"][-self.event_limit:]
            record["events_truncated"] = True

    def session_lock(self, sid):
        """同一会话的锁：串行化含远程调用的事务，避免 Redis 重放重复执行工具。"""
        with self.session_locks_guard:
            lock = self.session_locks.get(sid)
            if lock is None:
                lock = self.session_locks[sid] = threading.RLock()
            return lock

    def execute(self, sid, name, args, call_id=None):
        arguments = copy.deepcopy(args)
        if isinstance(arguments, dict):
            embedded = arguments.pop("_call_id", None)
            call_id = call_id or embedded
        call_id = call_id or uuid.uuid4().hex
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("call_id must be a nonempty string")
        fingerprint = hashlib.sha256(json.dumps(
            {"name": name, "arguments": arguments}, sort_keys=True, ensure_ascii=False,
            allow_nan=False).encode()).hexdigest()

        def transaction(record):
            cached = record["idempotency"].get(call_id)
            if cached and cached["fingerprint"] == fingerprint:
                return {"result": copy.deepcopy(cached["result"]), "lost": False}
            status = "ok"
            lost = False
            before_state = after_state = None
            try:
                if cached:
                    raise ValueError("call_id reused with different request")
                if name not in self.server.definitions:
                    raise ValueError("unknown tool: " + str(name))
                if name not in record["tool_profile"]:
                    raise PermissionError("tool disabled by session profile")
                selected = None
                for index, fault in enumerate(record["faults"]):
                    if (fault["tool"] == name
                            and all(arguments.get(key) == value for key, value in fault.get("arguments", {}).items())
                            and record["fault_counts"].get(str(index), 0) < fault.get("times", 1)):
                        selected = fault
                        record["fault_counts"][str(index)] = record["fault_counts"].get(str(index), 0) + 1
                        break
                if selected and selected["kind"] == "reject":
                    raise ValueError(selected.get("message", "injected execution rejection"))
                before_state = self.server.read_state(sid)
                envelope = self.server.call_tool(sid, name, arguments)
                after_state = self.server.read_state(sid)
                if not isinstance(envelope, dict):
                    raise ServerError("MCP server returned an unusable tool envelope")
                if envelope.get("isError"):
                    status = "error"
                    structured = envelope.get("structuredContent")
                    result = structured if isinstance(structured, dict) else {
                        "error": {"code": "execution_error", "message": _text_of(envelope)}}
                else:
                    result = envelope.get("structuredContent")
                    if result is None:
                        result = {"content": envelope.get("content") or []}
                if selected and selected["kind"] == "missing_field":
                    node = result
                    parts = selected["field"].split(".")
                    for part in parts[:-1]:
                        node = node.get(part, {}) if isinstance(node, dict) else {}
                    if isinstance(node, dict):
                        node.pop(parts[-1], None)
                    if isinstance(envelope.get("structuredContent"), dict):
                        envelope["structuredContent"] = result
                lost = bool(selected and selected["kind"] == "response_lost")
                output = envelope
            except Exception as error:
                status = "error"
                if isinstance(error, PermissionError):
                    code = "permission_denied"
                elif isinstance(error, ServerError):
                    code = "server_error"
                else:
                    code = "execution_error"
                result = {"error": {"code": code, "message": str(error) or type(error).__name__}}
                output = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                          "structuredContent": result, "isError": True}
            # 只有前后两次状态都读到时才计算差异。任一次读取失败（超时、5xx、坏 JSON）
            # 时状态未知，不能把 changes(None) 的 path "/" 写进事件：空路径会命中每一条
            # forbidden_changes 规则，把一次 server 故障变成硬违规并虚增 state_version。
            delta = (changes(before_state, after_state)
                     if isinstance(before_state, dict) and isinstance(after_state, dict) else [])
            before_version = record["state_version"]
            if delta:
                record["state_version"] += 1
            record["event_sequence"] += 1
            record["events"].append({"sequence": record["event_sequence"], "call_id": call_id, "tool": name,
                                     "arguments": arguments, "result": result, "status": status,
                                     "before_version": before_version, "after_version": record["state_version"],
                                     "changes": delta, "response_lost": lost})
            self.trim(record)
            if not cached:
                record["idempotency"][call_id] = {"fingerprint": fingerprint, "result": copy.deepcopy(output)}
            if before_state is not None and status == "ok":
                with self.counts_lock:
                    self.call_counts[name] = self.call_counts.get(name, 0) + 1
            return {"result": output, "lost": lost}

        with self.session_lock(sid):
            committed = self.store.transact(sid, transaction)
        if committed["lost"]:
            raise ResponseLost("injected response loss after committed call " + call_id)
        return committed["result"]


def make_handler(recorder):
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
                split = urlsplit(self.path)
                path = split.path
                query = parse_qs(split.query)
                if method == "GET" and path == "/health":
                    return self.send_json(200, {
                        "status": "ok", "dataset_version": recorder.server.data_version,
                        "enabled_tools": len(recorder.server.tools),
                        "store": type(recorder.store).__name__,
                        "server_version": recorder.server.version,
                        "server_commit": recorder.server.commit})
                if method == "GET" and path in ("/manifest", "/eval/api/v1/manifest"):
                    return self.send_json(200, recorder.manifest_route())
                if method == "POST" and path == "/eval/api/v1/sessions":
                    return self.send_json(201, recorder.create(self.body()))
                match = _SESSIONS_RE.fullmatch(path)
                if match:
                    sid, action = match[1], match["action"]
                    if method == "GET" and action == "snapshot":
                        return self.send_json(200, recorder.snapshot(sid))
                    if method == "GET" and action == "events":
                        return self.send_json(200, recorder.events(sid, int(query.get("after", ["0"])[0]),
                                                                   query.get("call_id", [None])[0]))
                    if method == "POST" and action in ("reset", "round"):
                        if self.headers.get("Content-Length", "0") != "0":
                            payload = self.body()
                            if not isinstance(payload, dict):
                                raise ValueError("lifecycle body must be an object")
                        result = recorder.reset(sid) if action == "reset" else recorder.begin_round(sid)
                        return self.send_json(200, result)
                match = _DELETE_RE.fullmatch(path)
                if match and method == "DELETE":
                    return self.send_json(200, recorder.delete(match[1]))
                match = _MCP_RE.fullmatch(path)
                if match and method == "POST":
                    return self.mcp(match[1], self.body())
                return self.send_json(404, {"error": "route not found"})
            except ResponseLost:
                self.close_connection = True
                self.connection.shutdown(2)
            except MissingSession:
                self.send_json(404, {"error": "session not found"})
            except (ValueError, TypeError, KeyError) as error:
                self.send_json(400, {"error": str(error)})
            except Conflict as error:
                self.send_json(409, {"error": str(error)})
            except ServerError as error:
                self.send_json(502, {"error": str(error)})
            except Exception:
                self.send_json(503, {"error": "cockpit recorder unavailable"})

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
            if method == "tools/list":
                result = recorder.list_tools(sid)
            elif method == "tools/call":
                result = recorder.execute(sid, params.get("name"), params.get("arguments", {}), params.get("call_id"))
            elif method == "initialize":
                recorder.store.read(sid)
                result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "esa-cockpit-recorder", "version": RECORDER_VERSION}}
            elif method == "notifications/initialized":
                return self.send_json(200, {})
            else:
                return self.send_json(200, {"jsonrpc": "2.0", "id": rid,
                                            "error": {"code": -32601, "message": "method not found"}})
            return self.send_json(200, {"jsonrpc": "2.0", "id": rid, "result": result})

    return Handler


def build_server(base_url, *, session_mode="path", inject_mode="http", state_tool=None,
                 state_url=None, timeout=60.0, startup_timeout=60.0):
    """连接用户 MCP server，启动期等待其就绪。

    compose 并发启动服务，recorder 可能早于用户 MCP server 起来；在
    ``startup_timeout`` 秒内重试，超时才失败，避免容器直接退出。
    """
    deadline = time.monotonic() + startup_timeout
    announced = False
    while True:
        try:
            return CockpitServer(base_url, session_mode=session_mode, inject_mode=inject_mode,
                                 state_tool=state_tool, state_url=state_url, timeout=timeout)
        except ServerError as error:
            if not announced:
                announced = True
                print("等待用户 MCP server 就绪：" + str(error), flush=True)
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description="座舱 MCP 记录代理")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--store", choices=["redis", "memory-test"],
                        default=os.getenv("ESA_STORE", "redis"))
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://redis:6379/0"))
    parser.add_argument("--mcp-url", default=os.getenv("COCKPIT_MCP_URL", ""))
    parser.add_argument("--session-mode", choices=["path", "param"],
                        default=os.getenv("COCKPIT_MCP_SESSION_MODE", "path"))
    parser.add_argument("--inject-mode", choices=["http", "mcp"],
                        default=os.getenv("COCKPIT_INJECT_MODE", "http"))
    parser.add_argument("--state-tool", default=os.getenv("COCKPIT_STATE_TOOL", DEFAULT_STATE_TOOL))
    parser.add_argument("--state-url", default=os.getenv("COCKPIT_STATE_URL", ""))
    parser.add_argument("--mcp-timeout", type=float, default=float(os.getenv("COCKPIT_MCP_TIMEOUT", "60")))
    parser.add_argument("--mcp-startup-timeout", type=float,
                        default=float(os.getenv("COCKPIT_MCP_STARTUP_TIMEOUT", "60")))
    parser.add_argument("--event-limit", type=int, default=int(os.getenv("COCKPIT_EVENT_LIMIT", "10000")))
    args = parser.parse_args()
    if not args.mcp_url:
        parser.error("--mcp-url or COCKPIT_MCP_URL is required")
    store = (MemoryStore(test_only=True) if args.store == "memory-test"
             else RedisStore(args.redis_url, ttl=int(os.getenv("ESA_SESSION_TTL", "86400"))))
    server = build_server(args.mcp_url, session_mode=args.session_mode, inject_mode=args.inject_mode,
                          state_tool=args.state_tool, state_url=args.state_url,
                          timeout=args.mcp_timeout, startup_timeout=args.mcp_startup_timeout)
    recorder = CockpitRecorder(store, server, event_limit=args.event_limit)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(recorder))
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
