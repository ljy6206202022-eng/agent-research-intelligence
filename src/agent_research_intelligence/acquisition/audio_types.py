"""Original-timeline ASR/diarization merging, with no identity inference."""
from dataclasses import dataclass, asdict
import math

from agent_research_intelligence.governance.paths import BoundaryError


def interval(start, end):
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in (start, end)) or start < 0 or end <= start:
        raise BoundaryError('Invalid audio interval')


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str
    probability: float | None = None

    def __post_init__(self):
        if self.start == self.end:
            interval(self.start, self.end + .000001)
        else:
            interval(self.start, self.end)
        if not isinstance(self.text, str) or not self.text.strip():
            raise BoundaryError('Empty word')
        if self.probability is not None and (not math.isfinite(self.probability) or not 0 <= self.probability <= 1):
            raise BoundaryError('Invalid probability')


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    label: int

    def __post_init__(self):
        interval(self.start, self.end)
        if type(self.label) is not int or not 0 <= self.label < 100:
            raise BoundaryError('Expected recording-local anonymous cluster')


def merge(words, turns, *, offset_s, duration_s):
    """Map local times to source times once. Ambiguous/overlapping words stay unknown.

    A word is assigned only when one speaker covers at least 60% and no second
    speaker covers 20%. This is a merge policy, NOT a calibrated confidence.
    Diarization model probabilities are unavailable and remain None.
    """
    interval(offset_s, offset_s + duration_s)
    if list(words) != sorted(words, key=lambda x: x.start):
        raise BoundaryError('Words must be ordered')
    for item in [*words, *turns]:
        if item.end > duration_s + .1:
            raise BoundaryError('Provider time outside normalized audio')
    labels = {}
    for turn in sorted(turns, key=lambda x: x.start):
        if turn.label not in labels:
            index = len(labels)
            labels[turn.label] = 'Speaker ' + (chr(65 + index) if index < 26 else str(index + 1))
    result = []
    for word in words:
        coverage = {}
        for turn in turns:
            overlap = max(0, min(word.end, turn.end) - max(word.start, turn.start))
            if overlap:
                coverage.setdefault(turn.label, []).append((max(word.start, turn.start), min(word.end, turn.end)))
        # Union intervals from the same cluster so duplicate turns cannot inflate coverage.
        scores = []
        for label, spans in coverage.items():
            total = 0.; last = -1.
            for start, end in sorted(spans):
                total += max(0, end - max(start, last)); last = max(last, end)
            scores.append((total / (word.end - word.start), label))
        scores.sort(reverse=True)
        competing = len(scores) > 1 and scores[1][0] >= .2
        assigned = labels[scores[0][1]] if scores and scores[0][0] >= .6 and not competing else None
        result.append({**asdict(word), 'start': round(offset_s + word.start, 4),
                       'end': round(offset_s + word.end, 4), 'speaker_id': assigned,
                       'speaker_name': None, 'speaker_confidence': None,
                       'alignment_status': 'ZERO_LENGTH_WORD' if word.start == word.end else 'ALIGNED',
                       'speaker_status': 'AMBIGUOUS_OR_OVERLAP' if competing else 'ASSIGNED' if assigned else 'UNKNOWN'})
    return {'authority': 'EXTERNAL_EVIDENCE', 'transcript_source': 'asr_generated',
            'identity_resolution': 'NOT_PERFORMED', 'quality': 'UNASSESSED',
            'timeline': 'ORIGINAL_VIDEO', 'source_offset_s': offset_s, 'duration_s': duration_s,
            'speaker_turns': [{'start': round(offset_s+t.start,4), 'end': round(offset_s+t.end,4),
                               'speaker_id': labels[t.label], 'speaker_name': None, 'confidence': None} for t in turns],
            'words': result}
