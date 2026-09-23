"""audio qualification. A failed caption request can never prove absence."""
import hashlib
import json
import re
from datetime import datetime, timezone

from agent_research_intelligence.acquisition.youtube import player_response, video_id
from agent_research_intelligence.connectors.public_http import AcquisitionError


def inventory(data, identifier):
    if not isinstance(data, dict) or data.get('playabilityStatus', {}).get('status') != 'OK':
        raise AcquisitionError('CAPTION_INVENTORY_ACCESS_UNKNOWN')
    if data.get('videoDetails', {}).get('videoId') != identifier:
        raise AcquisitionError('CAPTION_INVENTORY_VIDEO_MISMATCH')
    captions = data.get('captions', {})
    if not isinstance(captions, dict):
        raise AcquisitionError('CAPTION_INVENTORY_INVALID')
    renderer = captions.get('playerCaptionsTracklistRenderer', {})
    if not isinstance(renderer, dict):
        raise AcquisitionError('CAPTION_INVENTORY_INVALID')
    tracks = renderer.get('captionTracks', [])
    if not isinstance(tracks, list):
        raise AcquisitionError('CAPTION_INVENTORY_INVALID')
    result = []
    for track in tracks:
        if not isinstance(track, dict) or not all(isinstance(track.get(k), str) and track[k] for k in ('languageCode', 'baseUrl')):
            raise AcquisitionError('CAPTION_INVENTORY_INVALID')
        result.append({'language': track['languageCode'],
                       'kind': 'automatic' if track.get('kind') == 'asr' else 'manual'})
    return result


def inspect_video(http, url):
    """Inspect all languages from TWO matching public players, not a language filter.

    Returns a sanitized durable receipt and ephemeral player data (signed audio
    URLs are never included in the receipt). Failure must be persisted by caller
    as UNKNOWN. Captionless means zero advertised manual AND automatic tracks;
    inaccessible tracks do not qualify under this conservative rule.
    """
    identifier = video_id(url)
    page = http.get(f'https://www.youtube.com/watch?v={identifier}')
    text = page.body.decode('utf-8', errors='replace')
    web = player_response(text)
    web_tracks = inventory(web, identifier)
    key = re.search(r'"INNERTUBE_API_KEY":\s*"([A-Za-z0-9_-]{1,200})"', text)
    if not key:
        raise AcquisitionError('CAPTION_INVENTORY_SECOND_PROVIDER_UNAVAILABLE')
    response = http.read_youtube_player(identifier, key.group(1))
    try:
        android = json.loads(response.body)
    except (ValueError, TypeError) as exc:
        raise AcquisitionError('CAPTION_INVENTORY_INVALID') from exc
    android_tracks = inventory(android, identifier)
    details = web['videoDetails']
    if details.get('isLive') or web.get('playabilityStatus', {}).get('liveStreamability'):
        raise AcquisitionError('ACTIVE_LIVE_SAMPLE_DENIED')
    receipt = {
        'at': datetime.now(timezone.utc).isoformat(), 'video_id': identifier,
        'url': f'https://www.youtube.com/watch?v={identifier}',
        'title': details.get('title'), 'creator': details.get('author'),
        'channel_id': details.get('channelId'), 'duration_s': details.get('lengthSeconds'),
        'description': details.get('shortDescription'),
        'status': 'CAPTIONS_PRESENT' if web_tracks or android_tracks else 'NO_USABLE_CAPTION',
        'basis': 'ALL_LANGUAGES_ZERO_ADVERTISED_MANUAL_AND_AUTOMATIC_TRACKS_ON_BOTH_PLAYERS',
        'providers': [
            {'name': 'web', 'sha256': hashlib.sha256(page.body).hexdigest(), 'tracks': web_tracks},
            {'name': 'public_android', 'sha256': hashlib.sha256(response.body).hexdigest(), 'tracks': android_tracks},
        ],
        'authority': 'EXTERNAL_EVIDENCE',
    }
    return receipt, android
