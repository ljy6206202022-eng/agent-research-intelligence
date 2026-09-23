"""audio audio-only bounded streaming. No cookies, arbitrary URLs, or FFmpeg network."""
import hashlib
from http.client import HTTPException
import os
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urljoin

from agent_research_intelligence.connectors.public_http import AcquisitionError
from agent_research_intelligence.connectors.youtube_proxy import YouTubeProxyHTTP, YOUTUBE_HOSTS
from agent_research_intelligence.governance.paths import BoundaryError


def media_host(host):
    return bool(host) and host.endswith('.googlevideo.com') and host != '.googlevideo.com'


class AudioHTTP(YouTubeProxyHTTP):
    @staticmethod
    def _proxy_host(host):
        return host in YOUTUBE_HOSTS or media_host(host)


def select_audio(player, *, max_bytes=256 * 1024**2):
    """Only original/default audio, never an automatically dubbed translation."""
    choices = []
    for item in player.get('streamingData', {}).get('adaptiveFormats', []):
        mime = item.get('mimeType', '')
        track = item.get('audioTrack', {})
        if not mime.startswith(('audio/mp4;', 'audio/webm;')) or item.get('drmFamilies'):
            continue
        if track and not track.get('audioIsDefault'):
            continue
        try:
            size = int(item.get('contentLength', 0))
        except (ValueError, TypeError):
            continue
        if not 0 < size <= max_bytes or not isinstance(item.get('url'), str):
            continue
        # Prefer AAC 128k for deterministic container timestamps, then Opus.
        choices.append((item.get('itag') != 140, item.get('itag') != 251, -int(item.get('bitrate', 0)), item))
    if not choices:
        raise AcquisitionError('PUBLIC_ORIGINAL_AUDIO_UNAVAILABLE')
    item = sorted(choices, key=lambda x: x[:3])[0][3]
    return item


def _audio_range(http, url, start, stop):
    """YouTube's public range query; bounded reads avoid stalled unbounded streams."""
    from urllib.parse import parse_qs
    base = urlsplit(url)
    if 'range' in parse_qs(base.query):
        raise BoundaryError('Provider URL already contains a range')
    url += ('&' if base.query else '?') + f'range={start}-{stop}'
    expected = stop-start+1
    for hop in range(4):
        parsed = urlsplit(url)
        if not media_host(parsed.hostname) or parsed.path != '/videoplayback':
            raise BoundaryError('Only YouTube audio CDN paths may be downloaded')
        parsed,host,address = http._destination(url)
        conn = http.connection_factory(host,address,http.timeout)
        row = {'host':host,'path':parsed.path,'address':address,'hop':hop,
               'at':datetime.now(timezone.utc).isoformat(), 'method':'GET',
               'provider':'DirectMediaProvider', 'proxy_enabled':True,
               'range_semantics':'QUERY_PARAMETER_NOT_HTTP_RANGE',
               'request_headers':{'Accept-Encoding':'identity','Connection':'close',
                                  'User-Agent':'com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip'},
               'phase':'AUDIO_REQUEST','range_start':start,'range_end':stop}
        began = time.monotonic()
        try:
            conn.request('GET',parsed.path+'?'+parsed.query,headers={
                'Accept-Encoding':'identity',
                'User-Agent':'com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip',
                'Connection':'close'})
            row['phase']='AUDIO_RESPONSE_HEADERS'
            response=conn.getresponse();row['status']=response.status
            row['response_headers']={key:response.getheader(key) for key in
                                     ('Content-Range','Content-Length','Content-Type','Content-Encoding','Date')}
            if response.status in (301,302,303,307,308):
                location=response.getheader('Location')
                if not location: raise AcquisitionError('MEDIA_REDIRECT_INVALID')
                url=urljoin(url,location)
                row['redirect_target_host']=urlsplit(url).hostname
                continue
            if response.status!=200:
                raise AcquisitionError('AUDIO_ACCESS_DENIED' if response.status in (401,403) else 'AUDIO_HTTP_'+str(response.status))
            if response.getheader('Content-Encoding','identity') not in ('','identity'):
                raise AcquisitionError('MEDIA_ENCODING_DENIED')
            length=response.getheader('Content-Length')
            if length is not None and int(length)!=expected:
                raise AcquisitionError('AUDIO_LENGTH_MISMATCH')
            row['phase']='AUDIO_RESPONSE_BODY'
            body=response.read(expected+1);row['bytes']=len(body)
            if len(body)>expected: raise AcquisitionError('MEDIA_SIZE_EXCEEDED')
            if len(body)!=expected: raise AcquisitionError('AUDIO_TRUNCATED')
            return body
        except (OSError,HTTPException) as exc:
            row['error']=type(exc).__name__
            if row['phase']=='AUDIO_REQUEST': row['phase']=getattr(conn,'phase',row['phase'])
            raise AcquisitionError('AUDIO_TRANSPORT_FAILED',phase=row['phase']) from exc
        finally:
            conn.close();row['elapsed_s']=round(time.monotonic()-began,3)
            row['finished_at']=datetime.now(timezone.utc).isoformat();http._record(row)
    raise AcquisitionError('MEDIA_REDIRECT_LIMIT')


def download_audio(http, workspace, url, relative, *, expected_size, check_budget=lambda: None):
    """A complete audio-only file, assembled in order and removed on ANY failure.

    Chunk order and byte count are checked; the file hash is observed, not an
    upstream integrity attestation. Signed URLs are never persisted in logs.
    """
    if not relative.startswith('tmp/media/') or not relative.endswith('/source.audio'):
        raise BoundaryError('Audio destination must be an owned job temporary file')
    if type(expected_size) is not int or not 0 < expected_size <= 256*1024**2:
        raise BoundaryError('Audio size exceeds approved per-job bound')
    started=time.monotonic();digest=hashlib.sha256();size=0
    with workspace.parent_fd(relative,create=True) as (parent,name):
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        try:
            with os.fdopen(fd,'wb') as target:
                while size<expected_size:
                    check_budget()
                    if time.monotonic()-started>600: raise AcquisitionError('MEDIA_DEADLINE')
                    stop=min(expected_size-1,size+1024**2-1)
                    try:
                        chunk=_audio_range(http,url,size,stop)
                    except AcquisitionError as exc:
                        if exc.code != 'AUDIO_TRANSPORT_FAILED': raise
                        # One retry of the same public range, never another account,
                        # format, credential, or an ACCESS_DENIED response.
                        check_budget()
                        if time.monotonic()-started>570: raise
                        chunk=_audio_range(http,url,size,stop)
                    target.write(chunk);digest.update(chunk);size+=len(chunk)
                target.flush();os.fsync(target.fileno())
        except BaseException:
            os.unlink(name,dir_fd=parent);raise
    return {'bytes':size,'sha256':digest.hexdigest(),'host':urlsplit(url).hostname,
            'retention':'TEMPORARY_DELETE_AFTER_PROCESSING','original_audio':True,
            'acquisition':'ORDERED_1_MIB_PUBLIC_BYTE_RANGES','complete_audio':True}
