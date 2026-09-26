"""Offline checks for identity proof, robots.txt, refresh diffing, evidence retention and input parsing.

Run: python3 tests/test_agent.py
"""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from signalpost.__main__ import read_inputs  # noqa: E402
from signalpost.envelope import assemble, failed_envelope  # noqa: E402
from signalpost.net import MAX_CRAWL_DELAY, Resp, Robots  # noqa: E402
from signalpost.profile import Builder  # noqa: E402
from signalpost.web import Page, org_numbers, valid_org, verify  # noqa: E402

ENTITY = {"organisasjonsnummer": "810034882", "navn": "SANDNES ELEKTRISKE AS", "telefon": "51 68 57 00",
          "epostadresse": "post@sandneselektriske.no", "forretningsadresse": {"adresse": ["Strandgata 21"]}}


def page(url: str, html: str) -> tuple[Resp, Page]:
    resp = Resp(url, url, 200, html.encode(), "text/html; charset=utf-8", "2026-09-26T00:00:00Z", "x" * 64, "snapshots/x.html")
    p = Page(url)
    p.feed(html)
    return resp, p


def decide(url: str, html: str, origin: str = "name_domain_guess", people=(), extra: str | None = None) -> str:
    pages = [page(url, html)] + ([page(url + "kontakt", extra)] if extra else [])
    return verify(ENTITY, ENTITY["navn"], pages, origin, list(people))[0]


def test_org_numbers():
    assert valid_org("810034882") and not valid_org("810034883")
    found, labelled = org_numbers("Sandnes Elektriske AS · Org.nr: 810 034 882 MVA · tlf 51 68 57 00")
    assert found == {"810034882"} and labelled == {"810034882"}


def test_identity_needs_the_site_operator():
    # Ownership comes from where a site names its operator (footer, title, og:site_name, JSON-LD, contact page).
    assert decide("https://x.no/", "<footer>Org nr 810034882</footer>") == "exact"
    assert decide("https://sandneselektriske.no/", "<title>Sandnes Elektriske AS</title>") == "exact"  # unique name + form, .no
    assert decide("https://sandneselektriske.com/", "<title>Sandnes Elektriske</title><footer>Tlf 51 68 57 00</footer>") == "exact"
    assert decide("https://x.com/", "<title>Sandnes Elektriske</title>", people=["Morten Aarrestad"],
                  extra="<p>Daglig leder Morten Aarrestad</p>") == "exact"  # a registered role holder on the contact page
    # A mention in body copy is not ownership (reviewer reproduction: a customer list with another operator in the footer).
    customers = "<h1>Our customers</h1><p>Sandnes Elektriske AS, Strandgata 21, tlf 51 68 57 00</p><footer>Webbyrå AS Org.nr 979543883</footer>"
    assert decide("https://sandneselektriske.no/", customers) == "related"
    assert decide("https://sandneselektriske.com/", "<p>Sandnes Elektriske AS</p>") == "weak"
    # An org number labelled in body copy counts only when it is the only labelled number on the site.
    assert decide("https://x.com/", "<p>Org.nr 810034882</p>") == "exact"
    assert decide("https://x.com/", "<p>Org.nr 810034882</p><p>Org.nr 979543883</p>") == "weak"
    assert decide("https://x.com/", '<a href="https://proff.no/x/810034882">Proff</a>') == "weak"  # no link-substring shortcut
    # A register-listed site whose operator is another organisation is related, not exact.
    assert decide("https://group.no/", "<footer>Org.nr 979543883</footer>", origin="registry_website") == "related"
    # A register e-mail domain that does not spell the name needs more than the name (parent/developer sites).
    assert decide("https://developer.no/", "<title>Sandnes Elektriske</title><footer>post@sandneselektriske.no</footer>",
                  origin="registry_email_domain") == "weak"


def test_robots_rfc9309():
    r = Robots("User-agent: *\nDisallow: /private/*\nDisallow: /private$\nAllow: /private/ok\nDisallow: /*.pdf$\nCrawl-delay: 2\n")
    assert not r.allowed("https://x/private/secret") and not r.allowed("https://x/private")
    assert r.allowed("https://x/private/ok") and r.allowed("https://x/privateer") and r.allowed("https://x/")
    assert not r.allowed("https://x/a/doc.pdf") and r.allowed("https://x/a/doc.pdf?page=2") and r.allowed("https://x/robots.txt")
    assert r.delay == 2
    tie = Robots("User-agent: *\nDisallow: /a\nAllow: /a$\n")
    assert tie.allowed("https://x/a") and not tie.allowed("https://x/ab")  # longest match; Allow wins a tie
    ours = Robots("User-agent: SignalpostResearchAgent\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert not ours.allowed("https://x/anything")  # our product-token group overrides "*"
    assert Robots("User-agent: *\nCrawl-delay: 30\n").delay > MAX_CRAWL_DELAY  # Fetcher skips such a site rather than go faster


def snapshot(out: Path, body: bytes) -> Resp:
    digest = hashlib.sha256(body).hexdigest()
    (out / "snapshots").mkdir(parents=True, exist_ok=True)
    (out / "snapshots" / f"{digest}.json").write_bytes(body)
    return Resp("https://api/x", "https://api/x", 200, body, "application/json", "2026-09-26T00:00:00Z", digest, f"snapshots/{digest}.json")


def build(out: Path, values: dict, sources: dict) -> Builder:
    """values: claim id -> (section, value, basis); one snapshot per claim so every change has distinct evidence."""
    b = Builder("810034882")
    for cid, (section, value, basis) in values.items():
        e = b.ev(snapshot(out, json.dumps({cid: value}).encode()), "official_registry", "test")
        b.claim(cid, section, cid.split(".")[-1], value, [e], basis=basis)
    for name, ok in sources.items():
        b.source(name, ok)
    return b


BASE = {"identity.legal_name": ("identity", "SANDNES ELEKTRISKE AS", None), "identity.registered_employees": ("identity", 11, None),
        "locations.business_address": ("locations", {"address": "Strandgata 21"}, None),
        "locations.subunit.973150480": ("locations", {"name": "AVD INSTALLASJON", "organisation_number": "973150480"}, None),
        "website.official_website": ("website", {"url": "https://x.no/", "domain": "x.no"}, "registry_website"),
        "social_profiles.facebook": ("social_profiles", {"platform": "facebook", "url": "https://facebook.com/x"}, "linked_from_verified_official_website"),
        "jobs.posting.abc": ("jobs", {"title": "Elektriker", "url": "https://x.no/jobb/elektriker"}, "linked_from_verified_official_website"),
        "jobs.posting.nav.u1": ("jobs", {"title": "Montør", "url": "https://arbeidsplassen.nav.no/stillinger/stilling/u1"}, "official_job_feed")}
ALL_OK = {s: True for s in ("brreg_entity", "brreg_subunits", "site_home", "site_jobs", "nav")}


def run(n: int) -> dict:
    return {"run_id": f"r{n}", "completed_at": f"2026-09-0{n}T00:00:00Z"}


def roundtrip(env: dict) -> dict:
    return json.loads(json.dumps(env))


def test_refresh_changes_and_partial_sources():
    with tempfile.TemporaryDirectory() as tmp:
        d1, d2 = Path(tmp, "1"), Path(tmp, "2")
        first = roundtrip(assemble(build(d1, BASE, ALL_OK), run(1), None, d1, None))
        assert roundtrip(assemble(build(d2, BASE, ALL_OK), run(2), first, d2, d1))["changes"] == []  # idempotent
        changed = dict(BASE, **{"identity.registered_employees": ("identity", 12, None)})
        second = assemble(build(d2, changed, ALL_OK), run(2), first, d2, d1)
        assert [(c["claim_id"], c["old_value"], c["new_value"]) for c in second["changes"]] == [("identity.registered_employees", 11, 12)]
        # Reviewer reproduction: subunit list 503 while the business address (entity record) succeeds -> no location_closed.
        partial = {k: v for k, v in BASE.items() if not k.startswith(("locations.subunit", "jobs"))}
        kept = assemble(build(d2, partial, dict(ALL_OK, brreg_subunits=False, site_jobs=False, nav=False)), run(2), first, d2, d1)
        assert kept["changes"] == [], kept["changes"]
        stale = {c["id"] for c in kept["claims"] if c.get("stale")}
        assert stale == {"locations.subunit.973150480", "jobs.posting.abc", "jobs.posting.nav.u1"}
        # Fully re-read sources that no longer list a fact do report it.
        gone = assemble(build(d2, partial, ALL_OK), run(2), first, d2, d1)
        assert {c["event"] for c in gone["changes"]} == {"location_closed", "job_posting_closed"}
        # A fact from a source that failed last time is a first observation, not an "added" change.
        prev_failed = roundtrip(assemble(build(d1, partial, dict(ALL_OK, brreg_subunits=False, site_jobs=False, nav=False)), run(1), None, d1, None))
        later = assemble(build(d2, BASE, ALL_OK), run(2), prev_failed, d2, d1)
        assert [c for c in later["changes"] if c["change_type"] == "added"] == []


def test_evidence_retained_across_refreshes_and_failures():
    with tempfile.TemporaryDirectory() as tmp:
        dirs = [Path(tmp, str(i)) for i in range(4)]
        changed = dict(BASE, **{"identity.registered_employees": ("identity", 12, None)})
        a = roundtrip(assemble(build(dirs[0], BASE, ALL_OK), run(1), None, dirs[0], None))
        b = roundtrip(assemble(build(dirs[1], changed, ALL_OK), run(2), a, dirs[1], dirs[0]))
        c = roundtrip(assemble(build(dirs[2], changed, ALL_OK), run(3), b, dirs[2], dirs[1]))  # unchanged B again
        d = roundtrip(failed_envelope("810034882", run(4), "source outage", c, dirs[3], dirs[2]))  # failed refresh, new folder
        for env, out in ((c, dirs[2]), (d, dirs[3])):
            evidence = {e["id"]: e for e in env["evidence"]}
            refs = {i for x in env["claims"] + env["history"] for i in x["evidence_ids"]}
            refs |= {i for x in env["change_log"] for i in x["old_evidence_ids"] + x["new_evidence_ids"]}
            assert refs <= set(evidence), refs - set(evidence)
            for eid in refs:
                path = out / evidence[eid]["snapshot_path"]
                assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == evidence[eid]["content_sha256"]
        assert len(c["change_log"]) == 1 and len(d["change_log"]) == 1 and c["changes"] == [] and d["changes"] == []
        assert all(x.get("stale") for x in d["claims"]) and d["history"][0]["value"] == 11


def test_privacy_redaction():
    from signalpost.brreg import drop_birth_dates
    from signalpost.nav import drop_contacts
    assert b"fodselsdato" not in drop_birth_dates(b'{"roller":[{"person":{"fodselsdato":"1981-09-27","navn":{"fornavn":"A"}}}]}')
    assert b"contactList" not in drop_contacts(b'{"ad_content":{"title":"T","contactList":[{"name":"X","phone":"1"}]}}')


def test_inputs():
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in (("a.txt", "810034882\n979 543 883\n"), ("a.jsonl", '{"organisation_number":"810034882"}\n{"organisation_number":"979543883"}\n'),
                           ("a.json", '["810034882","979543883"]'), ("b.json", '{"organisation_numbers":["810034882","979543883"]}'),
                           ("a.csv", "organisation_number,name\n810034882,SANDNES\n979543883,DIPS\n"),
                           ("b.jsonl", '{"orgnr":"810034882"}\n{"organisation_number":979543883}\n')):
            path = Path(tmp) / name
            path.write_text(text)
            assert read_inputs(path) == ["810034882", "979543883"], name


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in list(globals().items()) if name.startswith("test_")]
    assert tests
    for name, fn in tests:
        fn()
        print("ok", name)
