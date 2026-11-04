"""按实际候选的功率、可用数量和距离选择充电站。"""

def candidates(ctx):
    result = ctx.call('search_charging_station', {'query': '充电站', 'search_type': 'nearby', 'charging_type': 'default', 'sort_type': 'distance', 'limit': 10})
    usable = [point for point in result['pois'] if point['power_kw'] >= 30 and point['available'] > 0]
    assert len(usable) >= 2
    return sorted(usable, key=lambda point: (point['distance_m'], point['id']))


def navigate(ctx, point):
    result = ctx.call('start_navi', {'poi_list': [f"{point['id']}:{point['name']}:{point['lon']},{point['lat']}"]})
    assert result['active'] is True
    ctx.say(f"选择{point['name']}，它是查询结果中距离最近的可用快充站，已开始导航。")


def solve(ctx):
    navigate(ctx, candidates(ctx)[0])
