"""Fully offline: network replaced by a fixture router. Run: python -m unittest -v"""
import hashlib
import json
import sys
import urllib.error
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parents[1] / "src"))
import tempfile
import unittest
from pathlib import Path

from signalpost import core
from signalpost.core import Claim, EntityLink
from signalpost.fetch import FetchResult, Fetcher
from signalpost.pipeline import Agent, State, diff, run_batch, validate
from signalpost.render import render

ORG = "923609016"
ER = "https://data.brreg.no/enhetsregisteret/api"
RR = "https://data.brreg.no/regnskapsregisteret/regnskap"


def res(url, payload=None, text=None, status=200, ctype="application/json", error=None):
    body = (json.dumps(payload) if payload is not None else (text or "")).encode()
    ok = status == 200 and not error
    return FetchResult(url=url, final_url=url, status=status, ok=ok, body=body, text=body.decode(),
                       content_type=ctype, content_sha256=hashlib.sha256(body).hexdigest(), error=error)


ENTITY = {"organisasjonsnummer": ORG, "navn": "FIXTURE INDUSTRIER AS",
          "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
          "registreringsdatoEnhetsregisteret": "1995-09-18", "antallAnsatte": 412,
          "naeringskode1": {"kode": "25.620", "beskrivelse": "Bearbeiding av metaller"},
          "konkurs": False, "underAvvikling": False, "hjemmeside": "www.fixtureindustrier.no",
          "forretningsadresse": {"adresse": ["Industriveien 4"], "postnummer": "4001", "poststed": "STAVANGER"}}
ROLES = {"rollegrupper": [{"roller": [{"type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}, "fratraadt": False,
                                       "person": {"navn": {"fornavn": "Kari", "etternavn": "Nordmann"}}}]}]}
ACCOUNTS = [{"regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"}, "valuta": "NOK",
             "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 884000000},
                                                              "driftsresultat": 83000000}, "aarsresultat": 61500000},
             "eiendeler": {"sumEiendeler": 1204000000}}]
HOME = (f'<html><head><title>Fixture Industrier AS</title><script type="application/ld+json">'
        '{"@type":"Organization","sameAs":["https://www.linkedin.com/company/fixture-industrier"]}</script></head>'
        f'<body><p>Fixture Industrier AS, org.nr. 923 609 016</p><a href="/kontakt">Kontakt</a>'
        '<a href="/karriere">Karriere</a><a href="/nyheter">Nyheter</a></body></html>')
CAREERS = ('<html><script type="application/ld+json">{"@type":"JobPosting","title":"Sveiser",'
           '"datePosted":"2026-09-30","jobLocation":{"address":{"addressLocality":"Stavanger"}}}</script></html>')
NEWS = ('<html><script type="application/ld+json">[{"@type":"NewsArticle","headline":"Ny produksjonslinje i drift",'
        '"datePublished":"2026-08-14"},{"@type":"NewsArticle","headline":"Artikkel uten dato her"}]</script></html>')
CONTACT = '<html><a href="mailto:post@fixtureindustrier.no">p</a><a href="mailto:x@gmail.com">x</a><a href="tel:+4751000000">t</a></html>'

ROUTES = {f"{ER}/enheter/{ORG}/roller": res("", ROLES), f"{ER}/enheter/{ORG}": res("", ENTITY),
          f"{RR}/{ORG}": res("", ACCOUNTS),
          "https://www.fixtureindustrier.no/kontakt": res("", text=CONTACT, ctype="text/html"),
          "https://www.fixtureindustrier.no/karriere": res("", text=CAREERS, ctype="text/html"),
          "https://www.fixtureindustrier.no/nyheter": res("", text=NEWS, ctype="text/html"),
          "https://www.fixtureindustrier.no/": res("", text=HOME, ctype="text/html")}


class FakeFetcher(Fetcher):
    def __init__(self, routes=None, default=None):
        super().__init__(None)
        self.routes, self.default = ROUTES if routes is None else routes, default

    def get(self, url, *, ttl=0, accept=None):
        self.requests += 1
        for k, v in sorted(self.routes.items(), key=lambda kv: -len(kv[0])):
            if url == k or url.startswith(k):
                return FetchResult(**{**v.__dict__, "url": url, "final_url": url})
        if self.default:
            return self.default(url)
        if "wikidata" in url:
            return res(url, {"results": {"bindings": []}})
        if "gleif" in url:
            return res(url, {"data": []})
        return res(url, text="", status=404, error="http_404")


def agent(tmp, fetcher=None):
    return Agent(fetcher or FakeFetcher(), State(Path(tmp) / "state"))


class Tests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = self._t.name

    def tearDown(self):
        self._t.cleanup()

    def test_orgnum(self):
        self.assertTrue(core.is_valid(ORG))
        self.assertFalse(core.is_valid("923609017"))
        self.assertFalse(core.is_valid("12345"))
        self.assertEqual(core.find_orgnums("org.nr. 923 609 016"), {ORG})

    def test_claim_contract(self):
        with self.assertRaises(ValueError):
            Claim("k", "f", "x", "available", 1)  # no evidence
        with self.assertRaises(ValueError):
            Claim("k", "f", "x", "not_available", 0, note="n")  # absence as zero
        with self.assertRaises(ValueError):
            Claim("k", "f", "x", "not_available")  # no explanation

    def test_happy_path(self):
        env = agent(self.tmp).run_one(f" {ORG} ")
        self.assertEqual(validate(env), [])
        keys = {f"{c['field_family']}|{c['field']}" for c in env["claims"] if c["availability"] == "available"}
        for k in ("legal_identity|legal_name", "financials|latest_revenue", "digital_presence|official_website",
                  "hiring|open_position", "public_activity|news_item", "social_profiles|linkedin_url", "contact|email"):
            self.assertIn(k, keys)
        self.assertNotIn("gmail", json.dumps(env["claims"]))  # third-party address never published
        self.assertIn("884 000 000", " ".join(env["synthesis"]["summary"]))

    def test_undated_news_is_ambiguous_not_published(self):
        env = agent(self.tmp).run_one(ORG)
        und = [c for c in env["claims"] if c["field"] == "news_item" and c["availability"] == "ambiguous"]
        self.assertEqual(len(und), 1)
        self.assertIsNone(und[0]["alternatives"][0]["date_published"])

    def test_wrong_company_site_rejected(self):
        routes = dict(ROUTES)
        routes["https://www.fixtureindustrier.no/"] = res("", text="<html>Helt annet AS org.nr. 999 999 999</html>", ctype="text/html")
        env = agent(self.tmp, FakeFetcher(routes)).run_one(ORG)
        site = next(c for c in env["claims"] if c["field"] == "official_website")
        self.assertEqual(site["availability"], "ambiguous")
        self.assertEqual(validate(env), [])

    def test_weak_match_not_published(self):
        routes = dict(ROUTES)
        routes["https://www.fixtureindustrier.no/"] = res("", text="<html><title>x</title>hello world</html>", ctype="text/html")
        env = agent(self.tmp, FakeFetcher(routes)).run_one(ORG)
        site = next(c for c in env["claims"] if c["field"] == "official_website")
        self.assertNotEqual(site["availability"], "available")

    def test_invalid_input_one_envelope(self):
        env = agent(self.tmp).run_one("not-a-number")
        self.assertFalse(env["input_valid"])
        self.assertEqual(env["run"]["terminal_status"], "failed")
        self.assertEqual(validate(env), [])

    def test_network_down_is_typed(self):
        env = agent(self.tmp, FakeFetcher({}, default=lambda u: res(u, text="", status=None, error="timeout"))).run_one(ORG)
        self.assertEqual(env["run"]["terminal_status"], "failed")
        self.assertTrue(all(c["availability"] != "available" for c in env["claims"]))
        self.assertEqual(validate(env), [])

    def test_missing_registry_company(self):
        env = agent(self.tmp, FakeFetcher({})).run_one(ORG)
        self.assertEqual(env["claims"][0]["availability"], "not_available")

    def test_batch_returns_one_per_input(self):
        batch = [ORG, "", "abc", "000000000", "12 34", ORG]
        envs = run_batch(batch, agent(self.tmp), workers=3)
        self.assertEqual(len(envs), len(batch))
        self.assertTrue(all(validate(e) == [] for e in envs))

    def test_batch_timeout_envelope(self):
        import time

        class Slow(FakeFetcher):
            def get(self, url, **kw):
                time.sleep(2)
                return super().get(url, **kw)
        envs = run_batch([ORG], agent(self.tmp, Slow()), workers=1, company_timeout=0.2)
        self.assertEqual(len(envs), 1)
        self.assertTrue(envs[0]["input_valid"])  # valid number must not be marked invalid
        self.assertEqual(envs[0]["run"]["terminal_status"], "failed")

    def test_rerun_idempotent_and_change_detection(self):
        a = agent(self.tmp)
        first, second = a.run_one(ORG), a.run_one(ORG)
        self.assertTrue(first["refresh"]["is_first_observation"])
        self.assertFalse(second["refresh"]["is_first_observation"])
        self.assertEqual([c for c in second["changes"] if c["change_type"] != "added"], [])
        link = EntityLink(ORG, "m", 1.0, ["x"])
        prev = {"claims": {"k": {"family": "legal_identity", "field": "legal_name", "availability": "available",
                                 "hash": "a", "value": "OLD", "first": "2026-01-01T00:00:00Z"}}, "runs": 1}
        ch, _ = diff(prev, [Claim("k", "legal_identity", "legal_name", "available", "NEW", 1, ["e"], link)])
        self.assertEqual((ch[0]["change_type"], ch[0]["materiality"]), ("changed", "material"))

    def test_render(self):
        env = agent(self.tmp).run_one(ORG)
        self.assertEqual(render([env], Path(self.tmp) / "site"), 1)
        page = (Path(self.tmp) / "site" / f"{ORG}.html").read_text("utf-8")
        self.assertIn("data.brreg.no", page)
        self.assertIn("Kari Nordmann", page)


class GateTests(unittest.TestCase):
    NAME = "BLUE BAY AS"

    def gate(self, routes, cands, official=None):
        from signalpost.core import Ctx
        from signalpost.sources import verify_site
        c = Ctx(ORG)
        res = verify_site(c, FakeFetcher(routes), self.NAME, cands, official=official or set(),
                          places=["SANDVIKA", "BAERUM"], postal="1337")
        site = [x for x in c.claims if x.field == "official_website"]
        return res, site

    def page(self, html):
        return res("", text=f"<html><head><title>Blue Bay</title></head><body>{html}</body></html>", ctype="text/html")

    def test_lookalike_without_location_not_published(self):
        # right name + exact domain, but an asset manager in London: must stay ambiguous
        r, site = self.gate({"https://www.bluebay.no/": self.page("Blue Bay institutional investors London")},
                            ["https://www.bluebay.no/"])
        self.assertIsNone(r)
        self.assertEqual(site[0].availability, "ambiguous")

    def test_guess_with_postal_code_on_contact_page_published(self):
        routes = {"https://www.bluebay.no/": self.page('Blue Bay AS <a href="/kontakt">Kontakt</a>'),
                  "https://www.bluebay.no/kontakt": self.page("Blue Bay, Postveien 1, 1337 Sandvika")}
        r, site = self.gate(routes, ["https://www.bluebay.no/"])
        self.assertIsNotNone(r)
        self.assertEqual(site[0].entity_link.method, "exact_name_domain_and_registered_location")

    def test_substring_domain_never_matches(self):
        routes = {"https://www.rbcbluebay.com/": self.page("Blue Bay 1337 Sandvika")}
        r, site = self.gate(routes, ["https://www.rbcbluebay.com/"])
        self.assertIsNone(r)  # host token != legal-name token and not from an official source

    def test_official_source_candidate_with_name_published(self):
        routes = {"https://carlin.no/": self.page("Blue Bay")}
        r, site = self.gate(routes, ["https://carlin.no/"], official={"carlin.no"})
        self.assertIsNotNone(r)
        self.assertEqual(site[0].entity_link.method, "official_source_candidate_name_match")

    def test_legal_form_phrase_published(self):
        r, site = self.gate({"https://www.bluebay.no/": self.page("Velkommen. © 2026 Blue Bay AS. Alle rettigheter")},
                            ["https://www.bluebay.no/"])
        self.assertIsNotNone(r)
        self.assertIn("legal_name_with_legal_form_on_page", site[0].entity_link.matched_on)

    def test_orgnr_on_privacy_page_published_with_hop_evidence(self):
        from signalpost.core import Ctx
        from signalpost.sources import verify_site
        routes = {"https://www.bluebay.no/": self.page('Blue Bay <a href="/personvern">Personvern</a>'),
                  "https://www.bluebay.no/personvern": self.page("Blue Bay, org.nr 923 609 016")}
        c = Ctx(ORG)
        out = verify_site(c, FakeFetcher(routes), self.NAME, ["https://www.bluebay.no/"], places=[], postal=None)
        self.assertIsNotNone(out)
        self.assertEqual(out[0], "https://www.bluebay.no/")          # claim value is the homepage
        self.assertTrue(out[1].url.endswith("bluebay.no/"))            # mining continues on the homepage
        self.assertTrue(any(e.source_url.endswith("/personvern") for e in c.evidence))

    def test_norid_rdap_registrant_orgnr_published(self):
        routes = {"https://www.bluebay.no/": self.page("Blue Bay"),
                  "https://rdap.norid.no/domain/bluebay.no": res("", {"entities": [{"roles": ["registrant"],
                                                                                    "handle": "NO923609016"}]})}
        r, site = self.gate(routes, ["https://www.bluebay.no/"])
        self.assertIsNotNone(r)
        self.assertEqual(site[0].entity_link.method, "norid_rdap_registrant_orgnr")

    def test_rdap_for_other_org_ignored(self):
        routes = {"https://www.bluebay.no/": self.page("Blue Bay"),
                  "https://rdap.norid.no/domain/bluebay.no": res("", {"handle": "NO974760673"})}
        r, site = self.gate(routes, ["https://www.bluebay.no/"])
        self.assertIsNone(r)

    def test_dead_domain_not_retried(self):
        import socket
        import urllib.request
        from signalpost.fetch import Fetcher
        calls = []
        orig = urllib.request.urlopen

        def boom(*a, **k):
            calls.append(1)
            raise urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))
        urllib.request.urlopen = boom
        try:
            r = Fetcher(None, delay=0, obey_robots=False).get("https://no-such-host-xyz.no/")
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(len(calls), 1)
        self.assertFalse(r.ok)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = self._t.name

    def tearDown(self):
        self._t.cleanup()

    def test_soft_deadline_registry_only(self):
        import time
        ff = FakeFetcher()
        a = Agent(ff, State(Path(self.tmp) / "s"), soft_deadline=time.monotonic() - 1)
        env = a.run_one(ORG)
        self.assertEqual(validate(env), [])
        fams = {c["field_family"] for c in env["claims"] if c["availability"] == "available"}
        self.assertIn("financials", fams)
        self.assertNotIn("hiring", fams)
        self.assertFalse(any("fixtureindustrier" in u for u in [e["source_url"] for e in env["evidence"]]))
        self.assertTrue(any(x["kind"] == "SoftDeadline" for x in env["errors"]))

    def test_hard_deadline_still_one_envelope_each(self):
        import time

        class Slow(FakeFetcher):
            def get(self, url, **kw):
                time.sleep(1.5)
                return super().get(url, **kw)
        ag = Agent(Slow(), State(Path(self.tmp) / "s"))
        envs = run_batch([ORG, "923609016", "abc"], ag, workers=2, company_timeout=60,
                         hard_deadline=time.monotonic() + 0.3)
        self.assertEqual(len(envs), 3)
        self.assertTrue(all(validate(e) == [] for e in envs))


class ContractTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def test_manifest_official_jsonl(self):
        from signalpost.pipeline import read_manifest
        p = self.tmp / "m.jsonl"
        p.write_text('{"organisation_number":"923609016","name":"A AS","website":""}\n'
                     '{"organisation_number":"810034882","name":"B AS","website":"b.no"}\n'
                     '{"organisation_number":"923609016","name":"dup"}\n')
        orgs, hints = read_manifest(p)
        self.assertEqual(orgs, ["923609016", "810034882"])
        self.assertEqual(hints["810034882"]["website"], "b.no")

    def test_manifest_other_formats(self):
        from signalpost.pipeline import read_manifest
        for name, body in (("a.txt", "923609016\n# c\n\n810034882\n"),
                           ("b.csv", "organisation_number,name\n923609016,X\n810034882,Y\n"),
                           ("c.json", '[{"organisation_number":"923609016"},"810034882"]'),
                           ("d.json", '{"organisations":["923609016","810034882"]}')):
            p = self.tmp / name
            p.write_text(body)
            self.assertEqual(read_manifest(p)[0], ["923609016", "810034882"], name)

    def test_candidates(self):
        from signalpost.sources import candidates
        c = candidates("SANDNES ELEKTRISKE AS", "", [])
        self.assertIn("https://www.sandneselektriske.no/", c)
        self.assertIn("https://www.sandnes-elektriske.no/", c)
        self.assertEqual(candidates("X AS", "", []), [])  # too short to guess
        self.assertEqual(candidates("A AS", "firma.no", [])[0], "https://firma.no/")

    def test_cli_end_to_end_and_previous(self):
        import signalpost.cli as cli
        import sys
        man = self.tmp / "in.jsonl"
        man.write_text(f'{{"organisation_number":"{ORG}","name":"FIXTURE INDUSTRIER AS"}}\nbad\n')
        out, rep = self.tmp / "o.jsonl", self.tmp / "r.json"
        orig = cli.Fetcher
        cli.Fetcher = lambda *a, **k: FakeFetcher()
        try:
            argv = ["signalpost", "run", "--organisations", str(man), "--output", str(out), "--report", str(rep),
                    "--run-id", "t1", "--expected-count", "2", "--state", str(self.tmp / "s1")]
            sys.argv = argv
            self.assertEqual(cli.main(), 0)
            self.assertEqual(len(out.read_text().splitlines()), 2)
            self.assertEqual(json.loads(rep.read_text())["silent_drops"], 0)
            out2 = self.tmp / "o2.jsonl"
            sys.argv = ["signalpost", "run", "--organisations", str(man), "--output", str(out2), "--report", str(rep),
                        "--run-id", "t2", "--expected-count", "2", "--state", str(self.tmp / "s2"), "--previous", str(out)]
            self.assertEqual(cli.main(), 0)
            env = json.loads(out2.read_text().splitlines()[0])
            self.assertFalse(env["refresh"]["is_first_observation"])
            self.assertEqual([c for c in env["changes"] if c["change_type"] != "added"], [])
            sys.argv = ["signalpost", "run", "--organisations", str(man), "--output", str(out2), "--run-id", "t3",
                        "--expected-count", "5", "--state", str(self.tmp / "s3")]
            self.assertEqual(cli.main(), 1)  # wrong expected count must fail loudly
        finally:
            cli.Fetcher = orig


if __name__ == "__main__":
    unittest.main()
