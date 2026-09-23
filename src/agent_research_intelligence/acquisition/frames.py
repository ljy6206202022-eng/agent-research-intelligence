"""Transcript-guided small frame sets; scene/difference is not visual understanding."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
import math
import os
import re
from pathlib import Path
import subprocess
from uuid import uuid4

from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import canonical
from agent_research_intelligence.connectors.public_http import AcquisitionError

CUE=re.compile(r'(?:look|see|shown?|here).{0,70}(?:code|diagram|architecture|config|terminal|prompt|demo|benchmark)|(?:code|diagram|config).{0,35}(?:here|screen)|看.{0,20}(?:代码|图|配置|终端|演示)',re.I)


def frame_times(segments,duration):
    if not math.isfinite(duration) or duration<=0:raise BoundaryError('Known video duration required')
    candidates=[]
    for i,s in enumerate(segments):
        if not CUE.search(s['text']):continue
        t=s['start']
        if not math.isfinite(t) or not 0<=t<duration:continue
        for offset in (-1,0,1):
            time=max(0,min(duration-.05,t+offset))
            if all(abs(time-c['requested_original_s'])>.1 for c in candidates):
                candidates.append({'requested_original_s':time,'transcript_index':i,'cue':s['text']})
            if len(candidates)>=12:return candidates
    return candidates


def frame_difference(previous,current):
    if not previous or len(previous)!=len(current):return None
    return sum(abs(a-b) for a,b in zip(previous,current))/(len(current)*255)


def extract_local(binary,source,directory,candidates):
    """Fixed FFmpeg operations, local protocols only, no commands from sources."""
    outputs=[];previous=None
    for i,c in enumerate(candidates):
        target=directory/f'candidate-{i}.png'
        common=[str(binary),'-nostdin','-hide_banner','-threads','2','-ss',str(c['requested_original_s']),
                '-copyts','-protocol_whitelist','file,pipe','-i',str(source),'-map','0:v:0','-frames:v','1','-an','-sn','-dn','-filter_threads','2']
        # showinfo reports the actual decoded PTS; request time alone is not the frame timestamp.
        run=subprocess.run(common+['-vf','scale=960:-2,showinfo','-n',str(target)],capture_output=True,timeout=30)
        if run.returncode or not target.exists():raise AcquisitionError('FRAME_DECODE_FAILED')
        pts=re.findall(rb'pts_time:([-+0-9.eE]+)',run.stderr)
        if not pts:raise AcquisitionError('FRAME_PTS_UNKNOWN')
        actual=float(pts[0])
        if not math.isfinite(actual):raise AcquisitionError('FRAME_PTS_INVALID')
        thumb=subprocess.run(common+['-loglevel','error','-vf','scale=144:81,format=gray','-f','rawvideo','pipe:1'],capture_output=True,timeout=30)
        if thumb.returncode or len(thumb.stdout)!=144*81:raise AcquisitionError('FRAME_DIFFERENCE_DECODE_FAILED')
        difference=frame_difference(previous,thumb.stdout)
        keep=difference is None or difference>=.025
        row={**c,'original_pts_s':actual,'difference_from_previous_retained':difference,
             'difference_policy':'MEAN_ABSOLUTE_GRAY_144x81_GE_0.025_v1','selected':keep,
             'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'temporary_file':target.name,
             'visual_content':'UNVERIFIED_CANDIDATE','HUMAN_VERIFIED':False}
        outputs.append(row)
        if keep:previous=thumb.stdout
    return outputs


def extract_video_frames(ws,row,segments):
    times=frame_times(segments,row['metadata']['duration_s'])
    if not times:return {'status':'CONDITION_NOT_TRIGGERED','frames':[],'model_runs':0}
    import psutil
    from agent_research_intelligence.acquisition.audio_runtime import Budget,heavy_slot,run_worker,clean_media
    from agent_research_intelligence.acquisition.research_audio import validate_selection
    from agent_research_intelligence.acquisition.asr_profile import file_hash
    job_id=uuid4().hex;prefix='data/artifacts/frames/'+job_id;temp='tmp/media/'+job_id
    job={'job_id':job_id,'video_id':row['video_id'],'source_url':row['url'],'purpose':'VERSIONED_RQ_FRAME','media_kind':'video',
         'selection_artifact':row['selection_artifact'],'selection_sha256':row['selection_sha256'],
         'offset_s':0,'duration_s':row['metadata']['duration_s'],'speakers':-1,'frame_candidates':times}
    validate_selection(ws,job);budget=Budget(ws,psutil);budget.check(force=True)
    result={'status':'RUNNING','frames':[],'video_id':row['video_id'],'model_runs':0};owned=False
    try:
        with heavy_slot(ws):
            ws.write('data/audio/'+job_id+'/job.json',canonical(job))
            ws.write(temp+'/owned.json',canonical({'job_id':job_id,'purpose':'AUDIO_TEMP_MEDIA','supervisor_pid':os.getpid(),
                'supervisor_created':psutil.Process().create_time()}));owned=True
            run_worker(ws,job_id,'media',budget,timeout_s=1200)
            media=json.loads(ws.read(temp+'/media.json'));result['media_receipt']=media
            run_worker(ws,job_id,'frames',budget,timeout_s=600)
            decoded=json.loads(ws.read(temp+'/frames.json'));frames=decoded['frames']
            for f in frames:
                budget.check()
                if f['selected']:
                    p=prefix+'/'+f['temporary_file'];ws.write(p,ws.read(temp+'/'+f['temporary_file']))
                    f['artifact']=p
            result.update(status='FRAME_CANDIDATES_READY',frames=frames,ffmpeg_sha256=decoded['ffmpeg_sha256'],
                frame_retention_expires_at=(datetime.now(timezone.utc)+timedelta(days=30)).isoformat(),
                limits='Retained frames require visual inspection to establish code/diagram content. Native PTS is not human timestamp validation.')
            return {**result,'artifact':prefix+'/result.json'}
    except BaseException as e:
        result.update(status='BLOCKED',error=getattr(e,'code',type(e).__name__));raise
    finally:
        result['media_removed']=clean_media(ws,job_id) if owned else []
        ws.write(prefix+'/result.json',canonical(result))
