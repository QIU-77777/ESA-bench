import copy
import concurrent.futures
import hashlib
import json
import os
import pathlib
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from environment.data import Dataset, ROOT
from environment.generate_catalog import generate
from environment.handlers import REGISTRY, CAPABILITIES, poi
from environment.service import Environment, ResponseLost
from environment.server import make_handler
from environment.store import MemoryStore, RedisStore

P='poi_company:公司:120.22,30.25'
W='poi_mall:杭州万象城:120.212,30.253'

class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=Environment(MemoryStore(test_only=True),workspace_root=self.tmp.name)
        self.sid=self.env.create({})['session_id']
    def tearDown(self): self.tmp.cleanup()
    def call(self,name,args=None,sid=None,cid=None):
        out=self.env.execute(sid or self.sid,name,args or {},cid)
        self.assertFalse(out['isError'],out)
        return out['structuredContent']
    def bad(self,name,args=None,sid=None,cid=None):
        before=self.env.snapshot(sid or self.sid)
        out=self.env.execute(sid or self.sid,name,args or {},cid)
        self.assertTrue(out['isError'],out)
        after=self.env.snapshot(sid or self.sid)
        self.assertEqual(before['state'],after['state'])
        self.assertEqual(before['state_version'],after['state_version'])
        return out

    def test_all61_schema_handler_manifest_and_default59(self):
        self.assertEqual(61,len(self.env.tools));self.assertEqual(set(self.env.tools),set(REGISTRY));self.assertEqual(set(self.env.tools),set(CAPABILITIES));self.assertEqual(59,len(self.env.list_tools(self.sid)['tools']))
        checked=json.loads((ROOT/'manifest.json').read_text())
        self.assertEqual(self.env.manifest(),checked)
        for entry in checked['tools']:
            for field in ('handler','reads','writes','constraints','result','limitations'): self.assertIn(field,entry)
        # Every disabled tool must execute an explicit permission failure and event.
        for n in set(self.env.tools)-set(self.env.enabled): self.bad(n,{'path':'.'})

    def test_canonical_source_hash_and_regeneration(self):
        source=os.getenv('ESA_GOLDEN_SOURCE')
        if not source: self.skipTest('set ESA_GOLDEN_SOURCE to verify external canonical source')
        output=pathlib.Path(self.tmp.name)/'catalog.json';generate(source,output)
        self.assertEqual(json.loads(output.read_text()),json.loads((ROOT/'catalog.json').read_text()))

    def test_all59_named_behaviors(self):
        args={
            'a2a_invoke':{'agent_identifier':'after_sale_agent','task':'空调保养'},'activate_autopilot':{},'add_memory':{'content':'记住杭州西湖'},'after_sale_agent':{'query':'空调保养'},'control_map_zoom':{'action':'zoom_in'},'digital_service_agent':{'historys':[],'query':'订座外婆家'},'display_candidate_cards':{},'eco_info_search':{'query':'美食','category':'food'},'eco_info_search_1':{'query':'酒店','category':'hotel'},'end_chat':{},'estimate_trip_info':{'to_poi':P},'execute_vehicle_control':{'query':'打开空调，温度22度'},'financial_information_agent':{'query':'股票指数'},'get_favorite_poi':{'address_type':'favorite','limit':10},'get_home_or_company_address':{'poi_type':'home'},'get_joke':{'query':'冷笑话'},'get_skill_prompt':{},'get_story':{'query':'睡前故事'},'get_vehicle_status':{'query_items':['GEAR_STATUS','REMAINING_BATTERY']},'get_weather':{'district':'杭州市'},'list_skills':{},'map_search':{'query':'西湖'},'multi_task_agent':{'query':'外婆家订座'},'music_search':{'query':'周杰伦'},'open_or_close_map':{'action':'open'},'open_special_map':{'map_type':'food','action':'open'},'overview_navi_route':{'action':'enter'},'play_playlist':{'playlist':[{'name':'晴天','artist':'周杰伦'}]},'query_adas_status':{},'query_traffic_ahead':{},'query_vehicle_exterior_vision':{'direction':'front','query':'前面什么车'},'remote_map_search':{'location':'杭州万象城','query':'餐厅'},'scheduled_task':{'title':'开会提醒','task_type':'general','steps':[{'condition':{'duration':'PT5M'},'actions':[{'type':'remind','content':'开会'}]}]},'search_along_way_poi':{'query':'充电'},'search_charging_station':{'query':'充电','sort_type':'distance'},'search_memory':{'query':'杭州'},'search_nearby_poi':{'query':'餐厅','center_poi_pos':'120.212,30.253'},'search_parking_charging_station':{'query':'停车','location':'杭州万象城'},'search_poi_by_keyword':{'query':'西湖'},'search_vehicle_knowledge':{'category':'manual','query':'空调'},'set_display_focus':{'position':'PASSENGER'},'set_favorite_poi':{'address_type':'favorite','poi':W},'set_map_auto_scale':{'enabled':False},'set_map_view':{'view':'3d_head_up'},'set_navi_route_preference':{'route_preference':'no_highway'},'set_navi_voice_mode':{'voice_mode':'simple'},'share_route':{},'web_search':{'query':'杭州'},'wiki_web_search':{'query':'沸点','tag':'natural_science'},'get_navi_info':{},'get_all_waypoint':{},'plan_navi_route':{'poi_list':[P]},'start_navi':{'poi_list':[P]},'exit_navigation':{},'reroute':{},'optimize_navi_plan':{},'add_waypoint':{'via_poi':W},'delete_waypoint':{'via_poi':W},'delete_favorite_poi':{'poi':W}}
        self.assertEqual(set(args),set(self.env.enabled))
        for name,a in args.items():
            with self.subTest(tool=name):
                sid=self.env.create({})['session_id']
                self.call('start_navi',{'poi_list':[W,P]},sid=sid)
                self.call('set_favorite_poi',{'address_type':'favorite','poi':W},sid=sid)
                self.call('add_memory',{'content':'杭州西湖'},sid=sid)
                self.call(name,a,sid=sid)

    def test_state_effects_read_after_write_and_changes(self):
        self.call('start_navi',{'poi_list':[W,P]})
        self.assertEqual('poi_company',self.call('get_navi_info')['destination']['id'])
        self.assertEqual(1,len(self.call('get_all_waypoint')['waypoints']))
        self.call('set_favorite_poi',{'address_type':'company','poi':P})
        self.assertEqual('poi_company',self.call('get_home_or_company_address',{'poi_type':'company'})['poi']['id'])
        self.call('add_memory',{'content':'周五去西湖','poi':W})
        self.assertEqual(1,len(self.call('search_memory',{'query':'周五'})['memories']))
        self.call('play_playlist',{'playlist':[{'name':'晴天','artist':'周杰伦'}]})
        snap=self.env.snapshot(self.sid)
        self.assertEqual('S10001',snap['state']['media']['current_track']['id']);self.assertTrue(snap['state']['media']['playing'])
        events=self.env.events(self.sid)['events']
        self.assertTrue(any(c['path']=='/navigation/destination' for e in events for c in e['changes']))
        for prev,event in zip(events,events[1:]): self.assertEqual(prev['after_version'],event['before_version'])

    def test_queries_filters_sort_limit_and_seed_determinism(self):
        a=self.call('search_poi_by_keyword',{'query':'西湖','limit':1})['pois'];b=self.call('search_poi_by_keyword',{'query':'咖啡'})['pois']
        self.assertNotEqual(a,b)
        stations=self.call('search_charging_station',{'query':'充电','brand':'极氪','charging_type':'super'})['pois']
        self.assertEqual(['poi_charge'],[x['id'] for x in stations])
        self.assertEqual([],self.call('search_nearby_poi',{'query':'西湖','radius':1})['pois'])
        near=self.call('search_poi_by_keyword',{'query':'','sort_by':'distance','limit':4})['pois']
        self.assertEqual(sorted(x['distance_m'] for x in near),[x['distance_m'] for x in near])
        sid=self.env.create({'seed':42})['session_id']
        self.assertEqual(self.call('music_search',{'query':'周杰伦'}),self.call('music_search',{'query':'周杰伦'},sid=sid))
        self.assertFalse(self.call('get_weather',{'district':'不存在'})['found'])
        self.assertNotEqual(self.call('get_weather',{'district':'杭州市','date':'2026-10-20'}),self.call('get_weather',{'district':'杭州市','date':'2026-10-21'}))

    def test_rollback_schema_unknown_permission_and_error_audit(self):
        self.bad('not_a_tool',{'x':1});self.bad('add_memory',{});self.bad('set_map_view',{'view':'invalid'});self.bad('execute_vehicle_control',{'query':'打开空调和车门'})
        self.bad('play_playlist',{'playlist':[{'name':'晴天','artist':'周杰伦'},{'name':'不存在','artist':''}]})
        self.bad('start_navi',{'poi_list':[]});self.bad('start_navi',{'poi_list':['spoof:名称:120,30']});self.bad('start_navi',{'poi_list':['poi_company:名称:121,30']})
        sid=self.env.create({'tool_profile':['get_navi_info']})['session_id']
        self.assertEqual(['get_navi_info'],[t['name'] for t in self.env.list_tools(sid)['tools']]);self.bad('add_memory',{'content':'x'},sid=sid)
        self.assertEqual(8,len(self.env.events(self.sid)['events']))
        self.assertTrue(all(e['status']=='error' and not e['changes'] for e in self.env.events(self.sid)['events']))
        parked=self.env.create({'initial_state':{'vehicle':{'gear':'P'}}})['session_id'];self.bad('start_navi',{'poi_list':[P]},sid=parked)

    def test_idempotency_same_request_and_id_mismatch(self):
        result=self.call('add_memory',{'content':'x'},cid='call-1')
        self.assertEqual(result,self.call('add_memory',{'content':'x','_call_id':'call-1'}))
        self.assertEqual(1,len(self.env.events(self.sid)['events']))
        self.bad('add_memory',{'content':'y'},cid='call-1')
        self.assertEqual(1,len(self.env.snapshot(self.sid)['state']['memory']))
        self.assertEqual(result,self.call('add_memory',{'content':'x'},cid='call-1'))
        self.assertEqual(2,len(self.env.events(self.sid)['events']))

    def test_fault_strict_match_counts_missing_and_response_loss(self):
        sid=self.env.create({'faults':[{'tool':'add_memory','arguments':{'content':'blocked'},'kind':'reject','times':1}]})['session_id']
        self.call('add_memory',{'content':'normal'},sid=sid);self.bad('add_memory',{'content':'blocked'},sid=sid);self.call('add_memory',{'content':'blocked'},sid=sid)
        sid=self.env.create({'faults':[{'tool':'get_navi_info','kind':'missing_field','field':'active'}]})['session_id']
        self.assertNotIn('active',self.call('get_navi_info',sid=sid));self.assertIn('active',self.call('get_navi_info',sid=sid))
        sid=self.env.create({'faults':[{'tool':'add_memory','kind':'response_lost'}]})['session_id']
        with self.assertRaises(ResponseLost): self.env.execute(sid,'add_memory',{'content':'committed'},'lost-1')
        self.assertEqual(1,len(self.env.events(sid,call_id='lost-1')['events']))
        self.call('add_memory',{'content':'committed'},sid=sid,cid='lost-1');self.assertEqual(1,len(self.env.snapshot(sid)['state']['memory']))

    def test_overlay_validation_and_database_read_only(self):
        with self.assertRaises(ValueError): self.env.create({'data_overlays':{'pois':[{'id':'poi_charge','expected':True}]}})
        with self.assertRaises(ValueError): self.env.create({'data_overlays':{'unknown':[]}})
        with self.assertRaises(ValueError): self.env.create({'data_overlays':{'pois':[{'id':'unknown','name':'x'}]}})
        sid=self.env.create({'data_overlays':{'pois':[{'id':'poi_charge','available':0}]}})['session_id']
        self.assertEqual([],self.call('search_charging_station',{'query':'充电','brand':'极氪'},sid=sid)['pois'])
        self.assertTrue(self.call('search_charging_station',{'query':'充电','brand':'极氪'})['pois'])
        conn=sqlite3.connect('file:'+str(self.env.dataset.db_path)+'?mode=ro',uri=True)
        with self.assertRaises(sqlite3.OperationalError): conn.execute("DELETE FROM pois")
        conn.close()
        seed=copy.deepcopy(self.env.dataset.seed);seed['routes'][0]['from_poi']='missing';p=pathlib.Path(self.tmp.name)/'broken.json';p.write_text(json.dumps(seed))
        with self.assertRaises(ValueError): Dataset(p)

    def test_config_fail_closed_and_test_store_explicit(self):
        with self.assertRaises(ValueError): MemoryStore()
        original=json.loads((ROOT.parent/'configs/tools.json').read_text())
        for mode in ('duplicate','unknown','missing'):
            config=copy.deepcopy(original)
            if mode=='duplicate': config['tools'].append(config['tools'][0])
            elif mode=='unknown': config['tools'][0]['name']='unknown'
            elif mode=='missing': config['tools'].pop()
            p=pathlib.Path(self.tmp.name)/'config.json';p.write_text(json.dumps(config))
            with self.assertRaises(ValueError): Environment(MemoryStore(test_only=True),config_path=p)
        for request in ({'tool_profile':['unknown']},{'tool_profile':['read_file']},{'tool_profile':['add_memory','add_memory']},{'tool_overrides':{}},{'dataset_version':'bad'}):
            with self.assertRaises(ValueError): self.env.create(request)

    def test_file_escape(self):
        config=json.loads((ROOT.parent/'configs/tools.json').read_text())
        for item in config['tools']:
            if item['name'] in ('read_file','list_directory'): item['enabled']=True
        p=pathlib.Path(self.tmp.name)/'config.json';p.write_text(json.dumps(config))
        self.env=Environment(MemoryStore(test_only=True),config_path=p,workspace_root=pathlib.Path(self.tmp.name)/'work');self.sid=self.env.create({})['session_id']
        root=self.env.workspace_root/self.sid;root.mkdir(parents=True);(root/'hello.txt').write_text('hello');(root/'escape').symlink_to('/etc')
        self.assertEqual('hello',self.call('read_file',{'path':'hello.txt'})['content'])
        for path in ('../config.json','/etc/passwd','escape/passwd'): self.bad('read_file',{'path':path})
        self.assertIn('hello.txt',self.call('list_directory',{'path':'.'})['entries'])
        self.bad('read_file',{'path':'notfound'})
        other=self.env.create({})['session_id']
        (self.env.workspace_root/other).symlink_to(root,target_is_directory=True)
        self.bad('read_file',{'path':'hello.txt'},sid=other)
        self.bad('list_directory',{'path':'.'},sid=other)

    def test_schedule_constraints_service_tasks_and_isolation(self):
        self.bad('scheduled_task',{'title':'x','task_type':'general','steps':[{'condition':{'event_type':'board','duration':'PT5M'},'actions':[{'type':'remind','content':'x'}]}]})
        pending=self.call('a2a_invoke',{'agent_identifier':'digital_service_agent','task':'订座','async_mode':True});self.assertEqual('pending',pending['status'])
        finished=self.call('a2a_invoke',{'agent_identifier':'digital_service_agent','task':'查询订座','task_id':pending['id'],'context_id':pending['context_id']});self.assertEqual('completed_local',finished['status']);self.assertEqual(4,len(finished['interactions']))
        self.bad('a2a_invoke',{'agent_identifier':'after_sale_agent','task':'x','task_id':pending['id']})
        other=self.env.create({})['session_id'];self.assertEqual([],self.env.snapshot(other)['state']['service_tasks'])

    def test_concurrent_calls_version_events_and_truncation(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda i:self.env.execute(self.sid,'add_memory',{'content':str(i)},'c-'+str(i)),range(40)))
        self.assertTrue(all(not x['isError'] for x in results));snap=self.env.snapshot(self.sid)
        self.assertEqual(40,snap['state_version']);self.assertEqual(40,len(snap['state']['memory']))
        self.assertEqual(list(range(1,41)),[e['sequence'] for e in self.env.events(self.sid)['events']])
        self.env.event_limit=2
        self.call('get_navi_info');self.assertEqual(40,self.env.snapshot(self.sid)['state_version']);self.assertTrue(self.env.events(self.sid)['truncated']);self.assertEqual(2,len(self.env.events(self.sid)['events']))
        self.env.reset(self.sid);self.assertEqual([],self.env.snapshot(self.sid)['state']['memory'])

    def test_legacy_benchmark_fixture_compatibility(self):
        fixtures=list((ROOT.parent/'tasks').glob('esa-*/environment/esa/initial_state.json'))
        if not fixtures: self.skipTest('benchmark fixture directory unavailable')
        for p in fixtures:
            d=json.loads(p.read_text());sid=self.env.create({k:v for k,v in d.items() if k in ('context','initial_state','dataset_version','seed')})['session_id'];snap=self.env.snapshot(sid)
            self.assertNotIn('vehicle',snap['context']);self.assertIn('lon',snap['context']['location']);self.assertEqual('100001',snap['state']['addresses']['home']['id'])
            if 'a3' in str(p):
                stations=self.call('search_charging_station',{'query':'快充桩','sort_type':'distance'},sid=sid)['pois'];self.assertEqual('400003',stations[0]['id'])
            if 'a4' in str(p): self.assertEqual(12,snap['state']['vehicle']['volume']);self.assertNotIn('volume',snap['state']['media'])
            if 'b1' in str(p): self.assertEqual(3,len(snap['state']['memory']))

    def test_active_navigation_and_modeled_traffic(self):
        self.call('plan_navi_route',{'poi_list':[P]})
        self.bad('query_traffic_ahead');self.bad('add_waypoint',{'via_poi':W});self.bad('search_along_way_poi',{'query':'充电'})
        self.call('start_navi',{'poi_list':[P]})
        self.assertEqual('畅通',self.call('query_traffic_ahead')['traffic'])
        sid=self.env.create({'data_overlays':{'routes':[{'id':'route_home_company','traffic':'模拟拥堵'}]}})['session_id']
        self.call('start_navi',{'poi_list':[P]},sid=sid);self.assertEqual('模拟拥堵',self.call('query_traffic_ahead',sid=sid)['traffic'])

    def test_round_reset_atomic_changes_and_invalid_overlay_type(self):
        self.call('set_display_focus',{'position':'PASSENGER'})
        version=self.env.snapshot(self.sid)['state_version']
        self.env.begin_round(self.sid)
        snap=self.env.snapshot(self.sid);event=self.env.events(self.sid)['events'][-1]
        self.assertEqual(version+1,snap['state_version']);self.assertEqual('FRONT_ROW',snap['state']['display']['focus']['position']);self.assertTrue(event['changes'])
        self.call('add_memory',{'content':'reset'})
        self.env.reset(self.sid);self.assertTrue(self.env.events(self.sid)['events'][-1]['changes'])
        self.assertEqual(self.env.manifest(),self.env.snapshot(self.sid)['manifest'])
        with self.assertRaises(ValueError): self.env.create({'data_overlays':{'pois':[{'id':'poi_charge','price':'invalid'}]}})

    def test_http_driver_root_routes_and_mcp_call_id(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.env));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base='http://127.0.0.1:'+str(server.server_port)
        def request(path,body=None,method=None):
            req=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'},method=method)
            with urllib.request.urlopen(req) as response: return json.load(response)
        try:
            created=request('/eval/api/v1/sessions',{'type':'car','tool_profile':['add_memory']});sid=created['session_id']
            listed=request('/eval/mcp/'+sid,{'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}});self.assertEqual(1,len(listed['result']['tools']))
            call={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'add_memory','arguments':{'content':'http','_call_id':'http-1'},'call_id':'http-1'}}
            self.assertEqual(request('/eval/mcp/'+sid,call),request('/eval/mcp/'+sid,call))
            self.assertEqual(1,len(request('/api/v1/rl/sessions/'+sid+'/events')['events']))
            self.assertEqual(1,request('/api/v1/rl/sessions/'+sid+'/snapshot')['state_version'])
            self.assertTrue(request('/eval/api/v1/sessions/'+sid,method='DELETE')['deleted'])
        finally: server.shutdown();server.server_close();thread.join()

@unittest.skipUnless(os.getenv('ESA_TEST_REDIS_URL'),'set ESA_TEST_REDIS_URL for real Redis integration')
class RedisIntegrationTests(EnvironmentTests):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.prefix='esa:test:'+os.urandom(8).hex()+':'
        store=RedisStore(os.environ['ESA_TEST_REDIS_URL'],prefix=self.prefix,ttl=60,retries=100)
        self.env=Environment(store,workspace_root=self.tmp.name);self.sid=self.env.create({})['session_id']
    def tearDown(self):
        keys=list(self.env.store.client.scan_iter(match=self.prefix+'*')) if isinstance(self.env.store,RedisStore) else []
        if keys: self.env.store.client.delete(*keys)
        self.tmp.cleanup()
    def test_redis_ttl_and_authoritative_snapshot(self):
        self.call('add_memory',{'content':'redis'})
        self.assertGreater(self.env.store.client.ttl(self.prefix+self.sid),0)
        independent=Environment(RedisStore(os.environ['ESA_TEST_REDIS_URL'],prefix=self.prefix,ttl=60))
        self.assertEqual(self.env.snapshot(self.sid),independent.snapshot(self.sid))

if __name__=='__main__': unittest.main()
