"""Fixed offline worker. Invoked only by the audio supervisor inside a native sandbox."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import wave
import threading
import time
import signal

from agent_research_intelligence.governance.paths import APP_ROOT, Workspace, BoundaryError


def model_files(workspace):
    manifest = json.loads(workspace.read('config/permissions/audio-models.json'))
    if manifest.get('status') != 'USER_APPROVED' or manifest.get('purpose') != 'AUDIO_LOCAL_AUDIO_ONLY':
        raise BoundaryError('MODEL_DOWNLOAD_GATE_CLOSED')
    files = manifest.get('files', [])
    if not files: raise BoundaryError('No approved model files')
    for item in files:
        rel = item['path']
        if not rel.startswith('cache/models/audio/'):
            raise BoundaryError('Model path outside approved cache')
        path = workspace.checked_path(rel)
        h = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024**2): h.update(chunk)
        if h.hexdigest() != item['sha256'] or path.stat().st_size != item['bytes']:
            raise BoundaryError('Model hash drift')
    return manifest


def run(stage, relative):
    os.nice(10)
    supervisor = os.getppid()
    def stop_when_orphaned():
        while True:
            time.sleep(1)
            if os.getppid() != supervisor:
                # This worker is session leader; kills only its own FFmpeg children.
                if os.getpgrp() == os.getpid(): os.killpg(os.getpgrp(),signal.SIGKILL)
                os._exit(3)
    threading.Thread(target=stop_when_orphaned,daemon=True).start()
    workspace = Workspace()
    job = json.loads(workspace.read(relative)); identifier = job['job_id']
    if relative != f'data/audio/{identifier}/job.json' or len(identifier) != 32 or any(x not in '0123456789abcdef' for x in identifier):
        raise BoundaryError('Invalid worker input')
    temp = f'tmp/media/{identifier}'
    wav = workspace.checked_path(temp+'/normalized.wav')
    if stage == 'media':
        from agent_research_intelligence.acquisition.ytdlp_provider import execute_ytdlp
        result = execute_ytdlp(workspace,job)
    elif stage == 'normalize':
        import imageio_ffmpeg
        binary = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if not binary.resolve().is_relative_to(APP_ROOT/'environments/audio/.venv'):
            raise BoundaryError('Global FFmpeg denied')
        source = workspace.checked_path(temp+'/source.audio')
        args = [str(binary),'-nostdin','-hide_banner','-loglevel','error','-n',
                '-protocol_whitelist','file,pipe','-threads','2','-i',str(source),
                '-ss',str(job['offset_s']),'-t',str(job['duration_s']),'-map','0:a:0',
                '-vn','-sn','-dn','-map_metadata','-1','-ac','1','-ar','16000',
                '-threads','2','-filter_threads','2','-c:a','pcm_s16le','-f','wav',str(wav)]
        subprocess.run(args,check=True,stdin=subprocess.DEVNULL)
        os.chmod(wav,0o600)
        with wave.open(str(wav)) as stream:
            result = {'sample_rate':stream.getframerate(),'channels':stream.getnchannels(),
                      'sample_width':stream.getsampwidth(),'duration_s':stream.getnframes()/stream.getframerate()}
        if result['sample_rate'] != 16000 or result['channels'] != 1 or result['sample_width'] != 2:
            raise BoundaryError('Unexpected normalization format')
        if abs(result['duration_s']-job['duration_s']) > .25:
            raise BoundaryError('Normalized duration mismatch')
        result['ffmpeg_version'] = imageio_ffmpeg.get_ffmpeg_version()
        result['sha256'] = hashlib.sha256(wav.read_bytes()).hexdigest()
        result['original_offset_s'] = job['offset_s']
        result['vad_compaction'] = False
    elif stage == 'asr':
        from agent_research_intelligence.acquisition.asr_profile import (
            load_selected_profile, require_profile_language, transcribe_profile,
        )
        model_files(workspace)
        profile = load_selected_profile(workspace, job.get('asr_profile_id'))
        require_profile_language(profile, job.get('language'))
        result = transcribe_profile(workspace, wav, profile)
        if not result['segments'] or not result['number_of_words']:
            raise BoundaryError('ASR produced no aligned speech')
        result.update(model='small', compute='cpu_int8', prompt=None)
    elif stage == 'diarize':
        import sherpa_onnx
        import numpy as np
        manifest = model_files(workspace)
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(APP_ROOT/manifest['segmentation'])),
                num_threads=2,provider='cpu'),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(APP_ROOT/manifest['embedding']),num_threads=2,provider='cpu'),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=job.get('speakers',-1),threshold=.5),
            min_duration_on=.2,min_duration_off=.5)
        if not config.validate(): raise BoundaryError('Diarization config invalid')
        diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
        with wave.open(str(wav)) as stream:
            samples = np.frombuffer(stream.readframes(stream.getnframes()),dtype=np.int16).astype(np.float32)/32768.
        result = {'provider':'sherpa-onnx','version':sherpa_onnx.__version__,
                  'turns':[{'start':s.start,'end':s.end,'label':int(s.speaker)} for s in diarizer.process(samples).sort_by_start_time()],
                  'identity_resolution':'NOT_PERFORMED','embeddings_persisted':False,'confidence':None}
        if not result['turns']: raise BoundaryError('Diarization produced no speech')
    else:
        raise BoundaryError('Unknown worker stage')
    workspace.write(temp+'/'+stage+'.json',json.dumps(result,ensure_ascii=False,allow_nan=False).encode())


if __name__ == '__main__':
    try:
        if len(sys.argv) != 3: raise BoundaryError('Expected stage and job input')
        run(sys.argv[1],sys.argv[2])
    except Exception as exc:
        # No provider exception text, URLs, input contents or environment dumps.
        print(json.dumps({'status':'FAILED','exception_type':type(exc).__name__,
                          'boundary_reason':str(exc) if isinstance(exc,BoundaryError) else None}),flush=True)
        raise SystemExit(2)
