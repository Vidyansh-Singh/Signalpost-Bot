# Signalpost bot

Evidence-backed public profiles for Norwegian organisation numbers. Python 3.11+, **standard library only** (no installs, no API keys, no paid services).

## Run (official command, any OS)
```
python run.py run --organisations companies.jsonl --output out/envelopes.jsonl --report out/report.json --run-id smoke-001 --expected-count 100 --state state --site site
```
(On Windows use `python` or `py`; on macOS/Linux `python3`. No environment variable or install step is needed.)
- `--organisations` takes the official JSONL manifest (objects with `organisation_number`), a JSON array, or a plain list of numbers. Starter-kit flags `--bulk`, `--profiles-output`, `--workers` are accepted.
- `--time-budget SECONDS` (optional, default off): at 70% of the budget external sources are skipped (registry data kept); at 95% any unfinished company gets a typed timeout envelope, so every input still returns one envelope.
- Default is 16 workers; progress prints every 10 companies.
- `--previous out/last-envelopes.jsonl` supplies the previous snapshot; material changes are written into each envelope's `changes`.
- Exit code is non-zero if envelope count != input count != `--expected-count`, if numbers are duplicated, or if any envelope breaks the contract.
- Outputs: `out/envelopes.jsonl` (one terminal envelope per input), `out/report.json` (requests, runtime, cost, per-family hit rate), optional `site/index.html`.
- `pip install -e .` also provides a `signalpost` command.

## Verify
```bash
python -m unittest discover -s tests -v    # offline, no network
python run.py validate --file out/envelopes.jsonl
```
The report's `envelopes` and `distinct_organisations` must both equal `inputs`, and `silent_drops` must be 0.

## Sources
Official: data.brreg.no (Enhetsregisteret, Regnskapsregisteret, roller, underenheter; NLOD 2.0). Open data: Wikidata (CC0), GLEIF.
Company-owned: the website verified for that exact company, under robots.txt and crawl-delay.
Wikidata's robots.txt disallows this bot on its query endpoint; the bot respects that and records `blocked` (low impact: very few small companies have items). Not used: LinkedIn/Meta/Glassdoor/Indeed clients, search-result scraping, any LLM, any paid API, NAV feed (needs an approved token).

## Rules the code enforces
- Website identity gate (publish only if one holds): (1) the exact organisation number is printed on the page; (2) the URL came from an official source (registry/Wikidata) and the legal name is on the page; (3) a guessed domain whose token equals the legal name exactly, with the name on the page, plus ONE of: the registered postal code or place on the page, the full legal name with its legal form (e.g. "Wedo AS") on the page, the organisation number on its contact/about/privacy/terms page, or (for .no domains) the organisation number in Norid's public RDAP registrant record. Anything weaker is `ambiguous` and never published. A smoke test showed substring-domain matches (e.g. a different firm's `rbcbluebay.com` for "BLUE BAY AS") are not acceptable, hence rule 3.
- A website is published only if the organisation number is on the page, or legal name + domain + title match strongly. Otherwise `ambiguous`/`not_available`.
- Absence is a typed state with a note, never zero. Available claims need evidence, entity link, hash, span; financial and activity claims need a date.
- Undated news is `ambiguous`, not published as dated activity.
- Re-runs overwrite per-company state (idempotent) and emit typed changes.

## Limitations
Recall is bounded by free official sources and company-owned pages. Untested against the live network from the authoring environment (egress blocked); run the 100-company smoke test yourself before submitting.
