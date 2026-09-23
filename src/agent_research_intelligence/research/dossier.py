"""Question-specific dossiers and explicit unresolved conflicts over located evidence."""
import hashlib
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import canonical
from agent_research_intelligence.research.catalog import Catalog

FIELDS=('Problem','Current Context','Sources Searched','Evidence Summary','Main Approaches',
        'Conflicting Evidence','Architecture Mapping','Risks','Recommended Status','Explicit Non-Recommendation')


def architecture_map(evidence, capabilities):
    text=evidence['claim'].casefold();matches=[]
    for c in capabilities:
        if not c.get('id') or not c.get('basis') or not c.get('terms'):raise BoundaryError('Located local capability description required')
        hits=[t for t in c['terms'] if t.casefold() in text]
        if hits:matches.append({'capability':c['id'],'matched_terms':hits,'context_basis':c['basis'],
            'impact':'CANDIDATE_RELEVANCE_NOT_IMPLEMENTATION_RECOMMENDATION',
            'owner':c.get('owner','UNKNOWN'),'existing_overlap':c.get('existing_overlap','UNKNOWN'),
            'permission_change':'NONE','canonical_write':'NONE','memory_write':'NONE',
            'migration_risk':'UNASSESSED','recommended_action':'REVIEW_LOCATED_EVIDENCE'})
    return {'mappings':matches,'status':'CANDIDATE_MAPPING' if matches else 'UNKNOWN',
            'limits':'Term matching locates review targets; it does not establish architectural applicability.'}


def conflict_case(a,b,*,hypothesis,experiment):
    for side in (a,b):
        if not side.get('claim') or not side.get('supporting_evidence') or not side.get('limitations'):
            raise BoundaryError('Each conflict side needs claim, evidence and limitations')
    if not hypothesis or not experiment:raise BoundaryError('Conflict hypothesis and resolving experiment required')
    return {'claim_a':a,'claim_b':b,'conflict_hypothesis':hypothesis,'resolving_experiment':experiment,
            'status':'UNRESOLVED','resolution_method':'NO_MAJORITY_VOTE','experiment_run':False}


def build(workspace,rq_id,*,evidence_ids,source_ids,analysis=None,identifier=None,expected_revision=0):
    c=Catalog(workspace);rq=c.read(rq_id)
    if rq['kind']!='question':raise BoundaryError('Dossier question required')
    evidence=[c.read(i) for i in evidence_ids];sources=[c.read(i) for i in source_ids]
    if any(e['kind']!='evidence' or e['payload']['rq_id']!=rq_id for e in evidence):raise BoundaryError('Cross-RQ evidence denied')
    if any(s['kind']!='source' for s in sources):raise BoundaryError('Dossier sources required')
    known={s['id'] for s in sources}
    if any(e['payload']['source_id'] not in known for e in evidence):raise BoundaryError('Missing dossier source')
    analysis=analysis or {}
    if set(analysis)-{'main_approaches','conflicts','mapping','risks','review_basis'}:raise BoundaryError('Unknown analysis field')
    if analysis and not analysis.get('review_basis'):raise BoundaryError('Semantic review needs actual local provenance')
    if analysis.get('review_basis'):
        from agent_research_intelligence.research.catalog import Basis
        c.validate_basis([Basis(**b) for b in analysis['review_basis']])
    if 'conflicts' in analysis:
        if not isinstance(analysis['conflicts'],list):raise BoundaryError('Explicit conflict cases must be a list; empty means none established')
        cases=[]
        for item in analysis['conflicts']:
            case=conflict_case(item['claim_a'],item['claim_b'],hypothesis=item['conflict_hypothesis'],experiment=item['resolving_experiment'])
            for side in (case['claim_a'],case['claim_b']):
                if not set(side['supporting_evidence'])<=set(evidence_ids):raise BoundaryError('Conflict evidence must belong to this dossier')
            cases.append(case)
        analysis={**analysis,'conflicts':cases}
    payload={'rq_id':rq_id,'question_revision':rq['revision'],'Problem':rq['payload']['problem'],
        'Current Context':rq['payload']['current_context'],
        'Sources Searched':[{'id':s['id'],'revision':s['revision'],'url':s['payload']['url'],
                            'provenance':s['payload']['source_provenance']} for s in sources],
        'Evidence Summary':[{'id':e['id'],'revision':e['revision'],**e['payload']} for e in evidence],
        'Main Approaches':analysis.get('main_approaches',{'status':'UNREVIEWED','located_source_claims':[e['payload']['claim'] for e in evidence]}),
        'Conflicting Evidence':analysis.get('conflicts',{'status':'NOT_ESTABLISHED','note':'Absence of a recorded conflict is not proof of agreement.'}),
        'Architecture Mapping':analysis.get('mapping',{'status':'UNASSESSED','related_capabilities':rq['payload']['related_capabilities']}),
        'Risks':analysis.get('risks',['Extractive research output; source claims may be wrong or inapplicable.']),
        'Recommended Status':'USER_REVIEW_REQUIRED' if analysis else 'EXTRACTIVE_DOSSIER_REQUIRES_REVIEW',
        'Explicit Non-Recommendation':'Do not implement automatically. External evidence is not an Accepted Decision.',
        'review_basis':analysis.get('review_basis',[]),'production_writes':False}
    result=c.put('dossier',payload,identifier=identifier,expected_revision=expected_revision,reason='Build located research dossier')
    # Markdown is a view of the immutable record, not an alternate authority.
    import json
    parts=['# Research Dossier',f"Record: {result['id']} revision {result['revision']}"]
    for field in FIELDS:
        value=payload[field]
        parts.extend(['',f'## {field}','',value if isinstance(value,str) else '```json\n'+json.dumps(value,ensure_ascii=False,indent=2)+'\n```'])
    path=f"data/catalog/dossiers/{result['id']}/v{result['revision']}.md"
    workspace.write(path,('\n'.join(parts)+'\n').encode())
    return {**result,'artifact':path}
