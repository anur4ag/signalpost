"""Terminal envelope: research one company, diff against the previous run, write the evidence-cited summary."""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from . import brreg, web
from .net import Fetcher, utc_now
from .profile import SECTIONS, STATES, Builder

AGENT_VERSION = "signalpost-agent/1.0.0"
DEFAULT_NOTES = {
    "activity": "No dated activity found in the official records or on a verified company website",
    "contact": "No contact details in the register or on a verified company website",
    "social_profiles": "No company-owned profiles linked from a verified company website",
    "jobs": "No job postings found on a verified company website",
    "group": "No group relationship returned by the register",
}
EVENTS = {
    "leadership": ("new_role", "role_ended", "role_changed"),
    "locations": ("new_location", "location_closed", "location_changed"),
    "financials": ("new_financial_figure", "financial_figure_removed", "financial_figure_revised"),
    "financial_history": ("new_filing", "filing_removed", "filing_changed"),
    "jobs": ("new_job_posting", "job_posting_closed", "job_posting_changed"),
    "activity": ("new_activity", "activity_removed", "activity_updated"),
    "website": ("website_found", "website_lost", "website_changed"),
    "social_profiles": ("new_profile", "profile_removed", "profile_changed"),
    "group": ("new_group_relation", "group_relation_ended", "group_relation_changed"),
    "contact": ("new_contact_detail", "contact_detail_removed", "contact_detail_changed"),
    "identity": ("new_identity_fact", "identity_fact_removed", "identity_fact_changed"),
}


def canon(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def research(org: str, fx: Fetcher, previous: dict | None, deadline: float, nav=None) -> Builder:
    b = Builder(org)
    entity = brreg.fetch_entity(org, fx, b)
    if entity is None and b.sections["identity"]["availability"] == "not_available":
        for section in SECTIONS:
            b.state(section, "not_available", "Organisation number not found in the live register")
        return b
    brreg.fetch_roles(org, fx, b)
    brreg.fetch_subunits(org, entity, fx, b)
    brreg.fetch_group(org, entity, fx, b)
    brreg.fetch_accounts(org, fx, b)
    brreg.fetch_account_years(org, fx, b)
    if brreg.is_deleted(entity):
        # A deleted entity has no current operator: a site naming it is history, not current enrichment. The register keeps
        # only a tombstone, and a missing filing of a deleted company is not evidence that the filing is gone, so those
        # reads are partial: earlier facts stay (labelled), while the active -> deleted status change is still reported.
        when = f" on {entity['slettedato']}" if entity.get("slettedato") else ""  # a class-only tombstone has no date
        note = f"Entity deleted from the register{when}; no current website, profiles or hiring are attributed"
        for section in ("website", "social_profiles", "jobs"):
            b.state(section, "not_applicable", note)
        for source in (*web.SITE_SOURCES, "nav"):
            b.withheld[source] = note + "; earlier website facts are kept as history, not re-attributed"
        for source, what in (("brreg_entity", "registry details"), ("brreg_accounts", "filed accounts"), ("brreg_years", "filing copies")):
            b.source(source, "partial")
            b.withheld[source] = f"Entity deleted from the register{when}; the register now shows only a tombstone, so earlier {what} are kept as history"
        return b
    if time.monotonic() > deadline:
        b.state("website", "failed", "Run deadline reached before website research")
        b.error("website", "deadline reached")
        return b
    prior = next((c for c in (previous or {}).get("claims", []) if c["id"] == "website.official_website"), None)
    try:
        web.research_website(entity, fx, b, prior_site=prior and prior["value"].get("url"))
    except Exception as error:  # a website bug must not cost the company its registry data: only the site sources fail
        web.site_sources(b, False)
        for section in ("website", "social_profiles", "jobs"):
            b.state(section, "failed", f"Website research error: {type(error).__name__}")
        b.error("website", f"{type(error).__name__}: {error}")
    if nav and entity:
        try:
            nav.research(b, entity.get("navn") or "")
        except Exception as error:
            b.source("nav", False)
            b.error("jobs", f"Nav research error: {type(error).__name__}: {error}")
    return b


SOURCES = [  # claim-id prefix -> the one fetch that produces it (first match wins)
    ("jobs.posting.nav.", "nav"), ("jobs.", "site_jobs"),
    ("activity.site.sitemap.", "site_news_sitemap"), ("activity.site.feed.", "site_news_feed"), ("activity.site.page.", "site_news_page"),
    ("website.", "site_home"), ("social_profiles.", "site_home"), ("contact.site_", "site_home"), ("identity.public_brand", "site_home"),
    ("activity.registry.roles_", "brreg_roles"), ("activity.registry.accounts_filed", "brreg_years"),
    ("activity.registry.", "brreg_entity"), ("contact.registry_", "brreg_entity"), ("locations.business_address", "brreg_entity"),
    ("identity.", "brreg_entity"), ("locations.subunit.", "brreg_subunits"), ("leadership.", "brreg_roles"), ("group.", "brreg_group"),
    ("financials.", "brreg_accounts"), ("financial_history.", "brreg_years"),
]


def source_of(claim: dict) -> str:
    """The fetch that must have succeeded in this run before a missing claim may be reported as removed."""
    return next((source for prefix, source in SOURCES if claim["id"].startswith(prefix)), "unmapped")  # unmapped: never removable


def assemble(b: Builder, run: dict, previous: dict | None, out_dir: Path, prev_dir: Path | None) -> dict:
    now = run["completed_at"]
    claims = {cid: dict(c) for cid, c in sorted(b.claims.items())}
    evidence = dict(b.evidence)
    sections = {}
    for key in SECTIONS:
        section = dict(b.sections.get(key) or {"availability": "not_available", "note": DEFAULT_NOTES.get(key, "Not found")})
        if section["availability"] == "available":
            section.pop("note", None)
        section["claim_ids"] = [cid for cid, c in claims.items() if c["section"] == key]
        sections[key] = section

    status, prev_status = b.sources, (previous or {}).get("sources", {})
    changes, history, carried = [], list((previous or {}).get("history", [])), []
    if previous:
        prev_claims = {c["id"]: c for c in previous.get("claims", [])}
        for cid, claim in claims.items():
            old = prev_claims.get(cid)
            if old:
                claim["first_observed_at"] = old.get("first_observed_at") or previous["run"]["completed_at"]
                if canon(old["value"]) != canon(claim["value"]):
                    changes.append(change(b.org, "changed", old, claim, now))
                    history.append({**old, "observed_until": now})
            else:
                claim["first_observed_at"] = now
                if prev_status.get(source_of(claim)) in {"ok", "partial"}:  # the same source was read last time, so this is new
                    changes.append(change(b.org, "added", None, claim, now))
            claim["last_verified_at"] = now
        for cid, old in prev_claims.items():
            if cid in claims:
                continue
            if status.get(source_of(old)) == "ok":  # re-read in full this run and the fact is gone
                changes.append(change(b.org, "removed", old, None, now))
                history.append({**old, "observed_until": now})
            else:
                reason = b.withheld.get(source_of(old)) or f"source '{source_of(old)}' could not be re-read in full in this run; last supported value kept"
                carried.append({**old, "stale": True, "stale_reason": reason})
    else:
        for claim in claims.values():
            claim["first_observed_at"] = claim["last_verified_at"] = now
    for c in carried:
        claims[c["id"]] = c
        sections[c["section"]].setdefault("stale_claim_ids", []).append(c["id"])
        sections[c["section"]].setdefault("note", "Part of this section could not be re-read in this run; stale items keep their last supported value")
    change_log = {c["change_id"]: c for c in (previous or {}).get("change_log", [])}
    change_log.update({c["change_id"]: c for c in changes})

    # Every evidence record still referenced (current or stale claims, history, change log) stays, with its saved copy.
    prev_evidence = {e["id"]: e for e in (previous or {}).get("evidence", [])}
    refs = {eid for c in claims.values() for eid in c["evidence_ids"]}
    refs |= {eid for h in history for eid in h.get("evidence_ids", [])}
    refs |= {eid for c in change_log.values() for eid in c["old_evidence_ids"] + c["new_evidence_ids"]}
    for eid in sorted(refs - set(evidence)):
        if eid in prev_evidence:
            evidence[eid] = {**prev_evidence[eid], "superseded": True}
    for ev in evidence.values():
        if ev.get("superseded"):
            copy_snapshot(ev.get("snapshot_path"), prev_dir, out_dir)

    identity_state = sections["identity"]["availability"]
    envelope = {
        "organisation_number": b.org,
        "availability": identity_state if identity_state in {"not_available", "failed", "blocked"} else "available",
        "terminal_status": "completed" if identity_state != "failed" else "failed",
        "run": run,
        "name": claims.get("identity.legal_name", {}).get("value"),
        "sections": sections,
        "sources": dict(sorted(status.items())),
        "claims": list(claims.values()),
        "evidence": sorted(evidence.values(), key=lambda e: e["id"]),
        "summary": None,
        "changes": changes,
        "change_log": sorted(change_log.values(), key=lambda c: (c["detected_at"], c["change_id"])),
        "history": history,
        "website_candidates": b.candidates,
        "errors": b.errors,
    }
    envelope["summary"] = summarize(envelope, previous)
    assert all(s["availability"] in STATES for s in sections.values())
    return envelope


def change(org: str, kind: str, old: dict | None, new: dict | None, now: str) -> dict:
    ref = new or old
    added, removed, changed = EVENTS.get(ref["section"], ("added", "removed", "changed"))
    digest = hashlib.sha256(f"{org}|{ref['id']}|{canon(old and old['value'])}|{canon(new and new['value'])}".encode()).hexdigest()[:16]
    return {"change_id": f"chg-{digest}", "claim_id": ref["id"], "section": ref["section"], "field": ref["field"], "change_type": kind,
            "event": {"added": added, "removed": removed, "changed": changed}[kind],
            "materiality": ref.get("materiality", "material"),
            "old_value": old["value"] if old else None, "new_value": new["value"] if new else None,
            "old_evidence_ids": old["evidence_ids"] if old else [], "new_evidence_ids": new["evidence_ids"] if new else [],
            "previously_observed_at": old.get("last_verified_at") if old else None, "detected_at": now}


def copy_snapshot(rel: str | None, prev_dir: Path | None, out_dir: Path) -> None:
    if rel and prev_dir and prev_dir.resolve() != out_dir.resolve():
        src, dst = prev_dir / rel, out_dir / rel
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)


def failed_envelope(org: str, run: dict, message: str, previous: dict | None = None,
                    out_dir: Path = Path("."), prev_dir: Path | None = None) -> dict:
    """No source was read, so every earlier claim is carried forward as stale with its evidence and saved copies."""
    b = Builder(org)
    for section in SECTIONS:
        b.state(section, "failed", message)
    b.error("run", message)
    return assemble(b, run, previous, out_dir, prev_dir)


# ---------------------------------------------------------------------------------------------
# Synthesis: every sentence cites the claims it is built from. Nothing is said without a claim.

FORMS = {"AS": "private limited company", "ASA": "public limited company", "ENK": "sole proprietorship", "ANS": "general partnership",
         "DA": "partnership with shared liability", "SA": "cooperative", "BRL": "housing cooperative", "BBL": "housing building cooperative",
         "STI": "foundation", "NUF": "Norwegian branch of a foreign company", "FLI": "association", "KS": "limited partnership",
         "ESEK": "jointly owned property", "SAM": "co-ownership", "IKS": "inter-municipal company", "KF": "municipal enterprise",
         "SF": "state enterprise", "SPA": "savings bank", "PK": "pension fund", "VPFO": "securities fund", "BA": "limited-liability company",
         "PRE": "shipping partnership", "GFS": "mutual insurance company"}


def money(amount, currency) -> str:
    if amount is None:
        return "not reported"
    sign = "-" if amount < 0 else ""
    value = abs(amount)
    for unit, size in (("billion", 1e9), ("million", 1e6), ("thousand", 1e3)):
        if value >= size:
            return f"{sign}{currency or ''} {value / size:,.1f} {unit}".strip()
    return f"{sign}{currency or ''} {value:,.0f}".strip()


def summarize(env: dict, previous: dict | None) -> dict:
    claims = {c["id"]: c for c in env["claims"] if not c.get("stale")}
    v = lambda cid: (claims.get(cid) or {}).get("value")
    sentences: list[dict] = []

    def say(text: str, ids: list[str]) -> None:
        ids = [i for i in ids if i in claims]
        if ids:
            sentences.append({"text": text, "claim_ids": ids})

    name = v("identity.legal_name")
    if not name:
        return {"text": "No profile could be built for this organisation number in this run.", "sentences": [],
                "unknowns": [f"{k}: {s.get('note', s['availability'])}" for k, s in env["sections"].items()], "changes": None}
    code = (v("identity.legal_form") or {}).get("code")
    form = FORMS.get(code) or (v("identity.legal_form") or {}).get("description")
    industry = (v("identity.industry") or [{}])[0]
    brand = v("identity.public_brand")
    deleted = v("identity.status") == "deleted"
    text = (f"{name}{f' (trading as {brand})' if brand else ''} {'was' if deleted else 'is'} "
            f"a{'n' if (form or 'x')[0].lower() in 'aeiouæøå' else ''} {form or 'registered entity'}{f' ({code})' if code else ''}")
    text += f" based in {v('identity.municipality').title()}" if v("identity.municipality") else ""
    text += f", founded {v('identity.founded')}" if v("identity.founded") else ""
    text += f", registered under industry {industry.get('code')} “{industry.get('description')}”." if industry.get("code") else "."
    say(text, ["identity.legal_name", "identity.public_brand", "identity.legal_form", "identity.municipality", "identity.founded", "identity.industry"])
    status = v("identity.status")
    if status == "deleted":
        when = claims["identity.status"].get("effective_date")
        say(f"It was deleted from the Central Coordinating Register{f' on {when}' if when else ''}; only historical records remain.",
            ["identity.status"])
    elif status and status != "active":
        say(f"Register status: {status.replace('_', ' ')}.", ["identity.status"])
    if v("website.description"):
        say(f"Its website describes it as: “{v('website.description')[:300]}” (company's own wording).", ["website.description"])
    elif v("identity.registered_activity") or v("identity.statutory_purpose"):
        say(f"Registered activity: “{(v('identity.registered_activity') or v('identity.statutory_purpose'))[:300]}”.",
            ["identity.registered_activity", "identity.statutory_purpose"])

    fin = sorted((c for c in claims.values() if c["section"] == "financials" and c["field"] != "statement"), key=lambda c: c["id"])
    if fin:
        latest_year = max(c["id"].rsplit(".", 1)[1] for c in fin)
        kind = "selskap" if any(".selskap." in c["id"] for c in fin) else fin[0]["id"].split(".")[1]
        pick = {c["field"]: c for c in fin if c["id"].endswith("." + latest_year) and f".{kind}." in c["id"]}
        period = next(iter(pick.values())).get("reporting_period") or {}
        parts = [f"{label} {money(pick[f]['value']['amount'], pick[f]['value']['currency'])}" for f, label in
                 (("revenue", "revenue"), ("operating_result", "operating result"), ("net_result", "net result"),
                  ("total_assets", "total assets"), ("equity", "equity")) if f in pick]
        say(f"Latest filed accounts ({period.get('from')} to {period.get('to')}): " + ", ".join(parts) + ".", [pick[f]["id"] for f in pick])
    if v("identity.registered_employees") is not None:
        say(f"The register lists {v('identity.registered_employees')} employees.", ["identity.registered_employees"])

    roles = [c for c in claims.values() if c["section"] == "leadership"]
    lead = [c for c in roles if c["value"]["role_code"] in {"DAGL", "LEDE", "INNH", "DTPR", "DTSO", "KOMP", "FFØR"}]
    board = [c for c in roles if c["value"]["role_code"] in {"MEDL", "NEST"}]
    if lead or board:
        text = "; ".join(f"{c['value']['role']}: {c['value']['name']}" for c in lead[:4])
        if board:
            text += ("; " if text else "") + f"{len(board)} further board member{'s' if len(board) != 1 else ''}"
        auditor = next((c for c in roles if c["value"]["role_code"] == "REVI"), None)
        if auditor:
            text += f"; auditor {auditor['value']['name']}"
        say("Leadership — " + text + ".", [c["id"] for c in lead[:4] + board + ([auditor] if auditor else [])])
    workplaces = [c for c in claims.values() if c["field"] == "registered_workplace"]
    if workplaces:
        places = sorted({(c["value"].get("municipality") or "").title() for c in workplaces} - {""})
        say(f"It has {len(workplaces)} registered workplace{'s' if len(workplaces) != 1 else ''}" + (f" in {', '.join(places[:5])}" if places else "") + ".",
            [c["id"] for c in workplaces])
    parent = claims.get("group.parent")
    if parent:
        say(f"It is part of a group: {parent['value'].get('relationship') or 'subsidiary'} of {parent['value']['name']} ({parent['value']['organisation_number']}).", ["group.parent"])
    subs = [c for c in claims.values() if c["field"] == "subsidiary"]
    if subs:
        say(f"It has {len(subs)} registered subsidiar{'ies' if len(subs) != 1 else 'y'}.", [c["id"] for c in subs])

    site = claims.get("website.official_website")
    if site:
        say(f"Official website: {site['value']['domain']} (identity basis: {', '.join(site.get('identity_proof', [])).replace('_', ' ')})"
            + (f"; note: {site['relationship']}" if site.get("relationship") else "") + ".", ["website.official_website"])
    socials = [c for c in claims.values() if c["section"] == "social_profiles"]
    if socials:
        say("Company-owned profiles linked from the website: " + ", ".join(c["value"]["platform"].title() for c in socials) + ".", [c["id"] for c in socials])
    jobs = [c for c in claims.values() if c["field"] == "job_posting"]
    if jobs:
        say(f"Hiring: {len(jobs)} job posting{'s' if len(jobs) != 1 else ''} linked from its website, e.g. “{jobs[0]['value']['title']}”.", [c["id"] for c in jobs])
    news = sorted((c for c in claims.values() if c["field"] == "company_news"), key=lambda c: c["value"]["date"], reverse=True)
    if news:
        say(f"Most recent dated company update: “{news[0]['value']['title']}” ({news[0]['value']['date']}).", [news[0]["id"]])

    if previous:
        material = [c for c in env["changes"] if c["materiality"] == "material"]
        if env["changes"]:
            changes_text = f"Since the previous run ({previous['run']['completed_at']}): {len(env['changes'])} change{'s' if len(env['changes']) != 1 else ''}" + \
                           (f", {len(material)} material — " + "; ".join(sorted({c['event'].replace('_', ' ') for c in material})) if material else "") + "."
        else:
            changes_text = f"No changes since the previous run ({previous['run']['completed_at']})."
    else:
        changes_text = "First observation of this company; no earlier run to compare."
    unknowns = [f"{SECTIONS[k]}: {s.get('note') or s['availability'].replace('_', ' ')}" for k, s in env["sections"].items() if s["availability"] != "available"]
    text = " ".join(s["text"] for s in sentences) + " " + changes_text
    if unknowns:
        text += " Not established: " + "; ".join(u.split(":")[0].lower() for u in unknowns) + "."
    return {"text": text.strip(), "sentences": sentences, "changes": changes_text, "unknowns": unknowns}
