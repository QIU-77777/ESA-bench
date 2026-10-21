"""确定性本地 Responses 接口，用于离线冒烟测试。支持 JSON 与 SSE 两种响应。"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REASONING_TEXT = "先判断用户意图，再选择工具或直接回复。"


def build_output(request):
    """按历史中的工具调用数量决定本次输出项。"""
    input_items = request.get("input", [])
    results = [x for x in input_items if x.get("type") == "function_call_output"]
    calls = [x for x in input_items if x.get("type") == "function_call"]
    reasoning = {"type": "reasoning", "summary": [{"type": "summary_text", "text": REASONING_TEXT}]}
    if not calls:
        item = {"type": "function_call", "name": "get_favorite_poi", "call_id": "offline-favorite", "arguments": json.dumps({"address_type": "company", "limit": 10})}
    elif len(calls) == 1:
        previous = json.loads(results[-1]["output"])
        assert not previous["isError"], previous
        item = {"type": "function_call", "name": "start_navi", "call_id": "offline-navigation", "arguments": json.dumps({"poi_list": ["200001:公司:121.42,31.19"]})}
    else:
        previous = json.loads(results[-1]["output"])
        assert not previous["isError"], previous
        nav = previous["structuredContent"]
        assert nav["active"] is True
        text = "已开始导航前往公司，距离 %.1f 公里，预计 %.0f 分钟。" % (nav["distance_m"] / 1000, nav["duration_s"] / 60)
        item = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    payload = {"id": "offline-" + str(len(calls)), "status": "completed", "output": [reasoning, item], "usage": {"input_tokens": 100, "output_tokens": 10}}
    return payload, reasoning, item


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert request.get("store") is False
        assert "previous_response_id" not in request
        payload, reasoning, item = build_output(request)
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def emit(event):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()

            emit({"type": "response.created", "response": {"id": payload["id"], "status": "in_progress"}})
            time.sleep(0.05)
            emit({"type": "response.reasoning_summary_text.delta", "output_index": 0, "delta": REASONING_TEXT})
            time.sleep(0.05)
            if item["type"] == "function_call":
                emit({"type": "response.output_item.added", "output_index": 1, "item": {"type": "function_call", "name": item["name"], "call_id": item["call_id"], "arguments": ""}})
                emit({"type": "response.function_call_arguments.delta", "output_index": 1, "delta": item["arguments"]})
                emit({"type": "response.output_item.done", "output_index": 1, "item": item})
            else:
                text = item["content"][0]["text"]
                for part in (text[: len(text) // 2], text[len(text) // 2 :]):
                    emit({"type": "response.output_text.delta", "output_index": 1, "delta": part})
                    time.sleep(0.05)
                emit({"type": "response.output_item.done", "output_index": 1, "item": item})
            emit({"type": "response.completed", "response": payload})
            return
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=19090)
    args = parser.parse_args()
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
