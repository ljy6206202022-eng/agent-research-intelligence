"""audio media contract; downstream ASR never imports a downloader implementation."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Protocol

from .caption_inventory import inspect_video
from .media_http import select_audio, download_audio, _audio_range
from .audio_runtime import run_worker, clean_media
from agent_research_intelligence.connectors.public_http import AcquisitionError


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class AudioArtifact:
    source: str
    provider: str
    acquired_at: str
    media_type: str
    duration_s: float
    bytes: int
    sha256: str
    temporary_path: str
    acquisition_result: str = 'COMPLETE'
    cleanup_status: str = 'PENDING_PIPELINE_FINALLY'


@dataclass
class MediaContext:
    workspace: object
    job_id: str
    receipt: dict
    player: dict  # ephemeral signed URLs, never serialized
    http: object
    budget: object
    persist: object
    purpose: str

    @property
    def path(self):
        return f'tmp/media/{self.job_id}/source.audio'


class MediaAcquisitionProvider(Protocol):
    name: str
    def acquire(self, context: MediaContext) -> AudioArtifact: ...


class DirectMediaProvider:
    name = 'DirectMediaProvider'

    def acquire(self, c):
        chosen = select_audio(c.player)
        attempts=[]
        for attempt in range(3):
            c.persist('media-url-observation.json',{'provider':self.name,
                      'url_observed_at':c.receipt['at'], 'query_recorded':False})
            try:
                data = _audio_range(c.http,chosen['url'],0,min(int(chosen['contentLength']),1024)-1)
                attempts.append({'attempt':attempt+1,'status':'PREFIX_READ_PASS'})
                break
            except AcquisitionError as exc:
                attempts.append({'attempt':attempt+1,'status':exc.code,'phase':exc.phase})
                c.persist('media-preflight-attempts.json',attempts)
                if exc.code != 'AUDIO_TRANSPORT_FAILED' or attempt==2: raise
                refreshed, player = inspect_video(c.http,c.receipt['url'])
                c.persist(f'caption-inventory-refresh-{attempt+1}.json',refreshed)
                if c.purpose=='NO_SUBTITLE' and refreshed['status']!='NO_USABLE_CAPTION':
                    raise AcquisitionError('SAMPLE_CAPTION_STATUS_CHANGED')
                c.receipt, c.player = refreshed, player
                chosen=select_audio(player)
        c.persist('media-preflight-attempts.json',attempts)
        c.persist('media-preflight.json',{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
                                       'status':'PREFIX_ONLY_NOT_COMPLETE_AUDIO'})
        receipt=download_audio(c.http,c.workspace,chosen['url'],c.path,
                               expected_size=int(chosen['contentLength']),check_budget=c.budget.check)
        return AudioArtifact(c.receipt['url'],self.name,now(),chosen['mimeType'],
                             float(c.receipt['duration_s']),receipt['bytes'],receipt['sha256'],c.path)


# User authorized an additional, separately evaluated clear-audio candidate.
# This remains an exact audio sample allowlist, not open-world acquisition.
APPROVED_YTDLP_SAMPLES = frozenset({'9hBEeGOQS9A', 'GXtx16Kraxk'})
FALLBACK_ERRORS = frozenset({'AUDIO_ACCESS_DENIED','AUDIO_TRANSPORT_FAILED','AUDIO_TRUNCATED',
                           'AUDIO_LENGTH_MISMATCH','PUBLIC_ORIGINAL_AUDIO_UNAVAILABLE','MEDIA_DEADLINE'})


class YouTubeMediaAcquisitionProvider:
    name = 'YouTubeMediaAcquisitionProvider'

    def acquire(self,c):
        if c.receipt['video_id'] not in APPROVED_YTDLP_SAMPLES:
            raise AcquisitionError('YTDLP_SAMPLE_NOT_APPROVED')
        try:
            resources=run_worker(c.workspace,c.job_id,'media',c.budget,timeout_s=1200)
            c.persist('media-resources.json',resources)
        finally:
            for name in ('media-transport.json','media-provider-result.json'):
                relative=f'tmp/media/{c.job_id}/{name}'
                if not c.workspace.checked_path(relative).exists(): continue
                value=json.loads(c.workspace.read(relative))
                c.persist(name,value)
        data=json.loads(c.workspace.read(f'tmp/media/{c.job_id}/media.json'))
        return AudioArtifact(**data)


def acquire_media(c):
    attempts=[]
    for provider in (DirectMediaProvider(),YouTubeMediaAcquisitionProvider()):
        record={'provider':provider.name,'started_at':now()}
        try:
            artifact=provider.acquire(c)
            record.update(result='COMPLETE',finished_at=now())
            attempts.append(record);c.persist('provider-attempts.json',attempts)
            return asdict(artifact)
        except AcquisitionError as exc:
            record.update(result='FAILED_STABILITY_GATE',error=exc.code,phase=exc.phase,
                          finished_at=now(),root_cause='UNKNOWN')
            attempts.append(record)
            c.persist('provider-attempts.json',attempts)
            if (provider.name != 'DirectMediaProvider' or exc.code not in FALLBACK_ERRORS or
                    c.receipt['video_id'] not in APPROVED_YTDLP_SAMPLES): raise
            removed=clean_media(c.workspace,c.job_id)
            c.persist('fallback.json',{'from':provider.name,'to':'YouTubeMediaAcquisitionProvider',
                       'reason':exc.code,'max_provider_fallbacks':1,'removed_partial':removed,'at':now()})
    raise AcquisitionError('MEDIA_PROVIDERS_EXHAUSTED')
