"""一次拒绝后重新查询，播放实际返回的原版歌曲。"""

def search_after_reject(ctx):
    args = {'query': '周杰伦 晴天', 'limit': 10}
    rejected = ctx.call('music_search', args, expect='error', call_id='music-query-rejected')
    assert 'error' in rejected
    result = ctx.call('music_search', args, call_id='music-query-retry')
    tracks = [track for track in result['tracks'] if track['name'] == '晴天' and track['artist'] == '周杰伦' and track.get('version') == 'original']
    assert len(tracks) == 1
    track = tracks[0]
    return {'playlist': [{'name': track['name'], 'artist': track['artist']}]}


def solve(ctx):
    args = search_after_reject(ctx)
    result = ctx.call('play_playlist', args, call_id='play-confirmed')
    assert result['playing'] is True
    track = result['current_track']
    ctx.say(f"已开始播放{track['artist']}的《{track['name']}》原版。")
