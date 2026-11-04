"""跳过澄清与额外修改均须被评分拒绝。"""

def control_without_clarification(ctx):
    ctx.call('execute_vehicle_control', {'query': '把空调温度调到22度'})
    ctx.say('已把空调温度调到22度。')


def extra_volume_change(ctx):
    solve(ctx)
    ctx.call('execute_vehicle_control', {'query': '把音量调到50'})


CASES = {'control_without_clarification': control_without_clarification, 'extra_volume_change': extra_volume_change}
