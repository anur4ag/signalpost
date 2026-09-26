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
    # Body copy never proves ownership, not even a labelled org number (reviewer: a customer list labels its customers).
    assert decide("https://x.com/", "<p>Org.nr 810034882</p>") == "weak"
    assert decide("https://x.com/", "<h2>Kunder</h2><p>Sandnes Elektriske AS (org.nr 810034882)</p>") == "weak"
    assert decide("https://x.com/", '<a href="https://proff.no/x/810034882">Proff</a>') == "weak"  # no link-substring shortcut
    # A register-listed site whose operator is another organisation is related, not exact.
    assert decide("https://group.no/", "<footer>Org.nr 979543883</footer>", origin="registry_website") == "related"
    # A register e-mail domain that does not spell the name needs more than the name (parent/developer sites).
    assert decide("https://developer.no/", "<title>Sandnes Elektriske</title><footer>post@sandneselektriske.no</footer>",
                  origin="registry_email_domain") == "weak"
    # Credits and list members are someone else (reviewer reproductions a and b).
    assert decide("https://sandneselektriske.no/", "<title>Different Business AS</title><footer>Website by Sandnes Elektriske AS</footer>") == "weak"
    assert decide("https://sandneselektriske.no/", "<title>Different Business AS</title><footer>Nettside levert av "
                  "<a href='https://byraa.no'>Sandnes Elektriske AS</a></footer>") == "weak"
    item_list = ('<title>Different Business AS</title><script type="application/ld+json">{"@type":"ItemList","itemListElement":'
                 '[{"@type":"ListItem","item":{"@type":"Organization","name":"Sandnes Elektriske AS"}}]}</script>')
    assert decide("https://sandneselektriske.no/", item_list) == "weak"
    # ...while the site's own organisation node, or a footer naming it beside its registered address, is the operator.
    own = '<script type="application/ld+json">{"@type":"Organization","name":"Sandnes Elektriske AS","url":"https://sandneselektriske.no/"}</script>'
    assert decide("https://sandneselektriske.no/", own) == "exact"
    assert decide("https://sandneselektriske.no/", own.replace("https://sandneselektriske.no/", "https://other.no/")) == "weak"
    assert decide("https://sandneselektriske.com/", "<footer>Sandnes Elektriske AS · Strandgata 21, 4307 Sandnes</footer>") == "exact"
    assert decide("https://sandneselektriske.no/", "<footer>© 2026 Sandnes Elektriske AS</footer>") == "exact"


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
    # RFC 9309 §2.2.2-3 encoding: %2A/%24 are literal characters, not operators; unreserved escapes decode; non-ASCII encodes.
    enc = Robots("User-agent: *\nDisallow: /private/\nAllow: /private/%2A$\n")
    assert not enc.allowed("https://x/private/secret") and enc.allowed("https://x/private/*") and enc.allowed("https://x/private/%2A")
    table = Robots("User-agent: *\nDisallow: /path/file-with-a-%2A.html\nDisallow: /path/foo-%24\nDisallow: /foo/bar/%62%61%7A\n"
                   "Disallow: /foo/bar/ツ\n")
    assert not table.allowed("https://x/path/file-with-a-*.html") and table.allowed("https://x/path/file-with-a-X.html")
    assert not table.allowed("https://x/path/foo-$") and not table.allowed("https://x/foo/bar/baz")
    assert not table.allowed("https://x/foo/bar/%E3%83%84") and not table.allowed("https://x/foo/bar/%e3%83%84")
    query = Robots("User-agent: *\nDisallow: /foo/bar?baz=https%3A%2F%2Ffoo.bar\nDisallow: /q?id=\n")
    assert not query.allowed("https://x/foo/bar?baz=https://foo.bar") and not query.allowed("https://x/foo/bar?baz=https%3A%2F%2Ffoo.bar")
    assert not query.allowed("https://x/q?id=7") and query.allowed("https://x/foo/bar")


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


NOW = "2026-09-26T00:00:00Z"
HOME = "https://www.sandneselektriske.no"
HOME_HTML = '<title>Sandnes Elektriske AS</title><a href="/kontakt">Kontakt oss</a><footer>Org.nr 810 034 882</footer>'


class StubFetcher:
    """Serves canned responses: (status, html) tuples, or an error string; unknown URLs are 404."""

    def __init__(self, responses: dict):
        self.responses = responses

    def get(self, url, **kwargs):
        value = self.responses.get(url.rstrip("/"))
        if isinstance(value, str):
            return Resp(url, url, None, retrieved_at=NOW, error=value)
        status, html, *ctype = value or (404, "")
        body = html.encode()
        digest = hashlib.sha256(body).hexdigest()
        return Resp(url, url, status, body, (ctype or ["text/html; charset=utf-8"])[0], NOW, digest, f"snapshots/{digest}.html")

    def remaining(self):
        return 10_000

    def sitemaps(self, origin):
        return []


def site_run(responses: dict, previous: dict | None, n: int) -> dict:
    from signalpost import web
    web.dns_exists = lambda url: False  # no live DNS for name guesses in tests
    entity = dict(ENTITY, hjemmeside="www.sandneselektriske.no")
    b = Builder("810034882")
    prior = next((c for c in (previous or {}).get("claims", []) if c["id"] == "website.official_website"), None)
    web.research_website(entity, StubFetcher(responses), b, prior_site=prior and prior["value"]["url"])
    return roundtrip(assemble(b, run(n), previous, Path(tempfile.gettempdir()), None))


def test_website_fetch_paths_never_fake_removals():
    contact = (200, '<a href="mailto:ola@sandneselektriske.no">ola@sandneselektriske.no</a>')
    first = site_run({HOME: (200, HOME_HTML), HOME + "/kontakt": contact}, None, 1)
    assert "contact.site_email.ola@sandneselektriske.no" in {c["id"] for c in first["claims"]} and first["sources"]["site_home"] == "ok"
    for label, responses in (("homepage 403", {HOME: (403, "")}),
                             ("robots crawl-delay too long", {HOME: "robots_crawl_delay_too_long"}),
                             ("robots disallow", {HOME: "robots_disallowed"}),
                             ("network error", {HOME: "network_error:TimeoutError"}),
                             ("contact page 503", {HOME: (200, HOME_HTML), HOME + "/kontakt": (503, "")}),
                             ("homepage served as text/plain", {HOME: (200, HOME_HTML, "text/plain"), HOME + "/kontakt": contact}),
                             ("contact page served as text/plain", {HOME: (200, HOME_HTML), HOME + "/kontakt": (*contact, "text/plain")})):
        env = site_run(responses, first, 2)
        assert env["changes"] == [], (label, env["changes"])
        assert env["sources"]["site_home"] == "failed", label
        assert "contact.site_email.ola@sandneselektriske.no" in {c["id"] for c in env["claims"] if c.get("stale")}, label
    # Positive control: every page read in full and the address is gone -> a real removal is reported.
    gone = site_run({HOME: (200, HOME_HTML), HOME + "/kontakt": (200, "<p>Ring oss</p>")}, first, 2)
    assert [c["event"] for c in gone["changes"]] == ["contact_detail_removed"], gone["changes"]
    # The registry site now 404s (definitively gone): the website is lost, not kept stale.
    lost = site_run({}, first, 2)
    assert "website_lost" in {c["event"] for c in lost["changes"]}


def test_identity_is_decided_on_every_published_page():
    # Exact on the homepage (name in title + registered phone), but the contact page names another operator:
    # nothing from these pages is attributed; the register-listed URL is kept only with a relationship label.
    home = ('<title>Sandnes Elektriske AS</title><a href="/kontakt">Kontakt</a><a href="https://facebook.com/agency">f</a>'
            '<footer>Tlf 51 68 57 00</footer>')
    env = site_run({HOME: (200, home), HOME + "/kontakt": (200, "<p>Byrå AS, Org.nr 979543883</p>")}, None, 1)
    ids = {c["id"] for c in env["claims"]}
    assert "social_profiles.facebook" not in ids and "website.official_website" in ids, ids
    site = next(c for c in env["claims"] if c["id"] == "website.official_website")
    assert site["other_organisation_numbers_on_site"] == ["979543883"] and "related" in site["relationship"]


def test_only_the_operators_own_profiles_and_brand():
    customer = ('<script type="application/ld+json">{"@type":"ItemList","itemListElement":[{"@type":"ListItem","item":'
                '{"@type":"Organization","name":"Customer Company AS","sameAs":"https://facebook.com/customercompany"}}]}</script>'
                '<p>Our customer <a href="https://www.facebook.com/customercompany">Customer Company AS</a></p>')
    env = site_run({HOME: (200, HOME_HTML.replace("<footer>", customer + '<footer><a href="https://www.facebook.com/sandneselektriske">f</a>'))},
                   None, 1)
    claims = {c["id"]: c["value"] for c in env["claims"]}
    assert claims["social_profiles.facebook"]["url"] == "https://www.facebook.com/sandneselektriske"
    assert "identity.public_brand" not in claims
    listed = site_run({HOME: (200, HOME_HTML.replace("<footer>", '<p>Kunde: <a href="mailto:post@customer.no">post@customer.no</a></p><footer>'))},
                      None, 1)
    assert not [c for c in listed["claims"] if c["id"].startswith("contact.site_")]  # a customer's address in body copy
    # A recruiter's JobPosting for a client is not this company's hiring.
    client_job = ('<script type="application/ld+json">{"@type":"JobPosting","title":"Kokk","hiringOrganization":{"name":"Hotell Client AS"}}'
                  '</script>')
    env = site_run({HOME: (200, HOME_HTML.replace("<footer>", client_job + "<footer>"))}, None, 1)
    assert not [c for c in env["claims"] if c["field"] == "job_posting"]


def test_windowed_and_capped_lists_never_fake_removals():
    feed_home = HOME_HTML + '<link rel="alternate" type="application/rss+xml" href="/feed">'
    item = "<item><title>{0}</title><link>https://www.sandneselektriske.no/nyheter/{0}</link><pubDate>2026-09-01</pubDate></item>"
    feed = lambda *titles: (200, "<rss><channel>" + "".join(item.format(t) for t in titles) + "</channel></rss>")
    news = lambda env: sorted(c["value"]["title"] for c in env["claims"] if c["field"] == "company_news" and not c.get("stale"))
    # A feed is a window: an item dropping out of it (even leaving it empty, with no sitemap) is never a removal.
    first = site_run({HOME: (200, feed_home), HOME + "/feed": feed("a", "b")}, None, 1)
    for label, responses in (("window shrinks", {HOME: (200, feed_home), HOME + "/feed": feed("b")}),
                             ("window empties, sitemap 404", {HOME: (200, feed_home), HOME + "/feed": feed()})):
        env = site_run(responses, first, 2)
        assert env["changes"] == [] and env["sources"]["site_news_feed"] == "partial", (label, env["changes"])
    # ...but a new item in the window is real new activity.
    newer = site_run({HOME: (200, feed_home), HOME + "/feed": feed("a", "b", "c")}, first, 2)
    assert [c["event"] for c in newer["changes"]] == ["new_activity"] and news(newer) == ["a", "b", "c"]
    # The same for homepage JSON-LD articles (reviewer: the last article disappears and there is no sitemap).
    article = '<script type="application/ld+json">{"@type":"NewsArticle","headline":"Ny avtale","datePublished":"2026-09-01",' \
              '"url":"https://www.sandneselektriske.no/nyheter/ny-avtale"}</script>'
    first = site_run({HOME: (200, HOME_HTML.replace("<footer>", article + "<footer>"))}, None, 1)
    assert news(first) == ["Ny avtale"]
    env = site_run({HOME: (200, HOME_HTML)}, first, 2)
    assert env["changes"] == [], env["changes"]
    # A complete sitemap is a full list, so an item that disappears from it is a real removal.
    smap = lambda *slugs: (200, "<urlset>" + "".join(f"<url><loc>https://www.sandneselektriske.no/nyheter/{x}</loc>"
                                                        f"<lastmod>2026-09-01</lastmod></url>" for x in slugs) + "</urlset>")
    first = site_run({HOME: (200, HOME_HTML), HOME + "/sitemap.xml": smap("a", "b")}, None, 1)
    assert first["sources"]["site_news_sitemap"] == "ok"
    later = site_run({HOME: (200, HOME_HTML), HOME + "/sitemap.xml": smap("b")}, first, 2)
    assert [c["event"] for c in later["changes"]] == ["activity_removed"], later["changes"]
    # Only a fully parsed leaf <urlset> is a complete list: a truncated body, or an index whose only child is another
    # index (never followed), proves nothing; a sole child that is a complete urlset still proves a removal.
    index = lambda *children: (200, "<sitemapindex>" + "".join(f"<sitemap><loc>{HOME}/{c}</loc></sitemap>" for c in children) + "</sitemapindex>")
    for label, bad in (("truncated urlset", {HOME: (200, HOME_HTML), HOME + "/sitemap.xml":
                                             (200, f"<urlset><url><loc>{HOME}/nyheter/b</loc>")}),
                       ("nested index", {HOME: (200, HOME_HTML), HOME + "/sitemap.xml": index("news-index.xml"),
                                         HOME + "/news-index.xml": index("news.xml"), HOME + "/news.xml": smap("a", "b")})):
        env = site_run(bad, first, 2)
        assert env["changes"] == [] and env["sources"]["site_news_sitemap"] in {"partial", "failed"}, (label, env["changes"])
    sole = site_run({HOME: (200, HOME_HTML), HOME + "/sitemap.xml": index("news.xml"), HOME + "/news.xml": smap("b")}, first, 2)
    assert [c["event"] for c in sole["changes"]] == ["activity_removed"] and sole["sources"]["site_news_sitemap"] == "ok"
    # A capped list (21 footer e-mails, 20 kept) cannot prove the one beyond the cut is gone; the new one is still "added".
    mail = lambda ids: HOME_HTML.replace("</footer>", "".join(f'<a href="mailto:{i}@company.no">e</a>' for i in ids) + "</footer>")
    addresses = [f"person{i:02d}" for i in range(20)]
    old = site_run({HOME: (200, mail(addresses))}, None, 1)
    new = site_run({HOME: (200, mail(["aaa", *addresses]))}, old, 2)
    assert [c["change_type"] for c in new["changes"]] == ["added"] and new["sources"]["site_home"] == "partial"
    assert [c["id"] for c in new["claims"] if c.get("stale")] == ["contact.site_email.person19@company.no"]
    # A sitemap that vanishes (404), answers with an HTML page (soft 404) or is served by a non-feed body proves nothing.
    for label, bad in (("sitemap 404", {HOME: (200, HOME_HTML)}),
                       ("soft 404", {HOME: (200, HOME_HTML), HOME + "/sitemap.xml": (200, "<html>Not found</html>")}),
                       ("feed not a feed", {HOME: (200, feed_home), HOME + "/feed": (200, "<html>maintenance</html>")})):
        env = site_run(bad, first, 2)
        assert env["changes"] == [] and env["sources"]["site_news_sitemap"] in {"partial", "failed"}, (label, env["changes"])


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
