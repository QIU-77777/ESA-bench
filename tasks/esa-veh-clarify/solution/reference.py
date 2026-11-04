"""从真实澄清回答解析温度。"""
import re


def solve(ctx):
    answer = ctx.ask('空调温度需要设置为多少度？')
    match = re.search(r'(\d+(?:\.\d+)?)\s*(?:度|℃)', answer)
    assert match is not None
    temperature = match.group(1)
    result = ctx.call('execute_vehicle_control', {'query': f'把空调温度调到{temperature}度'})
    assert result['controls']['ac_temperature'] == float(temperature)
    ctx.say(f"已把空调温度调到{result['controls']['ac_temperature']:g}度。")
