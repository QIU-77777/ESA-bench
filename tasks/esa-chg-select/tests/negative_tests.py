"""错误解只使用实际候选，不构造不存在的实体。"""

def farther_station(ctx):
    navigate(ctx, candidates(ctx)[-1])


def extra_vehicle_change(ctx):
    solve(ctx)
    ctx.call('execute_vehicle_control', {'query': '把音量调到50'})


CASES = {'farther_station': farther_station, 'extra_vehicle_change': extra_vehicle_change}
