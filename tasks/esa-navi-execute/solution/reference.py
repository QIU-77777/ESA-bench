"""通过实际地址查询结果构造导航参数。"""

def navigate(ctx, target):
    point = f"{target['id']}:{target['name']}:{target['lon']},{target['lat']}"
    result = ctx.call('start_navi', {'poi_list': [point]})
    assert result['active'] is True
    ctx.say(f"已开始导航前往{target['name']}，距离{result['distance_m'] / 1000:.1f}公里，预计{result['duration_s'] / 60:.0f}分钟。")


def solve(ctx):
    assert 'get_home_or_company_address' in ctx.tools
    result = ctx.call('get_home_or_company_address', {'poi_type': 'company'})
    assert result['found'] and result['poi']
    navigate(ctx, result['poi'])


def favorite_query(ctx):
    result = ctx.call('get_favorite_poi', {'address_type': 'company', 'limit': 10})
    assert len(result['pois']) == 1
    navigate(ctx, result['pois'][0])


ALTERNATIVES = {'favorite_address_query': favorite_query}
