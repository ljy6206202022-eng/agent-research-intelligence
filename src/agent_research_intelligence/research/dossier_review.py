"""Bind a local semantic review to immutable source snapshots; never promote authority."""
import hashlib
import json
from datetime import datetime,timezone
from uuid import uuid4

from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import canonical
from agent_research_intelligence.storage.database import Store
from agent_research_intelligence.storage.models import ResearchOutput
from .source_intelligence import compare_claims, shared_passages, shared_referenced_works


def build_review(workspace, review_path):
    review=json.loads(workspace.read(review_path))
    job=review['job']
    if len(job)!=32 or any(c not in '0123456789abcdef' for c in job):raise BoundaryError('Invalid research job')
    prefix='data/discovery/'+job
    sources=json.loads(workspace.read(prefix+'/sources.json'));by_id={s['id']:s for s in sources}
    selected=[];documents={}
    for item in review['evidence']:
        sid=item['source_id'];path=item['document']
        if sid not in by_id or not path.startswith(prefix+'/'):raise BoundaryError('Evidence must come from this discovery job')
        raw=workspace.read(path);doc=json.loads(raw)
        documents[sid]=doc
        if hashlib.sha256(raw).hexdigest()!=item['document_sha256']:raise BoundaryError('Reviewed document drift')
        start,end=item['lines'];lines=doc['lines']
        if not 1<=start<=end<=len(lines):raise BoundaryError('Evidence locator outside source')
        selected.append({**item,'kind':doc['kind'],'source_url':doc['url'],'version':doc['version'],
                         'excerpt_sha256':hashlib.sha256('\n'.join(lines[start-1:end]).encode()).hexdigest(),
                         'source_assertion_verified':'LOCATION_ONLY_NOT_FACTUAL_TRUTH'})
    for claim in review['claims']:
        if not any(e['id']==claim['evidence_id'] and e['source_id']==claim['source_id'] for e in selected):
            raise BoundaryError('Claim lacks selected evidence')
    relations=compare_claims(review['claims'],sources)+shared_passages(documents)+shared_referenced_works(documents)
    version=uuid4().hex;out=prefix+'/reviews/'+version
    text=[f"# {review['title']}",'',
          '分类：PRIVATE。Authority：RESEARCH_OUTPUT。分析来源：Codex 对封存原文的本地语义审阅；不是工具自主事实裁定、人工实验复现或 Project 实施授权。',
          '',review['answer'],'','## 关键结论与原文定位']
    for e in selected:
        text.extend(['',f"- **{e['id']} — {e['finding']}** [{e['kind']} 原来源]({e['locator']})；固定版本 `{e['version']}`。本地 `{e['document']}` lines {e['lines'][0]}–{e['lines'][1]}。{e['limits']}"])
    for title,key in [('发现、筛选与排除','screening'),('同源、支持、冲突与不足','cross_validation'),('与当前 Project 的对应','architecture_mapping'),('建议与适用限制','recommendation')]:
        text+=['','## '+title,review[key]]
    text+=['','## 状态','RESEARCH FURTHER / USER REVIEW。来源一致不等于因果证明；本轮不改变主线、不安装来源代码或 Skill。']
    with Store(workspace) as store:
        workspace.write(out+'/evidence.json',canonical(selected));workspace.write(out+'/claim-relationships.json',canonical(relations))
        workspace.write(out+'/review-input.json',canonical(review));workspace.write(out+'/dossier.md','\n'.join(text).encode())
        ids=[]
        for e in selected:
            ids.append(store.append(ResearchOutput(kind='evidence',classification='PUBLIC',text=json.dumps(e),source_refs=(e['document'],))))
        dossier_id=store.append(ResearchOutput(kind='dossier',classification='PRIVATE',text='\n'.join(text),source_refs=(out+'/dossier.md',prefix+'/project-receipt.json')))
        for sid in {e['source_id'] for e in selected}:
            r=dict(by_id[sid]);directory=workspace.root/'data/sources'/sid
            if directory.exists():
                previous=sorted(directory.glob('*.json'),key=lambda p:p.stat().st_mtime)
                if previous:r=json.loads(workspace.read(str(previous[-1].relative_to(workspace.root))))
            r.setdefault('history_utility',[]).append({'at':datetime.now(timezone.utc).isoformat(),
                'event':'CITED_IN_DOSSIER','dossier':out+'/dossier.md','evidence_ids':[e['id'] for e in selected if e['source_id']==sid],
                'actor':'CODEX_LOCAL_SEMANTIC_REVIEW','user_usefulness':'NOT_REVIEWED'})
            workspace.write('data/sources/'+sid+'/'+version+'.json',canonical(r))
        receipt={'dossier':out+'/dossier.md','dossier_store_id':dossier_id,'evidence_store_ids':ids,
                 'evidence_types':sorted({e['kind'] for e in selected}),'authority':'RESEARCH_OUTPUT','classification':'PRIVATE',
                 'analysis_origin':'CODEX_LOCAL_SEMANTIC_REVIEW','human_verified_experiment':False,
                 'source_snapshots_verified':True,'input_sha256':hashlib.sha256(workspace.read(review_path)).hexdigest()}
        workspace.write(out+'/receipt.json',canonical(receipt))
        return receipt
