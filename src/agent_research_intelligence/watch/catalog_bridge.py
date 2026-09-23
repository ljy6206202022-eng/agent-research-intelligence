"""Register catalog channel targets in the existing watch engine, without a new poller."""
from datetime import timedelta
from uuid import uuid4
from agent_research_intelligence.research.catalog import Catalog
from agent_research_intelligence.watch.service import digest,entries,normalized
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import canonical
from agent_research_intelligence.connectors.youtube_sources import channel_id
from agent_research_intelligence.storage.database import Store


def register(watch,*,dossier_id,source_ids,watch_days=90):
    ws=watch.ws;watch.guard();c=Catalog(ws);d=c.read(dossier_id)
    if d['kind']!='dossier':raise BoundaryError('Catalog dossier required')
    rq=c.read(d['payload']['rq_id']);sources=[c.read(i) for i in source_ids]
    if not 1<=len(sources)<=8 or len(set(source_ids))!=len(source_ids):raise BoundaryError('One to eight distinct targets required')
    if not 1<=watch_days<=90:raise BoundaryError('Watch expiry exceeds policy')
    allowed={s['id'] for s in d['payload']['Sources Searched']}
    if not set(source_ids)<=allowed:raise BoundaryError('Target must belong to dossier sources')
    wid=uuid4().hex;p=watch.prefix(wid);targets={};corpus=[]
    for s in sources:
        source=s['payload']
        if watch.evidence_mode=='REAL' and any(b['classification']=='FIXTURE' for b in source['basis']):
            raise BoundaryError('Fixture catalog cannot seed REAL watch')
        if s['kind']!='source' or source['kind']!='channel':raise BoundaryError('This bridge handles evidenced channel sources only')
        cid=channel_id(source['platform_id']);url='https://www.youtube.com/feeds/videos.xml?channel_id='+cid
        response=watch.http.get(url)
        # Check channel identity, as well as generic feed syntax, before committing.
        import xml.etree.ElementTree as ET
        baseline=entries(response.body,url,'feed')
        root=ET.fromstring(response.body)
        if root.findtext('{http://www.youtube.com/xml/schemas/2015}channelId')!=cid:raise BoundaryError('Channel baseline identity mismatch')
        rawpath=p+'/'+s['id']+'.baseline.xml';ws.write(rawpath,response.body)
        targets[s['id']]={'url':url,'source_url':source['url'],'kind':'feed','raw_hash':digest(response.body),
            'etag':response.headers.get('etag'),'modified':response.headers.get('last-modified'),
            'seen':{e['key']:e['revision'] for e in baseline},'next_poll_at':None,'last_change_at':None,
            'interval_s':3600,'baseline':rawpath,'baseline_sha256':digest(response.body),
            'catalog_source_revision':s['revision'],'source_provenance':source['source_provenance']}
        corpus.extend({'ref':'BASELINE:'+s['id']+':'+e['key'],'text':normalized(e['text'])} for e in baseline)
    original=f"data/catalog/dossiers/{d['id']}/v{d['revision']}.md";body=ws.read(original)
    for e in d['payload']['Evidence Summary']:corpus.append({'ref':e['id'],'text':normalized(e['claim'])})
    state={'id':wid,'job':rq['id'],'question_sha256':digest(rq),'original_dossier':original,'original_dossier_sha256':digest(body),
        'current_dossier':original,'rq_status':'WATCHING','watch_status':'WATCHING','created_at':watch.now(),
        'watch_until':(watch.clock()+timedelta(days=watch_days)).isoformat(),'reopen_threshold':.8,
        'local_terms':rq['payload']['search_terms'],'targets':targets,'corpus':corpus,'processed_events':[],
        'reviewed_events':{},'state_revision':0,'evidence_mode':watch.evidence_mode,
        'scope':'READ_ANALYZE_PROPOSE_ONLY','classification':'PRIVATE','account_mutation':'recommend_only',
        'catalog_question':{'id':rq['id'],'revision':rq['revision']},'catalog_dossier':{'id':d['id'],'revision':d['revision']}}
    with Store(ws):
        watch.save(p+'/registration.json',{'source':'VERSIONED_CATALOG_CHANNEL_BRIDGE','at':watch.now(),
            'dossier_id':dossier_id,'source_ids':source_ids,'account_mutations':0})
        watch.commit(wid,state,'REGISTERED_CATALOG_CHANNELS')
    return {'watch_id':wid,'state':'WATCHING','targets':len(targets),'background_processes':0,'model_calls':0}
