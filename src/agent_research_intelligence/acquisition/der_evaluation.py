"""Validated segments to a pinned external evaluator; no custom DER mathematics."""
import hashlib
import json
import math
import subprocess
import tempfile
from pathlib import Path

from agent_research_intelligence.governance.paths import APP_ROOT, BoundaryError, Workspace
from agent_research_intelligence.acquisition.audio_types import interval

CONTRACT_PATH = 'config/evaluation/AUDIO_DER_EVALUATION_CONTRACT_v0.1.json'
CONTRACT_HASH = '72ff2d26a9c057ddb64b5ac453ce3c1c1fc601e979be1a902771aa0b159bb67f'


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def load_contract():
    workspace = Workspace()
    contract = json.loads(workspace.read(CONTRACT_PATH))
    if canonical_hash(contract) != CONTRACT_HASH:
        raise BoundaryError('DER_CONTRACT_DRIFT')
    protected = {**contract['implementation_files'], **contract['provider']['files']}
    for relative, expected in protected.items():
        if hashlib.sha256(workspace.read(relative)).hexdigest() != expected:
            raise BoundaryError('DER_EVALUATOR_IMPLEMENTATION_DRIFT')
    for item in contract['provider']['model_files']:
        path = workspace.checked_path(item['path'])
        digest = hashlib.sha256()
        with path.open('rb') as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
        if path.stat().st_size != item['bytes'] or digest.hexdigest() != item['sha256']:
            raise BoundaryError('DER_PROVIDER_MODEL_DRIFT')
    return contract


def validate_segments(segments):
    if not isinstance(segments, list) or len(segments) > 10000:
        raise BoundaryError('Invalid segment list or input size limit')
    for item in segments:
        if not isinstance(item, dict) or set(item) != {'start', 'end', 'speaker_id'}:
            raise BoundaryError('DER requires raw segments, not transcript or identity records')
        interval(item['start'], item['end'])
        if not isinstance(item['speaker_id'], str) or not item['speaker_id'] or len(item['speaker_id']) > 128:
            raise BoundaryError('Invalid anonymous speaker label')


def raw_sherpa_segments(record, *, original_offset_s):
    """Lossless label/time adapter. No merging, ASR assignment, pruning or renaming."""
    if (record.get('provider') != 'sherpa-onnx' or record.get('version') != '1.13.8'
            or record.get('identity_resolution') != 'NOT_PERFORMED'
            or set(record) - {'provider', 'version', 'turns', 'identity_resolution', 'embeddings_persisted', 'confidence'}):
        raise BoundaryError('Raw sherpa provider record required')
    if isinstance(original_offset_s, bool) or not isinstance(original_offset_s, (int, float)) or not math.isfinite(original_offset_s) or original_offset_s < 0:
        raise BoundaryError('Invalid original timeline offset')
    turns = record.get('turns')
    if not isinstance(turns, list) or len(turns) > 10000:
        raise BoundaryError('Invalid raw turn list')
    output = []
    for turn in turns:
        if not isinstance(turn, dict) or set(turn) != {'start', 'end', 'label'}:
            raise BoundaryError('Raw turn schema required')
        interval(turn['start'], turn['end'])
        if type(turn['label']) is not int or turn['label'] < 0:
            raise BoundaryError('Raw integer speaker label required')
        output.append({'start': turn['start'] + original_offset_s,
                       'end': turn['end'] + original_offset_s, 'speaker_id': str(turn['label'])})
    validate_segments(output)
    return output


def diarization_error(reference, hypothesis, *, start, end, collar=.25):
    """Synthetic verification entry point; real acceptance is deliberately closed."""
    interval(start, end)
    if collar != .25 or isinstance(collar, bool):
        raise BoundaryError('Versioned audio collar is TOTAL 0.25 seconds')
    validate_segments(reference)
    validate_segments(hypothesis)
    if not reference:
        raise BoundaryError('Empty reference is not valid DER ground truth')
    contract = load_contract()
    request = {'reference': reference, 'hypothesis': hypothesis, 'uem': [start, end],
        'classification': 'SYNTHETIC_CONTRACT_TEST_NOT_AUDIO_ACCEPTANCE',
        'runtime_identity': contract['evaluator']['runtime_identity']}
    payload = json.dumps(request, allow_nan=False).encode()
    if len(payload) > 2 * 1024 * 1024:
        raise BoundaryError('DER input size limit')
    workspace = Workspace()
    # Temporary output is owned by the tool; no inherited credentials/proxy/cache.
    workspace.checked_path('tmp/der-evaluation/placeholder', create_parent=True)
    with tempfile.TemporaryDirectory(dir=APP_ROOT/'tmp/der-evaluation') as directory:
        policy = '(version 1) (allow default) (deny network*) (deny file-write*)\n' + \
            '(allow file-write* (subpath '+json.dumps(directory)+') (literal "/dev/null"))\n'
        profile = Path(directory)/'worker.sb'
        profile.write_text(policy)
        environment = {'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8',
            'HOME': directory, 'TMPDIR': directory, 'XDG_CACHE_HOME': directory,
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
            'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
            'VECLIB_MAXIMUM_THREADS': '1', 'PYTHONHASHSEED': '0'}
        args = ['/usr/bin/sandbox-exec', '-f', str(profile),
            str(APP_ROOT/'environments/evaluation/.venv/bin/python'), '-B',
            str(APP_ROOT/'environments/evaluation/worker.py')]
        try:
            result = subprocess.run(args, input=payload, capture_output=True, cwd=APP_ROOT,
                env=environment, timeout=120, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise BoundaryError('DER_EVALUATOR_FAILED_NO_SCORE') from exc
        score = json.loads(result.stdout)
        score['contract_hash'] = CONTRACT_HASH
        score['input_hash'] = canonical_hash({'reference': reference, 'hypothesis': hypothesis, 'uem': [start, end]})
        return score
