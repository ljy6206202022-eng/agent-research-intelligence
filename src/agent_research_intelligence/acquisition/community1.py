"""Thin fixed Community-1 adapter; no diarization algorithm or identity inference."""
import hashlib
import json
import math

from agent_research_intelligence.governance.paths import BoundaryError

PROVIDER = 'community1'
MANIFEST = 'config/permissions/audio-community1.json'


def selected(job):
    provider = job.get('diarization_provider', 'sherpa-onnx')
    if provider not in ('sherpa-onnx', PROVIDER):
        raise BoundaryError('UNKNOWN_DIARIZATION_PROVIDER')
    if provider == PROVIDER:
        if (type(job.get('speakers', -1)) is not int or job.get('speakers', -1) != -1
                or job.get('diarization_mode', 'AUDIO_ONLY_AUTO') != 'AUDIO_ONLY_AUTO'
                or any(k in job for k in ('context_decision', 'context_count', 'num_speakers', 'min_speakers', 'max_speakers'))):
            raise BoundaryError('COMMUNITY1_AUTO_ONLY_NO_COUNT_HINTS')
    return provider


def manifest(workspace):
    body = workspace.read(MANIFEST)
    value = json.loads(body)
    if value.get('status')!='USER_CONFIGURED_LOCAL_ONLY' or not value.get('files'):
        raise BoundaryError('COMMUNITY1_MODEL_MANIFEST_REQUIRED')
    if not value.get('directory','').startswith('cache/models/'):
        raise BoundaryError('COMMUNITY1_MODEL_DIRECTORY_DENIED')
    lock = workspace.read('environments/pyannote_candidate/pylock.toml')
    if hashlib.sha256(lock).hexdigest() != value['lock_sha256']:
        raise BoundaryError('COMMUNITY1_LOCK_DRIFT')
    for relative, expected in value['files'].items():
        path = workspace.checked_path(value['directory']+'/'+relative)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise BoundaryError('COMMUNITY1_MODEL_DRIFT')
    return value


def adapt(raw, *, duration_s):
    """Lossless label conversion only; preserve overlap, ordering and float times."""
    labels = {}
    turns = []
    for segment in raw:
        if set(segment) != {'start', 'end', 'speaker_id'}:
            raise BoundaryError('INVALID_COMMUNITY1_RAW_SCHEMA')
        start, end, label = segment['start'], segment['end'], segment['speaker_id']
        if (not all(type(x) in (int, float) and math.isfinite(x) for x in (start, end))
                or not 0 <= start < end <= duration_s + .1
                or not isinstance(label, str) or not label or len(label) > 128):
            raise BoundaryError('INVALID_COMMUNITY1_RAW_SEGMENT')
        if label not in labels:
            labels[label] = len(labels)
        turns.append({'start': start, 'end': end, 'label': labels[label]})
    return {'provider': PROVIDER, 'version': 'pyannote.audio 4.0.7',
            'raw_segments': raw, 'turns': turns, 'label_adapter': labels,
            'output_kind': 'speaker_diarization', 'overlap_preserved': True,
            'identity_resolution': 'NOT_PERFORMED', 'embeddings_persisted': False,
            'confidence': None, 'quality': 'UNASSESSED'}
