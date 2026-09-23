"""A normal acquisition entry shared by selected YouTube research candidates."""
import hashlib
import json
from uuid import uuid4
from agent_research_intelligence.acquisition.youtube import YouTube
from agent_research_intelligence.connectors.public_http import AcquisitionError
from agent_research_intelligence.connectors.research_documents import Documents
from agent_research_intelligence.governance.policy import canonical
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.research.scoring import screen
from agent_research_intelligence.research.external_artifacts import resolve_description


def acquire_content(ws,row,sources,guard,*,terms,deep,asr_profile=None):
    guard();prefix='data/youtube/content/'+uuid4().hex;provider=YouTube(sources.http)
    fallback=None;identity={'status':'DIARIZATION_NOT_RUN','speakers':[],'HUMAN_VERIFIED':False}
    try:
        if row.get('light_artifact'):
            if not row['light_artifact'].startswith('data/youtube/content/'):raise BoundaryError('Owned light-content artifact required')
            raw=ws.read(row['light_artifact'])
            if hashlib.sha256(raw).hexdigest()!=row['light_sha256']:raise BoundaryError('Light-content artifact changed')
            content=json.loads(raw)
            if content['video_id']!=row['video_id']:raise BoundaryError('Light-content identity mismatch')
            segments=content['segments']
        else:
            video=provider.fetch(row['url'],languages=('en','zh'))
            ws.write(prefix+'/caption.raw',provider.caption_body)
            content=video.model_dump(mode='json');segments=content['segments']
    except AcquisitionError as e:
        ws.write(prefix+'/caption-failure.json',canonical({'code':e.code,'trace':provider.trace}))
        if not deep or asr_profile is None:raise
        from agent_research_intelligence.acquisition.research_audio import transcribe_video
        fallback=transcribe_video(ws,row['url'],selection_artifact=row['selection_artifact'],
            selection_sha256=row['selection_sha256'],caption_failure=e.code,
            material_type=row.get('material_type','UNKNOWN'),profile_id=asr_profile,
            diarization_provider=row.get('diarization_provider'))
        transcript=json.loads(ws.read(fallback['outputs']['transcript.json']))
        reading=json.loads(ws.read(fallback['outputs']['reading.json']))
        # Native sentence times are kept, no sorting or correction. Raw word provenance remains linked.
        segments=[{'start':r['start'],'duration':r['end']-r['start'],'text':r['text'],
                   'raw_sentence_index':r['raw_sentence_index'],'word_indices':r['word_indices']} for r in reading['rows']]
        content={'video_id':row['video_id'],'transcript_source':'asr_generated','segments':segments,
                 'raw_transcript_artifact':fallback['outputs']['transcript.json'],
                 'limitations':transcript['limitations'],'anomalies':transcript['anomalies']}
        if transcript.get('diarization_raw'):
            from agent_research_intelligence.acquisition.identity_resolution import resolve_speakers
            pair=transcript['diarization_raw'];offset=transcript['source']['offset_s']
            turns=[{**t,'start':t['start']+offset,'end':t['end']+offset} for t in pair['normal']]
            named_spans=[]
            for s in segments:
                words=[transcript['words'][i] for i in s['word_indices']]
                labels={w['speaker_id'] for w in words}
                named_spans.append({**s,'speaker_id':next(iter(labels)) if len(labels)==1 and None not in labels else None})
            identity=resolve_speakers(turns,named_spans,row.get('identity_roster',[]))
    caption_speakers=None
    if deep and fallback is None and row.get('diarization_provider')=='community1':
        from agent_research_intelligence.acquisition.research_audio import diarize_caption_video
        try:
            caption_speakers=diarize_caption_video(ws,row,segments);identity=caption_speakers['identity']
        except (AcquisitionError,BoundaryError,OSError,ValueError) as e:
            caption_speakers={'status':'BLOCKED','reason':getattr(e,'code',type(e).__name__)}
            identity={'status':'REQUESTED_DIARIZATION_BLOCKED','speakers':[],'HUMAN_VERIFIED':False}
    # Caption acquisition is inexpensive L2; L3 follows resources for a bounded subset only.
    light=[s for s in segments if any(t.casefold() in s['text'].casefold() for t in terms)][:12]
    resources=None;frames=None
    if deep:
        from agent_research_intelligence.connectors.browser import Browser
        resources=resolve_description(ws,row['metadata'],segments,Documents(browser=Browser(ws),guard=guard),
            recheck=lambda:sources.metadata(row['url']))
        from agent_research_intelligence.acquisition.frames import extract_video_frames
        try:frames=extract_video_frames(ws,row,segments)
        except (AcquisitionError,BoundaryError,OSError,ValueError) as e:
            frames={'status':'BLOCKED','reason':getattr(e,'code',type(e).__name__),'frames':[]}
    content.update(level='L3' if deep else 'L2',light_segments=light,
        source_assessment=screen(row['metadata'],terms,segments=light,
            verified_resources=[r['artifact'] for r in resources['resources'] if r['state']=='FETCHED'] if resources else None),
        external_resources=resources,frames=frames,speaker_identity=identity,caption_speaker_association=caption_speakers,asr_receipt=fallback,HUMAN_VERIFIED=False)
    artifact=prefix+'/transcript.json';ws.write(artifact,canonical(content))
    return {**content,'artifact':artifact,'model_runs':fallback['model_runs'] if fallback else 0}
