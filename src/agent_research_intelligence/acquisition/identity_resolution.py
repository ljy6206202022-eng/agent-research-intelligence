"""Evidence-only speaker names. Metadata rosters alone never map a voice."""
import re


def resolve_speakers(turns, segments, roster, *, visual_bindings=()):
    """Return a sidecar; do not modify raw turns, labels or transcript.

    roster: [{name, basis}] from observed title/description/source profile.
    visual bindings require an actual reviewed name-bar + speaker link record;
    mere frame existence is insufficient.
    """
    labels={t['speaker_id'] for t in turns}
    result=[]
    for label in sorted(labels):
        candidates=[]
        for i,s in enumerate(segments):
            if s.get('speaker_id')!=label or s.get('assignment_status') not in ('ASSIGNED',None):continue
            start,end=s['start'],s.get('end',s['start']+s.get('duration',0))
            active=[t for t in turns if min(end,t['end'])>max(start,t['start'])]
            if {t['speaker_id'] for t in active}!={label}:continue
            for person in roster:
                name=person.get('name','').strip()
                if not name or not person.get('basis'):continue
                pattern=r"(?:\bmy name is\s+|\bi am\s+|\bi'm\s+|我是\s*)"+re.escape(name)+r'(?!\w)'
                if re.search(pattern,s['text'],re.I):
                    candidates.append({'name':name,'basis':{'kind':'SELF_INTRODUCTION_TEXT_WITH_UNAMBIGUOUS_SEGMENT',
                        'segment_index':i,'start':start,'end':end,'exact_text':s['text'],'roster':person['basis']},
                        'confidence':'LOW','limitation':'Caption/ASR text and speaker association are not human acoustic identity confirmation.'})
        for binding in visual_bindings:
            if (binding.get('speaker_id')==label and binding.get('review_source')
                    and binding.get('reviewed_at') and binding.get('frame_basis')
                    and binding.get('explicit_speaker_link') is True and binding.get('name')):
                candidates.append({'name':binding['name'],'basis':binding,'confidence':'MEDIUM',
                                   'limitation':'Limited to the reviewed visual/time association; not voiceprint identification.'})
        names={c['name'].casefold() for c in candidates}
        chosen=candidates[0] if len(names)==1 else None
        result.append({'speaker_id':label,'speaker_name':chosen['name'] if chosen else None,
            'speaker_name_confidence':chosen['confidence'] if chosen else 'UNKNOWN',
            'attribution_basis':[c['basis'] for c in candidates],
            'status':'EVIDENCED_CANDIDATE' if chosen else 'CONFLICTING_IDENTITY_EVIDENCE' if names else 'INSUFFICIENT_EVIDENCE',
            'HUMAN_VERIFIED':False,'voiceprint_used':False,
            'limitations':[c['limitation'] for c in candidates] or ['Roster membership alone does not identify an anonymous speaker.']})
    return {'identity_resolution':'EVIDENCE_SIDECAR','speakers':result,'raw_segments_modified':False}
