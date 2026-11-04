"""每个错误解正常执行，由完整评分拒绝。"""


def wrong_destination(ctx):
    result = ctx.call('get_favorite_poi', {'address_type': 'home', 'limit': 10})
    assert len(result['pois']) == 1
    target = result['pois'][0]
    navigation = ctx.call('start_navi', {'poi_list': [f"{target['id']}:{target['name']}:{target['lon']},{target['lat']}"]})
    ctx.say(f"已开始导航前往{target['name']}，距离{navigation['distance_m'] / 1000:.1f}公里，预计{navigation['duration_s'] / 60:.0f}分钟。")


def skip_query(ctx):
    ctx.call('start_navi', {'poi_list': ['200001:公司:121.42,31.19']})
    ctx.say('已开始导航前往公司。')


CASES = {'wrong_destination': wrong_destination, 'skip_query': skip_query}
