"""依据模型可见工具目录确认执行能力缺失。"""

def solve(ctx):
    assert set(ctx.tools) == {'get_vehicle_status'}
    ctx.say('当前工具只能查询车辆状态，没有执行空调控制的能力，无法把空调温度调到22度。')


def with_status_query(ctx):
    assert set(ctx.tools) == {'get_vehicle_status'}
    ctx.call('get_vehicle_status', {'query_items': ['INSIDE_TEMPERATURE']})
    ctx.say('当前只能查询车辆状态，缺少执行空调控制的工具，不能调整空调温度。')


ALTERNATIVES = {'read_status_then_acknowledge': with_status_query}
