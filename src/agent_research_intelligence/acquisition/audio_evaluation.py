"""Fixed audio quality measures. References must be independent human annotations."""
import re
import unicodedata

from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.acquisition.audio_types import interval


def error_rate(reference, hypothesis, *, unit='word'):
    def normalize(text):
        text=unicodedata.normalize('NFKC',text).lower()
        text=''.join(' ' if unicodedata.category(c).startswith('P') else c for c in text)
        if unit=='word': return text.split()
        if unit=='character': return [c for c in text if not c.isspace()]
        raise BoundaryError('Unknown text comparison unit')
    ref,hyp=normalize(reference),normalize(hypothesis)
    if not ref: raise BoundaryError('Empty ground truth')
    row=list(range(len(hyp)+1))
    for i,left in enumerate(ref,1):
        next_row=[i]
        for j,right in enumerate(hyp,1):
            next_row.append(min(row[j]+1,next_row[-1]+1,row[j-1]+(left!=right)))
        row=next_row
    return {'rate':row[-1]/len(ref),'edit_errors':row[-1],'reference_units':len(ref),'unit':unit,
            'normalization':'NFKC lowercase punctuation-to-space; whitespace word split or character units'}


# Compatibility import; DER mathematics lives only in the pinned mature evaluator.
from agent_research_intelligence.acquisition.der_evaluation import diarization_error


def validate_reference(record, *, audio_sha256):
    if record.get('provenance')!='INDEPENDENT_HUMAN_ANNOTATION' or not record.get('reviewer'):
        raise BoundaryError('Provider output / fixture is not real accuracy ground truth')
    if not re.fullmatch(r'[a-f0-9]{64}',audio_sha256) or record.get('audio_sha256')!=audio_sha256:
        raise BoundaryError('Ground truth must bind to the evaluated source audio')
    if record.get('reference_created_from_model_output') is not False:
        raise BoundaryError('Independent annotation required')
