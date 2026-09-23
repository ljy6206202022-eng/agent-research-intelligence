"""Cheap, inspectable screening signals. Unknown is not zero or authority.

Weights are a versioned budget ranking policy, not an accuracy/acceptance bar.
The reach contribution is capped at two percent of the full budget score.
"""
import math
import re

POLICY='frozen-source-screening-v1'
WEIGHTS={'task_relevance':.25,'authority_expertise':.10,'information_density':.15,
         'practical_evidence':.12,'historical_utility':.08,'cross_validation':.10,
         'freshness':.05,'code_data_benchmark':.10,'community_reach':.02,'marketing_hype_penalty':.03}
TECH=re.compile(r'\b(?:code|architecture|config(?:uration)?|benchmark|implementation|algorithm|dataset|evaluation|experiment|latency|token|context|agent|memory|handoff)\b|代码|架构|配置|实验|评测',re.I)
PROMO=re.compile(r'\b(?:sponsor(?:ed)?|subscribe|promo code|affiliate|limited.offer)\b|赞助|广告|优惠码',re.I)


def screen(metadata, terms, *, segments=None, verified_resources=None, observations=None):
    observations=observations or {}
    text=' '.join(str(metadata.get(k) or '') for k in ('title','description','channel','tags','chapters'))
    hits=[t for t in terms if t.casefold() in text.casefold()]
    dimensions={k:{'value':None,'basis':[],'confidence':'UNKNOWN'} for k in WEIGHTS}
    dimensions['task_relevance']={'value':len(hits)/max(1,len(terms)),
        'basis':[{'kind':'METADATA_KEYWORD_MATCH','terms':hits}], 'confidence':'LOW'}
    dimensions['code_data_benchmark']={'value':min(1,len(verified_resources)/3),
        'basis':list(verified_resources),'confidence':'MEDIUM'} if verified_resources else dimensions['code_data_benchmark']
    counts={'technical_s':None,'sampled_s':None,'advertising_s':None,'repeated_s':None,
            'code_architecture_demo_benchmark_mentions':None,'verified_external_links':len(verified_resources) if verified_resources is not None else None}
    if segments is not None:
        total=tech=ad=repeat=0.;seen=set();mentions=0
        for s in segments:
            duration=s.get('duration',max(0,s.get('end',0)-s.get('start',0)))
            if not math.isfinite(duration) or duration<0:raise ValueError('Invalid density interval')
            t=s['text'];norm=' '.join(t.casefold().split());total+=duration
            if TECH.search(t):tech+=duration;mentions+=len(TECH.findall(t))
            if PROMO.search(t):ad+=duration
            if norm in seen:repeat+=duration
            seen.add(norm)
        counts.update(technical_s=tech,sampled_s=total,advertising_s=ad,repeated_s=repeat,
                      code_architecture_demo_benchmark_mentions=mentions)
        if total:
            dimensions['information_density']={'value':tech/total,'basis':[counts], 'confidence':'LOW'}
            dimensions['marketing_hype_penalty']={'value':min(1,(ad+repeat)/total),'basis':[counts],'confidence':'LOW'}
    # Human/local semantic observations must carry locators, never fabricated history.
    for key, obs in observations.items():
        if key not in dimensions or not obs.get('basis') or obs.get('confidence') not in ('HIGH','MEDIUM','LOW','UNKNOWN'):
            raise ValueError('Scoring observation needs dimension, confidence and evidence')
        if obs.get('value') is not None and (isinstance(obs['value'],bool) or not math.isfinite(obs['value']) or not 0<=obs['value']<=1):
            raise ValueError('Score outside [0,1]')
        dimensions[key]=obs
    observed=sum(WEIGHTS[k] for k,v in dimensions.items() if v['value'] is not None)
    score=sum(WEIGHTS[k]*v['value']*(-1 if k=='marketing_hype_penalty' else 1)
              for k,v in dimensions.items() if v['value'] is not None)
    return {'policy':POLICY,'dimensions':dimensions,'known_weight':observed,'budget_score_lower_bound':score,
            'counts':counts,'unknown_dimensions':[k for k,v in dimensions.items() if v['value'] is None],
            'deep_analysis_calls':0,'tokens':0,
            'limitations':['Lexical technical/promotion indicators are screening proxies, not human content judgments.',
                          'Overlapping caption durations may overlap; ratios describe sampled caption spans, not unique video duration.',
                          'Unknown dimensions are unscored, not measured zero. Ranking is not trust or acceptance.']}


def creator_density(samples):
    if not 1<=len(samples)<=5:raise ValueError('One to five inspected samples required')
    return {'status':'SAMPLED' if len(samples)>=3 else 'INSUFFICIENT_EVIDENCE',
            'sample_count':len(samples),'samples':samples,
            'aggregate':None if len(samples)<3 else {
                'mean_observed_density':sum(s['dimensions']['information_density']['value'] or 0 for s in samples)/len(samples)
                if all(s['dimensions']['information_density']['value'] is not None for s in samples) else None},
            'claim':'Research-budget screening only; not a creator quality verdict'}
