"""Core types: organisation numbers, evidence, claims, envelope helpers.

Standard library only. Validation lives in the constructors so an unsupported
claim cannot be built, let alone published.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

AGENT_VERSION = "1.2.0"
STATES = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}
WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)
_ORG_IN_TEXT = re.compile(r"(?<!\d)(\d{3})[\s. ]?(\d{3})[\s. ]?(\d{3})(?!\d)")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise(raw: Any) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def is_valid(raw: Any) -> bool:
    s = normalise(raw)
    if len(s) != 9 or s == "000000000":
        return False
    rem = sum(int(d) * w for d, w in zip(s[:8], WEIGHTS)) % 11
    check = 0 if rem == 0 else 11 - rem
    return check < 10 and check == int(s[8])


def find_orgnums(text: str) -> set[str]:
    out = set()
    for m in _ORG_IN_TEXT.finditer(text or ""):
        cand = "".join(m.groups())
        if is_valid(cand):
            out.add(cand)
    return out


@dataclass
class Evidence:
    id: str
    source_url: str
    source_class: str          # official_registry | open_data_api | company_owned
    source_policy: str
    retrieved_at: str
    content_sha256: str
    claim_span: str
    selector: str | None = None
    extractor: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    publication_date: str | None = None
    reporting_period: str | None = None


@dataclass
class EntityLink:
    organisation_number: str
    method: str
    score: float
    matched_on: list[str] = field(default_factory=list)


@dataclass
class Claim:
    claim_key: str
    field_family: str
    field: str
    availability: str
    value: Any = None
    confidence: float = 0.0
    evidence_ids: list[str] = field(default_factory=list)
    entity_link: EntityLink | None = None
    alternatives: list[dict] = field(default_factory=list)
    unit: str | None = None
    period: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.availability not in STATES:
            raise ValueError(f"{self.claim_key}: invalid state {self.availability!r}")
        if self.availability == "available":
            if self.value in (None, "", [], {}):
                raise ValueError(f"{self.claim_key}: available with empty value")
            if not self.evidence_ids:
                raise ValueError(f"{self.claim_key}: available without evidence")
            if self.entity_link is None:
                raise ValueError(f"{self.claim_key}: available without entity link")
        else:
            if isinstance(self.value, (int, float)) and not isinstance(self.value, bool):
                raise ValueError(f"{self.claim_key}: absence rendered as a number")
            if not self.note:
                raise ValueError(f"{self.claim_key}: absence without explanation")


class Ctx:
    """Collects claims and evidence for one company."""

    def __init__(self, org: str):
        self.org = org
        self.claims: list[Claim] = []
        self.evidence: list[Evidence] = []
        self._n = 0

    def ev(self, fetch, cls: str, policy: str, selector: str, span: str,
           extractor: str, pub: str | None = None, period: str | None = None,
           url: str | None = None) -> str:
        self._n += 1
        eid = f"ev{self._n}"
        self.evidence.append(Evidence(
            id=eid, source_url=url or fetch.url, source_class=cls,
            source_policy=policy, retrieved_at=fetch.retrieved_at,
            content_sha256=fetch.content_sha256 or "none",
            claim_span=(span or "")[:400] or "(no text span)", selector=selector,
            extractor=extractor, final_url=fetch.final_url,
            http_status=fetch.status, publication_date=pub, reporting_period=period))
        return eid

    def link(self, method: str, on: str, score: float = 1.0) -> EntityLink:
        return EntityLink(self.org, method, score, [on])

    def add(self, family: str, fld: str, value: Any, eid: str, link: EntityLink,
            conf: float = 0.95, key_extra: str = "", unit: str | None = None,
            period: str | None = None, note: str | None = None) -> None:
        key = f"{self.org}|{family}|{fld}" + (f"|{key_extra}" if key_extra else "")
        self.claims.append(Claim(key, family, fld, "available", value, conf, [eid],
                                 link, unit=unit, period=period, note=note))

    def absent(self, family: str, fld: str, state: str, note: str,
               key_extra: str = "", alternatives: list[dict] | None = None) -> None:
        key = f"{self.org}|{family}|{fld}" + (f"|{key_extra}" if key_extra else "")
        self.claims.append(Claim(key, family, fld, state, None, 0.0, [], None,
                                 alternatives=alternatives or [], note=note))
