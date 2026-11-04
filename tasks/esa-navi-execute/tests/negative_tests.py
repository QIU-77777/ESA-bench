"""每个错误解正常执行，由完整评分拒绝。"""

def wrong_destination(ctx):
    result = ctx.call('get_home_or_company_address', {'poi_type': 'home'})
    navigate(ctx, result['poi'])


def extra_vehicle_change(ctx):
    solve(ctx)
    ctx.call('execute_vehicle_control', {'query': '把音量调到50'})


CASES = {'wrong_destination': wrong_destination, 'extra_vehicle_change': extra_vehicle_change}
