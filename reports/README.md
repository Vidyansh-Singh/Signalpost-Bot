# Smoke-test evidence
`smoke-companies.jsonl`: the 100-company batch (kit selector, seed 20260823).
Add your own run output here: `smoke-002-report.json` and `smoke-002-envelopes.jsonl`
(produced by `python run.py run --organisations reports/smoke-companies.jsonl --output out/smoke.jsonl --report out/smoke-report.json --run-id smoke-002 --expected-count 100`).
Example result (v1.1.0 run; replace with the report of the version you submit): 100 inputs, 100 envelopes, 100 distinct organisations, 0 silent drops, 0 contract issues, 135 s, 875 requests, $0.
