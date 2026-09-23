"""Fixed frame operations within the existing resource supervisor and OS sandbox."""
import json,os,signal,sys,threading,time
from pathlib import Path
from agent_research_intelligence.governance.paths import Workspace,BoundaryError
from agent_research_intelligence.governance.policy import canonical
from agent_research_intelligence.acquisition.frames import extract_local
from agent_research_intelligence.acquisition.research_audio import validate_selection
from agent_research_intelligence.acquisition.asr_profile import file_hash

def main():
    stage,relative=sys.argv[1:];ws=Workspace();job=json.loads(ws.read(relative));identifier=job['job_id']
    if stage!='frames' or relative!=f'data/audio/{identifier}/job.json' or job.get('purpose')!='VERSIONED_RQ_FRAME':
        raise BoundaryError('FRAME_JOB_REQUIRED')
    validate_selection(ws,job)
    candidates=job['frame_candidates']
    if not 1<=len(candidates)<=12:raise BoundaryError('FRAME_COUNT_LIMIT')
    parent=os.getppid();os.nice(10)
    def watch():
        while True:
            time.sleep(1)
            if os.getppid()!=parent:os.killpg(os.getpgrp(),signal.SIGKILL)
    threading.Thread(target=watch,daemon=True).start()
    import imageio_ffmpeg
    binary=Path(imageio_ffmpeg.get_ffmpeg_exe())
    if not binary.resolve().is_relative_to(ws.root/'environments/audio/.venv'):raise BoundaryError('Global FFmpeg denied')
    directory=ws.root/'tmp/media'/identifier
    frames=extract_local(binary,ws.checked_path(f'tmp/media/{identifier}/source.video'),directory,candidates)
    ws.write(f'tmp/media/{identifier}/frames.json',canonical({'frames':frames,'ffmpeg_sha256':file_hash(binary)}))

if __name__=='__main__':
    try:main()
    except Exception as e:
        print(json.dumps({'status':'FAILED','error':type(e).__name__}));raise SystemExit(2)
