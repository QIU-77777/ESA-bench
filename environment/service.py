"""Session lifecycle, permission validation and atomic tool execution."""
import copy
import hashlib
import json
import os
import pathlib
import uuid
from jsonschema import Draft202012Validator, ValidationError
from .data import Dataset, ROOT
from .handlers import REGISTRY, CAPABILITIES
from .store import Conflict
from .compat import normalize, point

class ResponseLost(Exception):
    pass

def merge(base, patch):
    if not isinstance(patch,dict):
        raise ValueError('initial state must be an object')
    for key,value in patch.items():
        if key not in base:
            raise ValueError('unknown state field: '+key)
        if isinstance(base[key],dict) and isinstance(value,dict):
            merge(base[key],value)
        else:
            base[key]=copy.deepcopy(value)
    return base

def changes(before, after, path=''):
    if before == after: return []
    if isinstance(before,dict) and isinstance(after,dict):
        out=[]
        for key in sorted(set(before)|set(after)):
            escaped=str(key).replace('~','~0').replace('/','~1')
            out.extend(changes(before.get(key),after.get(key),path+'/'+escaped))
        return out
    return [{'path':path or '/', 'before':before, 'after':after}]

class Environment:
    def __init__(self, store, dataset=None, config_path=None, workspace_root=None, event_limit=10000):
        self.store=store
        self.dataset=dataset or Dataset()
        self.workspace_root=pathlib.Path(workspace_root or os.getenv('ESA_WORKSPACE_ROOT','/tmp/esa-workspaces')).resolve()
        self.event_limit=event_limit
        if event_limit < 1: raise ValueError('event_limit must be positive')
        self.catalog=json.loads((ROOT/'catalog.json').read_text())
        self.tools={t['name']:t for t in self.catalog['tools']}
        if len(self.tools)!=61 or len(self.catalog['tools'])!=61 or set(self.tools)!=set(REGISTRY):
            raise ValueError('catalog/handler coverage must be exactly all61')
        config=json.loads(pathlib.Path(config_path or ROOT.parent/'configs/tools.json').read_text())
        seen=set();enabled=[]
        for item in config['tools']:
            name=item['name']
            if name not in self.tools or name in seen or not isinstance(item.get('enabled'),bool): raise ValueError('unknown/duplicate/invalid tool configuration')
            seen.add(name)
            if item['enabled']:
                if name not in REGISTRY: raise ValueError('enabled handler unimplemented')
                enabled.append(name)
        if seen!=set(self.tools): raise ValueError('config must preserve every61 name')
        self.enabled=enabled
        self.validators={name:Draft202012Validator(t['inputSchema']) for name,t in self.tools.items()}
        for t in self.tools.values(): Draft202012Validator.check_schema(t['inputSchema'])

    def create(self, request):
        if not isinstance(request,dict): raise ValueError('session request must be an object')
        forbidden={'expected','expected_state','tool_overrides','forbidden_changes','process_constraints','reply','judge'}
        if set(request)&forbidden: raise ValueError('grading/override fields forbidden in environment session')
        if request.get('type','car')!='car': raise ValueError('session type must be car')
        version=request.get('dataset_version',self.dataset.version)
        if version!=self.dataset.version: raise ValueError('unknown dataset version')
        seed=request.get('seed',0)
        if not isinstance(seed,(int,str)) or isinstance(seed,bool): raise ValueError('seed must be an integer or string')
        profile=request.get('tool_profile',self.enabled)
        if profile is None: profile=self.enabled
        if not isinstance(profile,list) or any(not isinstance(x,str) for x in profile) or len(set(profile))!=len(profile) or set(profile)-set(self.enabled):
            raise ValueError('tool_profile must be unique enabled tool names')
        context={'time':'2026-10-20T09:00:00+08:00','location':{'id':'poi_home','name':'当前位置','lon':120.200,'lat':30.260},'vehicle_model':'local-electric-v1','timezone':'Asia/Shanghai'}
        supplied=request.get('initial_context',request.get('context',{})) or {}
        if not isinstance(supplied,dict): raise ValueError('context must be an object')
        dynamic=set(('vehicle','navigation','user_addresses','addresses','memories','memory','navi_history','media','display','map','schedules','service_tasks','chat','gear'))
        context.update({k:copy.deepcopy(v) for k,v in supplied.items() if k not in dynamic})
        if isinstance(supplied.get('datetime'),dict):
            clock=supplied['datetime']
            context['time']=clock.get('date','2026-10-20')+'T'+clock.get('time','09:00')+':00+08:00'
        if isinstance(context.get('location'),dict): context['location']=point(context['location'])
        # Legacy current location and clock inputs remain deterministic.
        if 'current_time' in supplied: context['time']=supplied['current_time']
        if 'current_location' in supplied: context['location']=copy.deepcopy(supplied['current_location'])
        if not isinstance(context['time'],str) or len(context['time'])<10: raise ValueError('invalid context time')
        loc=context['location']
        if isinstance(loc,str):
            from .handlers import poi
            context['location']=poi(self,{'data_overlays':{}},loc)
        if not isinstance(context['location'],dict) or not all(isinstance(context['location'].get(k),(int,float)) for k in ('lon','lat')): raise ValueError('location needs lon and lat')
        state={'vehicle':{'windows':{'driver':'closed','passenger':'closed','rear_left':'closed','rear_right':'closed'},'doors':{'driver':'closed','passenger':'closed','rear_left':'closed','rear_right':'closed','tailgate':'closed'},'inside_temperature':23,'gear':'D','speed':40,'fuel':0,'battery':80,'range_km':420,'ac_on':False,'ac_temperature':24,'volume':40,'seat_heating':False,'adas':{'available':True,'active':False,'mode':'NOA'}},'navigation':{'active':False,'planned':False,'destination':None,'waypoints':[],'route_preference':'smart_recommend','voice_mode':'smart','route_revision':0,'distance_m':0,'duration_s':0,'toll':0,'traffic_lights':0},'addresses':{'home':None,'company':None,'favorites':[]},'memory':[],'media':{'playlist':[],'current_track':None,'playing':False},'display':{'focus':{'screen_type':'CENTER_SCREEN','position':'FRONT_ROW'},'candidates':[],'cards':[],'share_panel':None},'map':{'open':False,'page':'map','special':None,'view':'2d_north_up','auto_scale':True,'zoom':10,'overview':False},'schedules':[],'service_tasks':[],'chat':{'ended':False}}
        legacy=normalize({k:v for k,v in supplied.items() if k in dynamic and k!='gear'})
        merge(state,legacy)
        if 'gear' in supplied: state['vehicle']['gear']=supplied['gear']
        merge(state,normalize(request.get('initial_state',{}) or {}))
        self.validate_state(state)
        overlays=self.dataset.validate_overlays(request.get('data_overlays',{}) or {})
        faults=request.get('faults',[]) or []
        self.validate_faults(faults)
        sid=uuid.uuid4().hex
        record={'session_id':sid,'context':context,'state':state,'initial_state':copy.deepcopy(state),'dataset_version':version,'seed':seed,'tool_profile':list(profile),'state_version':0,'data_overlays':overlays,'faults':copy.deepcopy(faults),'fault_counts':{},'events':[],'event_sequence':0,'events_truncated':False,'idempotency':{}}
        self.store.create(sid,record)
        return {'id':sid,'session_id':sid,'dataset_version':version,'seed':seed,'tool_profile':list(profile),'state_version':0,'mcp_url':'/eval/mcp/'+sid}

    @staticmethod
    def validate_state(state):
        for key in ('memory','schedules','service_tasks'):
            if not isinstance(state[key],list): raise ValueError('invalid state '+key)
        v=state['vehicle']
        if v['gear'] not in ('P','D','R','N'): raise ValueError('invalid gear')
        for key in ('speed','battery','range_km','inside_temperature','ac_temperature','volume','fuel'):
            if not isinstance(v[key],(int,float)) or isinstance(v[key],bool): raise ValueError('invalid vehicle numeric state')
        if not 0<=v['battery']<=100 or not 0<=v['volume']<=100: raise ValueError('invalid percentage')
        if not isinstance(state['navigation']['waypoints'],list) or not isinstance(state['addresses']['favorites'],list): raise ValueError('invalid state list')
        for key in ('windows','doors'):
            if not isinstance(v[key],dict) or any(x not in ('open','closed') for x in v[key].values()): raise ValueError('invalid door/window state')

    def validate_faults(self,faults):
        if not isinstance(faults,list): raise ValueError('faults must be a list')
        for f in faults:
            if not isinstance(f,dict) or set(f)-{'tool','arguments','times','kind','field','message'}: raise ValueError('invalid fault fields')
            if f.get('tool') not in self.tools or f.get('kind') not in ('reject','missing_field','response_lost'): raise ValueError('invalid fault tool/kind')
            if not isinstance(f.get('arguments',{}),dict) or not isinstance(f.get('times',1),int) or isinstance(f.get('times',1),bool) or f.get('times',1)<1: raise ValueError('invalid fault matcher/count')
            if f['kind']=='missing_field' and not isinstance(f.get('field'),str): raise ValueError('missing_field requires field')

    def snapshot(self,sid):
        r=self.store.read(sid)
        return {**{k:r[k] for k in ('session_id','context','state','initial_state','dataset_version','seed','tool_profile','state_version')},'environment_version':'1.0.0','source_sha256':self.catalog['source_sha256'],'seed_sha256':self.dataset.seed_sha256,'manifest':self.manifest()}

    def events(self,sid,after=0,call_id=None):
        r=self.store.read(sid)
        events=[e for e in r['events'] if e['sequence']>after and (call_id is None or e['call_id']==call_id)]
        return {'events':events,'truncated':r['events_truncated'],'total_events':r['event_sequence'],'state_version':r['state_version']}

    def list_tools(self,sid):
        r=self.store.read(sid)
        return {'tools':[{k:self.tools[n][k] for k in ('name','description','inputSchema')} for n in r['tool_profile']]}

    def manifest(self):
        return {'environment_version':'1.0.0','dataset_version':self.dataset.version,'seed_sha256':self.dataset.seed_sha256,'source_sha256':self.catalog['source_sha256'],'tools':[{ 'name':name,'enabled':name in self.enabled, 'availability':'implemented', **CAPABILITIES[name]} for name in self.tools]}

    def lifecycle(self,sid,action):
        def transaction(r):
            original=copy.deepcopy(r['state']);before=r['state_version']
            if action=='reset':
                r['state']=copy.deepcopy(r['initial_state']);r['idempotency']={};r['fault_counts']={}
            elif action=='round':
                r['state']['display']['focus']={'screen_type':'CENTER_SCREEN','position':'FRONT_ROW'}
            else: raise ValueError('unknown lifecycle action')
            delta=changes(original,r['state'])
            if delta: r['state_version']+=1
            r['event_sequence']+=1
            result={action:True,'state_version':r['state_version']}
            r['events'].append({'sequence':r['event_sequence'],'call_id':None,'tool':'session/'+action,'arguments':{},'result':result,'status':'ok','before_version':before,'after_version':r['state_version'],'changes':delta})
            self.trim(r)
            return result
        return self.store.transact(sid,transaction)

    def reset(self,sid): return self.lifecycle(sid,'reset')

    def begin_round(self,sid): return self.lifecycle(sid,'round')

    def trim(self,r):
        if len(r['events'])>self.event_limit:
            r['events']=r['events'][-self.event_limit:];r['events_truncated']=True

    def execute(self,sid,name,args,call_id=None):
        arguments=copy.deepcopy(args)
        if isinstance(arguments,dict):
            embedded=arguments.pop('_call_id',None)
            call_id=call_id or embedded
        call_id=call_id or uuid.uuid4().hex
        if not isinstance(call_id,str) or not call_id: raise ValueError('call_id must be a nonempty string')
        fingerprint=hashlib.sha256(json.dumps({'name':name,'arguments':arguments},sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        def transaction(r):
            cached=r['idempotency'].get(call_id)
            if cached and cached['fingerprint']==fingerprint:
                return {'result':copy.deepcopy(cached['result']),'lost':False}
            original=copy.deepcopy(r['state']);working=copy.deepcopy(r);status='ok';lost=False
            try:
                if cached: raise ValueError('call_id reused with different request')
                if name not in self.tools: raise ValueError('unknown tool: '+str(name))
                if name not in r['tool_profile']: raise PermissionError('tool disabled by session profile')
                self.validators[name].validate(arguments)
                selected=None
                for idx,f in enumerate(r['faults']):
                    if f['tool']==name and all(arguments.get(k)==v for k,v in f.get('arguments',{}).items()) and r['fault_counts'].get(str(idx),0)<f.get('times',1):
                        selected=f;r['fault_counts'][str(idx)]=r['fault_counts'].get(str(idx),0)+1;break
                if selected and selected['kind']=='reject': raise ValueError(selected.get('message','injected execution rejection'))
                result=REGISTRY[name](self,working,arguments)
                self.validate_state(working['state'])
                json.dumps(result,allow_nan=False)
                if selected and selected['kind']=='missing_field':
                    node=result;parts=selected['field'].split('.')
                    for p in parts[:-1]: node=node.get(p,{}) if isinstance(node,dict) else {}
                    if isinstance(node,dict): node.pop(parts[-1],None)
                lost=bool(selected and selected['kind']=='response_lost')
                output={'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}],'structuredContent':result,'isError':False}
                r['state']=working['state']
            except Exception as exc:
                status='error'
                code='permission_denied' if isinstance(exc,PermissionError) else 'schema_error' if isinstance(exc,ValidationError) else 'execution_error'
                message=exc.message if isinstance(exc,ValidationError) else str(exc)
                result={'error':{'code':code,'message':message}}
                output={'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}],'structuredContent':result,'isError':True}
            delta=changes(original,r['state'])
            before=r['state_version']
            if delta: r['state_version']+=1
            r['event_sequence']+=1
            r['events'].append({'sequence':r['event_sequence'],'call_id':call_id,'tool':name,'arguments':arguments,'result':result,'status':status,'before_version':before,'after_version':r['state_version'],'changes':delta,'response_lost':lost})
            self.trim(r)
            if not cached: r['idempotency'][call_id]={'fingerprint':fingerprint,'result':copy.deepcopy(output)}
            return {'result':output,'lost':lost}
        committed=self.store.transact(sid,transaction)
        if committed['lost']: raise ResponseLost('injected response loss after committed call '+call_id)
        return committed['result']
