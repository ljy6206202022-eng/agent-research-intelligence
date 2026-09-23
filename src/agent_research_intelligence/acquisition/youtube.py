"""Structured public-page adapter; explicit failure if provider shape changes.

Official Data API authentication is not enabled. This adapter never treats a
blocked page as proof that captions are absent and never downloads audio.
"""
from __future__ import annotations
import html
import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field

from agent_research_intelligence.connectors.public_http import AcquisitionError, PublicHTTP
from agent_research_intelligence.governance.paths import BoundaryError


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    start: float = Field(ge=0)
    duration: float = Field(ge=0)
    text: str = Field(min_length=1)
    speaker_id: str | None = None
    speaker_name: None = None


class Video(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    video_id: str
    title: str
    channel_id: str
    description: str
    published: str | None = None
    transcript_source: str
    language: str
    segments: tuple[Segment, ...]
    caption_sha256: str | None = None
    acquisition: str = "public_player_response_and_caption_track"


def video_id(url: str) -> str:
    p = urlsplit(url)
    if p.scheme != "https" or p.username or p.password or p.port not in {None, 443}:
        raise BoundaryError("Invalid YouTube URL")
    if p.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        value = parse_qs(p.query).get("v", [""])[0] if p.path == "/watch" else p.path.removeprefix("/shorts/") if p.path.startswith("/shorts/") else ""
    elif p.hostname == "youtu.be":
        value = p.path.lstrip("/")
    else:
        raise BoundaryError("A public YouTube video URL is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise BoundaryError("Invalid video ID")
    return value


def player_response(page: str) -> dict:
    for marker in ("ytInitialPlayerResponse =", "ytInitialPlayerResponse=", '"ytInitialPlayerResponse":'):
        position = page.find(marker)
        if position >= 0:
            try:
                value, _ = json.JSONDecoder().raw_decode(page[position + len(marker):].lstrip())
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                pass
    raise AcquisitionError("PLAYER_RESPONSE_UNAVAILABLE")


def parse_captions(raw: bytes) -> tuple[Segment, ...]:
    if not raw.strip():
        raise AcquisitionError("EMPTY_CAPTION_RESPONSE", phase="CAPTION_ACQUISITION")
    segments = []
    if raw.lstrip().startswith(b"{"):
        try:
            for event in json.loads(raw).get("events", []):
                text = "".join(x.get("utf8", "") for x in event.get("segs", [])).strip()
                if text:
                    segments.append(Segment(start=event["tStartMs"] / 1000,
                                            duration=event.get("dDurationMs", 0) / 1000, text=text))
        except (ValueError, KeyError, TypeError) as exc:
            raise AcquisitionError("CAPTION_PARSE_FAILED") from exc
    else:
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise AcquisitionError("UNSAFE_CAPTION_XML")
        try:
            root = ET.fromstring(raw)
            for item in root.iter():
                if item.tag not in {"text", "p"}:
                    continue
                start = float(item.get("start", "0")) if item.tag == "text" else float(item.get("t", "0")) / 1000
                duration = float(item.get("dur", "0")) if item.tag == "text" else float(item.get("d", "0")) / 1000
                text = html.unescape("".join(item.itertext())).strip()
                if text:
                    segments.append(Segment(start=start, duration=duration, text=text))
        except (ET.ParseError, ValueError) as exc:
            raise AcquisitionError("CAPTION_PARSE_FAILED") from exc
    if not segments:
        raise AcquisitionError("EMPTY_CAPTION_RESPONSE")
    return tuple(sorted(segments, key=lambda s: s.start))


def description_links(description: str, segments: tuple[Segment, ...]) -> dict:
    links = []
    seen = set()
    for match in re.finditer(r"https?://[^\s<>\"\u3000]+", description):
        url = html.unescape(match.group().rstrip(".,;!。，；！)）]"))
        p = urlsplit(url)
        if url in seen:
            continue
        seen.add(url)
        host = (p.hostname or "").lower()
        category = "github" if host == "github.com" else "gist" if host == "gist.github.com" else "paper" if host in {"arxiv.org", "doi.org", "www.semanticscholar.org"} else "web"
        # Non-host classifications describe claims in the URL, not verified artifacts.
        if category == "web":
            for term in ("dataset", "benchmark", "skill", "plugin", "demo", "docs"):
                if term in p.path.lower():
                    category = term
                    break
        links.append({"url": url, "description_offset": match.start(), "category": category, "handoff": "NOT_RUN"})
    # "Tool descriptions" is not a spoken link reference. Match contextual
    # link cues, including adjacent caption segments, and retain their timing.
    cue_pattern = re.compile(r"(?:\b(?:github|code|repo(?:sitory)?|paper|slides|link)\b.{0,70}\b(?:description|below)\b|\b(?:video|the)\s+description\b.{0,40}\b(?:link|repo|github|code)\b|\b(?:in|check|see)\s+(?:the\s+)?description\b|(?:链接|代码|论文).{0,20}(?:简介|描述|下面)|简介.{0,20}(?:链接|代码|论文))", re.I)
    cues = []
    seen_cues = set()
    for index, segment in enumerate(segments):
        window = [s for s in segments[index:index + 3] if s.start - segment.start <= 10]
        text = " ".join(s.text for s in window)
        match = cue_pattern.search(text)
        if not match:
            continue
        offset = 0
        matched = segment
        for candidate in window:
            if match.start() < offset + len(candidate.text):
                matched = candidate
                break
            offset += len(candidate.text) + 1
        if matched.start != segment.start:
            continue  # Process at its own segment, retaining following context.
        if matched.start not in seen_cues:
            cues.append({"start": matched.start, "text": text[match.start():match.end()], "context": text[match.start():]})
            seen_cues.add(matched.start)
    github_links = [link for link in links if link['category'] == 'github']
    for link in github_links:
        parts = urlsplit(link['url']).path.strip('/').split('/')
        repo_words = re.sub(r'[-_]+', ' ', parts[1].removesuffix('.git')).lower() if len(parts) >= 2 else ''
        references = []
        for cue in cues:
            context = cue['context'].lower()
            basis = None
            if repo_words and re.search(r'\b' + re.escape(repo_words) + r'\b', context):
                basis = 'EXPLICIT_REPOSITORY_NAME'
            elif len(github_links) == 1 and re.search(r'\bgithub\b', context):
                basis = 'UNIQUE_GITHUB_URL_WITH_EXPLICIT_GITHUB_CUE'
            if basis:
                references.append({**cue, 'match_basis': basis})
        link['spoken_references'] = references
    return {"links": links, "spoken_references": cues,
            "status": "DESCRIPTION_URLS_FOUND" if links else "UNRESOLVED_EXTERNAL_REFERENCE" if cues else "NO_EXTERNAL_REFERENCE"}


class YouTube:
    def __init__(self, http: PublicHTTP):
        self.http = http
        self.trace = []
        self.caption_body = None

    def _stage(self, stage, state, **fields):
        self.trace.append({"stage": stage, "state": state, **fields})

    @staticmethod
    def _track(data, languages):
        tracks = data.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
        if not tracks:
            raise AcquisitionError("NO_CAPTIONS_AUDIO_GATE_REQUIRED", phase="CAPTION_DISCOVERY")
        if not isinstance(tracks, list) or any(not isinstance(t, dict) or not isinstance(t.get('baseUrl'), str) or not isinstance(t.get('languageCode'), str) for t in tracks):
            raise AcquisitionError('CAPTION_TRACK_METADATA_INVALID', phase="CAPTION_DISCOVERY")
        matching = [t for t in tracks if any(t.get("languageCode", "").startswith(x) for x in languages)]
        if not matching:
            raise AcquisitionError("REQUESTED_CAPTION_LANGUAGE_UNAVAILABLE", phase="CAPTION_DISCOVERY")
        return sorted(matching, key=lambda t: (t.get("kind") == "asr", next(i for i, x in enumerate(languages) if t.get("languageCode", "").startswith(x))))[0]

    def fetch(self, url: str, *, languages=("zh", "en")) -> Video:
        self.trace = []
        self.caption_body = None
        try:
            return self._fetch(url, languages=languages)
        except (AcquisitionError, BoundaryError) as exc:
            self._stage("failure", "FAILED", code=getattr(exc, "code", "BOUNDARY_DENIED"), phase=getattr(exc, "phase", None))
            raise

    def _fetch(self, url, *, languages):
        identifier = video_id(url)
        self._stage("selected_video", "SELECTED", video_id=identifier)
        self._stage("watch_page", "STARTED")
        page = self.http.get(f"https://www.youtube.com/watch?v={identifier}")
        self._stage("watch_page", "COMPLETE", status=page.status, bytes=len(page.body), sha256=hashlib.sha256(page.body).hexdigest())
        data = player_response(page.body.decode("utf-8", errors="replace"))
        if data.get("playabilityStatus", {}).get("status") != "OK":
            raise AcquisitionError("VIDEO_UNAVAILABLE_OR_AUTH_REQUIRED")
        details = data.get("videoDetails", {})
        if details.get("videoId") != identifier or "shortDescription" not in details:
            raise AcquisitionError("INCOMPLETE_VIDEO_METADATA")
        self._stage("metadata", "COMPLETE", title=details.get("title"), channel_id=details.get("channelId"), creator=details.get("author"))
        self._stage("description", "COMPLETE", via="SHARED_WATCH_PAGE", characters=len(details["shortDescription"]))
        self._stage("caption_discovery", "STARTED")
        track = self._track(data, languages)
        self._stage("caption_discovery", "COMPLETE", language=track["languageCode"], kind=track.get("kind", "manual"))
        self._stage("caption_acquisition", "STARTED", provider="web_player")
        captions = self.http.get(track["baseUrl"])
        acquisition = "public_player_response_and_caption_track"
        if not captions.body.strip():
            self._stage("caption_acquisition", "EMPTY_RESPONSE", provider="web_player", status=captions.status, bytes=len(captions.body))
            # One bounded public-player fallback, as used by the caption provider.
            # Never ask for user sessions, consent cookies, browser or audio here.
            key = re.search(r'"INNERTUBE_API_KEY":\s*"([A-Za-z0-9_-]{1,200})"', page.body.decode("utf-8", errors="replace"))
            if key is None:
                raise AcquisitionError("EMPTY_CAPTION_RESPONSE", phase="CAPTION_ACQUISITION")
            self._stage("caption_provider_fallback", "STARTED", provider="public_android_player")
            response = self.http.read_youtube_player(identifier, key.group(1))
            try:
                alternative = json.loads(response.body)
                if alternative.get("playabilityStatus", {}).get("status") != "OK":
                    raise AcquisitionError("CAPTION_PROVIDER_ACCESS_REQUIRED", phase="CAPTION_DISCOVERY")
                if alternative.get("videoDetails", {}).get("videoId") != identifier:
                    raise AcquisitionError("CAPTION_PROVIDER_VIDEO_MISMATCH", phase="CAPTION_DISCOVERY")
                track = self._track(alternative, languages)
            except (ValueError, TypeError, AttributeError) as exc:
                raise AcquisitionError("CAPTION_PROVIDER_INVALID_RESPONSE", phase="CAPTION_DISCOVERY") from exc
            captions = self.http.get(track["baseUrl"])
            acquisition = "public_web_metadata_android_player_caption_track"
            self._stage("caption_provider_fallback", "COMPLETE", status=captions.status, bytes=len(captions.body))
        self._stage("caption_acquisition", "COMPLETE" if captions.body.strip() else "EMPTY_RESPONSE", status=captions.status,
                    bytes=len(captions.body), sha256=hashlib.sha256(captions.body).hexdigest())
        self._stage("transcript_parsing", "STARTED")
        segments = parse_captions(captions.body)
        self.caption_body = captions.body
        self._stage("transcript_parsing", "COMPLETE", segments=len(segments))
        return Video(video_id=identifier, title=details.get("title", ""), channel_id=details.get("channelId", ""),
                     description=details["shortDescription"],
                     published=data.get("microformat", {}).get("playerMicroformatRenderer", {}).get("publishDate"),
                     transcript_source="auto_caption" if track.get("kind") == "asr" else "manual_caption",
                     language=track["languageCode"], segments=segments, acquisition=acquisition,
                     caption_sha256=hashlib.sha256(captions.body).hexdigest())
