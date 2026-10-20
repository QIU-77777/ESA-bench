"""Deterministic local Responses endpoint for offline Harbor acceptance only."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        request=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert request.get("store") is False
        assert "previous_response_id" not in request
        input_items=request.get("input",[])
        results=[x for x in input_items if x.get("type")=="function_call_output"]
        calls=[x for x in input_items if x.get("type")=="function_call"]
        if not calls:
            item={"type":"function_call","name":"get_favorite_poi","call_id":"offline-favorite","arguments":json.dumps({"address_type":"company","limit":10})}
        elif len(calls)==1:
            previous=json.loads(results[-1]["output"])
            assert not previous["isError"],previous
            item={"type":"function_call","name":"start_navi","call_id":"offline-navigation","arguments":json.dumps({"poi_list":["200001:上海市徐汇区科技园 A 座:121.42,31.19"]})}
        else:
            previous=json.loads(results[-1]["output"])
            assert not previous["isError"],previous
            nav=previous["structuredContent"]
            assert nav["active"] is True
            text="已开始导航前往公司，距离 %.1f 公里，预计 %.0f 分钟。"%(nav["distance_m"]/1000,nav["duration_s"]/60)
            item={"type":"message","role":"assistant","content":[{"type":"output_text","text":text}]}
        payload={"id":"offline-"+str(len(calls)),"status":"completed","output":[item],"usage":{"input_tokens":100,"output_tokens":10}}
        if request.get("stream"):
            raw=("event: response.completed"+chr(10)+"data: "+json.dumps({"type":"response.completed","response":payload})+chr(10)*2).encode()
            content_type="text/event-stream"
        else:
            raw=json.dumps(payload).encode();content_type="application/json"
        self.send_response(200);self.send_header("Content-Type",content_type);self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--port",type=int,default=19090);args=parser.parse_args()
    ThreadingHTTPServer(("0.0.0.0",args.port),Handler).serve_forever()
