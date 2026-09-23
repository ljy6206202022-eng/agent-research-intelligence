"""Versioned safety envelope. No runtime privilege-upgrade API."""
from enum import StrEnum
import hashlib
import ipaddress
import json
import socket
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from .paths import BoundaryError, Workspace


class DataClass(StrEnum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"


class ClassifiedText(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    classification: DataClass

    @classmethod
    def derive(cls, text: str, inputs: list["ClassifiedText"]):
        if not inputs:
            raise BoundaryError("Derived content needs provenance")
        classification = DataClass.PRIVATE if any(x.classification == DataClass.PRIVATE for x in inputs) else DataClass.PUBLIC
        return cls(text=text, classification=classification)


POLICY = {
    "version": "0.1.0-alpha",
    "authority": "LOCAL_RESEARCH_ONLY",
    "source_accumulation": "recommend_only",
    "p3_effect": "BLOCKING_REVIEW_REQUIRED",
    "gates": {
        "cloud": False, "model_download": False, "authenticated_read": False,
        "desktop": False, "account_mutation": False, "canary": False,
        "ambient_browser": False, "system_automation": False,
    },
    "limits": {
        "active_questions": 1, "http_concurrency": 4, "per_host": 2,
        "llm_concurrency": 1, "heavy_jobs": 1, "cpu_threads": 2,
        "browser_process_groups": 1, "browser_pages": 2,
        "rss_warning_gib": 4, "rss_stop_gib": 6, "free_memory_min_gib": 6,
        "disk_gib": 30, "models_gib": 8, "media_gib": 2,
        "artifacts_gib": 8, "logs_mib": 512, "free_disk_min_gib": 20,
    },
}


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


POLICY_HASH = hashlib.sha256(canonical(POLICY)).hexdigest()


def verify_policy(workspace: Workspace) -> None:
    actual = json.loads(workspace.read("config/policies/safety.json"))
    if hashlib.sha256(canonical(actual)).hexdigest() != POLICY_HASH:
        raise BoundaryError("Policy drift: stop and request independent architecture review")


def require_public(payload: ClassifiedText) -> None:
    if payload.classification != DataClass.PUBLIC:
        raise BoundaryError("Private data cannot cross the research network boundary")


def authorize(action: str, *, payload: ClassifiedText | None = None) -> None:
    if action not in {"public_read", "local_read", "local_analyze", "local_propose"}:
        raise BoundaryError("Action is outside the enabled read/analyze/propose policy")
    if action == "public_read":
        if payload is None:
            raise BoundaryError("Explicit classified input required")
        require_public(payload)


def validate_public_url(url: str, *, resolver=socket.getaddrinfo) -> str:
    """Preflight only; any future transport must also pin/check connected addresses."""
    try:
        p = urlsplit(url)
        if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in {None, 443}:
            raise BoundaryError("Only public HTTPS reads on port 443 are allowed")
        if p.hostname.lower().rstrip(".") in {"localhost", "metadata.google.internal"}:
            raise BoundaryError("Local/metadata destinations denied")
        records = resolver(p.hostname, 443, type=socket.SOCK_STREAM)
        if not records:
            raise BoundaryError("No destination addresses")
        for record in records:
            address = ipaddress.ip_address(record[4][0].split("%")[0])
            if not address.is_global or getattr(address, "ipv4_mapped", None) is not None:
                raise BoundaryError("Non-public destination denied")
    except (ValueError, OSError) as exc:
        raise BoundaryError("URL validation failed") from exc
    return url
