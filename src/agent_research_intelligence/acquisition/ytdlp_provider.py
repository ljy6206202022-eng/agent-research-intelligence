"""Pinned yt-dlp adapter for the explicitly approved audio sample only.

Library API, no CLI configuration loading, plugins, cookies,
external downloaders, browser profiles, credentials or remote components.
Only the hash-pinned local Node/EJS pair can solve public player challenges.
All library requests use this checked transport; native worker networking is
additionally confined to the exact approved proxy TCP endpoint.
"""
from dataclasses import asdict
import hashlib
import io
import json
import re
import time
import subprocess
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .media_provider import APPROVED_YTDLP_SAMPLES, AudioArtifact, now
from .media_http import AudioHTTP, media_host
from agent_research_intelligence.connectors.youtube_proxy import APPROVED_PROXY, YOUTUBE_HOSTS
from agent_research_intelligence.connectors.public_http import AcquisitionError
from agent_research_intelligence.governance.paths import BoundaryError

MAX_AUDIO = 256*1024**2
SAFE_RESPONSE = ('Content-Range','Content-Length','Content-Type','Content-Encoding','Date','Accept-Ranges')
SEND_HEADERS = frozenset({'user-agent','accept','accept-language','range','content-type',
                         'origin','referer','x-youtube-client-name','x-youtube-client-version','x-goog-visitor-id'})


def validate_request(url,method,data,video_id):
    parsed=urlsplit(url)
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443):
        raise BoundaryError('YTDLP_PUBLIC_HTTPS_ONLY')
    host=parsed.hostname
    if not (host in YOUTUBE_HOSTS or media_host(host)):
        raise BoundaryError('YTDLP_HOST_NOT_APPROVED')
    if method=='POST':
        if host not in YOUTUBE_HOSTS or parsed.path!='/youtubei/v1/player' or not isinstance(data,bytes):
            raise BoundaryError('YTDLP_POST_NOT_PLAYER_READ')
        if len(data)>128*1024 or json.loads(data).get('videoId')!=video_id:
            raise BoundaryError('YTDLP_PLAYER_VIDEO_MISMATCH')
    elif method not in ('GET','HEAD'):
        raise BoundaryError('YTDLP_METHOD_DENIED')
    if media_host(host) and not (parsed.path=='/videoplayback' or parsed.path.startswith(
            ('/videoplayback/','/api/manifest/hls_variant/','/api/manifest/hls_playlist/'))):
        raise BoundaryError('YTDLP_MEDIA_PATH_DENIED')
    if host in YOUTUBE_HOSTS and method in ('GET','HEAD'):
        if parsed.path not in ('','/','/watch','/iframe_api') and not parsed.path.startswith('/s/player/'):
            raise BoundaryError('YTDLP_METADATA_PATH_DENIED')
    return parsed


class _Body(io.RawIOBase):
    def __init__(self,response,conn,row,limit,transport):
        self.response,self.conn,self.row,self.limit,self.transport=response,conn,row,limit,transport
        self.count=0
    def readable(self): return True
    def read(self,amt=None):
        self.transport.check()
        maximum=self.limit-self.count+1
        if amt is None or amt<0: amt=maximum
        data=self.response.read(min(amt,maximum))
        self.count+=len(data);self.transport.total_bytes+=len(data);self.row['bytes_read']=self.count
        if self.count>self.limit or self.transport.total_bytes>300*1024**2:
            self.close();raise AcquisitionError('YTDLP_TRANSFER_BUDGET')
        return data
    def close(self):
        if not self.closed:
            self.response.close();self.conn.close();self.row['body_closed_at']=now()
        super().close()


class CheckedTransport:
    def __init__(self,video_id,check_budget=lambda:None):
        self.http=AudioHTTP(APPROVED_PROXY,timeout=25)
        self.video_id=video_id;self.check_budget=check_budget
        self.rows=[];self.started=time.monotonic();self.total_bytes=0
        self.url_observed_at=None;self.addresses={}
    def check(self):
        self.check_budget()
        if time.monotonic()-self.started>1100: raise AcquisitionError('YTDLP_DEADLINE')
        if len(self.rows)>=1024: raise AcquisitionError('YTDLP_REQUEST_LIMIT')
    def open(self,request):
        from yt_dlp.networking.common import Response
        from yt_dlp.networking.exceptions import HTTPError, TransportError
        url=request if isinstance(request,str) else request.url
        method='GET' if isinstance(request,str) else request.method
        data=None if isinstance(request,str) else request.data
        original_headers={} if isinstance(request,str) else dict(request.headers)
        headers={k:v for k,v in original_headers.items() if k.lower() in SEND_HEADERS}
        headers.update({'Accept-Encoding':'identity','Connection':'close'})
        for key in ('Range','range'):
            if key in headers and not re.fullmatch(r'bytes=\d*-\d*',headers[key]):
                raise BoundaryError('YTDLP_INVALID_RANGE')
        for hop in range(5):
            self.check();parsed=validate_request(url,method,data,self.video_id)
            host=parsed.hostname
            if host not in self.addresses:
                _,_,self.addresses[host]=self.http._destination(url)
            address=self.addresses[host]
            conn=self.http.connection_factory(host,address,25)
            row={'sequence':len(self.rows)+1,'provider':'YouTubeMediaAcquisitionProvider',
                 'at':now(),'method':method,'host':host,
                 'path':('/api/manifest/[REDACTED]' if parsed.path.startswith('/api/manifest/') else
                         '/videoplayback/[REDACTED]' if parsed.path.startswith('/videoplayback/') else parsed.path),
                 'address':address,
                 'proxy_enabled':True,'proxy_endpoint':APPROVED_PROXY,'hop':hop,
                 'url_observed_at':self.url_observed_at if media_host(host) else None,
                 'range_semantics':'HTTP_RANGE_HEADER' if any(k.lower()=='range' for k in headers) else 'NO_RANGE_HEADER',
                 'request_headers':{k:v for k,v in headers.items() if k.lower() in ('range','accept-encoding','content-type','user-agent','connection')},
                 'phase':'REQUEST_SEND'}
            self.rows.append(row)
            try:
                target=parsed.path+('?' + parsed.query if parsed.query else '')
                conn.request(method,target,body=data,headers=headers)
                row['phase']='RESPONSE_HEADERS';response=conn.getresponse()
                row.update(status=response.status,headers_at=now(),
                           response_headers={k:response.getheader(k) for k in SAFE_RESPONSE})
                if response.status in (301,302,303,307,308):
                    location=response.getheader('Location')
                    if not location or method=='POST': raise BoundaryError('YTDLP_REDIRECT_DENIED')
                    url=urljoin(url,location);row['redirect_target_host']=urlsplit(url).hostname
                    response.close();conn.close();continue
                # Never expose server cookies to the extractor's cookie jar.
                out_headers={k:v for k,v in response.getheaders() if k.lower()!='set-cookie'}
                limit=MAX_AUDIO if media_host(host) else 8*1024**2
                result=Response(_Body(response,conn,row,limit,self),url,out_headers,status=response.status)
                if not 200<=response.status<300: raise HTTPError(result)
                if response.getheader('Content-Encoding','identity') not in ('','identity'):
                    result.close();raise BoundaryError('YTDLP_ENCODING_DENIED')
                row['phase']='RESPONSE_BODY'
                return result
            except (OSError,TimeoutError) as exc:
                conn.close();row.update(error=type(exc).__name__,failed_at=now())
                raise TransportError('audio bounded media transport failed') from None
            except BaseException:
                conn.close();row['failed_at']=now();raise
        raise BoundaryError('YTDLP_REDIRECT_LIMIT')


def safe_notice(message):
    """No signed URLs, headers, cookies or dynamic tokens in diagnostic artifacts."""
    message=re.sub(r'https?://\S+','[URL_REDACTED]',str(message))
    message=re.sub(r'(?i)((?:po[_ -]?token|authorization|cookie|visitor_data|access_token|secret)\s*[:=]\s*)\S+',r'\1[REDACTED]',message)
    return message[:2000]


def format_inventory(info):
    keys=('format_id','ext','protocol','width','height','fps','vcodec','acodec','filesize','filesize_approx','format_note','language','language_preference')
    return [{k:f.get(k) for k in keys} for f in info.get('formats',[])]


def choose_format(info,video):
    formats=[f for f in info.get('formats',[]) if f.get('protocol') in ('https','m3u8_native')
        and (f.get('filesize') or f.get('filesize_approx') or 0)<=MAX_AUDIO
        and ((f.get('vcodec') not in (None,'none') and 0<int(f.get('height') or 0)<=720)
             if video else f.get('vcodec')=='none' and f.get('acodec') not in (None,'none'))]
    if not formats:raise AcquisitionError('YTDLP_NO_ELIGIBLE_OBSERVED_FORMAT')
    # Prefer a single HTTPS stream. Video-only is sufficient for frame extraction.
    return max(formats,key=lambda f:(0 if video else f.get('language_preference') or 0,
        f.get('protocol')=='https',int(f.get('height') or 0) if video else float(f.get('abr') or 0)))


def local_js_runtime(workspace):
    """Only a recorded local runtime and matching packaged EJS, never remote scripts."""
    manifest='config/media/ytdlp-ejs.json'
    if not (workspace.root/manifest).is_file():return {},None
    from importlib.metadata import version
    config=json.loads(workspace.read(manifest));runtime=config['runtime']
    if config.get('version')!='yt-dlp-ejs-0.8.0' or version('yt-dlp-ejs')!='0.8.0' or runtime['kind']!='node':
        raise BoundaryError('YTDLP_EJS_VERSION_DRIFT')
    if not runtime['path'].startswith('environments/research/.venv/'):
        raise BoundaryError('YTDLP_RUNTIME_OUTSIDE_TOOL')
    expected={runtime['path']:runtime['sha256'],**config['package_files']}
    for relative,digest in expected.items():
        if relative!=runtime['path'] and not relative.startswith('environments/audio/.venv/lib/python3.11/site-packages/yt_dlp_ejs/'):
            raise BoundaryError('YTDLP_EJS_PATH_DENIED')
        path=workspace.root/relative
        if path.is_symlink() or not path.resolve().is_relative_to(workspace.root) or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise BoundaryError('YTDLP_EJS_HASH_DRIFT')
    return {'node':{'path':str(workspace.root/runtime['path'])}},config


def execute_ytdlp(workspace,job):
    video=job.get('media_kind')=='video'
    if video and job.get('purpose')!='VERSIONED_RQ_FRAME':raise BoundaryError('VIDEO_REQUIRES_FRAME_SELECTION')
    if video or job['video_id'] not in APPROVED_YTDLP_SAMPLES:
        # Versioned remediation authorizes selected public RQ media, not arbitrary
        # downloader arguments or additional hosts. Preserve the old audio gate.
        from .research_audio import validate_selection
        validate_selection(workspace,job)
    # This runs in a fresh media worker, not a user's yt-dlp process.
    import yt_dlp
    from yt_dlp.globals import plugin_dirs
    from yt_dlp.version import __version__
    import psutil
    from .audio_runtime import Budget
    from importlib.metadata import version
    if version('yt-dlp')!='2026.8.19' or __version__!='2026.08.19':
        raise BoundaryError('YTDLP_VERSION_DRIFT')
    plugin_dirs.value=[]
    transport=CheckedTransport(job['video_id'],Budget(workspace,psutil).check)
    notices=[]
    class Logger:
        def debug(self,message):
            if any(c in message for c in ('Downloading','[jsc','[pot','JS runtimes','player','challenge')):
                notices.append({'level':'DEBUG','message':safe_notice(message)})
        def warning(self,message):
            categories=('JavaScript','PO Token','403','SABR','format','Sign in','timed out','challenge')
            notices.append({'level':'WARNING','message':safe_notice(message),'categories':[c for c in categories if c.lower() in message.lower()]})
        def error(self,message):
            notices.append({'level':'ERROR','message':safe_notice(message),'categories':[c for c in ('403','format','Sign in','timed out','challenge') if c.lower() in message.lower()]})
    class RestrictedDL(yt_dlp.YoutubeDL):
        selected_downloads=0
        def urlopen(self,req): return transport.open(req)
        def process_info(self,info_dict):
            if self.selected_downloads: raise BoundaryError('YTDLP_SECOND_DOWNLOAD_DENIED')
            self.selected_downloads+=1
            transport.url_observed_at=now()
            codec_ok=(info_dict.get('vcodec') not in (None,'none') and 0<int(info_dict.get('height') or 0)<=720) if video else info_dict.get('vcodec')=='none'
            if info_dict.get('id')!=job['video_id'] or not codec_ok or info_dict.get('protocol') not in ('https','m3u8_native'):
                raise BoundaryError('YTDLP_AUDIO_SELECTION_BOUNDARY')
            return super().process_info(info_dict)
    temp=f"tmp/media/{job['job_id']}";relative=temp+('/source.video' if video else '/source.audio')
    path=workspace.checked_path(relative,create_parent=True)
    if path.exists(): raise BoundaryError('YTDLP_DESTINATION_EXISTS')
    runtimes,ejs=local_js_runtime(workspace)
    params={'format':'all','outtmpl':str(path), 'ignore_no_formats_error':True,
            'noplaylist':True,'max_filesize':MAX_AUDIO,'cachedir':False,
            'quiet':True,'no_warnings':False,'noprogress':True,'logger':Logger(),
            'cookiefile':None,'cookiesfrombrowser':None,'usenetrc':False,'username':None,'password':None,
            'proxy':APPROVED_PROXY,'socket_timeout':25,'retries':1,'fragment_retries':0,'extractor_retries':0,
            'concurrent_fragment_downloads':1,'continuedl':False,'nopart':True,'overwrites':False,
            'hls_prefer_native':True,'skip_unavailable_fragments':False,'keep_fragments':False,
            'writeinfojson':False,'writethumbnail':False,'writesubtitles':False,'writeautomaticsub':False,
            'postprocessors':[],'external_downloader':None,'js_runtimes':runtimes,'remote_components':[],
            'progress_hooks':[lambda _:transport.check()]}
    outcome={'provider':'YouTubeMediaAcquisitionProvider','version':__version__,'source':job['source_url'],
             'status':'RUNNING','root_cause':'UNKNOWN','profile':'PINNED_RELEASE_DEFAULT_JSLESS_CLIENTS_NO_AUTH',
             'adapter_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if ejs:outcome.update(profile='PINNED_DEFAULT_CLIENTS_LOCAL_NODE_EJS_NO_AUTH',ejs_version=ejs['version'],runtime=ejs['runtime'])
    outcome['parameters']={k:v for k,v in params.items() if k not in ('logger','progress_hooks')}
    outcome['stage']='METADATA_AND_FORMATS'
    try:
        with RestrictedDL(params) as ydl:
            # Library normalization supplies protocol, unique IDs and original
            # audio-language preference. Raw extractor dictionaries omit these.
            info=ydl.extract_info(job['source_url'],download=False)
            outcome['available_formats']=format_inventory(info)
            outcome['stage']='FORMAT_SELECTION'
            selected=choose_format(info,video)
            outcome['selected_format']=format_inventory({'formats':[selected]})[0]
            outcome['selection_basis']='Observed single decodable stream; HTTPS preferred; video <=720p; no fixed format ID; video-only permitted.'
            outcome['stage']='MEDIA_DOWNLOAD'
            ydl.params['format']=selected['format_id']
            ydl.format_selector=ydl.build_format_selector(selected['format_id'])
            info=ydl.process_ie_result(info,download=True)
        if not path.is_file() or not 0<path.stat().st_size<=MAX_AUDIO:
            raise AcquisitionError('YTDLP_NO_COMPLETE_AUDIO')
        outcome['stage']='LOCAL_DECODE'
        # Library's downloader has completed and validated its transport length.
        digest=hashlib.sha256()
        with path.open('rb') as f:
            while chunk:=f.read(1024**2): digest.update(chunk)
        declared_duration=float(info.get('duration') or 0)
        if declared_duration<=0: raise AcquisitionError('YTDLP_DURATION_UNKNOWN')
        # Decode the entire local audio, not just the later ASR excerpt. This
        # verifies complete stream readability and measures actual duration.
        import imageio_ffmpeg
        binary=Path(imageio_ffmpeg.get_ffmpeg_exe())
        if not binary.resolve().is_relative_to(workspace.root/'environments/audio/.venv'):
            raise BoundaryError('YTDLP_GLOBAL_FFMPEG_DENIED')
        checked=subprocess.run([str(binary),'-nostdin','-hide_banner','-loglevel','error','-xerror',
            '-protocol_whitelist','file,pipe','-threads','2','-i',str(path),'-map','0:v:0' if video else '0:a:0',
            '-an' if video else '-vn','-sn','-dn','-threads','2','-progress','pipe:1','-nostats','-f','null','-'],
            capture_output=True,timeout=180,check=False)
        times=re.findall(rb'out_time_us=(\d+)',checked.stdout)
        if checked.returncode or not times: raise AcquisitionError('YTDLP_COMPLETE_AUDIO_DECODE_FAILED')
        duration=int(times[-1])/1_000_000
        if abs(duration-declared_duration)>2: raise AcquisitionError('YTDLP_COMPLETE_DURATION_MISMATCH')
        artifact=AudioArtifact(job['source_url'],'YouTubeMediaAcquisitionProvider',now(),
                               ('video/' if video else 'audio/')+str(info.get('ext')),duration,path.stat().st_size,digest.hexdigest(),relative)
        outcome.update(status='COMPLETE',format_id=info.get('format_id'),duration_s=duration,
                       declared_duration_s=declared_duration,duration_verification='FULL_LOCAL_AUDIO_DECODE',
                       url_observed_at=transport.url_observed_at,acquired_at=artifact.acquired_at)
        return asdict(artifact)
    except BaseException as exc:
        outcome.update(status='FAILED',exception_type=type(exc).__name__,error=safe_notice(str(exc)),failed_at=now())
        raise
    finally:
        outcome['notices']=notices[-80:]
        workspace.write(temp+'/media-provider-result.json',json.dumps(outcome,indent=2).encode())
        workspace.write(temp+'/media-transport.json',json.dumps({'requests':transport.rows,'dns':transport.http.dns.trace},indent=2).encode())
