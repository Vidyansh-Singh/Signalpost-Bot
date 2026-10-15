"""Per-company orchestration, refresh diffs, synthesis, validation.

Guarantees: exactly one terminal envelope per input; one company's failure cannot
abort the batch; absence is typed; re-running a company overwrites its state key.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import sources as S
from .core import AGENT_VERSION, Claim, Ctx, find_orgnums, is_valid, normalise, utcnow
from .fetch import Fetcher

FAMILIES = ["legal_identity", "financials", "leadership", "locations", "group_structure",
            "digital_presence", "social_profiles", "contact", "hiring", "public_activity",
            "workforce", "business_profile"]
MATERIAL = {"legal_identity", "financials", "leadership", "group_structure", "digital_presence"}


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class State:
    """One JSON file per organisation number: re-runs overwrite, never append."""

    def __init__(self, root: str | Path | None):
        self.root = Path(root) if root else None
        if self.root:
            (self.root / "companies").mkdir(parents=True, exist_ok=True)

    def load(self, org: str) -> dict | None:
        p = self.root / "companies" / f"{org}.json" if self.root else None
        try:
            return json.loads(p.read_text("utf-8")) if p and p.exists() else None
        except Exception:
            return None

    def save(self, org: str, snap: dict) -> None:
        if self.root:
            atomic_write(self.root / "companies" / f"{org}.json", json.dumps(snap, ensure_ascii=False, sort_keys=True))


def seed_from_envelopes(state: "State", path: str | Path) -> int:
    """Previous-snapshot input: rebuild per-company snapshots from an earlier envelope JSONL.
    Existing state wins; this only fills gaps."""
    n = 0
    for line in Path(path).read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            env = json.loads(line)
        except Exception:
            continue
        org = env.get("organisation_number")
        if not org or state.load(org) is not None:
            continue
        ts = (env.get("refresh") or {}).get("last_refreshed_at") or (env.get("run") or {}).get("completed_at")
        claims = {}
        for c in env.get("claims", []):
            ok = c.get("availability") == "available"
            claims[c["claim_key"]] = {"family": c.get("field_family"), "field": c.get("field"),
                                      "availability": c.get("availability"),
                                      "hash": vhash(c.get("value")) if ok else None,
                                      "value": c.get("value") if ok else None,
                                      "evidence_ids": c.get("evidence_ids", []), "first": ts, "last": ts}
        state.save(org, {"claims": claims, "last_run_at": ts, "runs": int((env.get("refresh") or {}).get("run_count", 1))})
        n += 1
    return n


def vhash(v: Any) -> str:
    return hashlib.sha256(json.dumps(v, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def dedupe(claims: list[Claim]) -> list[Claim]:
    best: dict[str, Claim] = {}
    for c in claims:
        cur = best.get(c.claim_key)
        if cur is None:
            best[c.claim_key] = c
        elif c.availability == "available" and cur.availability != "available":
            best[c.claim_key] = c
        elif c.availability == "available" and cur.availability == "available":
            if c.value != cur.value:
                cur.alternatives.append({"value": c.value, "evidence_ids": c.evidence_ids})
            else:
                cur.evidence_ids = list(dict.fromkeys(cur.evidence_ids + c.evidence_ids))
    return list(best.values())


def diff(prev: dict | None, claims: list[Claim]) -> tuple[list[dict], dict]:
    now = utcnow()
    cur = {c.claim_key: {"family": c.field_family, "field": c.field, "availability": c.availability,
                         "hash": vhash(c.value) if c.availability == "available" else None,
                         "value": c.value if c.availability == "available" else None,
                         "evidence_ids": c.evidence_ids, "first": now, "last": now} for c in claims}
    old = (prev or {}).get("claims") or {}
    changes: list[dict] = []
    for k, r in cur.items():
        o = old.get(k)
        if o and o.get("hash") == r["hash"]:
            r["first"] = o.get("first") or now
    if prev is not None:
        def mat(r): return "material" if r["family"] in MATERIAL else "minor"
        for k, r in cur.items():
            o = old.get(k)
            if r["availability"] == "available" and (o is None or o.get("hash") is None):
                changes.append({"claim_key": k, "field": r["field"], "change_type": "added",
                                "current_value": r["value"], "observed_at": now, "materiality": mat(r)})
            elif o and o.get("hash") and r["hash"] != o["hash"]:
                kind = "changed" if r["availability"] == "available" else "removed"
                changes.append({"claim_key": k, "field": r["field"], "change_type": kind,
                                "previous_value": o.get("value"), "current_value": r["value"],
                                "first_observed_at": o.get("first"), "observed_at": now, "materiality": mat(r)})
        for k, o in old.items():
            if k not in cur and o.get("hash"):
                changes.append({"claim_key": k, "field": o.get("field"), "change_type": "removed",
                                "previous_value": o.get("value"), "observed_at": now, "materiality": "minor"})
    return changes, {"claims": cur, "last_run_at": now, "runs": int((prev or {}).get("runs", 0)) + 1}


def coverage(claims: list[Claim]) -> dict:
    fams = {f: {"attempted": 0, "available": 0, "states": {}} for f in FAMILIES}
    for c in claims:
        f = fams.setdefault(c.field_family, {"attempted": 0, "available": 0, "states": {}})
        f["attempted"] += 1
        f["states"][c.availability] = f["states"].get(c.availability, 0) + 1
        f["available"] += c.availability == "available"
    with_data = sum(1 for v in fams.values() if v["available"])
    return {"families": fams, "families_expected": len(FAMILIES), "families_with_data": with_data,
            "family_coverage_ratio": round(with_data / len(FAMILIES), 4),
            "claims_total": len(claims), "claims_available": sum(c.availability == "available" for c in claims)}


def synthesise(org: str, ident: dict, claims: list[Claim], changes: list[dict], cov: dict) -> dict:
    """Extractive only: every sentence is built from claims that carry evidence."""
    idx = {}
    for c in claims:
        if c.availability == "available":
            idx.setdefault(f"{c.field_family}|{c.field}", c)
    used: list[str] = []

    def take(k):
        c = idx.get(k)
        if c:
            used.append(c.claim_key)
        return c

    name = ident.get("legal_name") or f"Organisation {org}"
    form, reg = take("legal_identity|organisation_form"), take("legal_identity|registration_date")
    ind = take("business_profile|industry_description")
    head = f"{name} ({org})" + (f" — {form.value}" if form else "") + (f", {ind.value}" if ind else "")
    s = [f"{name} is registered in the Norwegian Central Coordinating Register under organisation number {org}"
         + (f" as a {form.value}" if form else "") + (f", registered {reg.value}" if reg else "") + "."]
    flags = ident.get("status_flags") or []
    s.append("The registry records status flag(s): " + ", ".join(f.replace("_", " ") for f in flags) + "."
             if flags else "The registry records no bankruptcy or liquidation flag.")
    rev, prof, per = take("financials|latest_revenue"), take("financials|latest_profit_for_year"), take("financials|latest_period")
    parts = ([f"operating revenue of {int(rev.value):,} {rev.unit}".replace(",", " ")] if rev else []) + \
            ([f"a profit for the year of {int(prof.value):,} {prof.unit}".replace(",", " ")] if prof else [])
    s.append(f"The latest filed annual accounts report {' and '.join(parts)}" + (f" for the period ending {per.value}" if per else "") + "."
             if parts else "No filed annual-accounts figures were retrievable from the accounts register.")
    emp, wp = take("workforce|registered_employees"), take("locations|registered_workplace_count")
    bits = ([f"{emp.value} registered employees"] if emp else []) + ([f"{wp.value} registered workplace(s)"] if wp else [])
    if bits:
        s.append("The register lists " + " and ".join(bits) + ".")
    roles = [c for c in claims if c.field_family == "leadership" and c.availability == "available"
             and c.field.startswith("role_") and c.field != "role_holder_count" and isinstance(c.value, str)]
    if roles:
        used.extend(c.claim_key for c in roles[:4])
        s.append("Registered role holders include " + "; ".join(f"{c.value} ({c.note})" for c in roles[:4]) + ".")
    site = take("digital_presence|official_website")
    if site:
        s.append(f"The verified company website is {site.value} (identity proven by {', '.join(site.entity_link.matched_on)}).")
    else:
        amb = next((c for c in claims if c.field == "official_website" and c.availability == "ambiguous"), None)
        s.append("A candidate website was found but did not meet the identity threshold, so it is not published."
                 if amb else "No company-owned website could be verified for this organisation number.")
    jobs, act = take("hiring|open_position_count"), None
    if jobs:
        s.append(f"{jobs.value} open position(s) were listed on the company's own careers page at retrieval time.")
    dates = [c.value["date_published"] for c in claims if c.field == "news_item" and c.availability == "available"]
    if dates:
        s.append(f"The most recent dated public activity observed is {max(dates)}.")
    mat = [f"{c['field']}: {c.get('previous_value')!r} -> {c.get('current_value')!r} ({c['change_type']})"
           for c in changes if c["materiality"] == "material"][:12] or ["No material change detected since the previous run."]
    unknown = [f"{f}: no value published ({', '.join(sorted(i['states'])) or 'not attempted'})"
               for f, i in sorted(cov["families"].items()) if not i["available"]]
    return {"headline": head, "summary": s, "material_changes": mat, "unknowns": unknown,
            "grounded_claim_keys": sorted(set(used))}


def validate(env: dict) -> list[str]:
    """Hard contract gates; any issue blocks submission."""
    out: list[str] = []
    ids = {e["id"]: e for e in env["evidence"]}
    keys = [c["claim_key"] for c in env["claims"]]
    if len(keys) != len(set(keys)):
        out.append("duplicate claim_key")
    for c in env["claims"]:
        if c["availability"] == "available":
            if not c["evidence_ids"] or not c["entity_link"]:
                out.append(f"{c['claim_key']}: available without evidence/entity link")
            for i in c["evidence_ids"]:
                e = ids.get(i)
                if not e:
                    out.append(f"{c['claim_key']}: dangling evidence {i}")
                    continue
                if not (e["claim_span"] and e["content_sha256"] and e["source_policy"] and e["retrieved_at"]):
                    out.append(f"{c['claim_key']}: incomplete evidence {i}")
                if c["field_family"] in ("financials", "public_activity") and c["field"] != "open_position_count" \
                        and not (e.get("publication_date") or e.get("reporting_period")):
                    out.append(f"{c['claim_key']}: dated family without a date")
        elif isinstance(c["value"], (int, float)) and not isinstance(c["value"], bool):
            out.append(f"{c['claim_key']}: absence rendered as a number")
    for k in env["synthesis"]["grounded_claim_keys"]:
        if k not in keys:
            out.append(f"synthesis references unknown claim {k}")
    return out


class Agent:
    def __init__(self, fetcher: Fetcher, state: State, enable_site: bool = True, soft_deadline: float | None = None):
        self.f, self.state, self.enable_site = fetcher, state, enable_site
        self.soft_deadline = soft_deadline  # time.monotonic() value; after it, registry-only mode
        self.degraded = 0

    def _shell(self, org, valid, ident, claims, evidence, errors, run_id, started, t0, terminal=None,
               note_refresh=None) -> dict:
        claims = dedupe(claims)
        used = {i for c in claims for i in c.evidence_ids}
        evidence = [e for e in evidence if e.id in used]
        prev = self.state.load(org) if valid else None
        changes, snap = diff(prev, claims)
        cov = coverage(claims)
        syn = synthesise(org, ident, claims, changes, cov)
        if terminal is None:
            avail = cov["claims_available"]
            terminal = ("failed" if not avail else "partial" if errors or cov["family_coverage_ratio"] < 0.5 else "completed")
        env = {"organisation_number": org, "input_valid": valid, "legal_identity": ident,
               "claims": [asdict(c) for c in claims], "evidence": [asdict(e) for e in evidence],
               "changes": changes, "errors": errors, "synthesis": syn,
               "refresh": note_refresh or {"last_refreshed_at": utcnow(), "previous_run_at": (prev or {}).get("last_run_at"),
                                           "run_count": snap["runs"], "is_first_observation": prev is None,
                                           "material_change_count": sum(c["materiality"] == "material" for c in changes),
                                           "recommended_refresh_interval_days": 7 if changes else 14},
               "coverage": cov,
               "operations": {"requests": self.f.requests, "cache_hits": self.f.cache_hits,
                              "runtime_ms": int((time.monotonic() - t0) * 1000), "third_party_cost_usd": 0.0,
                              "robots_denied": self.f.robots_denied},
               "run": {"run_id": run_id, "agent_version": AGENT_VERSION, "started_at": started,
                       "completed_at": utcnow(), "terminal_status": terminal}}
        if valid and terminal != "failed" or (valid and cov["claims_available"]):
            self.state.save(org, snap)  # a failed refresh keeps the last supported state
        return env

    def _terminal(self, raw, kind: str, note: str, valid: bool) -> dict:
        org = normalise(raw) or str(raw)[:32]
        c = Ctx(org)
        c.absent("legal_identity", "legal_name", "failed" if valid else "not_applicable", note)
        err = [{"stage": "run", "target": org, "kind": kind, "message": note, "retryable": valid, "occurred_at": utcnow()}]
        t0 = time.monotonic()
        env = self._shell(org, valid, {"organisation_number": org, "legal_name": None, "status_flags": []},
                          c.claims, [], err, "terminal", utcnow(), t0, "failed",
                          {"last_refreshed_at": utcnow(), "is_first_observation": True, "note": note})
        return env

    def timeout_envelope(self, raw: str, limit: float) -> dict:
        return self._terminal(raw, "TimeoutError", f"per-company budget of {limit}s exceeded", is_valid(raw))

    def run_one(self, raw: str, hint: dict | None = None) -> dict:
        t0, started, run_id = time.monotonic(), utcnow(), uuid.uuid4().hex[:12]
        org = normalise(raw)
        if not is_valid(org):
            return self._terminal(raw, "InvalidOrganisationNumber",
                                  "input is not a valid Norwegian organisation number (9 digits, MOD-11)", False)
        c, errors = Ctx(org), []

        def fail(stage, e):
            errors.append({"stage": stage, "target": org, "kind": type(e).__name__, "message": str(e)[:300],
                           "retryable": True, "occurred_at": utcnow()})

        ident = {"organisation_number": org, "status_flags": []}
        data, r = self.f.get_json(f"{S.ER}/enheter/{org}", ttl=21_600)
        if not isinstance(data, dict):
            st = "blocked" if r.blocked_by_robots else "not_available" if r.status == 404 else "failed"
            c.absent("legal_identity", "legal_name", st,
                     "no unit exists in the register for this organisation number" if st == "not_available"
                     else f"registry lookup unsuccessful ({r.error})")
            return self._shell(org, True, ident, c.claims, c.evidence, errors, run_id, started, t0,
                               "partial" if st == "not_available" else "failed")
        try:
            ident = S.registry_identity(c, data, r)
        except Exception as e:  # noqa: BLE001
            fail("registry.identity", e)

        for label, url, mapper, fam, fld in (
                ("roles", f"{S.ER}/enheter/{org}/roller", S.registry_roles, "leadership", "role_holders"),
                ("subunits", f"{S.ER}/underenheter?overordnetEnhet={org}&size=100", S.registry_subunits, "locations", "registered_workplaces"),
                ("accounts", f"{S.RR}/{org}", S.registry_accounts, "financials", "latest_annual_accounts")):
            try:
                payload, rr = self.f.get_json(url, ttl=21_600)
                if payload is None:
                    st = "blocked" if rr.blocked_by_robots else "not_available" if rr.status in (404, 204) else "failed"
                    c.absent(fam, fld, st, f"{label} endpoint returned no payload ({rr.error})")
                else:
                    mapper(c, payload, rr)
            except Exception as e:  # noqa: BLE001
                fail(f"registry.{label}", e)
                c.absent(fam, fld, "failed", f"{label} raised {type(e).__name__}")
        late = self.soft_deadline is not None and time.monotonic() > self.soft_deadline
        if late:
            self.degraded += 1
            errors.append({"stage": "budget", "target": org, "kind": "SoftDeadline",
                           "message": "time budget nearly spent: external sources skipped, registry data kept",
                           "retryable": True, "occurred_at": utcnow()})
        for label, fn in (() if late else (("wikidata", S.wikidata), ("gleif", S.gleif))):
            try:
                fn(c, self.f)
            except Exception as e:  # noqa: BLE001
                fail(f"connector.{label}", e)

        if self.enable_site and not late:
            try:
                extra = [x.value for x in c.claims if x.field == "wikidata_official_website" and x.availability == "available"]
                hw = (hint or {}).get("website") if isinstance((hint or {}).get("website"), str) else None
                nm = ident.get("legal_name") or (hint or {}).get("name") or ""
                cands = S.candidates(nm, ident.get("registry_website_raw") or hw, extra)
                if not cands:
                    c.absent("digital_presence", "official_website", "not_available", "no website candidate could be generated")
                else:
                    off = set()
                    for u in [ident.get("registry_website_raw"), hw] + extra:
                        if isinstance(u, str) and u.strip():
                            host = urlparse(u if "//" in u else "//" + u).netloc.lower().removeprefix("www.")
                            if host:
                                off.add(host)
                    places = [ident.get("postal_place"), ident.get("municipality"), (hint or {}).get("municipality")]
                    res = S.verify_site(c, self.f, ident.get("legal_name") or "", cands, official=off,
                                        places=places, postal=ident.get("postal_code"))
                    if res:
                        url, hr, pg, link = res
                        S.mine_site(c, self.f, url, hr, pg, link)
            except Exception as e:  # noqa: BLE001
                fail("website", e)
        return self._shell(org, True, ident, c.claims, c.evidence, errors, run_id, started, t0)


def _org_from(obj) -> tuple[str, dict]:
    if isinstance(obj, dict):
        for k in ("organisation_number", "organisasjonsnummer", "orgnr", "org_number", "organization_number"):
            if obj.get(k) not in (None, ""):
                return str(obj[k]).strip(), obj
        return "", obj
    return str(obj).strip(), {}


def read_manifest(path: str | Path) -> tuple[list[str], dict[str, dict]]:
    """Accept JSONL objects, a JSON array/object, or plain text (one number per line,
    first CSV column). Returns ordered unique inputs and optional per-org hints."""
    raw = Path(path).read_text("utf-8-sig")
    items: list = []
    txt = raw.strip()
    parsed = False
    if txt[:1] in "[{":
        try:
            data = json.loads(txt)
            if isinstance(data, dict):
                data = data.get("organisations") or data.get("companies") or data.get("items") or [data]
            items, parsed = list(data), True
        except Exception:
            parsed = False
    if not parsed:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line[:1] == "{":
                try:
                    items.append(json.loads(line))
                    continue
                except Exception:
                    pass
            first = line.split(",")[0].split(";")[0].split("\t")[0].strip().strip('"')
            if first.lower().startswith(("org", "id")) and not first[:1].isdigit():
                continue
            items.append(first)
    orgs, hints, seen = [], {}, set()
    for it in items:
        o, hint = _org_from(it)
        if not o or o in seen:
            continue
        seen.add(o)
        orgs.append(o)
        if hint:
            hints[normalise(o)] = hint
    return orgs, hints


def read_inputs(path: str | Path) -> list[str]:
    return read_manifest(path)[0]


def run_batch(inputs: list[str], agent: Agent, *, workers: int = 16, company_timeout: float = 150.0,
              hints: dict[str, dict] | None = None, hard_deadline: float | None = None,
              on_progress=None) -> list[dict]:
    """Always returns len(inputs) envelopes in input order, even past the hard deadline."""
    hints = hints or {}
    ex = ThreadPoolExecutor(max_workers=workers)
    futs = [ex.submit(agent.run_one, raw, hints.get(normalise(raw))) for raw in inputs]
    out = []
    for i, (raw, fu) in enumerate(zip(inputs, futs), 1):
        limit = company_timeout
        if hard_deadline is not None:
            limit = max(0.01, min(company_timeout, hard_deadline - time.monotonic()))
        try:
            out.append(fu.result(timeout=limit))
        except FutTimeout:
            out.append(agent.timeout_envelope(raw, limit))
        except Exception as e:  # noqa: BLE001
            out.append(agent._terminal(raw, type(e).__name__, f"isolated failure: {str(e)[:200]}", is_valid(raw)))
        if on_progress and (i % 10 == 0 or i == len(inputs)):
            on_progress(i, len(inputs))
    ex.shutdown(wait=False, cancel_futures=True)
    return out
