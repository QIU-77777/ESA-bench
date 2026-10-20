"""Named local tool behaviors. Every tool is registered explicitly."""
import copy
import hashlib
import json
import math
import pathlib
import re
from datetime import date, timedelta
from .data import distance, relevance, limit

REGISTRY = {}
CAPABILITIES = {}

def tool(name, reads, writes, constraints, result, limitations='Deterministic local simulation; no network or physical vehicle operation.'):
    def register(fn):
        if name in REGISTRY:
            raise ValueError('duplicate handler')
        REGISTRY[name] = fn
        CAPABILITIES[name] = dict(handler=fn.__name__, reads=reads, writes=writes, constraints=constraints, result=result, limitations=limitations)
        return fn
    return register

def rows(env, record, table):
    return env.dataset.rows(table, record['data_overlays'])

def poi(env, record, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('POI must be a nonempty string')
    parts = value.split(':')
    candidates = rows(env, record, 'pois')
    if len(parts) == 1:
        for p in candidates:
            if value in (p['id'], p['name']):
                return copy.deepcopy(p)
        raise ValueError('POI not found')
    if len(parts) < 3:
        raise ValueError('POI format must be id:name:longitude,latitude')
    coordinates = parts[2].split(',')
    if len(coordinates) != 2:
        raise ValueError('invalid POI coordinates')
    lon, lat = map(float, coordinates)
    if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
        raise ValueError('coordinates outside valid range')
    base = next((p for p in candidates if p['id'] == parts[0]), {})
    if parts[0] and parts[0] not in ('cur','null'):
        if not base: raise ValueError('unknown POI identity')
        if abs(base['lon']-lon)>0.000001 or abs(base['lat']-lat)>0.000001: raise ValueError('POI identity coordinates mismatch')
    return dict(base, id=parts[0] or 'custom-'+hashlib.sha256(value.encode()).hexdigest()[:12], name=parts[1], lon=lon, lat=lat, stopover=len(parts)>3 and parts[3].lower() in ('true','1','是'))

def center(env, r, a):
    if a.get('center_poi_pos'):
        return poi(env,r, ':center:'+a['center_poi_pos'])
    location = a.get('location')
    if location:
        aliases = {'家':'home','公司':'company','home':'home','company':'company'}
        if location in aliases:
            p = r['state']['addresses'][aliases[location]]
            if not p:
                raise ValueError('address not set')
            return p
        if location in ('目的地','destination'):
            return require_route(r)['destination']
        matches = [p for p in rows(env,r,'pois') if location in p['name'] or location == p['city'] or location in p['address']]
        if not matches:
            raise ValueError('search location not found')
        return matches[0]
    return r['context']['location']

def search_pois(env,r,a,category=None,along=False):
    c = center(env,r,a)
    found=[]
    radius = a.get('radius', a.get('distance_range',{}).get('max'))
    if radius is not None and (not isinstance(radius,(int,float)) or radius < 0):
        raise ValueError('radius must be nonnegative')
    nav = require_route(r,active=True) if along else None
    for p in rows(env,r,'pois'):
        if category and p['category'] not in category:
            continue
        if category==['charge'] and p.get('available',0)<=0:
            continue
        if a.get('brand') and a['brand'] not in p.get('brand',''):
            continue
        power = p.get('power_kw',0)
        kind = a.get('charging_type','default')
        if category==['charge'] and kind=='default' and '快充' in a.get('query','') and power<30: continue
        if category==['charge'] and kind=='default' and '慢充' in a.get('query','') and power>=30: continue
        if kind != 'default' and not {'slow':3.5<=power<30,'fast':30<=power<120,'super':120<=power<480,'lightning':power>=480}[kind]:
            continue
        score=relevance(a.get('query',''),p)
        if not score:
            continue
        d=distance(c,p)
        if radius is not None and d > radius:
            continue
        if nav:
            start=r['context']['location']; end=nav['destination']
            if distance(start,p)+distance(p,end) > distance(start,end)*1.35+1000:
                continue
        item=dict(p,distance_m=d,relevance=score,poi=f"{p['id']}:{p['name']}:{p['lon']},{p['lat']}")
        precision=a.get('poi_search_precision','normal')
        item['precision']=precision
        found.append(item)
    sort=a.get('sort_by',a.get('sort_type','relevance'))
    key={'distance':lambda p:(p['distance_m'],p['id']),'price':lambda p:(p['price'],p['id']),'price_asc':lambda p:(p['price'],p['id']),'price_desc':lambda p:(-p['price'],p['id']),'rating':lambda p:(-p['rating'],p['id'])}.get(sort,lambda p:(-p['relevance'],p['distance_m'],p['id']))
    found.sort(key=key)
    result=found[:limit(a.get('limit'))]
    r['state']['display']['candidates']=copy.deepcopy(result)
    return {'pois':result,'count':len(result),'simulated':True}

def require_route(r,active=False):
    nav=r['state']['navigation']
    if not nav.get('destination'):
        raise ValueError('no planned navigation route')
    if active and not nav.get('active'): raise ValueError('active navigation required')
    return nav

def route(env,r,points,active=False,is_avp=False):
    if not points:
        raise ValueError('poi_list cannot be empty')
    parsed=[poi(env,r,p) for p in points]
    if active and r['state']['vehicle']['gear'] not in ('D','R'):
        raise ValueError('navigation requires driving gear D or R')
    if is_avp and not all(p.get('avp',False) for p in parsed):
        raise ValueError('POI outside distributed AVP coverage')
    previous=r['context']['location']; total=0
    for p in parsed:
        total+=distance(previous,p)*1.25;previous=p
    nav=r['state']['navigation']
    pref=nav['route_preference'];speed=16.7 if pref in ('fastest_speed','highway_priority') else 10.4
    known=next((x for x in rows(env,r,'routes') if x['from_poi']==r['context']['location'].get('id') and x['to_poi']==parsed[-1]['id'] and len(parsed)==1),None)
    nav.update(destination=parsed[-1],waypoints=parsed[:-1],active=active,planned=True,distance_m=round(known['distance_m'] if known else total),duration_s=round(known['duration_s'] if known else total/speed),simulated=True,is_avp=is_avp,traffic=known['traffic'] if known else '本地距离模型未观测实时事件',traffic_event=known.get('event') if known else None,toll=known.get('toll',0) if known else 0,route_revision=nav.get('route_revision',0)+1)
    return copy.deepcopy(nav)

@tool('search_poi_by_keyword',['pois','location'],['display.candidates'],'keyword, distance, sort and limit','matching POIs')
def search_poi_by_keyword(e,r,a): return search_pois(e,r,a)
@tool('search_nearby_poi',['pois','location'],['display.candidates'],'radius in meters','nearby POIs')
def search_nearby_poi(e,r,a): return search_pois(e,r,dict(a,radius=a.get('radius',3000)))
@tool('search_along_way_poi',['pois','navigation'],['display.candidates'],'active navigation required; route corridor filter','along-route POIs')
def search_along_way_poi(e,r,a): return search_pois(e,r,a,along=True)
@tool('search_charging_station',['pois','navigation'],['display.candidates'],'power range, brand, along/nearby, price sort','charging stations')
def search_charging_station(e,r,a): return search_pois(e,r,a,category=['charge'],along=a.get('search_type')=='along')
@tool('search_parking_charging_station',['pois','addresses'],['display.candidates'],'known location, parking/charging only','parking or charging POIs')
def search_parking_charging_station(e,r,a): return search_pois(e,r,a,category=['parking','charge'])
@tool('remote_map_search',['pois','addresses','navigation'],['display.candidates'],'location must resolve locally','POIs around location')
def remote_map_search(e,r,a): return search_pois(e,r,dict(a,radius=5000))
@tool('map_search',['pois','location'],['display.candidates'],'local keyword match','matching POIs')
def map_search(e,r,a): return search_pois(e,r,a)
@tool('plan_navi_route',['pois','routes','location'],['navigation'],'nonempty valid POIs','simulated planned route')
def plan_navi_route(e,r,a): return route(e,r,a['poi_list'],is_avp=a.get('is_avp',False))
@tool('start_navi',['pois','routes','vehicle.gear'],['navigation'],'driving gear required','active simulated navigation')
def start_navi(e,r,a): return route(e,r,a['poi_list'],active=True,is_avp=a.get('is_avp',False))
@tool('get_navi_info',['navigation'],[],'none','current navigation')
def get_navi_info(e,r,a): return copy.deepcopy(r['state']['navigation'])
@tool('exit_navigation',['navigation'],['navigation.active'],'none','inactive navigation')
def exit_navigation(e,r,a): r['state']['navigation']['active']=False;return get_navi_info(e,r,a)
@tool('add_waypoint',['navigation','pois'],['navigation'],'active navigation and valid integer insertion index required','updated route')
def add_waypoint(e,r,a):
    n=require_route(r,active=True);points=copy.deepcopy(n['waypoints']);idx=a.get('index',-1)
    if int(idx)!=idx or idx < -1 or idx > len(points): raise ValueError('invalid waypoint index')
    points.insert(len(points) if idx==-1 else int(idx),poi(e,r,a['via_poi']))
    return route(e,r,[f"{p['id']}:{p['name']}:{p['lon']},{p['lat']}" for p in points+[n['destination']]],n['active'],a.get('is_avp',False))
@tool('delete_waypoint',['navigation'],['navigation'],'active navigation required; unknown waypoint rejected','updated waypoints')
def delete_waypoint(e,r,a):
    n=require_route(r,active=True)
    if a['via_poi']=='all': new=[]
    else:
        target=poi(e,r,a['via_poi']);new=[p for p in n['waypoints'] if p['id']!=target['id']]
        if len(new)==len(n['waypoints']): raise ValueError('waypoint not found')
    return route(e,r,[f"{p['id']}:{p['name']}:{p['lon']},{p['lat']}" for p in new+[n['destination']]],n['active'])
@tool('get_all_waypoint',['navigation.waypoints'],[],'none','waypoint list')
def get_all_waypoint(e,r,a): return {'waypoints':copy.deepcopy(r['state']['navigation']['waypoints'])}
@tool('estimate_trip_info',['pois','routes','location'],[],'valid origin and destination','distance, duration, energy estimate')
def estimate_trip_info(e,r,a):
    start=poi(e,r,a['from_poi']) if a.get('from_poi') else r['context']['location'];end=poi(e,r,a['to_poi']);d=distance(start,end)*1.25
    return {'distance_m':round(d),'duration_s':round(d/10.4),'energy_kwh':round(d/1000*0.18,2),'simulated':True}
@tool('reroute',['navigation','routes'],['navigation'],'existing route required','recomputed route revision')
def reroute(e,r,a):
    n=require_route(r);return route(e,r,[f"{p['id']}:{p['name']}:{p['lon']},{p['lat']}" for p in n['waypoints']+[n['destination']]],n['active'],n.get('is_avp',False))
@tool('optimize_navi_plan',['navigation'],['navigation'],'existing route required; nearest-neighbor ordering','optimized simulated route')
def optimize_navi_plan(e,r,a):
    n=require_route(r);n['waypoints'].sort(key=lambda p:distance(r['context']['location'],p));return reroute(e,r,a)
@tool('query_traffic_ahead',['navigation','routes'],[],'active navigation required','route-data traffic and first modeled event')
def query_traffic_ahead(e,r,a):
    n=require_route(r,active=True);return {'distance_m':min(3000,n['distance_m']),'traffic':n['traffic'],'first_event':copy.deepcopy(n.get('traffic_event')),'delay_s':n.get('traffic_event',{}).get('delay_s',0) if n.get('traffic_event') else 0,'simulated':True}
@tool('share_route',['navigation'],['display.share_panel'],'existing route required','local share panel')
def share_route(e,r,a):
    n=require_route(r);r['state']['display']['share_panel']={'destination':n['destination'],'channels':['local_copy']};return copy.deepcopy(r['state']['display']['share_panel'])
@tool('set_navi_route_preference',['navigation'],['navigation.route_preference'],'canonical enum','stored preference')
def set_navi_route_preference(e,r,a): r['state']['navigation']['route_preference']=a['route_preference'];return {'route_preference':a['route_preference']}
@tool('set_navi_voice_mode',['navigation'],['navigation.voice_mode'],'canonical enum','stored voice mode')
def set_navi_voice_mode(e,r,a): r['state']['navigation']['voice_mode']=a['voice_mode'];return {'voice_mode':a['voice_mode']}
@tool('open_or_close_map',['map','navigation'],['map.open','map.page','navigation.active'],'continue requires route and driving gear','map state')
def open_or_close_map(e,r,a):
    action=a['action'];m=r['state']['map']
    if action=='continue':
        require_route(r)
        if r['state']['vehicle']['gear'] not in ('D','R'): raise ValueError('driving gear required')
        r['state']['navigation']['active']=True
    m.update(open=action!='close',page='map' if action=='return' else action);return copy.deepcopy(m)
@tool('open_special_map',['map'],['map.special'],'canonical map type','special-map state')
def open_special_map(e,r,a): r['state']['map']['special']=a['map_type'] if a['action']=='open' else None;return copy.deepcopy(r['state']['map'])
@tool('set_map_view',['map'],['map.view'],'canonical view','map state')
def set_map_view(e,r,a): r['state']['map']['view']=a['view'];return copy.deepcopy(r['state']['map'])
@tool('set_map_auto_scale',['map'],['map.auto_scale'],'boolean','map state')
def set_map_auto_scale(e,r,a): r['state']['map']['auto_scale']=a['enabled'];return copy.deepcopy(r['state']['map'])
@tool('control_map_zoom',['map'],['map.zoom'],'positive level; zoom clamped 1..20','zoom level')
def control_map_zoom(e,r,a):
    level=a.get('level',1)
    if level<=0: raise ValueError('level must be positive')
    m=r['state']['map'];action=a['action'];m['zoom']=20 if action=='zoom_in_max' else 1 if action=='zoom_out_min' else min(20,max(1,m['zoom']+(level if action=='zoom_in' else -level)));return {'zoom':m['zoom']}
@tool('overview_navi_route',['navigation','map'],['map.overview'],'existing route required','overview state')
def overview_navi_route(e,r,a): require_route(r);r['state']['map']['overview']=a['action']=='enter';return {'overview':r['state']['map']['overview']}
@tool('display_candidate_cards',['display.candidates'],['display.cards'],'candidate query must produce data','displayed candidate cards')
def display_candidate_cards(e,r,a): r['state']['display']['cards']=copy.deepcopy(r['state']['display']['candidates']);return {'cards':copy.deepcopy(r['state']['display']['cards'])}
@tool('set_display_focus',['display'],['display.focus'],'canonical screen/position; reset at explicit round boundary','focus state')
def set_display_focus(e,r,a): r['state']['display']['focus'].update(a);return copy.deepcopy(r['state']['display']['focus'])
@tool('set_favorite_poi',['pois','addresses'],['addresses'],'valid POI; replace home/company or deduplicate favorites','stored address')
def set_favorite_poi(e,r,a):
    p=poi(e,r,a['poi']);addresses=r['state']['addresses'];kind=a['address_type']
    if kind=='favorite':
        if not any(x['id']==p['id'] for x in addresses['favorites']): addresses['favorites'].append(p)
    else: addresses[kind]=p
    return {'address_type':kind,'poi':p}
@tool('get_favorite_poi',['addresses'],[],'integer limit','requested addresses')
def get_favorite_poi(e,r,a):
    kind=a['address_type'];values=r['state']['addresses']['favorites'] if kind=='favorite' else [r['state']['addresses'][kind]];return {'pois':[copy.deepcopy(p) for p in values if p][:limit(a['limit'])]}
@tool('get_home_or_company_address',['addresses'],[],'home or company only','address or explicit absence')
def get_home_or_company_address(e,r,a): return {'poi':copy.deepcopy(r['state']['addresses'][a['poi_type']]),'found':r['state']['addresses'][a['poi_type']] is not None}
@tool('delete_favorite_poi',['addresses'],['addresses.favorites'],'cannot delete home/company; known favorite required','deleted POI id')
def delete_favorite_poi(e,r,a):
    p=poi(e,r,a['poi']);arr=r['state']['addresses']['favorites'];new=[x for x in arr if x['id']!=p['id']]
    if len(new)==len(arr): raise ValueError('favorite not found')
    r['state']['addresses']['favorites']=new;return {'deleted':p['id']}
@tool('add_memory',['memory'],['memory'],'nonempty content; optional valid POI','memory entry')
def add_memory(e,r,a):
    if not a['content'].strip(): raise ValueError('memory content cannot be empty')
    item={'id':'memory-'+str(len(r['state']['memory'])+1),'content':a['content'],'created_at':r['context']['time']}
    if a.get('poi'): item['poi']=poi(e,r,a['poi'])
    r['state']['memory'].append(item);return copy.deepcopy(item)
@tool('search_memory',['memory'],[],'keyword score and integer limit','matching memory entries')
def search_memory(e,r,a):
    found=[dict(x,relevance=relevance(a['query'],x)) for x in r['state']['memory']];found=[x for x in found if x['relevance']];found.sort(key=lambda x:(-x['relevance'],x['id']));return {'memories':found[:limit(a.get('limit'))]}
@tool('music_search',['tracks'],[],'name, artist, tags; maximum 10','playable local tracks')
def music_search(e,r,a):
    found=[dict(x,relevance=relevance(a['query'],x)) for x in rows(e,r,'tracks')];found=[x for x in found if x['relevance']];found.sort(key=lambda x:(-x['relevance'],x['id']));return {'tracks':found[:limit(a.get('limit'),10,10)],'simulated':True}
@tool('play_playlist',['tracks','media'],['media'],'nonempty list; unknown or unplayable track rejected atomically','playing queue')
def play_playlist(e,r,a):
    if not a['playlist']: raise ValueError('playlist cannot be empty')
    catalog=rows(e,r,'tracks');selected=[]
    for requested in a['playlist']:
        track=next((x for x in catalog if x['name']==requested['name'] and (not requested['artist'] or x['artist']==requested['artist'])),None)
        if not track or not track['playable']: raise ValueError('track unavailable: '+requested['name'])
        selected.append(copy.deepcopy(track))
    media=r['state']['media'];media.update(playlist=selected+media['playlist'],current_track=selected[0],playing=True);return copy.deepcopy(media)

VEHICLE_ITEMS={'WINDOW_STATUS':'windows','DOOR_STATUS':'doors','INSIDE_TEMPERATURE':'inside_temperature','GEAR_STATUS':'gear','VEHICLE_SPEED':'speed','REMAINING_FUEL':'fuel','REMAINING_BATTERY':'battery','REMAINING_RANGE':'range_km'}
@tool('get_vehicle_status',['vehicle'],[],'canonical status items only','requested vehicle values')
def get_vehicle_status(e,r,a): return {key:copy.deepcopy(r['state']['vehicle'][VEHICLE_ITEMS[key]]) for key in a['query_items']}
@tool('execute_vehicle_control',['vehicle'],['vehicle'],'supported Chinese controls only; temperature 16..30; parked gear required for doors','actual simulated changed controls')
def execute_vehicle_control(e,r,a):
    q=a['query'];v=r['state']['vehicle'];changes={}
    on=any(x in q for x in ('打开','开启','启动'));off=any(x in q for x in ('关闭','关上','停止'))
    if '空调' in q or '温度' in q:
        number=re.search(r'(\d+(?:\.\d+)?)\s*(?:度|℃)',q)
        if number:
            temp=float(number[1])
            if not 16<=temp<=30: raise ValueError('temperature outside 16..30')
            v['ac_temperature']=temp;changes['ac_temperature']=temp
        if on or off: v['ac_on']=on and not off;changes['ac_on']=v['ac_on']
    if '车窗' in q or '窗户' in q:
        if not (on or off): raise ValueError('window control requires open or close')
        seats=['driver'] if '主驾' in q else ['passenger'] if '副驾' in q else list(v['windows'])
        for seat in seats: v['windows'][seat]='open' if on and not off else 'closed'
        changes['windows']=copy.deepcopy(v['windows'])
    if '车门' in q:
        if v['gear']!='P': raise ValueError('door control requires parked gear')
        if not (on or off): raise ValueError('door control requires open or close')
        for seat in v['doors']: v['doors'][seat]='open' if on and not off else 'closed'
        changes['doors']=copy.deepcopy(v['doors'])
    if '音量' in q:
        number=re.search(r'音量[^0-9]*(\d+)',q)
        if not number or not 0<=int(number[1])<=100: raise ValueError('volume requires value 0..100')
        v['volume']=int(number[1]);changes['volume']=v['volume']
    if '座椅加热' in q and (on or off): v['seat_heating']=on and not off;changes['seat_heating']=v['seat_heating']
    if not changes: raise ValueError('unsupported vehicle control command')
    return {'controls':changes,'simulated':True}
@tool('query_adas_status',['vehicle.adas'],[],'none','local ADAS state')
def query_adas_status(e,r,a): return copy.deepcopy(r['state']['vehicle']['adas'])
@tool('activate_autopilot',['vehicle.adas','vehicle.gear','vehicle.speed'],['vehicle.adas'],'available, driving gear and speed <=120','activated simulated ADAS')
def activate_autopilot(e,r,a):
    v=r['state']['vehicle']
    if not v['adas']['available'] or v['gear']!='D' or not 0<=v['speed']<=120: raise ValueError('ADAS activation preconditions not met')
    v['adas']['active']=True;return copy.deepcopy(v['adas'])
@tool('get_weather',['weather','time'],[],'date/dailysteps mutually exclusive; district match; fixed coverage','dated fixed forecast')
def get_weather(e,r,a):
    if 'date' in a and 'dailysteps' in a: raise ValueError('date and dailysteps are mutually exclusive')
    start=date.fromisoformat(a.get('date',r['context']['time'][:10]));days=limit(a.get('dailysteps'),1,10)
    dates={(start+timedelta(days=i)).isoformat() for i in range(days)}
    found=[x for x in rows(e,r,'weather') if x['district']==a['district'] and x['date'] in dates]
    if a.get('hourlysteps','all')!='all':
        for item in found: item['hourly']={a['hourlysteps']:item['hourly'][a['hourlysteps']]}
    return {'forecasts':found,'found':bool(found),'simulated':True}

def documents(e,r,a,categories):
    found=[dict(x,relevance=relevance(a.get('query',''),x)) for x in rows(e,r,'documents') if x['category'] in categories]
    found=[x for x in found if x['relevance']];found.sort(key=lambda x:(-x['relevance'],x['id']))
    return {'documents':found[:5],'found':bool(found),'source':'fixed-local-dataset','simulated':True}
@tool('search_vehicle_knowledge',['documents'],[],'manual/brand category filtering','matching fixed knowledge')
def search_vehicle_knowledge(e,r,a): return documents(e,r,a,[a['category']])
@tool('financial_information_agent',['documents'],[],'fixed finance only; no trading','matching finance records')
def financial_information_agent(e,r,a): return documents(e,r,a,['finance'])
@tool('web_search',['documents'],[],'fixed local public documents; no live data','matching fixed web records')
def web_search(e,r,a): return documents(e,r,a,['web','city_info','natural_science','brand'])
@tool('wiki_web_search',['documents'],[],'tag category filter; absent topics explicitly missing','matching fixed encyclopedia records')
def wiki_web_search(e,r,a): return documents(e,r,a,[a['tag']] if a['tag'] not in ('general_web_query','other') else ['web','city_info','natural_science','brand'])
@tool('get_joke',['documents'],[],'keyword matching fixed joke library','joke record or absence')
def get_joke(e,r,a): return documents(e,r,a,['joke'])
@tool('get_story',['documents'],[],'keyword matching fixed story library','story record or absence')
def get_story(e,r,a): return documents(e,r,a,['story'])
@tool('list_skills',['documents'],[],'local skill category only','local skill metadata')
def list_skills(e,r,a): return {'skills':[{'id':x['id'],'name':x['title']} for x in rows(e,r,'documents') if x['category']=='skill']}
@tool('get_skill_prompt',['documents'],[],'local skill category only','fixed skill prompts')
def get_skill_prompt(e,r,a): return documents(e,r,{'query':''},['skill'])
@tool('query_vehicle_exterior_vision',['visions'],[],'distributed direction-specific fixed descriptions','simulated asset description')
def query_vehicle_exterior_vision(e,r,a): return {'query':a['query'],'vision':next(x for x in rows(e,r,'visions') if x['direction']==a['direction']),'simulated':True}

def eco(e,r,a):
    found=[dict(x,relevance=relevance(a['query'],x)) for x in rows(e,r,'services') if x['category']==a['category']]
    if a.get('location'):
        c=center(e,r,a);pois={p['id']:p for p in rows(e,r,'pois')};found=[dict(x,distance_m=distance(c,pois[x['poi_id']])) for x in found]
    found=[x for x in found if x['relevance']];found.sort(key=lambda x:(-x['relevance'],x['id']));r['state']['display']['candidates']=copy.deepcopy(found)
    return {'offers':found,'simulated':True,'source':'fixed-local-dataset'}
@tool('eco_info_search',['services','pois'],['display.candidates'],'category and location filters','fixed local service offers')
def eco_info_search(e,r,a): return eco(e,r,a)
@tool('eco_info_search_1',['services','pois'],['display.candidates'],'category and location filters','fixed local service offers')
def eco_info_search_1(e,r,a): return eco(e,r,a)

def service_task(e,r,agent,query,task_id=None,history=None,async_mode=False):
    if not isinstance(query,str) or not query.strip(): raise ValueError('service task query must be nonempty')
    tasks=r['state']['service_tasks']
    if task_id:
        task=next((x for x in tasks if x['id']==task_id and x['agent']==agent),None)
        if task is None: raise ValueError('service task not found')
    else:
        task={'id':'service-task-'+str(len(tasks)+1),'context_id':'local-context-'+str(len(tasks)+1),'agent':agent,'interactions':[],'status':'pending','simulated':True};tasks.append(task)
    task['interactions'].append({'role':'user','content':query})
    if any(x in query for x in ('取消','cancel')): task['status']='cancelled';answer='本地服务任务已取消。'
    elif async_mode: task['status']='pending';answer='本地服务任务已登记，等待后续本地查询。'
    else:
        task['status']='completed_local'
        categories=['manual'] if agent=='after_sale_agent' else ['web','city_info']
        task['result']=documents(e,r,{'query':query},categories)
        task['contacts']=[x for x in rows(e,r,'contacts') if relevance(query,x)]
        task['offers']=[x for x in rows(e,r,'services') if relevance(query,x)]
        answer='本地服务任务已计算。外部预订、购买、维修与救援没有提交。'
    task['interactions'].append({'role':'assistant','content':answer})
    if history is not None: task['provided_history']=copy.deepcopy(history)
    return copy.deepcopy(task)
@tool('a2a_invoke',['documents','services','contacts','service_tasks'],['service_tasks'],'known agent; existing task validates ownership','local task and full interactions','No additional LLM, GUI automation, remote ordering or real reservation. Async tasks remain pending until queried.')
def a2a_invoke(e,r,a):
    if a.get('context_id') and a.get('task_id'):
        t=next((t for t in r['state']['service_tasks'] if t['id']==a['task_id']),None)
        if t is None or t['context_id']!=a['context_id']: raise ValueError('task context mismatch')
    return service_task(e,r,a['agent_identifier'],a['task'],a.get('task_id'),async_mode=a.get('async_mode',False))
@tool('after_sale_agent',['documents','contacts','service_tasks'],['service_tasks'],'nonempty query','local after-sale consultation with interactions')
def after_sale_agent(e,r,a): return service_task(e,r,'after_sale_agent',a.get('intermediate_query') or a['query'],history=a.get('historys'))
@tool('digital_service_agent',['services','service_tasks'],['service_tasks'],'query or intermediate_query required','local digital-service task')
def digital_service_agent(e,r,a): return service_task(e,r,'digital_service_agent',a.get('intermediate_query') or a.get('query'),history=a['historys'])
@tool('multi_task_agent',['services','pois','documents','service_tasks'],['service_tasks','display.candidates'],'nonempty task; fixed local candidates only','local multi-step task result')
def multi_task_agent(e,r,a):
    result=service_task(e,r,'multi_task_agent',a['query']);r['state']['display']['candidates']=copy.deepcopy(result.get('offers',[]));return result
@tool('scheduled_task',['schedules'],['schedules'],'each condition uses one trigger family; typed action validation','pending local schedule; no external scheduler')
def scheduled_task(e,r,a):
    if not a['title'].strip() or not a['steps']: raise ValueError('schedule title and steps must be nonempty')
    for step in a['steps']:
        if not isinstance(step,dict) or set(step)!={'condition','actions'}: raise ValueError('step requires condition and actions only')
        c=step['condition'];actions=step['actions']
        if not isinstance(c,dict) or not isinstance(actions,list) or not actions: raise ValueError('invalid schedule step')
        families=[bool(set(c)&{'event_type','event_position'}),bool(set(c)&{'poi','distance'}),bool(set(c)&{'datetime','duration'})]
        if sum(families)!=1 or set(c)-{'event_type','event_position','poi','distance','datetime','duration'}: raise ValueError('condition must use one trigger family')
        if families[0] and (set(c)!={'event_type','event_position'} or c['event_type'] not in ('board','exit') or c['event_position'] not in ('driver','copilot','rear_left','rear_right','any')): raise ValueError('invalid event condition')
        if families[1]:
            if set(c)!={'poi','distance'} or c['distance'] not in ('arrive','near','depart'): raise ValueError('invalid distance condition')
            poi(e,r,c['poi'])
        if families[2]:
            if 'duration' in c and not re.fullmatch(r'P(?=.+)(?:\d+D)?(?:T(?=.+)(?:\d+H)?(?:\d+M)?(?:\d+S)?)?',c['duration']): raise ValueError('invalid ISO duration')
            if 'datetime' in c and not re.fullmatch(r'(?:\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2})?|\d{2}:\d{2})',c['datetime']): raise ValueError('invalid ISO datetime')
            if len(c)==2 and not re.fullmatch(r'\d{2}:\d{2}',c['datetime']): raise ValueError('datetime with duration must be HH:MM')
        for action in actions:
            if not isinstance(action,dict): raise ValueError('invalid schedule action')
            if action.get('type')=='navigate':
                if set(action)!={'type','poi'}: raise ValueError('navigate action requires poi')
                poi(e,r,action['poi'])
            elif action.get('type') in ('remind','announce'):
                if set(action)!={'type','content'} or not isinstance(action['content'],str) or not action['content']: raise ValueError('action content required')
                if action['type']=='announce' and len(action['content'])>20: raise ValueError('announce maximum 20 characters')
            else: raise ValueError('unknown schedule action')
    task=dict(copy.deepcopy(a),id='schedule-'+str(len(r['state']['schedules'])+1),status='pending',next_step=0,simulated=True);r['state']['schedules'].append(task);return task
@tool('end_chat',['chat'],['chat.ended'],'none','ended local chat')
def end_chat(e,r,a): r['state']['chat']['ended']=True;return {'ended':True}

def confined_open(e,r,path,directory=False):
    import os
    root=e.workspace_root/r['session_id']
    if e.workspace_root.is_symlink() or e.workspace_root.resolve()!=e.workspace_root or root.is_symlink():
        raise ValueError('symlinks are not permitted in task roots')
    root.mkdir(parents=True,exist_ok=True)
    p=pathlib.Path(path)
    if p.is_absolute() or '..' in p.parts: raise ValueError('path escape rejected')
    flags=os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    fd=os.open('/',flags)
    try:
        # Open every ancestor relative to a pinned directory descriptor.
        for part in root.parts[1:]+p.parts[:-1]:
            child=os.open(part,flags,dir_fd=fd);os.close(fd);fd=child
        leaf=p.parts[-1] if p.parts else '.'
        target=os.open(leaf,flags if directory else os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,dir_fd=fd)
        return target
    finally:
        os.close(fd)
@tool('list_directory',['session workspace'],[],'relative confined path, symlink escapes rejected, maximum 200 entries','directory entry names','Filesystem tool disabled by default; task workspace only.')
def list_directory(e,r,a):
    import os
    fd=confined_open(e,r,a['path'],directory=True)
    try:
        entries=sorted(os.listdir(fd));return {'entries':entries[:200],'truncated':len(entries)>200}
    finally: os.close(fd)
@tool('read_file',['session workspace'],[],'relative confined UTF-8 file; maximum 64KiB','file content','Filesystem tool disabled by default; task workspace only.')
def read_file(e,r,a):
    import os,stat
    fd=confined_open(e,r,a['path'])
    with os.fdopen(fd,'rb') as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode): raise ValueError('regular file required')
        raw=f.read(65537)
    if len(raw)>65536: raise ValueError('file exceeds 64KiB')
    return {'content':raw.decode('utf-8')}
