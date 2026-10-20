"""Explicit normalization of legacy fixture state; no grading data is accepted."""
import copy

def point(p):
    if p is None or not isinstance(p,dict): return p
    p=copy.deepcopy(p)
    for old,new in [('longitude','lon'),('latitude','lat'),('poi_id','id'),('location_name','name')]:
        if old in p: p[new]=p.pop(old)
    return p

def normalize(patch):
    patch=copy.deepcopy(patch)
    if 'vehicle' in patch:
        v=patch['vehicle'];seats={'front_left':'driver','front_right':'passenger','rear_left':'rear_left','rear_right':'rear_right','tailgate':'tailgate'}
        for old,new in [('remaining_battery','battery'),('remaining_range','range_km'),('remaining_fuel','fuel')]:
            if old in v: v[new]=v.pop(old)
        for old,new in [('window','windows'),('door','doors')]:
            if old in v: v[new]={seats[k]:'open' if value else 'closed' for k,value in v.pop(old).items()}
    if isinstance(patch.get('media'),dict) and 'volume' in patch['media']:
        volume=patch['media'].pop('volume')
        vehicle=patch.setdefault('vehicle',{})
        if 'volume' in vehicle and vehicle['volume']!=volume: raise ValueError('conflicting legacy volume fields')
        vehicle['volume']=volume
    if 'navigation' in patch:
        n=patch['navigation']
        for old,new,factor in [('total_distance_km','distance_m',1000),('total_time_min','duration_s',60),('tolls_yuan','toll',1)]:
            if old in n: n[new]=n.pop(old)*factor
        if 'destination' in n: n['destination']=point(n['destination'])
        if 'waypoints' in n: n['waypoints']=[point(p) for p in n['waypoints']]
    if 'user_addresses' in patch:
        values=patch.pop('user_addresses');patch['addresses']={k:([point(x) for x in v] if k=='favorite' else point(v)) for k,v in values.items()};
        if 'favorite' in patch['addresses']: patch['addresses']['favorites']=patch['addresses'].pop('favorite')
    if 'addresses' in patch:
        for k,v in patch['addresses'].items(): patch['addresses'][k]=[point(p) for p in v] if k=='favorites' else point(v)
    if 'memories' in patch: patch['memory']=patch.pop('memories')
    if 'memory' in patch:
        for i,item in enumerate(patch['memory']):
            item.setdefault('id','memory-'+str(i+1))
            if 'poi' in item: item['poi']=point(item['poi'])
    if 'navi_history' in patch:
        history=patch.pop('navi_history')
        if 'memory' not in patch: patch['memory']=[{'id':'history-'+str(i+1),'content':'历史导航：'+p.get('name',''),'poi':point(p)} for i,p in enumerate(history)]
    return patch
