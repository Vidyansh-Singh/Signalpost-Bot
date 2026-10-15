"""Source mappers: official registry, open-data connectors, company-site extraction."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from .core import Claim, Ctx, EntityLink, find_orgnums
from .fetch import FetchResult, Fetcher

ER = "https://data.brreg.no/enhetsregisteret/api"
RR = "https://data.brreg.no/regnskapsregisteret/regnskap"
REG_POLICY = "Brønnøysundregistrene open data (Enhetsregisteret/Regnskapsregisteret), NLOD 2.0"
WD_POLICY = "Wikidata SPARQL endpoint, content CC0"
GLEIF_POLICY = "GLEIF LEI public API, open data"
SITE_POLICY = "company-owned public page, retrieved under robots.txt and crawl-delay"


def _fmt_addr(a: dict) -> str:
    lines = a.get("adresse")
    street = " ".join(lines) if isinstance(lines, list) else (lines or "")
    return ", ".join(str(x) for x in (street, a.get("postnummer"), a.get("poststed")) if x)


# ------------------------------------------------------------------ registry
def registry_identity(c: Ctx, d: dict, r: FetchResult) -> dict:
    link = c.link("official_registry_exact", "organisasjonsnummer")
    ident: dict[str, Any] = {"organisation_number": c.org, "status_flags": []}

    def add(fam, fld, val, sel, conf=0.99):
        eid = c.ev(r, "official_registry", REG_POLICY, sel, f"{fld}: {val}", "brreg_enhet.v1")
        c.add(fam, fld, val, eid, link, conf)

    name = d.get("navn")
    if isinstance(name, str) and name.strip():
        ident["legal_name"] = name.strip()
        add("legal_identity", "legal_name", name.strip(), "$.navn")
    else:
        c.absent("legal_identity", "legal_name", "not_available", "registry record has no name")
    form = (d.get("organisasjonsform") or {}).get("beskrivelse")
    if form:
        ident["organisation_form"] = form
        add("legal_identity", "organisation_form", form, "$.organisasjonsform.beskrivelse")
    for key, fld in (("registreringsdatoEnhetsregisteret", "registration_date"),
                     ("stiftelsesdato", "incorporation_date")):
        if d.get(key):
            add("legal_identity", fld, str(d[key])[:10], f"$.{key}")
    nk = d.get("naeringskode1") or {}
    if nk.get("beskrivelse"):
        add("business_profile", "industry_description", nk["beskrivelse"], "$.naeringskode1.beskrivelse", 0.97)
    if nk.get("kode"):
        add("business_profile", "industry_code", nk["kode"], "$.naeringskode1.kode", 0.97)
    for key, fld in (("konkurs", "bankrupt"), ("underAvvikling", "in_liquidation"),
                     ("underTvangsavviklingEllerTvangsopplosning", "forced_liquidation")):
        v = d.get(key)
        if isinstance(v, bool):
            add("legal_identity", fld, v, f"$.{key}")
            if v:
                ident["status_flags"].append(fld)
    emp = d.get("antallAnsatte")
    if isinstance(emp, int) and not isinstance(emp, bool):
        add("workforce", "registered_employees", emp, "$.antallAnsatte", 0.95)
    else:
        c.absent("workforce", "registered_employees", "not_available",
                 "registry reports no employee count (absence is not zero)")
    addr = d.get("forretningsadresse")
    if isinstance(addr, dict):
        ident["postal_code"] = str(addr.get("postnummer") or "") or None
        ident["postal_place"] = str(addr.get("poststed") or "") or None
        ident["municipality"] = str(addr.get("kommune") or "") or None
    if isinstance(addr, dict) and _fmt_addr(addr):
        add("locations", "registered_business_address", _fmt_addr(addr), "$.forretningsadresse", 0.97)
    parent = d.get("overordnetEnhet")
    if isinstance(parent, str) and parent.isdigit():
        add("group_structure", "parent_unit", parent, "$.overordnetEnhet", 0.95)
    else:
        c.absent("group_structure", "relationships", "not_available",
                 "registry record declares no superior unit")
    site = d.get("hjemmeside")
    ident["registry_website_raw"] = site if isinstance(site, str) else None
    return ident


def registry_roles(c: Ctx, d: dict, r: FetchResult) -> None:
    link = c.link("official_registry_roles", "organisasjonsnummer")
    people = []
    for grp in d.get("rollegrupper") or []:
        for role in grp.get("roller") or []:
            if role.get("fratraadt"):
                continue
            rt = role.get("type") or {}
            code, desc = rt.get("kode") or "UKJENT", rt.get("beskrivelse") or rt.get("kode") or "role"
            nm = (role.get("person") or {}).get("navn") or {}
            full = " ".join(x for x in (nm.get("fornavn"), nm.get("mellomnavn"), nm.get("etternavn")) if x) \
                if isinstance(nm, dict) else str(nm)
            if not full:
                full = (role.get("enhet") or {}).get("navn") or ""
                if isinstance(full, list):
                    full = full[0] if full else ""
            if full:  # birth dates are deliberately never read
                people.append((code, desc, full.strip()))
    if not people:
        c.absent("leadership", "role_holders", "not_available", "roles endpoint returned no active role holder")
        return
    for i, (code, desc, full) in enumerate(people[:40]):
        eid = c.ev(r, "official_registry", REG_POLICY, f"$.rollegrupper[].roller[{i}]",
                   f"{desc}: {full}", "brreg_roller.v1")
        c.add("leadership", f"role_{code.lower()}", full, eid, link, 0.97,
              key_extra=f"{i}:{full.lower()}", note=desc)
    eid = c.ev(r, "official_registry", REG_POLICY, "$.rollegrupper",
               f"{len(people)} active role assignments", "brreg_roller.v1")
    c.add("leadership", "role_holder_count", len(people), eid, link, 0.98)


def registry_subunits(c: Ctx, d: dict, r: FetchResult) -> None:
    link = c.link("official_subunit_parent_link", "overordnetEnhet")
    rows = ((d.get("_embedded") or {}).get("underenheter")) or []
    if not rows:
        c.absent("locations", "registered_workplaces", "not_available", "no sub-units registered for this unit")
        return
    for i, u in enumerate(rows[:60]):
        pretty = _fmt_addr(u.get("beliggenhetsadresse") or {})
        nm = u.get("navn") or u.get("organisasjonsnummer")
        eid = c.ev(r, "official_registry", REG_POLICY, f"$._embedded.underenheter[{i}]",
                   f"{nm} — {pretty}", "brreg_underenheter.v1")
        c.add("locations", "registered_workplace",
              {"organisation_number": u.get("organisasjonsnummer"), "name": nm,
               "address": pretty or None, "employees": u.get("antallAnsatte")},
              eid, link, 0.96, key_extra=str(u.get("organisasjonsnummer") or i))
    eid = c.ev(r, "official_registry", REG_POLICY, "$._embedded.underenheter",
               f"{len(rows)} registered sub-units", "brreg_underenheter.v1")
    c.add("locations", "registered_workplace_count", len(rows), eid, link, 0.97)


_FIN = [
    ("revenue", ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter")),
    ("operating_cost", ("resultatregnskapResultat", "driftsresultat", "driftskostnad", "sumDriftskostnad")),
    ("operating_result", ("resultatregnskapResultat", "driftsresultat", "driftsresultat")),
    ("profit_for_year", ("resultatregnskapResultat", "aarsresultat")),
    ("total_assets", ("eiendeler", "sumEiendeler")),
    ("equity", ("egenkapitalGjeld", "egenkapital", "sumEgenkapital")),
]


def _dig(o: Any, path) -> Any:
    for k in path:
        if not isinstance(o, dict):
            return None
        o = o.get(k)
    return o


def registry_accounts(c: Ctx, payload: Any, r: FetchResult) -> None:
    rows = [x for x in (payload if isinstance(payload, list) else [payload]) if isinstance(x, dict)]
    if not rows:
        c.absent("financials", "latest_annual_accounts", "not_available",
                 "accounts register holds no filed annual accounts (absence is not zero)")
        return
    link = c.link("official_accounts_lookup", "organisasjonsnummer")
    end = lambda x: str(_dig(x, ("regnskapsperiode", "tilDato")) or "")[:10]  # noqa: E731
    row = max(rows, key=end)
    period, unit = end(row), row.get("valuta") or "NOK"
    for fld, path in _FIN:
        v = _dig(row, path)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            c.absent("financials", f"latest_{fld}", "not_available",
                     f"filed accounts ({period or 'unknown period'}) do not expose this line item")
            continue
        eid = c.ev(r, "official_registry", REG_POLICY, "$." + ".".join(path),
                   f"{fld} {v} {unit} ({period})", "brreg_regnskap.v1", period=period or None)
        c.add("financials", f"latest_{fld}", v, eid, link, 0.98, unit=unit, period=period or None)
    if period:
        eid = c.ev(r, "official_registry", REG_POLICY, "$.regnskapsperiode",
                   f"accounting period ending {period}", "brreg_regnskap.v1", period=period)
        c.add("financials", "latest_period", period, eid, link, 0.99, period=period)


# ------------------------------------------------------------ open connectors
SPARQL = ('SELECT ?item ?itemLabel ?site WHERE { ?item wdt:P2333 "%s" . '
          'OPTIONAL { ?item wdt:P856 ?site } '
          'SERVICE wikibase:label { bd:serviceParam wikibase:language "nb,en" } } LIMIT 5')


def wikidata(c: Ctx, f: Fetcher) -> None:
    url = f"https://query.wikidata.org/sparql?query={quote(SPARQL % c.org)}&format=json"
    data, r = f.get_json(url, ttl=604_800)
    if data is None:
        st = "blocked" if r.blocked_by_robots else "failed"
        c.absent("digital_presence", "wikidata_entity", st, f"wikidata lookup unsuccessful ({r.error})")
        return
    rows = ((data.get("results") or {}).get("bindings")) or []
    if not rows:
        c.absent("digital_presence", "wikidata_entity", "not_available",
                 "no Wikidata item carries this organisation number (P2333)")
        return
    row, link = rows[0], c.link("wikidata_p2333_exact", "P2333", 0.99)
    qid = ((row.get("item") or {}).get("value") or "").rsplit("/", 1)[-1]
    if qid:
        eid = c.ev(r, "open_data_api", WD_POLICY, "$.bindings[0].item", f"{qid} has P2333={c.org}", "wikidata.v1")
        c.add("digital_presence", "wikidata_entity", qid, eid, link, 0.93)
    site = (row.get("site") or {}).get("value")
    if site:
        eid = c.ev(r, "open_data_api", WD_POLICY, "$.bindings[0].site", f"P856 {site}", "wikidata.v1")
        c.add("digital_presence", "wikidata_official_website", site, eid, link, 0.88)


def gleif(c: Ctx, f: Fetcher) -> None:
    url = (f"https://api.gleif.org/api/v1/lei-records?filter[entity.registeredAs]={c.org}"
           "&filter[entity.legalAddress.country]=NO&page[size]=5")
    data, r = f.get_json(url, ttl=604_800)
    if data is None:
        st = "blocked" if r.blocked_by_robots else "failed"
        c.absent("group_structure", "lei", st, f"GLEIF lookup unsuccessful ({r.error})")
        return
    rows = data.get("data") or []
    lei = rows[0].get("id") if rows else None
    if not lei:
        c.absent("group_structure", "lei", "not_available", "no LEI record registered against this organisation number")
        return
    eid = c.ev(r, "open_data_api", GLEIF_POLICY, "$.data[0].id", f"LEI {lei}", "gleif.v1")
    c.add("group_structure", "lei", lei, eid, c.link("gleif_registered_as_exact", "entity.registeredAs", 0.99), 0.94)


# ----------------------------------------------------------------- html parse
class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.jsonld: list[str] = []
        self.feeds: list[str] = []
        self.times: list[tuple[str, str]] = []
        self.title = ""
        self._skip = 0
        self._ld = False
        self._ld_buf: list[str] = []
        self._a: list | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            if tag == "script" and "ld+json" in (a.get("type") or "").lower():
                self._ld, self._ld_buf = True, []
            else:
                self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a" and a.get("href"):
            self._a = [a["href"], []]
        elif tag == "link" and a.get("href") and any(k in (a.get("type") or "").lower() for k in ("rss", "atom")):
            self.feeds.append(a["href"])
        elif tag == "time" and a.get("datetime"):
            self.times.append((a["datetime"], ""))

    def handle_endtag(self, tag):
        if tag == "script" and self._ld:
            self.jsonld.append("".join(self._ld_buf))
            self._ld = False
        elif tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._a:
            self.links.append((self._a[0], " ".join("".join(self._a[1]).split())))
            self._a = None

    def handle_data(self, data):
        if self._ld:
            self._ld_buf.append(data)
        elif not self._skip:
            self.text.append(data)
            if self._in_title:
                self.title += data
            if self._a:
                self._a[1].append(data)

    @property
    def body(self) -> str:
        return " ".join(" ".join(self.text).split())

    def ld_objects(self):
        for raw in self.jsonld:
            try:
                data = json.loads(raw.strip())
            except Exception:
                continue
            stack = [data]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    yield cur
                    stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
                elif isinstance(cur, list):
                    stack.extend(cur)


def parse(r: FetchResult) -> Page:
    p = Page()
    try:
        p.feed(r.text)
        p.close()
    except Exception:
        pass
    return p


def _types(o: dict) -> set[str]:
    t = o.get("@type")
    return {t.lower()} if isinstance(t, str) else {str(x).lower() for x in t} if isinstance(t, list) else set()


# ------------------------------------------------------------------ website
SUFFIX = re.compile(r"\b(as|asa|ans|da|ba|sa|nuf|enk|kf|ks|if|stiftelse)\b", re.I)
BUCKETS = {
    "contact": ("kontakt", "contact"),
    "about": ("om-oss", "omoss", "about"),
    "careers": ("karriere", "jobb", "stilling", "career", "jobs", "ledige"),
    "news": ("nyheter", "aktuelt", "news", "presse", "press", "blogg", "blog"),
    "locations": ("avdeling", "lokasjon", "kontorer", "locations", "offices", "finn-oss"),
    "team": ("ansatte", "team", "ledelse", "management", "people"),
}


def slug(s: str) -> str:
    s = SUFFIX.sub(" ", (s or "").lower()).replace("æ", "ae").replace("ø", "o").replace("å", "a")
    return re.sub(r"[^a-z0-9]", "", s)


def _words(name: str) -> list[str]:
    n = SUFFIX.sub(" ", (name or "").lower()).replace("æ", "ae").replace("ø", "o").replace("å", "a")
    return [w for w in re.sub(r"[^a-z0-9 ]", " ", n).split() if w]


def candidates(name: str, registry_raw: str | None, extra: list[str], limit: int = 8) -> list[str]:
    """Candidate homepages only. Nothing here is evidence; verify_site is the gate.
    Order: official/registry-provided first, then generated guesses by likelihood."""
    out: list[str] = []

    def push(u):
        u = (u or "").strip()
        if not u:
            return
        if not u.startswith(("http://", "https://")):
            u = "https://" + u.lstrip("/")
        p = urlparse(u)
        if p.netloc and "." in p.netloc:
            n = f"{p.scheme}://{p.netloc}/"
            if n not in out:
                out.append(n)

    push(registry_raw)
    for e in extra:
        push(e)
    w = _words(name)
    if w:
        joined, hyph = "".join(w), "-".join(w)
        stems = [joined]
        if len(w) > 1:
            stems.append(hyph)
            if len(w[0]) >= 5:
                stems.append(w[0])
        for st in stems:
            if 4 <= len(st) <= 40:
                push(f"https://www.{st}.no")
        for st in stems[:1]:
            if 4 <= len(st) <= 40:
                push(f"https://{st}.com")
    return out[:limit]


def _norm_text(t: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (t or "").lower().replace("æ", "ae").replace("ø", "o").replace("å", "a")) + " "


def _located(text: str, places: list[str], postal: str | None) -> list[str]:
    """Corroboration that a page belongs to the registered place: exact postal code or place name as a word."""
    hits, t = [], _norm_text(text)
    if postal and re.fullmatch(r"\d{4}", postal) and f" {postal} " in t:
        hits.append("postal_code")
    for pl in places:
        w = _norm_text(pl).strip()
        if len(w) >= 3 and f" {w} " in t:
            hits.append("place:" + w)
            break
    return hits


def _form_phrase(name: str, text: str) -> bool:
    """True when the full legal name INCLUDING its legal form (e.g. 'Wedo AS') appears as a phrase."""
    m = re.search(r"\b(as|asa|ans|da|ba|sa|nuf|enk|kf|ks|if)\s*$", (name or "").lower().strip())
    words = _words(name)
    if not m or not words:
        return False
    return f" {' '.join(words)} {m.group(1)} " in _norm_text(text)


def _norid_registrant(c: Ctx, f: Fetcher, host: str):
    """For .no domains: Norid's public RDAP record may list the registrant organisation number.
    Schema-agnostic: we only look for OUR exact organisation number in the response."""
    host = host.lower().removeprefix("www.")
    if not host.endswith(".no"):
        return None
    dom = ".".join(host.split(".")[-2:])
    r = f.get(f"https://rdap.norid.no/domain/{dom}", ttl=604_800, accept="application/rdap+json")
    if r.ok and c.org in find_orgnums(r.text):
        return r, dom
    return None


def rd_ok(c: Ctx, rd, final: str, name: str, home_r, home_pg):
    r, dom = rd
    eid = c.ev(r, "open_data_api", "Norid RDAP public domain registration data", "rdap:registrant",
               f"registrant record for {dom} contains organisation number {c.org}", "norid_rdap.v1", url=r.url)
    link = EntityLink(c.org, "norid_rdap_registrant_orgnr", 1.0, ["domain_registrant_organisation_number"])
    c.add("digital_presence", "official_website", final, eid, link, 0.97)
    return final, home_r, home_pg, link  # later mining must use the homepage, not the RDAP record


def _host_token(url: str) -> str:
    return slug(urlparse(url).netloc.lower().removeprefix("www.").rsplit(".", 1)[0])


def verify_site(c: Ctx, f: Fetcher, name: str, cands: list[str], *, official: set[str] | None = None,
                places: list[str] | None = None, postal: str | None = None):
    """Identity gate. Publishes only when ONE of these holds:
      1. the exact organisation number is printed on the page;
      2. the candidate came from an official source (registry/Wikidata) AND the legal name is on the page;
      3. a guessed domain whose token equals the legal name exactly, the legal name is on the page, AND the page
         (or its contact/about page) shows the registered postal code or place.
    Everything weaker is recorded as ambiguous and never published."""
    official = official or set()
    places = [p for p in (places or []) if p]
    state, note, alts = "not_available", "no candidate satisfied the identity gate", []
    nm = slug(name)
    for url in cands:
        r = f.get(url, ttl=43_200)
        if r.blocked_by_robots:
            if state == "not_available":
                state, note = "blocked", f"{url} disallowed by robots.txt"
            continue
        if not r.ok or not r.is_html:
            continue
        pg = parse(r)
        body, found = pg.body, find_orgnums(pg.body)
        final = r.final_url or url
        if c.org in found:
            return _site_ok(c, r, pg, final, "orgnr_published_on_site", 1.0, ["organisasjonsnummer"], c.org)
        if found:
            state, note = "ambiguous", f"site publishes a different organisation number ({sorted(found)[0]})"
            alts.append({"url": final, "reason": "conflicting_orgnr"})
            continue
        name_in = [k for k, v in (("legal_name_in_body", body), ("legal_name_in_title", pg.title)) if nm and nm in slug(v)]
        is_official = urlparse(url).netloc.lower().removeprefix("www.") in official
        if is_official and name_in:
            return _site_ok(c, r, pg, final, "official_source_candidate_name_match", 0.95,
                            ["official_source_listing"] + name_in, name)
        host_exact = bool(nm) and _host_token(final) == nm
        if host_exact and name_in:
            base = f"{urlparse(final).scheme}://{urlparse(final).netloc}"
            loc = _located(body, places, postal)
            if _form_phrase(name, body):
                loc = loc + ["legal_name_with_legal_form_on_page"]
            if not loc:
                rd = _norid_registrant(c, f, urlparse(final).netloc)
                if rd:
                    return rd_ok(c, rd, final, name, r, pg)
                # contact/about/privacy/terms pages carry address, legal form, often the organisation number
                keys = BUCKETS["contact"] + BUCKETS["about"] + ("personvern", "privacy", "vilkar", "terms", "impressum")
                hops = []
                for h, label in pg.links:
                    u = urljoin(base + "/", h).split("#")[0]
                    low = (urlparse(u).path + " " + label).lower()
                    if urlparse(u).netloc == urlparse(final).netloc and any(k in low for k in keys) and u not in hops:
                        hops.append(u)
                for u in hops[:4]:
                    rr = f.get(u, ttl=43_200)
                    if not (rr.ok and rr.is_html):
                        continue
                    hp = parse(rr)
                    if c.org in find_orgnums(hp.body):
                        got = _site_ok(c, rr, hp, final, "orgnr_published_on_site", 1.0, ["organisasjonsnummer"], c.org, ev_url=u)
                        return got[0], r, pg, got[3]  # evidence = the hop page; mining continues on the homepage
                    loc = _located(hp.body, places, postal) + (["legal_name_with_legal_form_on_page"] if _form_phrase(name, hp.body) else [])
                    if loc:
                        break
            if loc:
                return _site_ok(c, r, pg, final, "exact_name_domain_and_registered_location", 0.9,
                                ["domain_equals_legal_name"] + name_in + loc, name)
        if name_in or host_exact:
            if state in ("not_available", "blocked"):
                state, note = "ambiguous", "name/domain resembles the company but nothing ties it to the registered entity or location"
            alts.append({"url": final, "matched_on": (["domain_equals_legal_name"] if host_exact else []) + name_in})
    c.absent("digital_presence", "official_website", state, note, alternatives=alts[:5])
    return None


def _site_ok(c, r, pg, final, method, score, on, span, ev_url=None):
    eid = c.ev(r, "company_owned", SITE_POLICY, "html:body", f"identity confirmed by {', '.join(on)}: {span}",
               "website_gate.v1", url=ev_url or final)
    link = EntityLink(c.org, method, score, on)
    c.add("digital_presence", "official_website", final, eid, link, min(0.99, score))
    return final, r, pg, link


SOCIAL = {"linkedin.com": "linkedin", "facebook.com": "facebook", "instagram.com": "instagram",
          "x.com": "x", "twitter.com": "x", "youtube.com": "youtube", "tiktok.com": "tiktok"}
BAD = ("/share", "/sharer", "/intent", "/dialog", "login", "signup", "/plugins", "privacy")


def _social(c, r, link, url, sel, seen) -> bool:
    pr = urlparse(url)
    plat = SOCIAL.get(pr.netloc.lower().removeprefix("www."))
    if not plat or any(b in url.lower() for b in BAD) or len(pr.path.strip("/")) < 2:
        return False
    clean = f"{pr.scheme}://{pr.netloc.lower()}{pr.path.rstrip('/')}"
    if clean in seen:
        return True
    seen.add(clean)
    eid = c.ev(r, "company_owned", SITE_POLICY, sel, f"link to {plat}: {clean}", "social_links.v1")
    c.add("social_profiles", f"{plat}_url", clean, eid, link, 0.9, key_extra=clean)
    return True


EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
PHONE = re.compile(r"(?:\+47\s?)?(?:\d{2}[\s.]?){3}\d{2}\b")
ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})")


def mine_site(c: Ctx, f: Fetcher, base: str, home_r, home_pg: Page, link, budget: int = 12) -> None:
    seen_social: set[str] = set()
    _ld_org(c, home_r, home_pg, link, seen_social)
    n = sum(_social(c, home_r, link, urljoin(base, h), "html:a[href]", seen_social) for h, _ in home_pg.links)
    if not n:
        c.absent("social_profiles", "company_owned_social_links", "not_available",
                 "verified site checked; no company-owned social links found")
    host = urlparse(base).netloc
    buckets: dict[str, list[str]] = {k: [] for k in BUCKETS}
    for h, label in home_pg.links:
        u = urljoin(base, h).split("#")[0]
        if urlparse(u).netloc != host:
            continue
        low = (urlparse(u).path + " " + label).lower()
        for b, keys in BUCKETS.items():
            if any(k in low for k in keys) and u not in buckets[b]:
                buckets[b].append(u)
                break
    plan = [(b, u) for b in BUCKETS for u in buckets[b][:2]][:budget]
    for b, u in plan:
        r = f.get(u, ttl=43_200)
        fam = {"contact": "contact", "about": "business_profile", "careers": "hiring",
               "news": "public_activity", "locations": "locations", "team": "leadership"}[b]
        if r.blocked_by_robots:
            c.absent(fam, f"{b}_page", "blocked", f"{u} disallowed by robots.txt", key_extra=u)
            continue
        if not r.ok or not r.is_html:
            c.absent(fam, f"{b}_page", "failed", f"{u} unavailable ({r.error})", key_extra=u)
            continue
        pg = parse(r)
        try:
            {"contact": _contact, "about": lambda *a: _ld_org(*a, seen_social), "careers": _jobs,
             "news": _news, "locations": _locs, "team": _team}[b](c, r, pg, link)
        except Exception as e:  # noqa: BLE001
            c.absent(fam, f"{b}_extract", "failed", f"extraction raised {type(e).__name__}", key_extra=u)
    for fu in home_pg.feeds[:2]:
        fr = f.get(urljoin(base, fu), ttl=21_600)
        if fr.ok:
            _feed(c, fr, link)


def _ld_org(c, r, pg, link, seen) -> None:
    hit = False
    for o in pg.ld_objects():
        if not _types(o) & {"organization", "corporation", "localbusiness", "store", "ngo"}:
            continue
        hit = True
        for key, fam, fld in (("description", "business_profile", "site_description"),
                              ("telephone", "contact", "telephone"), ("email", "contact", "email"),
                              ("foundingDate", "legal_identity", "site_founding_date")):
            v = o.get(key)
            if isinstance(v, str) and v.strip():
                eid = c.ev(r, "company_owned", SITE_POLICY, f"jsonld:Organization.{key}", f"{key}: {v}", "jsonld.v1")
                c.add(fam, fld, v.strip()[:500], eid, link, 0.9, key_extra=v.strip()[:40])
        same = o.get("sameAs")
        for u in ([same] if isinstance(same, str) else (same or [])):
            _social(c, r, link, str(u), "jsonld:sameAs", seen)
    if not hit:
        c.absent("business_profile", "structured_organisation_data", "not_available",
                 "page fetched; no schema.org Organization block present", key_extra=r.url)


def _contact(c, r, pg, link) -> None:
    host = urlparse(r.final_url or r.url).netloc.lower().removeprefix("www.")
    mails = {h[7:].split("?")[0] for h, _ in pg.links if h.lower().startswith("mailto:")} | set(EMAIL.findall(pg.body)[:5])
    kept = 0
    for m in sorted(mails):
        dom = m.rsplit("@", 1)[-1].lower().removeprefix("www.")
        if dom != host and not host.endswith("." + dom) and not dom.endswith("." + host):
            continue  # third-party addresses are not claims about this company
        eid = c.ev(r, "company_owned", SITE_POLICY, "html:a[href^=mailto]", f"contact email: {m}", "contact.v1")
        c.add("contact", "email", m, eid, link, 0.9, key_extra=m)
        kept += 1
    if not kept:
        c.absent("contact", "email", "not_available", "contact page fetched; no same-domain email published")
    phones = {re.sub(r"[^\d+]", "", h[4:]) for h, _ in pg.links if h.lower().startswith("tel:")}
    phones |= {re.sub(r"[^\d+]", "", p) for p in PHONE.findall(pg.body)[:3]}
    phones = {p for p in phones if len(re.sub(r"\D", "", p)) >= 8}
    for p in sorted(phones)[:3]:
        eid = c.ev(r, "company_owned", SITE_POLICY, "html:a[href^=tel]", f"contact phone: {p}", "contact.v1")
        c.add("contact", "telephone", p, eid, link, 0.85, key_extra=p)
    if not phones:
        c.absent("contact", "telephone", "not_available", "contact page fetched; no telephone number published")


def _jobs(c, r, pg, link) -> None:
    found = 0
    for o in pg.ld_objects():
        if "jobposting" not in _types(o):
            continue
        title = o.get("title") or o.get("name")
        if not isinstance(title, str) or not title.strip():
            continue
        posted = o.get("datePosted")
        posted = posted[:10] if isinstance(posted, str) else None
        loc = o.get("jobLocation")
        loc = loc[0] if isinstance(loc, list) and loc else loc
        place = ((loc or {}).get("address") or {}).get("addressLocality") if isinstance(loc, dict) else None
        eid = c.ev(r, "company_owned", SITE_POLICY, "jsonld:JobPosting",
                   f"{title}" + (f" — {place}" if place else "") + (f" (posted {posted})" if posted else ""),
                   "jobposting_ld.v1", pub=posted)
        c.add("hiring", "open_position", {"title": title.strip()[:200], "location": place,
                                           "date_posted": posted, "url": o.get("url") or r.url},
              eid, link, 0.9, key_extra=title.strip().lower()[:80])
        found += 1
    if found:
        eid = c.ev(r, "company_owned", SITE_POLICY, "html:body", f"{found} vacancy entries listed", "jobs_count.v1")
        c.add("hiring", "open_position_count", found, eid, link, 0.85, key_extra=r.url)
    else:
        c.absent("hiring", "open_position", "not_available",
                 "careers page fetched; no structured vacancies listed at retrieval time", key_extra=r.url)


def _undated(c, r, link, headline, url, sel, ex) -> None:
    eid = c.ev(r, "company_owned", SITE_POLICY, sel, headline, ex)
    c.claims.append(Claim(
        f"{c.org}|public_activity|news_item|{headline.lower()[:80]}", "public_activity", "news_item",
        "ambiguous", None, 0.0, [eid], link,
        alternatives=[{"headline": headline[:240], "url": url, "date_published": None}],
        note="item found on a company-owned page but exposes no publication date; withheld from dated activity"))


def _news(c, r, pg, link) -> None:
    found = 0
    for o in pg.ld_objects():
        if not _types(o) & {"newsarticle", "blogposting", "article"}:
            continue
        head = o.get("headline") or o.get("name")
        if not isinstance(head, str) or len(head.strip()) < 6:
            continue
        pub = o.get("datePublished") or o.get("dateModified")
        pub = pub[:10] if isinstance(pub, str) and ISO.match(pub) else None
        if pub is None:
            _undated(c, r, link, head.strip(), o.get("url") or r.url, "jsonld:Article", "news_ld.v1")
            continue
        eid = c.ev(r, "company_owned", SITE_POLICY, "jsonld:Article", f"{head.strip()} ({pub})", "news_ld.v1", pub=pub)
        c.add("public_activity", "news_item", {"headline": head.strip()[:240], "date_published": pub,
                                                "url": o.get("url") or r.url},
              eid, link, 0.88, key_extra=head.strip().lower()[:80])
        found += 1
        if found >= 20:
            break
    if not found:
        c.absent("public_activity", "news_item", "not_available",
                 "news page fetched; no dated items extractable", key_extra=r.url)


def _feed(c, r, link) -> None:
    from email.utils import parsedate_to_datetime
    for raw in re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", r.text, re.S | re.I)[:20]:
        tm = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
        dm = re.search(r"<(?:pubDate|published|updated|dc:date)[^>]*>(.*?)</", raw, re.S | re.I)
        if not tm:
            continue
        title = " ".join(re.sub(r"<[^>]+>", "", tm.group(1).replace("<![CDATA[", "").replace("]]>", "")).split())
        if len(title) < 6:
            continue
        pub = None
        if dm:
            m = ISO.search(dm.group(1))
            try:
                pub = m.group(1) if m else parsedate_to_datetime(dm.group(1).strip()).date().isoformat()
            except Exception:
                pub = None
        if pub is None:
            _undated(c, r, link, title, r.url, "feed:item", "feed.v1")
            continue
        eid = c.ev(r, "company_owned", SITE_POLICY, "feed:item", f"{title} ({pub})", "feed.v1", pub=pub)
        c.add("public_activity", "news_item", {"headline": title[:240], "date_published": pub, "url": r.url},
              eid, link, 0.86, key_extra=title.lower()[:80])


def _locs(c, r, pg, link) -> None:
    found = 0
    for o in pg.ld_objects():
        if not _types(o) & {"localbusiness", "store", "place"}:
            continue
        a = o.get("address")
        if not isinstance(a, dict):
            continue
        pretty = ", ".join(str(a[k]) for k in ("streetAddress", "postalCode", "addressLocality") if a.get(k))
        if pretty:
            eid = c.ev(r, "company_owned", SITE_POLICY, "jsonld:Place.address", pretty, "locations_ld.v1")
            c.add("locations", "published_location", {"name": o.get("name"), "address": pretty},
                  eid, link, 0.85, key_extra=pretty.lower()[:80])
            found += 1
    if not found:
        c.absent("locations", "published_location", "not_available",
                 "locations page fetched; no structured addresses present", key_extra=r.url)


def _team(c, r, pg, link) -> None:
    found = 0
    for o in pg.ld_objects():
        n = o.get("name")
        if "person" in _types(o) and isinstance(n, str) and len(n.split()) >= 2:
            role = o.get("jobTitle") if isinstance(o.get("jobTitle"), str) else None
            eid = c.ev(r, "company_owned", SITE_POLICY, "jsonld:Person", f"{n}" + (f" — {role}" if role else ""), "team_ld.v1")
            c.add("leadership", "site_published_person", {"name": n.strip()[:120], "title": role},
                  eid, link, 0.82, key_extra=n.strip().lower())
            found += 1
    if not found:
        c.absent("leadership", "site_published_person", "not_available",
                 "team page fetched; no structured person entries present", key_extra=r.url)
