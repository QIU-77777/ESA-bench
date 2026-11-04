"""recorder 单元与集成测试：路由、事件、幂等、故障、权限、差异、快照指纹。

默认使用 MemoryStore 与进程内假 MCP server，不依赖外部服务；设置
ESA_TEST_REDIS_URL 后追加真实 Redis 的事务与隔离测试。
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

from cockpit import recorder
from cockpit.fake_mcp import DATA_VERSION, FakeCockpitMcp, make_handler as make_mcp_handler
from cockpit.recorder import CockpitRecorder, CockpitServer, ResponseLost, ServerError, build_server, make_handler
from environment.store import MemoryStore, RedisStore

PROFILE = ["get_favorite_poi", "start_navi", "get_vehicle_state", "set_ac_temperature"]
NAVI_STATE = {"vehicle": {"gear": "D"}, "navigation": {"active": False}}
COMPANY_POI = "200001:公司:121.42,31.19"


def http_json(url, method="GET", body=None, timeout=30):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8")
        status = error.code
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, {"raw": raw}
    return status, (json.loads(raw) if raw else {})


def rpc(url, method, params, rpc_id=1):
    status, payload = http_json(url, "POST", {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})
    return status, payload


class QuietHandler:
    """测试里不打印每条 HTTP 请求日志。"""

    def log_message(self, *args):
        pass


class ServiceHarness:
    """进程内假 MCP server 加 recorder，返回两者的基地址。

    ``server_kwargs`` 可以是字典，也可以是接收 MCP 基地址、返回字典的函数，
    用于状态端点这类运行期才能确定的配置。
    """

    def __init__(self, recorder_kwargs=None, server_kwargs=None):
        self.fake = FakeCockpitMcp()
        self.mcp = ThreadingHTTPServer(("127.0.0.1", 0), type("QuietMcp", (QuietHandler, make_mcp_handler(self.fake)), {}))
        self.mcp.daemon_threads = True
        threading.Thread(target=self.mcp.serve_forever, daemon=True).start()
        kwargs = server_kwargs(self.mcp_url) if callable(server_kwargs) else (server_kwargs or {})
        self.server = CockpitServer(self.mcp_url, **kwargs)
        self.recorder = CockpitRecorder(MemoryStore(test_only=True), self.server, **(recorder_kwargs or {}))
        self.recorder_http = ThreadingHTTPServer(("127.0.0.1", 0), type("QuietRecorder", (QuietHandler, make_handler(self.recorder)), {}))
        self.recorder_http.daemon_threads = True
        threading.Thread(target=self.recorder_http.serve_forever, daemon=True).start()

    @property
    def mcp_url(self):
        return "http://127.0.0.1:" + str(self.mcp.server_port)

    @property
    def url(self):
        return "http://127.0.0.1:" + str(self.recorder_http.server_port)

    def create(self, **overrides):
        body = {"type": "car", "initial_state": copy.deepcopy(NAVI_STATE),
                "tool_profile": list(PROFILE), "seed": 0, "dataset_version": DATA_VERSION}
        body.update(overrides)
        status, payload = http_json(self.url + "/eval/api/v1/sessions", "POST", body)
        assert status == 201, (status, payload)
        return payload["session_id"]

    def close(self):
        self.recorder_http.shutdown()
        self.recorder_http.server_close()
        self.mcp.shutdown()
        self.mcp.server_close()


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.harness = ServiceHarness()
        self.addCleanup(self.harness.close)
        self.sid = self.harness.create()

    def events(self, sid=None):
        status, payload = http_json(self.harness.url + "/api/v1/rl/sessions/" + (sid or self.sid) + "/events")
        self.assertEqual(status, 200)
        return payload["events"]

    def snapshot(self, sid=None):
        status, payload = http_json(self.harness.url + "/api/v1/rl/sessions/" + (sid or self.sid) + "/snapshot")
        self.assertEqual(status, 200)
        return payload

    def call(self, name, arguments, call_id=None, sid=None):
        status, payload = rpc(self.harness.url + "/eval/mcp/" + (sid or self.sid), "tools/call",
                              {"name": name, "arguments": arguments, "call_id": call_id}, rpc_id=uuid.uuid4().hex)
        return status, payload

    # ── 路由与会话 ──

    def test_health_reports_server_identity(self):
        status, payload = http_json(self.harness.url + "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["dataset_version"], DATA_VERSION)
        self.assertEqual(payload["enabled_tools"], 4)
        self.assertEqual(payload["store"], "MemoryStore")

    def test_create_injects_initial_state_into_server(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot["state"]["vehicle"]["gear"], "D")
        self.assertFalse(snapshot["state"]["navigation"]["active"])
        self.assertEqual(snapshot["state_version"], 0)
        self.assertEqual(snapshot["initial_state"], NAVI_STATE)
        self.assertEqual(snapshot["tool_profile"], PROFILE)
        self.assertEqual(self.harness.fake.state(self.sid)["vehicle"]["gear"], "D")

    def test_create_rejects_unknown_dataset_version(self):
        status, payload = http_json(self.harness.url + "/eval/api/v1/sessions", "POST",
                                    {"dataset_version": "other", "tool_profile": list(PROFILE)})
        self.assertEqual(status, 400)
        self.assertIn("dataset version", payload["error"])

    def test_create_rejects_unknown_tool_profile(self):
        status, payload = http_json(self.harness.url + "/eval/api/v1/sessions", "POST",
                                    {"tool_profile": ["not_a_tool"]})
        self.assertEqual(status, 400)

    def test_create_rejects_grading_fields(self):
        status, payload = http_json(self.harness.url + "/eval/api/v1/sessions", "POST",
                                    {"tool_profile": list(PROFILE), "expected_state": {"navigation.active": True}})
        self.assertEqual(status, 400)

    def test_tools_list_filters_by_profile(self):
        status, payload = rpc(self.harness.url + "/eval/mcp/" + self.sid, "tools/list", {})
        self.assertEqual(status, 200)
        self.assertEqual([tool["name"] for tool in payload["result"]["tools"]], PROFILE)

    def test_initialize_requires_known_session(self):
        status, payload = rpc(self.harness.url + "/eval/mcp/deadbeef", "initialize", {})
        self.assertEqual(status, 404)

    # ── 转发、权限与差异 ──

    def test_forwarded_call_records_event_and_changes(self):
        status, payload = self.call("start_navi", {"poi_list": [COMPANY_POI]}, call_id="call-1")
        self.assertEqual(status, 200)
        result = payload["result"]
        self.assertFalse(result["isError"])
        self.assertTrue(result["structuredContent"]["active"])
        events = self.events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["tool"], "start_navi")
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["call_id"], "call-1")
        self.assertEqual(event["before_version"], 0)
        self.assertEqual(event["after_version"], 1)
        paths = sorted(change["path"] for change in event["changes"])
        self.assertIn("/navigation/active", paths)
        self.assertIn("/navigation/destination", paths)
        self.assertEqual(self.snapshot()["state_version"], 1)
        self.assertEqual(self.snapshot()["state"]["navigation"]["destination"]["id"], "200001")

    def test_out_of_profile_call_is_permission_denied(self):
        harness = ServiceHarness()
        self.addCleanup(harness.close)
        sid = harness.create(tool_profile=["get_favorite_poi"])
        status, payload = rpc(harness.url + "/eval/mcp/" + sid, "tools/call",
                              {"name": "start_navi", "arguments": {"poi_list": [COMPANY_POI]}, "call_id": "c1"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["result"]["isError"])
        self.assertEqual(payload["result"]["structuredContent"]["error"]["code"], "permission_denied")
        _, data = http_json(harness.url + "/api/v1/rl/sessions/" + sid + "/events")
        self.assertEqual(data["events"][0]["status"], "error")
        self.assertEqual(data["events"][0]["changes"], [])
        self.assertEqual(data["state_version"], 0)
        self.assertFalse(harness.fake.state(sid)["navigation"]["active"])

    def test_unknown_tool_is_error_event(self):
        status, payload = self.call("nope", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["result"]["isError"])
        self.assertEqual(payload["result"]["structuredContent"]["error"]["code"], "execution_error")
        self.assertEqual(self.events()[0]["status"], "error")

    def test_state_reads_do_not_create_events(self):
        self.call("get_vehicle_state", {})
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]["tool"], "get_vehicle_state")

    # ── 幂等 ──

    def test_idempotent_call_returns_cached_result(self):
        first_status, first = self.call("start_navi", {"poi_list": [COMPANY_POI]}, call_id="same")
        second_status, second = self.call("start_navi", {"poi_list": [COMPANY_POI]}, call_id="same")
        self.assertEqual((first_status, second_status), (200, 200))
        self.assertEqual(first["result"], second["result"])
        self.assertEqual(len(self.events()), 1)

    def test_reused_call_id_with_different_arguments_is_rejected(self):
        self.call("start_navi", {"poi_list": [COMPANY_POI]}, call_id="same")
        status, payload = self.call("start_navi", {"poi_list": ["100001:家:121.445,31.21"]}, call_id="same")
        self.assertEqual(status, 200)
        self.assertTrue(payload["result"]["isError"])
        events = self.events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["status"], "error")
        self.assertEqual(events[1]["changes"], [])

    # ── 故障注入 ──

    def test_reject_fault_blocks_execution(self):
        sid = self.harness.create(faults=[{"tool": "start_navi", "kind": "reject", "message": "blocked"}])
        status, payload = rpc(self.harness.url + "/eval/mcp/" + sid, "tools/call",
                              {"name": "start_navi", "arguments": {"poi_list": [COMPANY_POI]}, "call_id": "c1"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["result"]["isError"])
        self.assertEqual(payload["result"]["structuredContent"]["error"]["message"], "blocked")
        _, data = http_json(self.harness.url + "/api/v1/rl/sessions/" + sid + "/events")
        self.assertEqual(data["events"][0]["status"], "error")
        self.assertFalse(self.harness.fake.state(sid)["navigation"]["active"])

    def test_missing_field_fault_strips_result_field(self):
        sid = self.harness.create(faults=[{"tool": "start_navi", "kind": "missing_field", "field": "destination.id"}])
        status, payload = rpc(self.harness.url + "/eval/mcp/" + sid, "tools/call",
                              {"name": "start_navi", "arguments": {"poi_list": [COMPANY_POI]}, "call_id": "c1"})
        self.assertEqual(status, 200)
        structured = payload["result"]["structuredContent"]
        self.assertNotIn("id", structured["destination"])
        self.assertTrue(structured["active"])

    def test_response_lost_fault_commits_event_then_drops_connection(self):
        sid = self.harness.create(faults=[{"tool": "start_navi", "kind": "response_lost"}])
        with self.assertRaises(Exception):
            rpc(self.harness.url + "/eval/mcp/" + sid, "tools/call",
                {"name": "start_navi", "arguments": {"poi_list": [COMPANY_POI]}, "call_id": "lost-1"})
        _, data = http_json(self.harness.url + "/api/v1/rl/sessions/" + sid + "/events")
        self.assertEqual(len(data["events"]), 1)
        self.assertTrue(data["events"][0]["response_lost"])
        self.assertEqual(data["events"][0]["call_id"], "lost-1")
        self.assertTrue(self.harness.fake.state(sid)["navigation"]["active"])

    def test_fault_times_limit_matches_environment_semantics(self):
        sid = self.harness.create(faults=[{"tool": "start_navi", "kind": "reject", "times": 1}])
        for index in range(2):
            status, payload = rpc(self.harness.url + "/eval/mcp/" + sid, "tools/call",
                                  {"name": "start_navi", "arguments": {"poi_list": [COMPANY_POI]},
                                   "call_id": "c" + str(index)})
            self.assertEqual(status, 200)
            self.assertEqual(payload["result"]["isError"], index == 0)
        self.assertTrue(self.harness.fake.state(sid)["navigation"]["active"])

    # ── 生命周期 ──

    def test_round_event_does_not_change_state(self):
        status, payload = http_json(self.harness.url + "/eval/api/v1/sessions/" + self.sid + "/round", "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["round"])
        events = self.events()
        self.assertEqual(events[0]["tool"], "session/round")
        self.assertEqual(events[0]["changes"], [])
        self.assertEqual(events[0]["before_version"], events[0]["after_version"])

    def test_reset_restores_initial_state_and_clears_idempotency(self):
        self.call("start_navi", {"poi_list": [COMPANY_POI]}, call_id="c1")
        status, payload = http_json(self.harness.url + "/eval/api/v1/sessions/" + self.sid + "/reset", "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["reset"])
        state = self.harness.fake.state(self.sid)
        self.assertFalse(state["navigation"]["active"])
        self.assertIsNone(state["navigation"]["destination"])
        events = self.events()
        self.assertEqual(events[-1]["tool"], "session/reset")
        self.assertTrue(events[-1]["changes"])
        # 幂等缓存已清空：同一 call_id 再次调用会真实执行
        status, payload = self.call("start_navi", {"poi_list": [COMPANY_POI]}, call_id="c1")
        self.assertEqual(status, 200)
        self.assertFalse(payload["result"]["isError"])
        self.assertEqual(len([event for event in self.events() if event["call_id"] == "c1"]), 2)

    def test_delete_removes_recorder_and_server_sessions(self):
        status, payload = http_json(self.harness.url + "/eval/api/v1/sessions/" + self.sid, "DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(payload["deleted"])
        with self.assertRaises(KeyError):
            self.harness.fake.state(self.sid)
        status, _ = http_json(self.harness.url + "/api/v1/rl/sessions/" + self.sid + "/snapshot")
        self.assertEqual(status, 404)

    # ── 快照指纹 ──

    def test_snapshot_manifest_is_static_and_well_formed(self):
        self.call("start_navi", {"poi_list": [COMPANY_POI]}, call_id="c1")
        initial = ServiceHarness()
        self.addCleanup(initial.close)
        sid = initial.create()
        fresh = http_json(initial.url + "/api/v1/rl/sessions/" + sid + "/snapshot")[1]
        used = self.snapshot()
        for snapshot in (fresh, used):
            manifest = snapshot["manifest"]
            self.assertEqual(manifest["environment_version"], "1.0.0")
            self.assertEqual(manifest["dataset_version"], DATA_VERSION)
            for key in ("implementation_sha256", "seed_sha256", "source_sha256"):
                self.assertRegex(manifest[key], r"^[a-f0-9]{64}$")
            self.assertEqual([tool["name"] for tool in manifest["tools"]], sorted(PROFILE))
            self.assertNotIn("call_counts", manifest)
        self.assertEqual(fresh["manifest"], used["manifest"])
        self.assertEqual(self.harness.fake.version, "1.0.0")

    def test_manifest_route_reports_call_counts(self):
        self.call("start_navi", {"poi_list": [COMPANY_POI]}, call_id="c1")
        status, payload = http_json(self.harness.url + "/eval/api/v1/manifest")
        self.assertEqual(status, 200)
        self.assertEqual(payload["call_counts"], {"start_navi": 1})
        self.assertEqual(payload["total_calls"], 1)

    def test_events_support_after_and_call_id_filters(self):
        self.call("get_vehicle_state", {}, call_id="c1")
        self.call("start_navi", {"poi_list": [COMPANY_POI]}, call_id="c2")
        status, payload = http_json(self.harness.url + "/api/v1/rl/sessions/" + self.sid + "/events?after=1")
        self.assertEqual([event["call_id"] for event in payload["events"]], ["c2"])
        status, payload = http_json(self.harness.url + "/api/v1/rl/sessions/" + self.sid + "/events?call_id=c1")
        self.assertEqual([event["call_id"] for event in payload["events"]], ["c1"])

    # ── 其他接入形态 ──

    def test_http_state_endpoint_mode(self):
        harness = ServiceHarness(server_kwargs=lambda base: {"state_url": base + "/state/{session_id}"})
        self.addCleanup(harness.close)
        sid = harness.create()
        status, payload = rpc(harness.url + "/eval/mcp/" + sid, "tools/call",
                              {"name": "start_navi", "arguments": {"poi_list": [COMPANY_POI]}, "call_id": "c1"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["result"]["isError"])
        _, snapshot = http_json(harness.url + "/api/v1/rl/sessions/" + sid + "/snapshot")
        self.assertEqual(snapshot["state"]["navigation"]["destination"]["id"], "200001")

    def test_param_session_mode(self):
        harness = ServiceHarness(server_kwargs={"session_mode": "param"})
        self.addCleanup(harness.close)
        sid = harness.create()
        status, payload = rpc(harness.url + "/eval/mcp/" + sid, "tools/call",
                              {"name": "start_navi", "arguments": {"poi_list": [COMPANY_POI]}, "call_id": "c1"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["result"]["isError"])
        self.assertTrue(harness.fake.state(sid)["navigation"]["active"])

    def test_mcp_inject_mode(self):
        harness = ServiceHarness(server_kwargs={"inject_mode": "mcp"})
        self.addCleanup(harness.close)
        sid = harness.create()
        state = harness.fake.state(sid)
        self.assertEqual(state["vehicle"]["gear"], "D")
        status, payload = rpc(harness.url + "/eval/mcp/" + sid, "tools/call",
                              {"name": "start_navi", "arguments": {"poi_list": [COMPANY_POI]}, "call_id": "c1"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["result"]["isError"])

    # ── 服务不可用 ──

    def test_server_failure_surfaces_as_502(self):
        harness = ServiceHarness()
        self.addCleanup(harness.close)
        sid = harness.create()
        harness.mcp.shutdown()
        harness.mcp.server_close()
        status, payload = http_json(harness.url + "/api/v1/rl/sessions/" + sid + "/snapshot")
        self.assertEqual(status, 502)

    def test_startup_rejects_server_without_data_version(self):
        broken = ThreadingHTTPServer(("127.0.0.1", 0), type("QuietMcp", (QuietHandler, make_mcp_handler(FakeCockpitMcp())), {}))
        broken.daemon_threads = True
        threading.Thread(target=broken.serve_forever, daemon=True).start()
        self.addCleanup(broken.shutdown)
        self.addCleanup(broken.server_close)
        original_health, original_manifest = FakeCockpitMcp.health, FakeCockpitMcp.manifest

        def health(self):
            payload = dict(original_health(self))
            payload.pop("data_version")
            payload.pop("data_sha256")
            return payload

        def manifest(self):
            payload = dict(original_manifest(self))
            payload.pop("data_version")
            payload.pop("data_sha256")
            return payload

        FakeCockpitMcp.health = health
        FakeCockpitMcp.manifest = manifest
        self.addCleanup(lambda: setattr(FakeCockpitMcp, "health", original_health))
        self.addCleanup(lambda: setattr(FakeCockpitMcp, "manifest", original_manifest))
        with self.assertRaises(ServerError):
            CockpitServer("http://127.0.0.1:" + str(broken.server_port))

    def test_startup_rejects_non_hex_data_sha256(self):
        broken = ThreadingHTTPServer(("127.0.0.1", 0), type("QuietMcp", (QuietHandler, make_mcp_handler(FakeCockpitMcp())), {}))
        broken.daemon_threads = True
        threading.Thread(target=broken.serve_forever, daemon=True).start()
        self.addCleanup(broken.shutdown)
        self.addCleanup(broken.server_close)
        original_health = FakeCockpitMcp.health

        def health(self):
            payload = dict(original_health(self))
            payload["data_sha256"] = "commit-4f2a91c"
            return payload

        FakeCockpitMcp.health = health
        self.addCleanup(lambda: setattr(FakeCockpitMcp, "health", original_health))
        with self.assertRaises(ServerError):
            CockpitServer("http://127.0.0.1:" + str(broken.server_port))

    def test_build_server_retries_until_mcp_server_ready(self):
        harness = ServiceHarness()
        self.addCleanup(harness.close)
        original = recorder.CockpitServer
        failures = []

        class FlakyServer:
            def __init__(self, *args, **kwargs):
                if len(failures) < 2:
                    failures.append(1)
                    raise ServerError("MCP server not ready yet")
                self._inner = original(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        recorder.CockpitServer = FlakyServer
        self.addCleanup(lambda: setattr(recorder, "CockpitServer", original))
        server = build_server(harness.mcp_url, startup_timeout=10.0)
        self.assertEqual(len(failures), 2)
        self.assertEqual(server.data_version, DATA_VERSION)

    def test_build_server_gives_up_after_startup_timeout(self):
        with self.assertRaises(ServerError):
            build_server("http://127.0.0.1:1", startup_timeout=0.2)


@unittest.skipUnless(os.getenv("ESA_TEST_REDIS_URL"), "set ESA_TEST_REDIS_URL for real Redis integration")
class RedisIntegrationTests(unittest.TestCase):
    """真实 Redis 下的事务、隔离与会话清理。"""

    def setUp(self):
        self.prefix = "esa-cockpit-test:" + uuid.uuid4().hex[:12] + ":"
        mcp = ThreadingHTTPServer(("127.0.0.1", 0), type("QuietMcp", (QuietHandler, make_mcp_handler(FakeCockpitMcp())), {}))
        mcp.daemon_threads = True
        threading.Thread(target=mcp.serve_forever, daemon=True).start()
        self.addCleanup(mcp.shutdown)
        self.addCleanup(mcp.server_close)
        self.server = CockpitServer("http://127.0.0.1:" + str(mcp.server_port))
        self.store = RedisStore(os.environ["ESA_TEST_REDIS_URL"], prefix=self.prefix, ttl=60, retries=100)
        recorder = CockpitRecorder(self.store, self.server)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), type("QuietRecorder", (QuietHandler, make_handler(recorder)), {}))
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        self.url = "http://127.0.0.1:" + str(httpd.server_port)

    def create(self, **overrides):
        body = {"type": "car", "initial_state": copy.deepcopy(NAVI_STATE),
                "tool_profile": list(PROFILE), "seed": 0, "dataset_version": DATA_VERSION}
        body.update(overrides)
        status, payload = http_json(self.url + "/eval/api/v1/sessions", "POST", body)
        self.assertEqual(status, 201)
        return payload["session_id"]

    def test_redis_sessions_are_isolated_and_atomic(self):
        first = self.create()
        second = self.create()
        rpc(self.url + "/eval/mcp/" + first, "tools/call",
            {"name": "start_navi", "arguments": {"poi_list": [COMPANY_POI]}, "call_id": "c1"}, rpc_id="a")
        _, snapshot = http_json(self.url + "/api/v1/rl/sessions/" + first + "/snapshot")
        self.assertEqual(snapshot["state_version"], 1)
        _, other = http_json(self.url + "/api/v1/rl/sessions/" + second + "/snapshot")
        self.assertEqual(other["state_version"], 0)
        self.assertFalse(other["state"]["navigation"]["active"])

    def test_redis_delete_cleans_up(self):
        sid = self.create()
        status, payload = http_json(self.url + "/eval/api/v1/sessions/" + sid, "DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(payload["deleted"])
        self.assertEqual(list(self.store.client.scan_iter(match=self.prefix + "*")), [])


if __name__ == "__main__":
    unittest.main()
