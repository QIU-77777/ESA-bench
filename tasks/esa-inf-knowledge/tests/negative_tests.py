"""数值编造与额外控制的错误答案测试。"""

def fabricated_values(ctx):
    status = ctx.call('get_vehicle_status', {'query_items': ['REMAINING_BATTERY', 'REMAINING_RANGE']})
    ctx.say(f"剩余电量{status['REMAINING_BATTERY'] + 10:g}%，剩余续航{status['REMAINING_RANGE'] + 100:g}公里。")


def extra_vehicle_change(ctx):
    solve(ctx)
    ctx.call('execute_vehicle_control', {'query': '把音量调到50'})


CASES = {'fabricated_values': fabricated_values, 'extra_vehicle_change': extra_vehicle_change}
