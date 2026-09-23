"""Additive discovery -> Versioned contracts projection, preserving every original artifact."""
import hashlib
import json
import re
from urllib.parse import urlsplit
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import canonical
from agent_research_intelligence.research.catalog import Catalog
from agent_research_intelligence.research.dossier import build


def project_discovery(ws,job,*,classification='REUSED_REAL'):
    if not re.fullmatch('[a-f0-9]{32}',job) or classification not in ('REAL','REUSED_REAL','FIXTURE'):
        raise BoundaryError('Explicit discovery identity/classification required')
    prefix='data/discovery/'+job;c=Catalog(ws)
    path='data/catalog/imports/'+job+'.json'
    if (ws.root/path).exists():
        prior=json.loads(ws.read(path))
        # Revalidate all located input hashes before returning the immutable import.
        # A later caller's requested classification cannot upgrade reused evidence.
        for identifier in [prior['catalog_rq'],*prior['catalog_sources'].values(),*prior['evidence_ids']]:
            item=c.read(identifier)['payload']
            from agent_research_intelligence.research.catalog import Basis
            c.validate_basis([Basis.model_validate(b) for b in item.get('basis',item.get('provenance',item.get('context_basis',[])))])
        return prior
    def load(name):return json.loads(ws.read(prefix+'/'+name))
    def basis(name,locator='whole document'):
        path=prefix+'/'+name
        return {'artifact':path,'sha256':hashlib.sha256(ws.read(path)).hexdigest(),'locator':locator,'classification':classification}
    question=load('question.json');brief=load('public-brief.json');summary=load('result.json')
    sources=load('sources.json');evidence=load('evidence.json');by_id={};eids=[]
    def put(kind,payload,key):
        identity=hashlib.sha256(('discovery:'+job+':'+kind+':'+key).encode()).hexdigest()[:32]
        try:old=c.read(identity)
        except BoundaryError:old=None
        if old:
            # Reuse exact prior projection; never rewrite its classification or payload.
            if old['payload']!=payload:raise BoundaryError('Legacy projection changed; explicit new revision required')
            return old
        return c.put(kind,payload,identifier=identity,reason='Additive located discovery projection; original artifacts preserved')
    from agent_research_intelligence.research.catalog import Question,Source,Evidence
    q=put('question',Question(problem=question['original'],current_context=json.dumps(load('project-receipt.json'),ensure_ascii=False),
        constraints=['READ / ANALYZE / PROPOSE only','Private context remains local'],need_to_learn=[question['original']],
        search_terms=brief['local_terms'],context_basis=[basis('project-receipt.json')]).model_dump(mode='json'),'question')
    for i,s in enumerate(sources):
        origin='DEVELOPMENT_SEED' if s['discovery_origin'].startswith('DEVELOPMENT_') else 'TOOL_DISCOVERY' if s['discovery_origin'].startswith('TOOL_') else 'HISTORICAL_REGISTRY'
        source=Source(url=s['url'],entity_name=s.get('title') or s['url'],platform=urlsplit(s['url']).hostname or 'UNKNOWN',
            kind=s['kind'] if s['kind'] in ('github','paper','feed','video','standard','engineering_blog','research_lab') else 'web',
            topics=s.get('assessment',{}).get('topic_terms',[]),status='CANDIDATE',source_provenance=origin,
            historical_utility={'observed_events':s.get('history_utility',[]),'human_usefulness':'UNKNOWN'},
            basis=[basis('sources.json',f'[{i}]')],limitations=['Historical registry observation, not a user subscription or accepted evidence.'])
        by_id[s['id']]=put('source',source.model_dump(mode='json'),s['id'])
    for e in evidence:
        s=by_id[e['source_id']];docname=e['source_id']+'.document.json';doc=load(docname)
        item=Evidence(rq_id=q['id'],source_id=s['id'],source_revision=s['revision'],published_at=None,
            captured_at=next(x.get('last_verified') for x in sources if x['id']==e['source_id']) or 'UNKNOWN',
            claim=e['text'],locator={'native':e['locator'],'local':e['local_locator'],'version':e['version']},
            evidence_type='CODE' if doc['kind']=='github' else 'PAPER' if doc['kind']=='paper' else 'SOURCE_ASSERTION',
            source_quality={'status':'UNASSESSED','legacy_source':e['source_id']},relevance={'status':'KEYWORD_SCREENED'},
            novelty={'status':'UNASSESSED'},impact={'status':'UNASSESSED'},confidence='LOW',limitations=[e['limits']],
            provenance=[basis(docname,e['local_locator']),basis('evidence.json',e['id'])],classification=classification)
        eids.append(put('evidence',item.model_dump(mode='json'),e['id'])['id'])
    path='data/catalog/imports/'+job+'.json'
    if (ws.root/path).exists():return json.loads(ws.read(path))
    dossier=build(ws,q['id'],evidence_ids=eids,source_ids=[r['id'] for r in by_id.values()])
    result={'job':job,'classification':classification,'catalog_rq':q['id'],'catalog_sources':{k:v['id'] for k,v in by_id.items()},
        'evidence_ids':eids,'dossier':dossier['artifact'],'dossier_id':dossier['id'],
        'legacy_dossier':summary['dossier'],'original_artifacts_modified':False,
        'limits':'Projection adds explicit UNKNOWN fields; it does not create new research or upgrade historical claims.'}
    ws.write(path,canonical(result));return result


def project_collection(ws,job,batch,*,classification='REAL'):
    """Project a bounded supplementary collection, retaining its original lineage."""
    if not re.fullmatch('[a-f0-9]{32}',batch) or classification not in ('REAL','REUSED_REAL','FIXTURE'):
        raise BoundaryError('Explicit collection identity/classification required')
    parent=project_discovery(ws,job,classification=classification);c=Catalog(ws)
    prefix='data/discovery/'+job+'/collections/'+batch
    raw=ws.read(prefix+'/result.json');report=json.loads(raw)
    path='data/catalog/imports/'+job+'-'+batch+'.json'
    if (ws.root/path).exists():
        saved=json.loads(ws.read(path))
        if saved['collection_sha256']!=hashlib.sha256(raw).hexdigest():raise BoundaryError('Collection input changed')
        from agent_research_intelligence.research.catalog import Basis
        for eid in saved['evidence_ids']:c.validate_basis([Basis.model_validate(b) for b in c.read(eid)['payload']['provenance']])
        return saved
    q=c.read(parent['catalog_rq']);terms=q['payload']['search_terms'];eids=[]
    for result in report['results']:
        if result['status']!='FETCHED':continue
        source=c.read(parent['catalog_sources'][result['source_id']])
        for document in [result['document']]+([result['pdf_document']] if result.get('pdf_document') else []):
            if not document.startswith(prefix+'/'):raise BoundaryError('Collection document outside owned batch')
            body=ws.read(document);doc=json.loads(body)
            spans=[(i,line) for i,line in enumerate(doc['lines'],1) if line.strip() and any(t.casefold() in line.casefold() for t in terms)][:4]
            for i,line in spans:
                evidence=c.put('evidence',dict(rq_id=q['id'],source_id=source['id'],source_revision=source['revision'],
                    published_at=doc.get('published_at'),captured_at=report['at'],claim=line,
                    locator={'artifact':document,'line':i,'url':doc['locator'],'version':doc['version'],'source_revision_kind':'registry profile; content version separately pinned'},
                    evidence_type='CODE' if doc['kind']=='github' else 'PAPER' if doc['kind']=='paper' else 'STANDARD' if doc['kind']=='standard' else 'SOURCE_ASSERTION',
                    source_quality={'status':'UNASSESSED'},relevance={'status':'KEYWORD_SCREENED'},novelty={'status':'UNASSESSED'},impact={'status':'UNASSESSED'},
                    confidence='LOW',limitations=['Located excerpt, not reviewed technical truth.'],
                    provenance=[{'artifact':document,'sha256':hashlib.sha256(body).hexdigest(),'locator':'lines['+str(i-1)+']','classification':classification}],
                    classification=classification),reason='Supplementary discovery collection projection')
                eids.append(evidence['id'])
    all_eids=parent['evidence_ids']+eids
    dossier=build(ws,q['id'],evidence_ids=all_eids,source_ids=list(parent['catalog_sources'].values()))
    output={'job':job,'batch':batch,'classification':classification,'collection_sha256':hashlib.sha256(raw).hexdigest(),
        'evidence_ids':eids,'dossier':dossier['artifact'],'parent_dossier_id':parent['dossier_id'],'original_artifacts_modified':False}
    ws.write(path,canonical(output));return output
