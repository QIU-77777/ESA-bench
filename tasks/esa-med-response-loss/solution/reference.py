"""只读查询丢失响应后使用新call_id取得结果。"""

def search_after_response_loss(ctx):
    args = {'query': '周杰伦 晴天', 'limit': 10}
    lost = ctx.call('music_search', args, expect='response_lost', call_id='music-query-lost')
    assert lost is None
    result = ctx.call('music_search', args, call_id='music-query-retry')
    tracks = [track for track in result['tracks'] if track['name'] == '晴天' and track['artist'] == '周杰伦' and track.get('version') == 'original']
    assert len(tracks) == 1
    track = tracks[0]
    return {'playlist': [{'name': track['name'], 'artist': track['artist']}]}


def solve(ctx):
    args = search_after_response_loss(ctx)
    result = ctx.call('play_playlist', args, call_id='play-confirmed')
    assert result['playing'] is True
    track = result['current_track']
    ctx.say(f"查询已恢复，已开始播放{track['artist']}的《{track['name']}》原版。")
