"""Python standard-library HTTP + MCP JSON-RPC endpoint."""
import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from .service import Environment, ResponseLost
from .store import RedisStore, MemoryStore, MissingSession, Conflict
from .data import Dataset

MAX_BODY = 2*1024*1024

def make_handler(environment):
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def send_json(self,status,value):
            data=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self):
            if self.headers.get('Transfer-Encoding'): raise ValueError('chunked requests unsupported')
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=MAX_BODY: raise ValueError('request body missing or too large')
            return json.loads(self.rfile.read(length),parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON number')))

        def do_GET(self): self.dispatch('GET')
        def do_POST(self): self.dispatch('POST')
        def do_DELETE(self): self.dispatch('DELETE')

        def dispatch(self,method):
            try:
                split=urlsplit(self.path);path=split.path;q=parse_qs(split.query)
                if method=='GET' and path=='/health':
                    return self.send_json(200,{'status':'ok','dataset_version':environment.dataset.version,'enabled_tools':len(environment.enabled),'store':type(environment.store).__name__})
                if method=='GET' and path in ('/manifest','/eval/api/v1/manifest'):
                    return self.send_json(200,environment.manifest())
                if method=='POST' and path=='/eval/api/v1/sessions':
                    return self.send_json(201,environment.create(self.body()))
                m=re.fullmatch(r'/(?:api/v1/rl|eval/api/v1)/sessions/([a-f0-9]+)/(?P<action>snapshot|events|reset|round)',path)
                if m:
                    sid=m[1];action=m['action']
                    if method=='GET' and action=='snapshot': return self.send_json(200,environment.snapshot(sid))
                    if method=='GET' and action=='events': return self.send_json(200,environment.events(sid,int(q.get('after',['0'])[0]),q.get('call_id',[None])[0]))
                    if method=='POST' and action in ('reset','round'):
                        if self.headers.get('Content-Length','0')!='0':
                            payload=self.body()
                            if not isinstance(payload,dict): raise ValueError('lifecycle body must be an object')
                        result=environment.reset(sid) if action=='reset' else environment.begin_round(sid)
                        return self.send_json(200,result)
                m=re.fullmatch(r'/eval/api/v1/sessions/([a-f0-9]+)',path)
                if m and method=='DELETE': return self.send_json(200,{'deleted':environment.store.delete(m[1])})
                m=re.fullmatch(r'/eval/mcp/([a-f0-9]+)',path)
                if m and method=='POST': return self.mcp(m[1],self.body())
                return self.send_json(404,{'error':'route not found'})
            except ResponseLost:
                self.close_connection=True
                self.connection.shutdown(2)
            except MissingSession:
                self.send_json(404,{'error':'session not found'})
            except (ValueError,TypeError,KeyError) as exc:
                self.send_json(400,{'error':str(exc)})
            except Conflict as exc:
                self.send_json(409,{'error':str(exc)})
            except Exception:
                self.send_json(503,{'error':'environment service unavailable'})

        def mcp(self,sid,request):
            if not isinstance(request,dict) or request.get('jsonrpc')!='2.0':
                return self.send_json(200,{'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'invalid JSON-RPC request'}})
            rid=request.get('id');method=request.get('method');params=request.get('params',{})
            if not isinstance(params,dict): return self.send_json(200,{'jsonrpc':'2.0','id':rid,'error':{'code':-32602,'message':'params must be an object'}})
            if method=='tools/list': result=environment.list_tools(sid)
            elif method=='tools/call': result=environment.execute(sid,params.get('name'),params.get('arguments',{}),params.get('call_id'))
            elif method=='initialize':
                environment.store.read(sid)
                result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'esa-local-environment','version':'1.0.0'}}
            elif method=='notifications/initialized': return self.send_json(200,{})
            else: return self.send_json(200,{'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'method not found'}})
            return self.send_json(200,{'jsonrpc':'2.0','id':rid,'result':result})
    return Handler

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',default='0.0.0.0')
    parser.add_argument('--port',type=int,default=8003)
    parser.add_argument('--store',choices=['redis','memory-test'],default=os.getenv('ESA_STORE','redis'))
    parser.add_argument('--redis-url',default=os.getenv('REDIS_URL','redis://redis:6379/0'))
    parser.add_argument('--config',default=None)
    parser.add_argument('--db',default=os.getenv('ESA_DATA_DB','/tmp/esa-seed-v1.sqlite3'))
    parser.add_argument('--seed-file',default=os.getenv('ESA_SEED_FILE'))
    args=parser.parse_args()
    store=MemoryStore(test_only=True) if args.store=='memory-test' else RedisStore(args.redis_url,ttl=int(os.getenv('ESA_SESSION_TTL','86400')))
    environment=Environment(store,Dataset(seed_path=args.seed_file,db_path=args.db),config_path=args.config)
    server=ThreadingHTTPServer((args.host,args.port),make_handler(environment))
    server.daemon_threads=True
    try: server.serve_forever()
    finally: server.server_close()

if __name__=='__main__': main()
