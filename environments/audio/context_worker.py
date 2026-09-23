"""Guarded current diarization entry; reuse the frozen worker without edits."""
import json
import sys
import importlib.util
from pathlib import Path
from agent_research_intelligence.governance.paths import Workspace, BoundaryError
from agent_research_intelligence.acquisition.speaker_context import validate_job

if __name__=='__main__':
    try:
        if len(sys.argv)!=3 or sys.argv[1]!='diarize':raise BoundaryError('Invalid guarded stage')
        ws=Workspace();job=json.loads(ws.read(sys.argv[2]))
        if sys.argv[2]!=f"data/audio/{job['job_id']}/job.json":raise BoundaryError('Job path mismatch')
        receipt=validate_job(ws,job)
        ws.write(f"tmp/media/{job['job_id']}/effective-diarization-parameters.json",json.dumps(receipt,sort_keys=True).encode())
        spec=importlib.util.spec_from_file_location('frozen_audio_worker',Path(__file__).with_name('worker.py'))
        old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
        old.run('diarize',sys.argv[2])
    except Exception as exc:
        print(json.dumps({'status':'FAILED','exception_type':type(exc).__name__,
                         'boundary_reason':str(exc) if isinstance(exc,BoundaryError) else None}))
        raise SystemExit(2)
