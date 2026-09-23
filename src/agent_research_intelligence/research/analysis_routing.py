"""Versioned analysis budget decisions. No implicit cloud authorization."""
from agent_research_intelligence.governance.paths import BoundaryError

POLICY='frozen-analysis-routing-v1'


def route(*,changed,relevant,novel,complexity,risk,classification,available_models=(),max_cost=0):
    if complexity not in ('LOW','MEDIUM','HIGH') or risk not in ('LOW','MEDIUM','HIGH'):
        raise BoundaryError('Explicit complexity and risk required')
    if classification not in ('PUBLIC','PRIVATE') or max_cost<0:raise BoundaryError('Classified input and bounded budget required')
    result={'policy':POLICY,'model_calls':0,'tokens':0,'route':'RULES_ONLY','model':None,'escalation_reason':None}
    if not changed:return {**result,'reason':'NO_CHANGE'}
    if not relevant:return {**result,'reason':'NOT_RELEVANT_OR_UNCERTAIN'}
    if not novel:return {**result,'reason':'DUPLICATE_OR_UNCONFIRMED_NOVELTY'}
    tier='STRONG' if risk=='HIGH' or complexity=='HIGH' else 'MEDIUM' if complexity=='MEDIUM' else 'CHEAP'
    candidates=[m for m in available_models if m.get('tier')==tier and m.get('explicitly_authorized') is True
                and m.get('estimated_cost') is not None and 0<=m['estimated_cost']<=max_cost
                and (classification=='PUBLIC' or m.get('local') is True)]
    return {**result,'route':'MODEL_CANDIDATE_REQUIRES_EXECUTION_RECEIPT' if candidates else 'LOCAL_REVIEW_REQUIRED',
            'model':min(candidates,key=lambda m:m['estimated_cost'])['id'] if candidates else None,
            'requested_tier':tier,'escalation_reason':{'complexity':complexity,'risk':risk},
            'reason':'No invocation or permission change occurs in routing. Private context cannot select a cloud model.'}


def plan(task,*,changed=True,relevant=True,novel=True,classification='PRIVATE',available_models=(),max_cost=0):
    """Deterministic classification/complexity/risk estimates, not LLM reasoning."""
    kinds={'incremental_check':('LOW','LOW'),'metadata_screen':('LOW','LOW'),
           'evidence_extraction':('MEDIUM','LOW'),'cross_source_synthesis':('HIGH','MEDIUM'),
           'architecture_impact':('HIGH','HIGH')}
    if task not in kinds:raise BoundaryError('Unknown analysis task classification')
    complexity,risk=kinds[task]
    return {'task_class':task,'estimation_basis':'VERSIONED_TASK_RULES_NOT_OBSERVED_MODEL_QUALITY',
            **route(changed=changed,relevant=relevant,novel=novel,complexity=complexity,risk=risk,
                    classification=classification,available_models=available_models,max_cost=max_cost)}
