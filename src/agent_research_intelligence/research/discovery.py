"""discovery vertical slice: local coverage -> approved public discovery -> located dossier."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

from agent_research_intelligence.connectors.public_http import AcquisitionError
from agent_research_intelligence.connectors.research_documents import Documents, public_link
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import ClassifiedText, canonical, verify_policy
from agent_research_intelligence.governance.project_reader import read_project_file, PROJECT_STATE
from agent_research_intelligence.storage.database import Store
from agent_research_intelligence.storage.models import ResearchOutput
from .source_intelligence import assess, decay, source_id, relationships, creator_record, known_coverage

MAX_JOB_SOURCES = 300
# Keep the existing total budget while ensuring that a large historical registry
# cannot consume every slot before this question's public searches return.
MAX_REGISTRY_SOURCES = 260


def utc(): return datetime.now(timezone.utc).isoformat()


def code_hashes(workspace):
    """Record package code from a checkout or an installed wheel without local paths."""
    checkout = workspace.root / 'src/agent_research_intelligence'
    package = checkout if checkout.is_dir() else Path(__file__).resolve().parents[1]
    return {str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in package.rglob('*.py') if path.is_file() and not path.is_symlink()}


class DiscoveryResearch:
    def __init__(self, workspace, documents=None):
        self.ws=workspace
        if documents is None:
            from agent_research_intelligence.connectors.browser import Browser
            from agent_research_intelligence.acquisition.audio_runtime import Budget
            budget=None
            def guard():
                nonlocal budget
                import psutil
                if budget is None:budget=Budget(workspace,psutil)
                budget.check()
                if psutil.Process().memory_info().rss>=6*1024**3:raise AcquisitionError('RESEARCH_RSS_LIMIT')
            documents=Documents(browser=Browser(workspace),guard=guard)
        self.docs=documents

    def run(self, intake_path, brief_path, approval_path, seeds_path):
        verify_policy(self.ws)
        intake=json.loads(self.ws.read(intake_path));brief=json.loads(self.ws.read(brief_path))
        approval=json.loads(self.ws.read(approval_path));seeds=json.loads(self.ws.read(seeds_path))
        if (approval.get('authority')!='USER_AUTHORIZED_PUBLIC_RESEARCH' or
            approval.get('brief_sha256')!=hashlib.sha256(canonical(brief)).hexdigest() or
            brief.get('classification')!='PUBLIC' or not approval.get('source_message')):
            raise BoundaryError('Explicit approved public brief required; private intake never becomes search text')
        queries=brief['queries'];terms=brief['local_terms']
        if not (1<=len(queries)<=4) or any(q['provider'] not in ('github','arxiv','crossref') or not 1<=len(q['text'])<=300 for q in queries):
            raise BoundaryError('Bounded supported public queries required')
        with Store(self.ws) as store:
            job=uuid4().hex; prefix='data/discovery/'+job
            def save(name,obj):
                return str(self.ws.write(prefix+'/'+name,canonical(obj)).relative_to(self.ws.root))
            save('question.json',{'original':intake['original_question'],'classification':'PRIVATE','authority':'RESEARCH_INPUT_ONLY'})
            start_hashes=code_hashes(self.ws)
            save('code-at-start.json',start_hashes)
            save('public-brief.json',brief);save('public-approval.json',approval)
            project={'version':{'revision':None}}
            receipt={'status':'NOT_CONFIGURED','sha256':None}
            if PROJECT_STATE is not None:
                context,receipt=read_project_file(PROJECT_STATE)
                project=json.loads(context)
                self.ws.write(prefix+'/project-context.private.json',context)
            save('project-receipt.json',{**receipt,'revision':project.get('version',{}).get('revision')})
            records=[]; documents={};trace=[];creators={};seen=set();historical_known_ids=set()
            def register(candidate,origin,parent=None):
                url=public_link(candidate['url'],candidate['url'])
                if not url: return None
                sid=source_id(url)
                if sid in seen:
                    trace.append({'event':'ALREADY_KNOWN_URL','url':url,'origin':origin,'parent':parent});return None
                if origin.startswith('TOOL_') and sid in historical_known_ids:
                    trace.append({'event':'ALREADY_KNOWN_URL','url':url,'origin':origin,
                                  'parent':parent,'basis':'PERSISTENT_REGISTRY_NOT_IN_JOB_SHORTLIST'});return None
                if len(records)>=MAX_JOB_SOURCES:
                    trace.append({'event':'CANDIDATE_LIMIT','url':url,'origin':origin});return None
                seen.add(sid)
                r={**candidate,'id':sid,'url':url,'discovered_at':utc(),'discovery_origin':origin,
                   'discovery_parent':parent,'lifecycle':'CANDIDATE','subscription':'NOT_REQUESTED',
                   'watch':'RECOMMEND_ONLY','authority':'EXTERNAL_EVIDENCE','history_utility':[],
                   'assessment':assess(candidate.get('title',''),candidate.get('summary',''),terms,fetched=False,now=utc()),
                   'last_verified':None,'identity_status':'UNKNOWN'}
                if candidate.get('creator') and candidate.get('api_identity') is not None:
                    c=creator_record(candidate['creator'],candidate.get('discovery_locator',''),api_identity=candidate['api_identity']);creators[c['id']]=c;r['creator_id']=c['id'];r['identity_status']='PLATFORM_API_ID'
                records.append(r);return r
            for seed in seeds:
                register(seed,'DEVELOPMENT_INPUT_REUSED_M1')
            # Explicit current seed metadata takes precedence over earlier acquisition notes.
            # Search the tool's previously collected public registry locally, never send it wholesale.
            source_root=self.ws.root/'data/sources'
            known_versions=[];known_probe=[]
            if source_root.exists():
                for directory in sorted(source_root.iterdir())[:1000]:
                    if directory.is_symlink() or not directory.is_dir():continue
                    files=sorted(directory.glob('*.json'),key=lambda p:p.stat().st_mtime)
                    if not files:continue
                    old=json.loads(self.ws.read(str(files[-1].relative_to(self.ws.root))))
                    url=public_link(old.get('url',''),old.get('url',''))
                    if not url:continue
                    sid=source_id(url)
                    historical_known_ids.add(sid)
                    if sid in seen:continue
                    assessment=assess(old.get('title',''),old.get('summary',''),terms,fetched=False,now=utc())
                    known_probe.append({'id':sid,'kind':old['kind'],'last_verified':old.get('last_verified'),
                                        'assessment':assessment,'cross_validation':old.get('cross_validation')})
                    known_versions.append((old,files[-1],len(assessment['topic_terms'])))
            coverage=known_coverage(records+known_probe,terms)
            # A sufficiently covered question retains the full historical job
            # budget. Only an actual open-discovery branch reserves new slots.
            registry_cap=MAX_REGISTRY_SOURCES if coverage['needs_discovery'] else MAX_JOB_SOURCES
            registry_slots=max(0,registry_cap-len(records))
            known_versions.sort(key=lambda row:(-row[2],-int(bool(row[0].get('last_verified'))),row[0].get('url','')))
            if len(known_versions)>registry_slots:
                trace.append({'event':'REGISTRY_JOB_BUDGET','available':len(known_versions),
                              'selected':registry_slots,
                              'reserved_for_new':MAX_JOB_SOURCES-len(records)-registry_slots,
                              'basis':'Current-question topic hits then verified acquisition; no registry deletion'})
            for old,version,_topic_hits in known_versions[:registry_slots]:
                r=register(old,'REUSED_SOURCE_REGISTRY',str(version.relative_to(self.ws.root)))
                if r:
                    for key in ('last_verified','history_utility','cross_validation'):
                        if key in old:r[key]=old[key]
            save('coverage-before.json',coverage)
            metadata_graph_candidates=[]
            for query in queries if coverage['needs_discovery'] else []:
                try:
                    fn=getattr(self.docs,{'github':'search_github','arxiv':'search_papers','crossref':'search_crossref'}[query['provider']])
                    response,candidates=fn(ClassifiedText(text=query['text'],classification='PUBLIC'),10)
                    n=len(trace);raw=self.ws.write(prefix+f'/search-{n}.raw',response.body)
                    trace.append({'event':'TOOL_SEARCH','provider':query['provider'],'query':query['text'],
                                  'url':response.url,'raw':str(raw.relative_to(self.ws.root)),
                                  'sha256':hashlib.sha256(response.body).hexdigest(),'candidate_count':len(candidates)})
                    for c in candidates:
                        parent=register(c,'TOOL_SEARCH',response.url)
                        if parent and c.get('kind')=='paper':
                            from agent_research_intelligence.connectors.research_documents import related_metadata_sources
                            related,graph=related_metadata_sources(c);parent['metadata_graph']=graph
                            metadata_graph_candidates.extend((linked,parent['id']) for linked in related)
                except (AcquisitionError,BoundaryError,ValueError,KeyError) as exc:
                    trace.append({'event':'DISCOVERY_FAILED','provider':query['provider'],'query':query['text'],'reason':str(exc)})
            # Direct search hits from every provider get a chance before
            # references generated by any one paper consume the reserved slots.
            for linked,parent_id in metadata_graph_candidates:
                register(linked,'TOOL_METADATA_GRAPH',parent_id)
            # Metadata screen first; known seeds remain eligible to expose their linked evidence.
            queue=[r for r in records if r['assessment']['decision']=='RETAIN_CANDIDATE']
            # Prioritize diversity without result-dependent quality cherry-picking.
            queue=sorted(queue,key=lambda r:({'paper':0,'web':1,'feed':2,'github':3,'video':4}.get(r['kind'],5),-r['assessment']['topic_match']))
            attempts=0;per_kind={};evidence=[]
            for r in queue:
                if attempts>=18: break
                kind=r['kind']
                if kind=='video':
                    r['fetch_state']='HISTORICAL_CAPTION_AVAILABLE_NOT_REFETCHED';continue
                if per_kind.get(kind,0)>=6:continue
                attempts+=1;per_kind[kind]=per_kind.get(kind,0)+1
                try:
                    doc=self.docs.fetch(r['url'],kind=kind)
                    raw=doc.pop('raw');self.ws.write(prefix+'/'+r['id']+'.raw',raw)
                    doc['raw_sha256']=hashlib.sha256(raw).hexdigest()
                    save(r['id']+'.document.json',doc);documents[r['id']]=doc
                    text='\n'.join(doc['lines']);r.update({'kind':doc['kind'],'title':doc['title'],
                        'canonical':doc['canonical'],'content_sha256':doc['raw_sha256'],
                        'text_sha256':hashlib.sha256(' '.join(text.split()).encode()).hexdigest(),
                        'version':doc['version'],'last_verified':utc(),'fetch_state':'FETCHED',
                        'assessment':assess(doc['title'],text,terms,fetched=True,now=utc())})
                    for line_no,line in enumerate(doc['lines'],1):
                        if sum(t.casefold() in line.casefold() for t in terms)>=2:
                            locator=doc['locator']+('#L'+str(line_no) if kind=='github' else '#local-extracted-line-'+str(line_no))
                            e={'source_id':r['id'],'id':r['id']+'-L'+str(line_no),'text':line[:1800],
                               'locator':locator,'local_locator':prefix+'/'+r['id']+'.document.json:lines['+str(line_no-1)+']',
                               'version':doc['version'],'claim_status':'SOURCE_ASSERTION_UNVERIFIED',
                               'limits':'Local extracted line locator is snapshot-relative, not a claimed native web anchor.'}
                            e['store_id']=store.append(ResearchOutput(kind='evidence',classification='PUBLIC',text=json.dumps(e),source_refs=(prefix+'/'+r['id']+'.document.json',)))
                            evidence.append(e)
                            if sum(x['source_id']==r['id'] for x in evidence)>=4:break
                    # Follow artifact pointers using the same connector, bounded and screened.
                    for link in doc['links']:
                        u=link['url'];h=urlsplit(u).hostname or '';path=urlsplit(u).path.lower()
                        inferred='paper' if h.endswith('arxiv.org') or path.endswith('.pdf') else 'feed' if any(s in path for s in ('feed','rss','.atom')) else 'github' if h=='github.com' and len(path.strip('/').split('/'))==2 else 'web'
                        if not any(t.casefold() in u.casefold() for t in terms) and inferred not in ('paper','feed'):continue
                        c=register({'url':u,'kind':inferred,'title':u,'summary':'Document link; content unverified','discovery_locator':doc['locator'],'link_relation':link.get('rel','')},'TOOL_DOCUMENT_LINK',r['id'])
                        if c and len(queue)<40:queue.append(c)
                except (AcquisitionError,BoundaryError,ValueError,KeyError,ImportError) as exc:
                    r['fetch_state']='FAILED';r['failure']=str(exc)
            for r in records:
                r['decay']=decay(r['last_verified'],utc());r.setdefault('fetch_state','NOT_FETCHED_BUDGET_OR_SCREEN')
                r['history_utility'].append({'job':job,'at':utc(),'evidence_items':sum(e['source_id']==r['id'] for e in evidence),
                                              'human_usefulness':'NOT_REVIEWED','is_first_observation':not bool(r['history_utility'])})
                self.ws.write('data/sources/'+r['id']+'/'+job+'.json',canonical(r))
            edges=relationships(records)
            save('sources.json',records);save('creators.json',list(creators.values()));save('discovery-trace.json',trace)
            save('relationships.json',edges);save('evidence.json',evidence)
            types=sorted({next(r['kind'] for r in records if r['id']==e['source_id']) for e in evidence})
            summary={'job':job,'state':'REVIEW_REQUIRED','authority':'RESEARCH_OUTPUT','classification':'PRIVATE',
                     'source_records':len(records),'new_candidates':sum(r['discovery_origin'].startswith('TOOL_') for r in records),
                     'evidence_types':types,'evidence_count':len(evidence),'project_revision':project.get('version',{}).get('revision'),
                     'source_sufficiency':'INSUFFICIENT' if len(types)<3 else 'DIVERSITY_PRESENT_SUPPORT_REQUIRES_REVIEW',
                     'dossier':prefix+'/dossier.md','cloud_calls':0,'account_mutations':0}
            summary['code_unchanged_during_run']=bool(start_hashes) and code_hashes(self.ws)==start_hashes
            text=['# Multi-source research draft: '+intake['original_question'],'',
                  'Classification PRIVATE; authority RESEARCH_OUTPUT. Machine-screened and extracted; semantic review pending.',
                  '## Research question',intake['original_question'],'## Source coverage',json.dumps(coverage,ensure_ascii=False),
                  '## Located evidence']
            for e in evidence:text.extend(['',f"- {e['id']} [{e['source_id']}]({e['locator']}): {e['text']}"])
            text.extend(['','## Conflicts and limitations','Keyword similarity does not prove semantic agreement. Conflict status: NOT_ASSESSED; inspect the underlying source passages.',
                         'See relationships.json for source/duplicate links and sources.json for fetch failures, omissions, and exclusions. Independent outcome verification is pending.',
                         '## Local architecture mapping',f"Optional project context: revision {project.get('version',{}).get('revision')}; SHA-256 {receipt.get('sha256')}.",
                         'Local implications require separate analysis. Source recommendations cannot change authority, permissions, or accepted state.',
                         '## Conclusion','RESEARCH FURTHER. This draft is neither an accepted decision nor authorization to implement.'])
            self.ws.write(summary['dossier'],'\n'.join(text).encode())
            summary['dossier_store_id']=store.append(ResearchOutput(kind='dossier',classification='PRIVATE',text='\n'.join(text),source_refs=(summary['dossier'],prefix+'/sources.json',prefix+'/project-receipt.json')))
            save('result.json',summary)
            return summary

    def collect_candidates(self, job, ids, *, include_pdf=False):
        """A local reviewer can select tool-discovered candidates without pretending new discovery."""
        with Store(self.ws):
            return self._collect_candidates(job,ids,include_pdf=include_pdf)

    def _collect_candidates(self, job, ids, *, include_pdf=False):
        started=time.monotonic()
        code_at_start=code_hashes(self.ws)
        if len(job)!=32 or any(c not in '0123456789abcdef' for c in job) or not 1<=len(ids)<=6:
            raise BoundaryError('One existing job and at most six candidates required')
        sources=json.loads(self.ws.read('data/discovery/'+job+'/sources.json'))
        by_id={r['id']:r for r in sources}
        if any(sid not in by_id for sid in ids):raise BoundaryError('Candidate was not discovered by the referenced job')
        batch=uuid4().hex;prefix='data/discovery/'+job+'/collections/'+batch
        results=[]
        for sid in ids:
            r=by_id[sid]
            directory=self.ws.root/'data/sources'/sid
            if directory.exists():
                previous=sorted(directory.glob('*.json'),key=lambda p:p.stat().st_mtime)
                if previous:r=json.loads(self.ws.read(str(previous[-1].relative_to(self.ws.root))))
            try:
                doc=self.docs.fetch(r['url'],kind=r['kind']);raw=doc.pop('raw')
                doc.update({'raw_sha256':hashlib.sha256(raw).hexdigest(),'source_id':sid,
                            'discovery_origin':r['discovery_origin'],'discovery_parent':r['discovery_parent'],
                            'selection':'LOCAL_REVIEWER_SELECTED_FROM_TOOL_CANDIDATES','acquired_at':utc()})
                self.ws.write(prefix+'/'+sid+'.raw',raw)
                self.ws.write(prefix+'/'+sid+'.document.json',canonical(doc))
                results.append({'source_id':sid,'status':'FETCHED','document':prefix+'/'+sid+'.document.json','kind':doc['kind']})
                r.update({'last_verified':doc['acquired_at'],'fetch_state':'FETCHED','version':doc['version'],
                          'canonical':doc['canonical'],'content_sha256':doc['raw_sha256'],
                          'text_sha256':hashlib.sha256(' '.join(doc['lines']).encode()).hexdigest(),
                          'latest_document':prefix+'/'+sid+'.document.json'})
                if include_pdf and r['kind']=='paper':
                    links=[x['url'] for x in doc['links'] if '/pdf/' in x['url'] or x['url'].endswith('.pdf')]
                    if links:
                        try:
                            pdf=self.docs.fetch(links[0],kind='paper');raw_pdf=pdf.pop('raw')
                            pdf['raw_sha256']=hashlib.sha256(raw_pdf).hexdigest();pdf['parent_locator']=doc['locator'];pdf['source_id']=sid
                            self.ws.write(prefix+'/'+sid+'.pdf',raw_pdf);self.ws.write(prefix+'/'+sid+'.pdf.json',canonical(pdf))
                            results[-1]['pdf_document']=prefix+'/'+sid+'.pdf.json'
                        except (AcquisitionError,BoundaryError,ValueError,KeyError,ImportError) as exc:
                            results[-1]['pdf_failure']=str(exc)
            except (AcquisitionError,BoundaryError,ValueError,KeyError,ImportError) as exc:
                results.append({'source_id':sid,'status':'FAILED','reason':str(exc)})
            r.setdefault('history_utility',[]).append({'at':utc(),'batch':batch,'event':'CANDIDATE_COLLECTION',
                'outcome':results[-1]['status'],'human_usefulness':'NOT_REVIEWED'})
            self.ws.write('data/sources/'+sid+'/'+batch+'.json',canonical(r))
        report={'job':job,'batch':batch,'results':results,'authority':'EXTERNAL_EVIDENCE',
                'code_at_start':code_at_start,'code_unchanged_during_run':bool(code_at_start) and code_hashes(self.ws)==code_at_start,
                'elapsed_s':time.monotonic()-started,'at':utc(),
                'transport_trace':getattr(getattr(self.docs,'http',None),'trace',[])}
        self.ws.write(prefix+'/result.json',canonical(report));return report
