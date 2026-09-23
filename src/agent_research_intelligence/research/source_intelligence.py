"""Inspectable source evidence and conservative relationships, not truth by vote."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import math
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


def identity(url):
    p=urlsplit(url)
    query=urlencode([(k,v) for k,v in parse_qsl(p.query) if not k.startswith('utm_') and k not in ('ref','source')])
    return urlunsplit((p.scheme,p.netloc.lower(),p.path.rstrip('/'),query,''))


def source_id(url): return hashlib.sha256(identity(url).encode()).hexdigest()[:24]


def known_coverage(records, terms):
    matches=[r for r in records if r.get('assessment',{}).get('topic_terms')]
    acquired=[r for r in matches if r.get('last_verified')]
    kinds=sorted({r['kind'] for r in acquired})
    found={t for r in acquired for t in r['assessment']['topic_terms']}
    missing=[t for t in terms if t not in found]
    independent=any(r.get('cross_validation')=='INDEPENDENT_SUPPORT_REVIEWED' for r in acquired)
    reasons=[]
    if len(kinds)<3:reasons.append('Fewer than three acquired source types')
    if missing:reasons.append('Uncovered research terms (screening signal, not semantic proof)')
    if not independent:reasons.append('No reviewed independent support; source same-publisher evidence does not supply this')
    return {'known_records':len(records),'matched_ids':[r['id'] for r in matches],
            'acquired_types':kinds,'uncovered_terms':missing,'independent_support':independent,
            'needs_discovery':bool(reasons),'reasons':reasons}


def assess(title, text, terms, *, fetched, now):
    lower=(title+' '+text).casefold(); hits=[t for t in terms if t.casefold() in lower]
    return {'policy':'discovery-source-assessment-v1','at':now,'topic_terms':hits,
            'topic_match':len(hits)/max(1,len(terms)), 'acquisition_verified':fetched,
            'authority':'UNASSESSED','historical_utility':None,'validation_count':None,
            'maintenance':'UNKNOWN','decision':'RETAIN_CANDIDATE' if hits else 'EXCLUDE_TOPIC_MISMATCH',
            'limits':'Keyword relevance is screening only, not authority or factual correctness.'}


def decay(last_verified, at, *, half_life_days=180):
    if last_verified is None: return {'freshness':None,'state':'UNVERIFIED','policy':'discovery-decay-v1'}
    if half_life_days<=0: raise ValueError('Positive half-life required')
    days=max(0,(datetime.fromisoformat(at)-datetime.fromisoformat(last_verified)).total_seconds()/86400)
    return {'age_days':days,'freshness':2**(-days/half_life_days),
            'state':'REVIEW_DUE' if days>=half_life_days else 'RECENT',
            'policy':'discovery-decay-v1','half_life_days':half_life_days,
            'meaning':'Acquisition freshness only; never auto-revokes or grants truth/subscription.'}


def relationships(sources):
    edges=[]
    for i,a in enumerate(sources):
        for b in sources[i+1:]:
            reason=None;relation=None
            if identity(a.get('canonical',a['url']))==identity(b.get('canonical',b['url'])):
                relation,reason='SAME_ORIGIN','same canonical URL (publisher assertion when supplied)'
            elif a.get('text_sha256') and a.get('text_sha256')==b.get('text_sha256'):
                relation,reason='DUPLICATE','identical normalized acquired text'
            elif a.get('creator_id') and a.get('creator_id')==b.get('creator_id'):
                relation,reason='SAME_PUBLISHER','same evidenced creator identity; not independent verification'
            if relation:edges.append({'a':a['id'],'b':b['id'],'relation':relation,'basis':reason})
    return edges


def shared_passages(documents):
    """Exact substantial passage reuse; flags a span, never collapses whole publishers."""
    seen={};edges=[]
    for sid,doc in documents.items():
        for n,line in enumerate(doc['lines'],1):
            normalized=' '.join(re.findall(r'\w+',line.casefold()))
            if len(normalized.split())<20:continue
            key=hashlib.sha256(normalized.encode()).hexdigest()
            for other,line_no in seen.get(key,[]):
                if other!=sid:edges.append({'a':other,'a_line':line_no,'b':sid,'b_line':n,
                    'relation':'DUPLICATED_PASSAGE','normalized_sha256':key,
                    'independent_verification':False,'limits':'Exact span match; direction and entire-source independence require cited provenance.'})
            seen.setdefault(key,[]).append((sid,n))
    return edges


def compare_claims(claims, sources):
    """Explicit scoped propositions. No sentiment/substring contradiction guesses."""
    by_id={s['id']:s for s in sources}; edges=[]
    for i,a in enumerate(claims):
        for b in claims[i+1:]:
            if (a['proposition'],a['scope'])!=(b['proposition'],b['scope']):continue
            if not a.get('locator') or not b.get('locator'):raise ValueError('Located claims required')
            sa,sb=by_id[a['source_id']],by_id[b['source_id']]
            same=sa['id']==sb['id'] or any(e['relation'] in ('SAME_ORIGIN','DUPLICATE','SAME_PUBLISHER') for e in relationships([sa,sb]))
            if a['stance'].upper() in ('UNKNOWN','UNCERTAIN') or b['stance'].upper() in ('UNKNOWN','UNCERTAIN'):
                relation='UNRESOLVED'
            else:relation='CONFLICT' if a['stance']!=b['stance'] else 'SUPPORT'
            edges.append({'a':a['id'],'b':b['id'],'relation':relation,
                          'independence':'SAME_ORIGIN' if same else 'NOT_ESTABLISHED',
                          'limits':'Source statements compared; no automatic factual resolution.'})
    return edges


def shared_referenced_works(documents):
    entries=[];edges=[]
    for sid,doc in documents.items():
        urls=[doc['url']]+[x['url'] for x in doc.get('links',[])]
        works={m.group(1) for u in urls if (m:=re.search(r'arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})',u))}
        entries.append((sid,works))
    for i,(sid,works) in enumerate(entries):
        for other,theirs in entries[i+1:]:
            for work in sorted(works & theirs):edges.append({'a':sid,'b':other,'relation':'SHARED_REFERENCED_WORK',
                'work':'arxiv:'+work,'independent_verification':False,
                'limits':'Both reference the same paper; this does not establish identical authors or independent replication.'})
    return edges


def creator_record(url, source, *, api_identity=None, observed_links=()):
    """Cross-platform identities only via an observed owner profile link, never a nickname."""
    if any(not x.get('evidence_locator') or not x.get('url') for x in observed_links):
        raise ValueError('Cross-platform identity links need an observed evidence locator')
    return {'id':source_id(url),'primary_url':url,'identity_basis':source,
            'platform_id':api_identity,'linked_profiles':list(observed_links),
            'history_utility':None,'strengths':[],'weaknesses':[],
            'subscription':'NOT_REQUESTED','recommendation_only':True}
