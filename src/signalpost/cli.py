from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .core import AGENT_VERSION
from .fetch import Fetcher
from .pipeline import Agent, State, atomic_write, read_manifest, run_batch, seed_from_envelopes, validate
from .render import render


def check_file(path: Path) -> tuple[int, list[str]]:
    issues, seen, n = [], set(), 0
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        n += 1
        env = json.loads(line)
        if env["organisation_number"] in seen:
            issues.append(f"duplicate envelope for {env['organisation_number']}")
        seen.add(env["organisation_number"])
        issues += [f"{env['organisation_number']}: {m}" for m in validate(env)]
    return n, issues


def cmd_run(a) -> int:
    src = a.organisations or a.input
    dst = Path(a.output or a.out or "out/profiles.jsonl")
    inputs, hints = read_manifest(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"[signalpost {AGENT_VERSION}] {len(inputs)} inputs from {src}", flush=True)
    t0 = time.monotonic()
    state = State(a.state or None)
    if a.previous and Path(a.previous).exists():
        print(f"seeded {seed_from_envelopes(state, a.previous)} previous snapshots", flush=True)
    f = Fetcher(a.cache or None, timeout=a.timeout, delay=a.delay, obey_robots=True)
    soft = hard = None
    if a.time_budget and a.time_budget > 0:
        soft, hard = time.monotonic() + 0.70 * a.time_budget, time.monotonic() + 0.95 * a.time_budget
    agent = Agent(f, state, soft_deadline=soft)
    envs = run_batch(inputs, agent, workers=a.workers or a.concurrency, company_timeout=a.company_timeout,
                     hints=hints, hard_deadline=hard,
                     on_progress=lambda i, n: print(f"  progress {i}/{n}  ({time.monotonic() - t0:.0f}s)", flush=True))
    atomic_write(dst, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in envs))  # replace, never append
    issues = [f"{e['organisation_number']}: {m}" for e in envs for m in validate(e)]
    term: dict[str, int] = {}
    for e in envs:
        term[e["run"]["terminal_status"]] = term.get(e["run"]["terminal_status"], 0) + 1
    expected = a.expected_count if a.expected_count is not None else len(inputs)
    report = {"agent_version": AGENT_VERSION, "run_id": a.run_id, "inputs": len(inputs), "expected_count": expected,
              "envelopes": len(envs), "distinct_organisations": len({e["organisation_number"] for e in envs}),
              "silent_drops": max(0, len(inputs) - len(envs)), "terminal_status": term,
              "contract_issues": len(issues), "issues_sample": issues[:25],
              "material_changes": sum(e["refresh"].get("material_change_count", 0) for e in envs),
              "wall_clock_s": round(time.monotonic() - t0, 1), "requests": f.requests, "cache_hits": f.cache_hits,
              "third_party_cost_usd": 0.0, "degraded_registry_only": agent.degraded,
              "family_hit_rate": {fam: round(sum(1 for e in envs if e["coverage"]["families"].get(fam, {}).get("available")) / max(len(envs), 1), 3)
                                  for fam in envs[0]["coverage"]["families"]} if envs else {}}
    rp = Path(a.report) if a.report else dst.with_suffix(".report.json")
    atomic_write(rp, json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    if a.profiles_output:
        atomic_write(Path(a.profiles_output), "".join(json.dumps({"organisation_number": e["organisation_number"],
                     "legal_identity": e["legal_identity"], "synthesis": e["synthesis"], "coverage": e["coverage"]},
                     ensure_ascii=False) + "\n" for e in envs))
    if a.site:
        print(f"rendered {render(envs, a.site)} pages -> {a.site}/index.html")
    ok = len(envs) == len(inputs) == expected and report["distinct_organisations"] == len(envs) and not issues
    if not ok:
        print(f"FAILED CONTRACT CHECK: inputs={len(inputs)} expected={expected} envelopes={len(envs)} issues={len(issues)}", file=sys.stderr)
    code = 0 if ok else 1
    if any(e["run"]["terminal_status"] == "failed" and any(x.get("kind") == "TimeoutError" for x in e["errors"]) for e in envs):
        sys.stdout.flush(); sys.stderr.flush()
        import os
        os._exit(code)  # abandoned worker threads must not delay process exit
    return code


def main() -> int:
    ap = argparse.ArgumentParser(prog="signalpost")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--organisations", help="JSON, JSONL or text list of organisation numbers (starter-kit flag)")
    r.add_argument("--input", help="alias of --organisations")
    r.add_argument("--output", help="terminal envelope JSONL (starter-kit flag)")
    r.add_argument("--out", help="alias of --output")
    r.add_argument("--report")
    r.add_argument("--profiles-output")
    r.add_argument("--run-id", default="local")
    r.add_argument("--expected-count", type=int)
    r.add_argument("--bulk", help="accepted for starter-kit compatibility; not needed")
    r.add_argument("--previous", help="previous envelope JSONL used as the snapshot to diff against")
    r.add_argument("--state", default="state")
    r.add_argument("--cache", default=".cache")
    r.add_argument("--site", default="")
    r.add_argument("--workers", type=int, default=0)
    r.add_argument("--concurrency", type=int, default=16)
    r.add_argument("--time-budget", type=float, default=0, help="total seconds allowed; 0 = unlimited. At 70%% external sources are skipped, at 95%% remaining companies get a timeout envelope")
    r.add_argument("--delay", type=float, default=1.0)
    r.add_argument("--timeout", type=float, default=15.0)
    r.add_argument("--company-timeout", type=float, default=150.0)
    v = sub.add_parser("validate")
    v.add_argument("--file", required=True)
    a = ap.parse_args()
    if a.cmd == "run":
        if not (a.organisations or a.input):
            ap.error("run needs --organisations")
        return cmd_run(a)
    n, issues = check_file(Path(a.file))
    print(f"{n} envelopes, {len(issues)} issues")
    for i in issues[:50]:
        print(" -", i)
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
