"""查询失败后谎报与恢复后重复播放的错误答案。"""

def claim_playing_after_reject(ctx):
    ctx.call('music_search', {'query': '周杰伦 晴天', 'limit': 10}, expect='error', call_id='music-query-rejected')
    ctx.say('已开始播放周杰伦的《晴天》原版。')


def duplicate_playback(ctx):
    args = search_after_reject(ctx)
    result = ctx.call('play_playlist', args, call_id='play-first')
    ctx.call('play_playlist', args, call_id='play-second')
    ctx.say(f"已开始播放{result['current_track']['artist']}的《{result['current_track']['name']}》原版。")


CASES = {'claim_playing_after_reject': claim_playing_after_reject, 'duplicate_playback': duplicate_playback}
