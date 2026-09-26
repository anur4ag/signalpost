"""Offline checks for identity proof, refresh diffing and input parsing. Run: python3 tests/test_agent.py"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from signalpost.__main__ import read_inputs  # noqa: E402
from signalpost.envelope import assemble  # noqa: E402
from signalpost.net import Resp  # noqa: E402
from signalpost.profile import Builder  # noqa: E402
from signalpost.web import Page, org_numbers, valid_org, verify  # noqa: E402

ENTITY = {"organisasjonsnummer": "810034882", "navn": "SANDNES ELEKTRISKE AS", "telefon": "51 68 57 00",
          "epostadresse": "post@sandneselektriske.no", "forretningsadresse": {"adresse": ["Strandgata 21"]}}


def page(url: str, html: str) -> tuple[Resp, Page]:
    resp = Resp(url, url, 200, html.encode(), "text/html; charset=utf-8", "2026-09-26T00:00:00Z", "x" * 64, "snapshots/x.html")
    p = Page(url)
    p.feed(html)
    return resp, p


def test_org_numbers():
    assert valid_org("810034882") and not valid_org("810034883")
    found, labelled = org_numbers("Sandnes Elektriske AS · Org.nr: 810 034 882 MVA · tlf 51 68 57 00")
    assert found == {"810034882"} and labelled == {"810034882"}


def test_identity_decisions():
    exact = verify(ENTITY, ENTITY["navn"], [page("https://x.no/", "<footer>Org nr 810034882</footer>")], "name_domain_guess")
    assert exact[0] == "exact" and "organisation_number_on_site" in exact[1]
    # Name alone on a guessed domain is not enough ...
    weak = verify(ENTITY, ENTITY["navn"], [page("https://sandneselektriske.com/", "<h1>Sandnes Elektriske</h1>")], "name_domain_guess")
    assert weak[0] == "weak"
    # ... but the full registered name incl. form on a .no domain is (Norwegian names are unique) ...
    assert verify(ENTITY, ENTITY["navn"], [page("https://sandneselektriske.no/", "<p>Sandnes Elektriske AS</p>")], "name_domain_guess")[0] == "exact"
    # ... and a registered phone number corroborates the name.
    assert verify(ENTITY, ENTITY["navn"], [page("https://sandneselektriske.com/", "<h1>Sandnes Elektriske</h1> 51685700")], "name_domain_guess")[0] == "exact"
    # Another company's org number with no corroboration marks a related-entity site.
    related = verify(ENTITY, ENTITY["navn"], [page("https://group.no/", "<p>Org.nr 979543883</p>")], "registry_website")
    assert related[0] == "related" and related[2] == {"979543883"}
    # A register e-mail domain that does not spell the name needs more than the name (parent/developer sites).
    assert verify(ENTITY, ENTITY["navn"], [page("https://developer.no/", "<p>Sandnes Elektriske</p> post@sandneselektriske.no")],
                  "registry_email_domain")[0] == "weak"
    # A role holder from the official register on the site corroborates.
    assert verify(ENTITY, ENTITY["navn"], [page("https://x.com/", "<p>Sandnes Elektriske – daglig leder Morten Aarrestad</p>")],
                  "name_domain_guess", ["Morten Aarrestad"])[0] == "exact"


def build(values: dict, website_state: str = "available") -> Builder:
    b = Builder("810034882")
    resp = Resp("https://api", "https://api", 200, b"{}", "application/json", "2026-09-26T00:00:00Z", "a" * 64, "snapshots/a.json")
    e = b.ev(resp, "official_registry", "test")
    for cid, (section, value, basis) in values.items():
        b.claim(cid, section, cid.split(".")[-1], value, [e], basis=basis)
    if website_state != "available":
        b.state("website", website_state, "test")
    return b


def test_refresh():
    run1 = {"run_id": "r1", "completed_at": "2026-09-01T00:00:00Z"}
    run2 = {"run_id": "r2", "completed_at": "2026-09-02T00:00:00Z"}
    base = {"identity.legal_name": ("identity", "SANDNES ELEKTRISKE AS", None), "identity.registered_employees": ("identity", 11, None),
            "social_profiles.facebook": ("social_profiles", {"platform": "facebook", "url": "https://facebook.com/x"}, "linked_from_verified_official_website"),
            "website.official_website": ("website", {"url": "https://x.no/", "domain": "x.no", "identity_proof": ["t"]}, "registry_website")}
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        first = json.loads(json.dumps(assemble(build(base), run1, None, out, None)))
        same = assemble(build(base), run2, first, out, out)
        assert same["changes"] == [], same["changes"]  # idempotent: unchanged sources, no changes
        changed = dict(base, **{"identity.registered_employees": ("identity", 12, None)})
        second = assemble(build(changed), run2, first, out, out)
        assert [(c["claim_id"], c["old_value"], c["new_value"]) for c in second["changes"]] == [("identity.registered_employees", 11, 12)]
        assert second["history"][0]["value"] == 11  # earlier evidence preserved
        third = assemble(build(changed), {"run_id": "r3", "completed_at": "2026-09-03T00:00:00Z"}, json.loads(json.dumps(second)), out, out)
        assert third["changes"] == [] and len(third["change_log"]) == 1  # no duplicate change on rerun
        # A website that cannot be re-checked keeps its last supported values instead of reporting removals.
        down = {k: v for k, v in base.items() if not k.startswith(("website", "social"))}
        kept = assemble(build(down, website_state="failed"), run2, first, out, out)
        assert kept["changes"] == [] and any(c.get("stale") for c in kept["claims"])
        gone = assemble(build(down, website_state="not_available"), run2, first, out, out)
        assert {c["event"] for c in gone["changes"]} == {"website_lost", "profile_removed"}


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
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
