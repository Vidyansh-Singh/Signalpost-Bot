"""Static, dependency-free viewer: searchable index + one page per company."""
from __future__ import annotations

import html
import json
from pathlib import Path

E = html.escape
CSS = ("*{box-sizing:border-box}body{margin:0;font:16px/1.5 system-ui,sans-serif;color:#10151c;background:#f6f7f9}"
       "header{background:#0f172a;color:#fff;padding:16px 20px}header a{color:#9fd0ff}main{max-width:1040px;margin:0 auto;padding:16px}"
       ".card{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:16px;margin:0 0 16px}"
       "table{width:100%;border-collapse:collapse;font-size:.92rem}th,td{text-align:left;padding:8px;border-bottom:1px solid #eef0f3;"
       "vertical-align:top;word-break:break-word}.pill{padding:1px 8px;border-radius:99px;font-size:.75rem;font-weight:600;background:#e2e8f0}"
       ".available{background:#dcfce7}.blocked,.failed{background:#fee2e2}.ambiguous{background:#fef3c7}"
       "input{width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:8px;font-size:1rem}small{color:#64748b}")


def _v(v) -> str:
    return E(json.dumps(v, ensure_ascii=False)[:300] if isinstance(v, (dict, list)) else str(v)[:300]) if v is not None else "<small>-</small>"


def company_page(env: dict) -> str:
    ev = {e["id"]: e for e in env["evidence"]}
    rows = []
    for c in sorted(env["claims"], key=lambda x: (x["field_family"], x["field"])):
        src = " ".join(f'<a href="{E(ev[i]["source_url"])}" rel="noopener" target="_blank">source</a>' for i in c["evidence_ids"] if i in ev)
        dates = " ".join(E(ev[i].get("publication_date") or ev[i].get("reporting_period") or "") for i in c["evidence_ids"] if i in ev)
        span = " | ".join(E(ev[i]["claim_span"][:120]) for i in c["evidence_ids"] if i in ev)
        rows.append(f'<tr><td><small>{E(c["field_family"])}</small><br><b>{E(c["field"])}</b></td><td>{_v(c["value"])}'
                    f'{"<br><small>" + E(c["note"]) + "</small>" if c.get("note") else ""}</td>'
                    f'<td><span class="pill {c["availability"]}">{c["availability"]}</span><br><small>{dates}</small></td>'
                    f"<td><small>{span}</small><br>{src}</td></tr>")
    s = env["synthesis"]
    ch = "".join(f"<li>{E(x)}</li>" for x in s["material_changes"])
    return (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{E(s["headline"])}</title><style>{CSS}</style><header><a href="index.html">&larr; All companies</a></header><main>'
            f'<div class="card"><h1>{E(s["headline"])}</h1><small>{E(env["run"]["terminal_status"])} &middot; {E(env["refresh"].get("last_refreshed_at", ""))}</small>'
            f'<h2>Summary</h2><ul>{"".join(f"<li>{E(x)}</li>" for x in s["summary"])}</ul><h2>Changes</h2><ul>{ch}</ul>'
            f'<h2>Not available</h2><ul>{"".join(f"<li>{E(x)}</li>" for x in s["unknowns"]) or "<li>None</li>"}</ul></div>'
            f'<div class="card"><table><tr><th>Field</th><th>Value</th><th>State / date</th><th>Evidence</th></tr>{"".join(rows)}</table></div></main></html>')


def render(envs: list[dict], out: str | Path) -> int:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for e in envs:
        org = e["organisation_number"]
        (out / f"{org}.html").write_text(company_page(e), "utf-8")
        name = e["legal_identity"].get("legal_name") or org
        rows.append(f'<tr data-q="{E(name.lower())} {E(org)}"><td><a href="{E(org)}.html">{E(name)}</a><br><small>{E(org)}</small></td>'
                    f'<td><span class="pill">{E(e["run"]["terminal_status"])}</span></td>'
                    f'<td>{e["coverage"]["families_with_data"]}/{e["coverage"]["families_expected"]}</td><td>{e["coverage"]["claims_available"]}</td></tr>')
    (out / "index.html").write_text(
        f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Signalpost</title><style>{CSS}</style><header><b>Signalpost</b> - evidence-backed Norwegian company profiles</header><main>'
        f'<div class="card"><input type="search" id="q" placeholder="Search name or organisation number"><p><small>{len(envs)} profiles</small></p>'
        f'<table><tr><th>Company</th><th>Run</th><th>Families</th><th>Claims</th></tr>{"".join(rows)}</table></div></main>'
        "<script>document.getElementById('q').addEventListener('input',function(e){var v=e.target.value.toLowerCase();"
        "document.querySelectorAll('tr[data-q]').forEach(function(r){r.style.display=r.dataset.q.indexOf(v)>-1?'':'none'})})</script></html>", "utf-8")
    return len(envs)
