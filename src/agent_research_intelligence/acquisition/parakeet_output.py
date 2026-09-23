"""Lossless native-output view; no word correction, sorting or automatic omissions."""
import copy,math,re,unicodedata
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.acquisition.joint_audio import merge_joint
from agent_research_intelligence.acquisition.community1 import adapt

def normalized_words(text):
 text=unicodedata.normalize('NFKC',text).lower()
 return ''.join(' ' if unicodedata.category(c).startswith('P') else c for c in text).split()

def adapt_parakeet(raw,identity):
    segments=[];lineage=[]
    for si,s in enumerate(raw['sentences']):
        spans=[];text=''
        for ti,t in enumerate(s['tokens']):
            a=len(text);text+=t['text'];spans.append((a,len(text),ti,t))
        assert normalized_words(text)==normalized_words(s['text']), 'LEXICAL_TOKEN_SENTENCE_MISMATCH'
        words=[]
        for wi,m in enumerate(re.finditer(r'\s*\S+',text)):
            # Whitespace is retained in each word string. Time comes from content-bearing tokens.
            a=m.start()+len(m.group())-len(m.group().lstrip());b=m.end()
            tokens=[(ti,t) for x,y,ti,t in spans if x<b and y>a]
            assert tokens
            word={'word':m.group(),'start':min(t['start'] for _,t in tokens),'end':max(t['end'] for _,t in tokens),'probability':None}
            words.append(word);lineage.append({'segment_index':si,'word_index':wi,'raw_sentence_index':si,'raw_token_indices':[ti for ti,_ in tokens],'raw_character_range':[m.start(),m.end()],'raw_character_range_source':'concatenated original token text','time_method':'envelope of contributing original tokens; not forced alignment'})
        if words:
            tail=text[sum(len(w['word']) for w in words):];assert not tail.strip();words[-1]['word']+=tail
        assert ''.join(w['word'] for w in words)==text
        segments.append({'source_sentence_text':s['text'],'text':text,'start':s['start'],'end':s['end'],'words':words})
    text=''.join(s['text'] for s in segments)
    assert normalized_words(text)==normalized_words(raw['text']), 'LEXICAL_RESULT_MISMATCH'
    return {'provider':'parakeet-mlx','classification':'DERIVED_RAW_TOKEN_WORD_VIEW_WITH_LEXICAL_PARITY','input':identity,'text':raw['text'],'token_stream_text':text,'segments':segments,'word_timing_source':'Grouped original model subword intervals','HUMAN_VERIFIED':False},lineage

def lexical_interface_view(asr):
 result=copy.deepcopy(asr);kept=[];sidecar=[];i=0
 for s in result['segments']:
  words=[]
  for w in s['words']:
   # Never discard a symbol, number, negation, partial word or unknown text.
   # Punctuation is retained verbatim as metadata with its original position/time.
   if not normalized_words(w['word']) and all(c.isspace() or unicodedata.category(c).startswith('P') for c in w['word']):
    sidecar.append({'raw_word_index':i,**w,'status':'NONLEXICAL_PUNCTUATION_PRESERVED_NO_SPEAKER_ASSIGNMENT'})
   else:words.append(w);kept.append(i)
   i+=1
  s['words']=words
 before=''.join(w['word'] for s in asr['segments'] for w in s['words']);after=''.join(w['word'] for s in result['segments'] for w in s['words'])
 assert normalized_words(before)==normalized_words(after)
 assert len(kept)+len(sidecar)==i
 return result,kept,sidecar
def make_output(raw, identity, source, profile, classification, pair=None, mode='ordinary'):
    try: view,lineage=adapt_parakeet(raw,identity)
    except (AssertionError,KeyError,TypeError,ValueError) as e:
        raise BoundaryError('PARAKEET_NATIVE_LEXICAL_PARITY_FAILED') from e
    lexical,indices,punctuation=lexical_interface_view(view)
    words=[w for s in view['segments'] for w in s['words']]
    duration=identity['frames']/identity['sample_rate'];offset=source['offset_s']
    bad=[i for i,w in enumerate(words) if normalized_words(w['word']) and (not all(math.isfinite(w[k]) for k in ('start','end')) or not 0<=w['start']<=w['end']<=duration+.1)]
    backward=[indices[i] for i in range(1,len(indices)) if words[indices[i]]['start']<words[indices[i-1]]['start']]
    if pair is not None:
        if pair.get('input_sha256')!=identity['normalized_wav_sha256'] or pair.get('source_offset_s')!=offset:
            raise BoundaryError('DIARIZATION_IDENTITY_OR_OFFSET_MISMATCH')
        if mode=='exclusive' and not pair.get('same_output_object'):
            raise BoundaryError('EXCLUSIVE_REQUIRES_SAME_RUN_NORMAL_AND_EXCLUSIVE')
        if pair.get('reference_mapping_used') is not False:raise BoundaryError('REFERENCE_SPEAKER_MAPPING_DENIED')
        if not set(t['speaker_id'] for t in pair.get('exclusive',[]))<=set(t['speaker_id'] for t in pair['normal']):
            raise BoundaryError('EXCLUSIVE_LABEL_DOMAIN_MISMATCH')
    joint=None
    if pair is not None and not bad and not backward:
        if mode=='exclusive' and 'exclusive' not in pair:raise BoundaryError('EXCLUSIVE_NOT_AVAILABLE')
        joint=merge_joint(lexical,adapt(pair['normal' if mode=='ordinary' else 'exclusive'],duration_s=duration),source={'offset_s':offset,'duration_s':duration})
    by_index={i:j for i,j in zip(indices,joint['words'])} if joint else {}
    output=[]
    for i,(word,origin) in enumerate(zip(words,lineage)):
        overlap=[]
        if pair:
            overlap=[{'raw_normal_segment_index':j,**turn} for j,turn in enumerate(pair['normal']) if min(word['end'],turn['end'])>max(word['start'],turn['start'])]
        assigned=by_index.get(i,{})
        speaker=assigned.get('speaker_id');state=assigned.get('speaker_status','UNKNOWN' if pair else 'NOT_RUN')
        if mode=='exclusive' and speaker and not any(t['speaker_id']==speaker for t in overlap):speaker=None;state='UNKNOWN'
        output.append({'raw_word_index':i,'text':word['word'],'native_start':word['start'],'native_end':word['end'],'start':offset+word['start'],'end':offset+word['end'],'lineage':origin,'speaker_id':speaker,'speaker_status':state,'speaker_name':None,'speaker_confidence':None,'ordinary_segment_evidence':overlap,'assignment_basis':mode+'_CANDIDATE' if pair else 'NOT_RUN','time_anomaly':i in bad or i in backward})
    kind=source.get('material_type','UNKNOWN')
    limits=['ASR生成，未经逐字核对；不是已验证直接引语。','数字、否定、术语、纠正及资料提示未经人工确认；不得自动补写或纠正。','原生时间并非本任务人工时间精度验收。']
    if kind=='ENGLISH_TECHNICAL_LECTURE':limits.append('仅为受限英文技术讲解候选；资料类型标签不赋予本任务质量PASS。')
    elif kind=='COMPLEX_OVERLAPPING_MEETING':limits.append('复杂多人重叠会议超出受限版本已验证用途；整体文字质量未验证，已测AMI历史未达标。')
    elif kind=='NORMAL_CONVERSATION':limits.append('多人轮流交流的整体质量尚未独立验收。')
    else:limits.append('资料类型UNKNOWN；未假定属于已验证用途。')
    if source.get('source_id','').startswith('IS1009a'):limits.append('该AMI开发输入历史完整WER=23.376623%，未达到20%；本片段/复用不更改历史结论。')
    result={'authority':'EXTERNAL_EVIDENCE','transcript_source':'asr_generated','quality':'UNASSESSED','HUMAN_VERIFIED':False,'source':source,'input':identity,'profile_id':profile['id'],'model_revision':profile['model']['revision'],'model_source':profile['model']['repository'],'asr_classification':classification,'material_type':kind,'limitations':limits,'raw_text':raw['text'],'words':output,'nonlexical_punctuation_sidecar':punctuation,'anomalies':{'lexical_invalid_or_outside':bad,'lexical_start_backwards':backward,'punctuation_times_not_repaired':True},'diarization_status':'NOT_RUN' if pair is None else 'CANDIDATE_ASSOCIATION' if joint else 'UNKNOWN_INVALID_ASR_TIMING','diarization_raw':pair,'offset_applied_once':True,'word_lineage':lineage}
    reading={'quality':'ASR_GENERATED_UNVERIFIED','source_raw_words':'transcript.json#/words','omissions':[],'no_automatic_filtering':True,'rows':[]}
    cursor=0
    for si,s in enumerate(view['segments']):
        n=len(s['words']);reading['rows'].append({'raw_sentence_index':si,'word_indices':list(range(cursor,cursor+n)),'native_sentence_text':s['source_sentence_text'],'start':offset+s['start'],'end':offset+s['end'],'text':s['source_sentence_text'],'speaker_ids':list(dict.fromkeys(w['speaker_id'] for w in output[cursor:cursor+n] if w['speaker_id'])),'speaker_note':'NOT_RUN' if pair is None else 'CANDIDATE_NOT_HUMAN_VERIFIED'});cursor+=n
    assert cursor==len(words)
    return result,reading


def reading_markdown(transcript, reading):
    lines=['# 可追溯阅读版','',*transcript['limitations'],'','本版仅按原生句子排版，省略项为0；词项与标点原始来源见 reading.json / transcript.json。','']
    for row in reading['rows']:
        label=','.join(row['speaker_ids']) if row['speaker_ids'] else ('说话人未运行' if row['speaker_note']=='NOT_RUN' else 'UNKNOWN')
        # Preserve sentence content; escaping presentation markup does not alter raw artifacts.
        text=row['text'].replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
        lines.append(f"[{row['start']:.3f}–{row['end']:.3f}] {label} | {text}\n")
    return '\n'.join(lines)
