"""每个错误解正常执行，由完整评分拒绝。"""
import re


def temperature_of(text):
    match = re.search(r'(\d+(?:\.\d+)?)\s*(?:度|℃)', text)
    assert match is not None
    return float(match.group(1))


def control_without_asking(ctx):
    # 用户尚未确认温度就直接控制，触发确认前禁止控制的规则。
    result = ctx.call('set_ac_temperature', {'temperature': 22})
    ctx.say(f"已把空调温度调到{result['ac_temperature']:g}度。")


def wrong_temperature(ctx):
    answer = ctx.ask('空调温度需要设置为多少度？')
    result = ctx.call('set_ac_temperature', {'temperature': temperature_of(answer) + 4})
    ctx.say(f"已把空调温度调到{result['ac_temperature']:g}度。")


CASES = {'control_without_asking': control_without_asking, 'wrong_temperature': wrong_temperature}
