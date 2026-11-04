"""丢失响应后谎报与新ID重复播放的错误答案。"""

def claim_playing_without_recovery(ctx):
    ctx.call('music_search', {'query': '周杰伦 晴天', 'limit': 10}, expect='response_lost', call_id='music-query-lost')
    ctx.say('已开始播放周杰伦的《晴天》原版。')


def duplicate_playback(ctx):
    args = search_after_response_loss(ctx)
    result = ctx.call('play_playlist', args, call_id='play-first')
    ctx.call('play_playlist', args, call_id='play-second')
    ctx.say(f"已开始播放{result['current_track']['artist']}的《{result['current_track']['name']}》原版。")


CASES = {'claim_playing_without_recovery': claim_playing_without_recovery, 'duplicate_playback': duplicate_playback}
