"""查询并直接使用返回数值回答。"""

def solve(ctx):
    status = ctx.call('get_vehicle_status', {'query_items': ['REMAINING_BATTERY', 'REMAINING_RANGE']})
    battery = status['REMAINING_BATTERY']
    distance = status['REMAINING_RANGE']
    assert isinstance(battery, (int, float)) and isinstance(distance, (int, float))
    ctx.say(f'剩余电量{battery:g}%，剩余续航{distance:g}公里。')
