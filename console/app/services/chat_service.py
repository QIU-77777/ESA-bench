"""Chat playground: talk to the esa cockpit agent from the console.

The console backend is the agent client, mirroring esa/cli.py: it creates
mock-system sessions, posts turns to the Go runtime over SSE and persists
transcript, messages and evidence files. The browser never talks to the
agent services directly.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import httpx

from .. import config
from ..db import connect, rows_to_dicts, utc_now

MOCK_URL = "http://127.0.0.1:8003"
MOCK_INTERNAL_URL = "http://mock-system:8003"
RUNTIME_URL = "http://127.0.0.1:18080"
AGENT_PAYLOAD_PATH = config.REPO_ROOT / "esa" / "agent.json"
TOOL_CATALOG_PATH = config.REPO_ROOT / "environment" / "catalog.json"
TOOL_CONFIG_PATH = config.REPO_ROOT / "configs" / "tools.json"
STACK_FILE = config.REPO_ROOT / "docker-compose.dev.yaml"
COCKPIT_STACK_FILE = config.REPO_ROOT / "docker-compose.cockpit.yaml"
COCKPIT_PROJECT = "esa-cockpit-dev"
COCKPIT_MOCK_URL = "http://127.0.0.1:18093"
COCKPIT_AGENT_URL = "http://127.0.0.1:18090"
EVIDENCE_ROOT = Path.home() / ".esa" / "sessions"

_READY_TIMEOUT_SEC = 120.0
_TURN_TIMEOUT_SEC = 300.0
_HTTP_TIMEOUT = 30.0

DEFAULT_CONFIG = {
    "model_id": "",
    "temperature": None,
    "top_p": None,
    "reasoning_effort": "",
    "instructions": "",
    "max_iterations": 16,
    "turn_timeout": 240,
    "tool_profile": None,
    "mode": "esa",
}


class ChatError(ValueError):
    pass


def session_mode(session: dict) -> str:
    """会话链路：esa 走 runtime，cockpit 走座舱 agent。"""
    merged = session_config(session)
    return "cockpit" if merged.get("mode") == "cockpit" else "esa"


# ── Readiness ──────────────────────────────────────────────────────────


def readiness() -> dict:
    """Health of the services both playground stacks depend on."""
    return {
        "mock": _health(MOCK_URL + "/health"),
        "runtime": _health(RUNTIME_URL + "/v1/health"),
        "recorder": _health(COCKPIT_MOCK_URL + "/health"),
        "agent": _health(COCKPIT_AGENT_URL + "/v1/health"),
    }


def _health(url: str) -> bool:
    try:
        with httpx.Client(timeout=3.0) as client:
            response = client.get(url)
            return response.status_code == 200
    except httpx.HTTPError:
        return False


def start_stack(mode: str = "esa") -> None:
    """Bring up the requested dev stack (esa: mock+runtime, cockpit: recorder+agent+mcp)."""
    config.load_env()
    cockpit = mode == "cockpit"
    subprocess.Popen(
        [
            "docker", "compose", "-p", COCKPIT_PROJECT if cockpit else "esa-dev",
            "-f", str(COCKPIT_STACK_FILE if cockpit else STACK_FILE), "up", "-d",
        ],
        cwd=str(config.REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + _READY_TIMEOUT_SEC
    while time.monotonic() < deadline:
        state = readiness()
        ready = (state["recorder"] and state["agent"]) if cockpit else (state["mock"] and state["runtime"])
        if ready:
            return
        time.sleep(2.0)
    raise ChatError("服务在 120 秒内未就绪，请执行 docker compose 查看日志")


# ── Agent definition ───────────────────────────────────────────────────


def agent_baseline() -> dict:
    if not AGENT_PAYLOAD_PATH.is_file():
        raise ChatError(f"agent 定义不存在：{AGENT_PAYLOAD_PATH}")
    return json.loads(AGENT_PAYLOAD_PATH.read_text(encoding="utf-8"))


def agent_defaults() -> dict:
    """Baseline model parameters from esa/agent.json, shown as form defaults."""
    try:
        model = agent_baseline().get("react", {}).get("model", {}) or {}
    except ChatError:
        return {"model_id": "", "temperature": None, "reasoning_effort": ""}
    return {
        "model_id": str(model.get("model_id") or ""),
        "temperature": model.get("temperature"),
        "reasoning_effort": str(model.get("reasoning_effort") or ""),
    }


def build_agent(config_overrides: dict) -> dict:
    """Baseline agent.json with this session's overrides applied."""
    agent = json.loads(json.dumps(agent_baseline()))
    react = agent.setdefault("react", {})
    model = react.setdefault("model", {})
    if config_overrides.get("model_id"):
        model["model_id"] = config_overrides["model_id"]
    if config_overrides.get("temperature") is not None:
        model["temperature"] = config_overrides["temperature"]
    if config_overrides.get("top_p") is not None:
        model["top_p"] = config_overrides["top_p"]
    if config_overrides.get("reasoning_effort"):
        model["reasoning_effort"] = config_overrides["reasoning_effort"]
    if config_overrides.get("instructions"):
        react["instructions"] = config_overrides["instructions"]
    return agent


def tool_catalog() -> list[dict]:
    """Enabled tools from configs/tools.json joined with catalog descriptions."""
    catalog = {}
    if TOOL_CATALOG_PATH.is_file():
        for tool in json.loads(TOOL_CATALOG_PATH.read_text(encoding="utf-8")).get("tools") or []:
            catalog[str(tool.get("name") or "")] = tool
    tools = []
    if TOOL_CONFIG_PATH.is_file():
        data = json.loads(TOOL_CONFIG_PATH.read_text(encoding="utf-8"))
        for item in data.get("tools") or []:
            name = str(item.get("name") or "")
            if not name:
                continue
            entry = catalog.get(name) or {}
            tools.append(
                {
                    "name": name,
                    "title": str(entry.get("title") or name),
                    "description": str(entry.get("description") or ""),
                    "enabled": bool(item.get("enabled")),
                    "schema": entry.get("inputSchema") or {},
                }
            )
    return tools


# ── Sessions ───────────────────────────────────────────────────────────


def list_sessions() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.*, (SELECT content FROM chat_messages m"
            " WHERE m.session_id = s.id AND m.role = 'user' ORDER BY m.id LIMIT 1) AS first_message,"
            " (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id) AS message_count"
            " FROM chat_sessions s ORDER BY s.updated_at DESC, s.id DESC"
        ).fetchall()
    return rows_to_dicts(rows)


def get_session(session_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE id=?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def session_config(session: dict) -> dict:
    merged = dict(DEFAULT_CONFIG)
    merged.update(json.loads(session["agent_config"] or "{}"))
    return merged


def create_session(name: str, overrides: dict | None = None, mode: str = "esa") -> int:
    cockpit = mode == "cockpit"
    state = readiness()
    ready = (state["recorder"] and state["agent"]) if cockpit else (state["mock"] and state["runtime"])
    if not ready:
        raise ChatError("座舱服务未就绪，请先在对话页启动服务")
    config_data = dict(DEFAULT_CONFIG)
    config_data.update(overrides or {})
    config_data["mode"] = "cockpit" if cockpit else "esa"
    config_data["last_sequence"] = 0
    tools = [] if cockpit else tool_catalog()
    tool_profile = config_data.get("tool_profile")
    if not cockpit and not tool_profile:
        tool_profile = [t["name"] for t in tools if t["enabled"]]
    config_data["tool_profile"] = tool_profile

    base = COCKPIT_MOCK_URL if cockpit else MOCK_URL
    body = {
        "type": "car",
        "context": {},
        "initial_state": {},
        "data_overlays": {},
        "faults": [],
        "seed": 0,
    }
    if tool_profile:
        body["tool_profile"] = tool_profile
    created = _post(base + "/eval/api/v1/sessions", body)
    mock_session_id = str(created.get("session_id") or "")
    if not mock_session_id:
        raise ChatError("环境服务没有返回 session_id")

    now = utc_now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO chat_sessions(name, mock_session_id, agent_config, evidence_dir, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                name.strip() or "新会话",
                mock_session_id,
                json.dumps(config_data, ensure_ascii=False),
                str(EVIDENCE_ROOT / mock_session_id),
                now,
                now,
            ),
        )
        session_id = int(cur.lastrowid)
    _write_evidence(session_id, [], [])
    return session_id


def update_session_config(session_id: int, overrides: dict) -> None:
    session = get_session(session_id)
    if session is None:
        raise ChatError(f"会话不存在：{session_id}")
    merged = session_config(session)
    for key, value in (overrides or {}).items():
        if key in DEFAULT_CONFIG and value not in (None, ""):
            merged[key] = value
    with connect() as conn:
        conn.execute(
            "UPDATE chat_sessions SET agent_config=?, updated_at=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), utc_now(), session_id),
        )


def rename_session(session_id: int, name: str) -> None:
    if not name.strip():
        raise ChatError("会话名称不能为空")
    with connect() as conn:
        conn.execute(
            "UPDATE chat_sessions SET name=?, updated_at=? WHERE id=?",
            (name.strip(), utc_now(), session_id),
        )


def delete_session(session_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))


def list_messages(session_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    result = []
    for row in rows_to_dicts(rows):
        row["events"] = json.loads(row["events"] or "[]")
        row["usage"] = json.loads(row["usage"] or "{}")
        result.append(row)
    return result


def session_transcript(session_id: int) -> list:
    """Rebuild the runtime transcript from stored assistant events."""
    transcript = []
    for message in list_messages(session_id):
        if message["role"] == "user":
            transcript.append({"role": "user", "content": message["content"]})
        else:
            for event in message["events"]:
                if event.get("type") == "transcript_item":
                    transcript.append(event.get("item") or {})
    return transcript


def session_tools(session_id: int) -> list[dict]:
    """Tools active in this session: snapshot tool_profile joined with catalog."""
    session = get_session(session_id)
    if session is None or not session["mock_session_id"]:
        return []
    base = COCKPIT_MOCK_URL if session_mode(session) == "cockpit" else MOCK_URL
    catalog = {t["name"]: t for t in tool_catalog()}
    try:
        snapshot = _get(
            base + f"/api/v1/rl/sessions/{session['mock_session_id']}/snapshot"
        )
    except ChatError:
        return []
    names = snapshot.get("tool_profile")
    if not isinstance(names, list) or not names:
        manifest = snapshot.get("manifest") or {}
        names = [
            str(tool.get("name") or "")
            for tool in manifest.get("tools") or []
            if tool.get("enabled")
        ]
    tools = []
    for name in names:
        entry = catalog.get(str(name)) or {}
        tools.append(
            {
                "name": str(name),
                "title": str(entry.get("title") or name),
                "description": str(entry.get("description") or ""),
                "enabled": True,
                "schema": entry.get("schema") or {},
            }
        )
    return tools


# ── Turns ──────────────────────────────────────────────────────────────


def send_message(session_id: int, text: str):
    """Yield SSE event dicts for one turn; persists messages and evidence."""
    session = get_session(session_id)
    if session is None:
        raise ChatError(f"会话不存在：{session_id}")
    text = text.strip()
    if not text:
        raise ChatError("消息不能为空")
    state = readiness()
    cockpit = session_mode(session) == "cockpit"
    ready = (state["recorder"] and state["agent"]) if cockpit else (state["mock"] and state["runtime"])
    if not ready:
        raise ChatError("座舱服务未就绪，请先在对话页启动服务")

    overrides = session_config(session)
    transcript = session_transcript(session_id)
    history = [
        {"role": message["role"], "content": message["content"]}
        for message in list_messages(session_id)
    ]
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages(session_id, role, content, events, usage, created_at)"
            " VALUES (?, 'user', ?, '[]', '{}', ?)",
            (session_id, text, now),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at=? WHERE id=?", (now, session_id)
        )
    yield {"type": "user_message", "content": text, "created_at": now}

    if cockpit:
        yield from _send_cockpit_turn(session_id, session, overrides, text, history)
        return

    body = {
        "agent": build_agent(overrides),
        "user_messages": [text],
        "transcript": transcript,
        "mockSystemUrl": MOCK_INTERNAL_URL,
        "sessionId": session["mock_session_id"],
        "toolProfile": overrides.get("tool_profile") or None,
        "maxIterations": int(overrides.get("max_iterations") or 16),
        "turnTimeoutSec": float(overrides.get("turn_timeout") or 240),
    }

    now = utc_now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages(session_id, role, content, events, usage, created_at)"
            " VALUES (?, 'user', ?, '[]', '{}', ?)",
            (session_id, text, now),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at=? WHERE id=?", (now, session_id)
        )
    yield {"type": "user_message", "content": text, "created_at": now}

    events: list[dict] = []
    result: dict = {}
    started = time.monotonic()
    try:
        with httpx.Client(timeout=_TURN_TIMEOUT_SEC) as client:
            with client.stream(
                "POST",
                RUNTIME_URL + "/v1/run",
                json=body,
                headers={"Accept": "text/event-stream"},
            ) as response:
                if response.status_code != 200:
                    detail = response.read().decode("utf-8", "replace")[:500]
                    raise ChatError(f"runtime 返回 {response.status_code}: {detail}")
                for event in _iter_sse(response):
                    events.append(event)
                    yield event
                    if event.get("type") == "run_end":
                        result = event.get("result") or {}
    except httpx.HTTPError as exc:
        raise ChatError(f"runtime 调用失败：{exc}") from exc

    content = str(result.get("reply") or "")
    usage = _usage_of(result, time.monotonic() - started)
    transcript_items = [
        {"type": "transcript_item", "item": item}
        for item in (result.get("transcript") or [])
        if isinstance(item, dict)
    ]
    stored_events = [e for e in events if e.get("type") != "run_end"] + transcript_items
    for iteration in result.get("iterations") or []:
        stored_events.append(
            {
                "type": "iteration_meta",
                "iteration": iteration.get("iteration"),
                "latency_ms": iteration.get("latencyMs") or 0,
                "usage": iteration.get("usage") or {},
                "status": iteration.get("status") or "",
            }
        )
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages(session_id, role, content, events, usage, created_at)"
            " VALUES (?, 'assistant', ?, ?, ?, ?)",
            (
                session_id,
                content,
                json.dumps(stored_events, ensure_ascii=False),
                json.dumps(usage, ensure_ascii=False),
                now,
            ),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at=? WHERE id=?", (now, session_id)
        )
    _write_evidence(session_id, result.get("transcript") or transcript, [result])
    yield {"type": "done", "content": content, "usage": usage}


def _send_cockpit_turn(session_id: int, session: dict, overrides: dict, text: str, history: list):
    """座舱链路：调用 agent /v1/turn，从 recorder 拉取本轮调用并落库。"""
    started = time.monotonic()
    try:
        with httpx.Client(timeout=_TURN_TIMEOUT_SEC) as client:
            response = client.post(
                COCKPIT_AGENT_URL + "/v1/turn",
                json={"session_id": session["mock_session_id"], "message": text, "history": history},
            )
    except httpx.HTTPError as exc:
        raise ChatError(f"座舱 agent 调用失败：{exc}") from exc
    if response.status_code != 200:
        raise ChatError(f"座舱 agent 返回 {response.status_code}: {response.text[:500]}")
    result = response.json()
    if result.get("error") or (result.get("status") is not None and result.get("status") != "completed"):
        raise ChatError("座舱 agent 本轮失败：" + str(result.get("error") or result.get("status")))
    content = str(result.get("reply") or "")

    after = int(overrides.get("last_sequence") or 0)
    events = _events(session["mock_session_id"], base=COCKPIT_MOCK_URL, after=after)
    records = events.get("events") or []
    if isinstance(events.get("total_events"), int):
        _store_sequence(session_id, overrides, events["total_events"])
    # 事件形状与 runtime 链路一致：入参与结果都是 JSON 字符串，前端直接解析。
    stored_events = []
    for event in records:
        if not event.get("call_id"):
            continue
        stored_events.append({"type": "tool_call", "name": event.get("tool"),
                              "arguments": json.dumps(event.get("arguments") or {}, ensure_ascii=False),
                              "call_id": event.get("call_id")})
        stored_events.append({"type": "tool_result", "status": "completed" if event.get("status") == "ok" else "failed",
                              "output": json.dumps(event.get("result") or {}, ensure_ascii=False),
                              "latency_ms": 0, "call_id": event.get("call_id")})
    for event in stored_events:
        yield event
    usage = {"input_tokens": int((result.get("usage") or {}).get("input_tokens") or 0),
             "output_tokens": int((result.get("usage") or {}).get("output_tokens") or 0),
             "total_sec": round(time.monotonic() - started, 1)}

    now = utc_now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages(session_id, role, content, events, usage, created_at)"
            " VALUES (?, 'assistant', ?, ?, ?, ?)",
            (session_id, content, json.dumps(stored_events, ensure_ascii=False),
             json.dumps(usage, ensure_ascii=False), now),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at=? WHERE id=?", (now, session_id)
        )
    _write_evidence(session_id, history + [{"role": "assistant", "content": content}], [result])
    yield {"type": "done", "content": content, "usage": usage}


def _store_sequence(session_id: int, overrides: dict, sequence: int) -> None:
    merged = dict(overrides)
    merged["last_sequence"] = sequence
    with connect() as conn:
        conn.execute(
            "UPDATE chat_sessions SET agent_config=?, updated_at=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), utc_now(), session_id),
        )


def _iter_sse(response):
    """Parse an SSE body into event dicts with a type field."""
    event_name = ""
    data_lines: list[str] = []
    for raw in response.iter_lines():
        line = raw.strip()
        if not line:
            if event_name or data_lines:
                payload = _parse_data("\n".join(data_lines))
                if payload is not None:
                    yield {"type": event_name or "message", **payload}
            event_name, data_lines = "", []
            continue
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    if event_name or data_lines:
        payload = _parse_data("\n".join(data_lines))
        if payload is not None:
            yield {"type": event_name or "message", **payload}


def _parse_data(text: str) -> dict | None:
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}
    return payload if isinstance(payload, dict) else {"value": payload}


def _usage_of(result: dict, elapsed_sec: float = 0.0) -> dict:
    """Aggregate token usage across iterations (the runtime reports per iteration)."""
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    for iteration in result.get("iterations") or []:
        usage = iteration.get("usage") or {}
        for key in totals:
            totals[key] += int(usage.get(key) or 0)
    totals["total_sec"] = round(elapsed_sec, 1)
    totals["iterations"] = len(result.get("iterations") or [])
    return totals


# ── Evidence ───────────────────────────────────────────────────────────


def _write_evidence(session_id: int, transcript: list, turn_results: list) -> None:
    """Write ~/.esa/sessions/<mock_session_id>/ files, cli.py format."""
    session = get_session(session_id)
    if session is None or not session["mock_session_id"]:
        return
    directory = EVIDENCE_ROOT / session["mock_session_id"]
    cockpit = session_mode(session) == "cockpit"
    base = COCKPIT_MOCK_URL if cockpit else MOCK_URL
    try:
        directory.mkdir(parents=True, exist_ok=True)
        overrides = session_config(session)
        initial_path = directory / "snapshot-initial.json"
        if not initial_path.is_file():
            initial_path.write_text(
                json.dumps(_snapshot(session["mock_session_id"], base=base) or {}, ensure_ascii=False, indent=2),
                "utf-8",
            )
        payloads = {
            "transcript.json": transcript,
            "snapshot-final.json": _snapshot(session["mock_session_id"], base=base) or {},
            "state-events.json": _events(session["mock_session_id"], base=base),
            "turn-results.json": turn_results,
            "meta.json": {
                "session_id": session["mock_session_id"],
                "model": overrides.get("model_id") or "",
                "task": None,
                "started_at": session["created_at"],
                "finished_at": utc_now(),
                "turns": len(turn_results),
                "runtime_url": RUNTIME_URL,
                "mock_url": base,
                "mode": "cockpit" if cockpit else "esa",
            },
        }
        for name, payload in payloads.items():
            (directory / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
            )
    except OSError:
        pass  # evidence is best-effort; the conversation lives in the database


def _snapshot(mock_session_id: str, base: str = MOCK_URL) -> dict:
    try:
        return _get(base + f"/api/v1/rl/sessions/{mock_session_id}/snapshot")
    except ChatError:
        return {}


def _events(mock_session_id: str, base: str = MOCK_URL, after: int = 0) -> dict:
    try:
        return _get(base + f"/api/v1/rl/sessions/{mock_session_id}/events?after={after}")
    except ChatError:
        return {}


# ── HTTP helpers ───────────────────────────────────────────────────────


def _get(url: str) -> dict:
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise ChatError(f"无法连接 {url}: {exc}") from exc
    if response.status_code != 200:
        raise ChatError(f"{url} 返回 {response.status_code}")
    return response.json()


def _post(url: str, body: dict) -> dict:
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.post(url, json=body)
    except httpx.HTTPError as exc:
        raise ChatError(f"无法连接 {url}: {exc}") from exc
    if response.status_code not in (200, 201):
        raise ChatError(f"{url} 返回 {response.status_code}: {response.text[:300]}")
    return response.json()
