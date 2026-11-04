"""从真实澄清回答解析温度。"""
import re


def solve(ctx):
    answer = ctx.ask('空调温度需要设置为多少度？')
    match = re.search(r'(\d+(?:\.\d+)?)\s*(?:度|℃)', answer)
    assert match is not None
    temperature = float(match.group(1))
    result = ctx.call('set_ac_temperature', {'temperature': temperature})
    assert result['ac_temperature'] == temperature
    ctx.say(f"已把空调温度调到{result['ac_temperature']:g}度。")


def query_state_first(ctx):
    state = ctx.call('get_vehicle_state', {})
    assert isinstance(state.get('vehicle'), dict)
    solve(ctx)


ALTERNATIVES = {'query_state_first': query_state_first}
