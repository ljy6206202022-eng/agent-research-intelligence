"""Description provenance and independent resource retrieval, never execution."""
import hashlib
import re
from urllib.parse import urlsplit
from uuid import uuid4

from agent_research_intelligence.acquisition.youtube import description_links, Segment
from agent_research_intelligence.connectors.public_http import AcquisitionError
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import canonical
from agent_research_intelligence.research.source_intelligence import identity


def classify(url):
    p=urlsplit(url);host=p.hostname or '';path=p.path.lower()
    if host=='github.com':
        return 'skill' if '/skill' in path else 'github'
    if host in ('arxiv.org','doi.org','www.semanticscholar.org') or path.endswith('.pdf'):return 'paper'
    for kind in ('dataset','benchmark','skill','demo','docs'):
        if kind in path:return kind
    return 'web'


def resolve_description(workspace, metadata, segments, documents, *, max_resources=6, recheck=None):
    if not 1<=max_resources<=10:raise BoundaryError('External resource budget required')
    typed=tuple(Segment(start=s['start'],duration=s.get('duration',max(0,s.get('end',s['start'])-s['start'])),text=s['text']) for s in segments)
    extracted=description_links(metadata['description'],typed)
    prefix='data/artifacts/description/'+uuid4().hex
    initial_hash=hashlib.sha256(metadata['description'].encode()).hexdigest()
    # A spoken cue requires an actual full-description re-read, not a cached snippet.
    rechecked=bool(extracted['spoken_references'])
    if rechecked:
        if recheck is None:raise BoundaryError('SPOKEN_CUE_REQUIRES_FULL_DESCRIPTION_RECHECK')
        response,current=recheck()
        if current['video_id']!=metadata['video_id']:raise BoundaryError('Description input mismatch')
        workspace.write(prefix+'/description-recheck.raw',response.body)
        metadata=current;extracted=description_links(metadata['description'],typed)
    workspace.write(prefix+'/description.json',canonical(metadata))
    resources=[];seen={}
    for link in extracted['links']:
        if len(resources)>=max_resources:break
        url=link['url'];row={**link,'state':'NOT_RUN','video_id':metadata['video_id'],
            'description_artifact':prefix+'/description.json'}
        if urlsplit(url).scheme!='https':
            row.update(state='UNRESOLVED_EXTERNAL_REFERENCE',reason='HTTPS_REQUIRED');resources.append(row);continue
        try:
            kind=classify(url)
            doc=documents.fetch(url,kind='github' if urlsplit(url).hostname=='github.com' else 'paper' if kind=='paper' else 'web')
            raw=doc.pop('raw');canonical_url=identity(doc.get('canonical') or doc['url'])
            row.update(canonical_url=canonical_url,resource_kind=kind,independent_fetch=True,
                       independent_support='NOT_ESTABLISHED',provenance=[
                           {'kind':'video','id':metadata['video_id']},
                           {'kind':'description','artifact':prefix+'/description.json','offset':link['description_offset']},
                           {'kind':'resource','requested_url':url,'resolved_url':doc['url'],'version':doc.get('version')}])
            if canonical_url in seen:
                row.update(state='DUPLICATE_CANONICAL_RESOURCE',duplicate_of=seen[canonical_url]);resources.append(row);continue
            index=len(resources);stem=prefix+'/resource-'+str(index)
            workspace.write(stem+'.raw',raw)
            doc['raw_sha256']=hashlib.sha256(raw).hexdigest()
            workspace.write(stem+'.json',canonical(doc))
            row.update(state='FETCHED',artifact=stem+'.json',raw_artifact=stem+'.raw',sha256=doc['raw_sha256'])
            seen[canonical_url]=stem+'.json'
        except (AcquisitionError,BoundaryError,ValueError,KeyError) as exc:
            row.update(state='UNRESOLVED_EXTERNAL_REFERENCE',reason=getattr(exc,'code',type(exc).__name__))
        resources.append(row)
    cues=[]
    for cue in extracted['spoken_references']:
        context=cue['context'].casefold();matches=[]
        for r in resources:
            if r['state'] not in ('FETCHED','DUPLICATE_CANONICAL_RESOURCE'):continue
            kind=r['resource_kind'];name=urlsplit(r['url']).path.rstrip('/').rsplit('/',1)[-1]
            explicit_name=name and len(name)>4 and re.sub('[-_]',' ',name).casefold() in context
            explicit_kind=any(x in context for x in {'github':['github','repo','代码'], 'paper':['paper','论文'],
                'docs':['documentation','docs','文档'],'skill':['skill'],'dataset':['dataset','数据集'],
                'demo':['demo']}.get(kind,[]))
            if explicit_name or explicit_kind:matches.append(r.get('artifact') or r['duplicate_of'])
        matches=list(dict.fromkeys(matches))
        cues.append({**cue,'matched_artifacts':matches,'status':'RESOLVED_RESOURCE_CANDIDATE' if len(matches)==1
                     else 'AMBIGUOUS_EXTERNAL_REFERENCE' if matches else 'UNRESOLVED_EXTERNAL_REFERENCE',
                     'HUMAN_VERIFIED':False})
    result={'video_id':metadata['video_id'],'original_description_sha256':initial_hash,
            'description_sha256':hashlib.sha256(metadata['description'].encode()).hexdigest(),
            'full_description_rechecked':rechecked,
            'resources':resources,'spoken_cues':cues,'not_fetched_due_to_budget':max(0,len(extracted['links'])-len(resources)),
            'commands_executed':0,'account_mutations':0,'deep_analysis_calls':0,
            'http_requests':getattr(getattr(documents,'http',None),'total_requests',None),
            'transport_trace':getattr(getattr(documents,'http',None),'trace',[]),
            'limits':'Independent retrieval is not independent scientific corroboration; redirects do not establish ownership.'}
    path=prefix+'/result.json';workspace.write(path,canonical(result))
    return {**result,'artifact':path}
