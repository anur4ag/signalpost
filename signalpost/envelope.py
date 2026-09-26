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
WEB_BASES = {"company_owned_website", "company_reported", "linked_from_verified_official_website"}
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
    if time.monotonic() > deadline:
        b.state("website", "failed", "Run deadline reached before website research")
        b.error("website", "deadline reached")
        return b
    prior = next((c for c in (previous or {}).get("claims", []) if c["id"] == "website.official_website"), None)
    web.research_website(entity, fx, b, prior_site=prior and prior["value"].get("url"))
    if nav and entity:
        nav.research(b, entity.get("navn") or "")
    return b


def source_family(claim: dict) -> str:
    """The section whose source must have been re-checked before a missing claim may count as removed."""
    cid = claim["id"]
    if cid.startswith("activity.registry.roles_"):
        return "leadership"
    if cid.startswith("activity.registry.accounts_filed"):
        return "financial_history"
    if cid.startswith(("activity.registry.", "contact.registry_", "locations.business_address")):
        return "identity"
    return "website" if claim.get("basis") in WEB_BASES and claim["section"] != "website" else claim["section"]


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

    changes, history, carried = [], list((previous or {}).get("history", [])), []
    if previous:
        prev_claims = {c["id"]: c for c in previous.get("claims", [])}
        prev_evidence = {e["id"]: e for e in previous.get("evidence", [])}
        prev_sections = previous.get("sections", {})
        checked = lambda family: sections.get(family, {}).get("availability") in {"available", "not_available", "not_applicable", "ambiguous"}

        def keep_evidence(ids):
            for eid in ids:
                if eid in prev_evidence and eid not in evidence:
                    evidence[eid] = {**prev_evidence[eid], "superseded": True}
                    copy_snapshot(prev_evidence[eid].get("snapshot_path"), prev_dir, out_dir)

        for cid, claim in claims.items():
            old = prev_claims.get(cid)
            if old:
                claim["first_observed_at"] = old.get("first_observed_at") or previous["run"]["completed_at"]
                if canon(old["value"]) != canon(claim["value"]):
                    changes.append(change(b.org, "changed", old, claim, now))
                    history.append({**old, "observed_until": now})
                    keep_evidence(old["evidence_ids"])
            else:
                claim["first_observed_at"] = now
                if prev_sections.get(source_family(claim), {}).get("availability") in {"available", "not_available", "not_applicable", "ambiguous"}:
                    changes.append(change(b.org, "added", None, claim, now))
            claim["last_verified_at"] = now
        for cid, old in prev_claims.items():
            if cid in claims:
                continue
            if not checked(source_family(old)):
                carried.append({**old, "stale": True, "stale_reason": "source could not be re-checked in this run; last supported value kept"})
                keep_evidence(old["evidence_ids"])
            else:
                changes.append(change(b.org, "removed", old, None, now))
                history.append({**old, "observed_until": now})
                keep_evidence(old["evidence_ids"])
    else:
        for claim in claims.values():
            claim["first_observed_at"] = claim["last_verified_at"] = now
    for c in carried:
        claims[c["id"]] = c
        sections[c["section"]].setdefault("stale_claim_ids", []).append(c["id"])
    change_log = {c["change_id"]: c for c in (previous or {}).get("change_log", [])}
    change_log.update({c["change_id"]: c for c in changes})

    identity_state = sections["identity"]["availability"]
    envelope = {
        "organisation_number": b.org,
        "availability": identity_state if identity_state in {"not_available", "failed", "blocked"} else "available",
        "terminal_status": "completed" if identity_state != "failed" else "failed",
        "run": run,
        "name": claims.get("identity.legal_name", {}).get("value"),
        "sections": sections,
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
            shutil.copyfile(src, dst)


def failed_envelope(org: str, run: dict, message: str, previous: dict | None = None) -> dict:
    b = Builder(org)
    for section in SECTIONS:
        b.state(section, "failed", message)
    b.error("run", message)
    env = assemble(b, run, None, Path("."), None)
    if previous:  # keep the last supported profile visible instead of erasing it
        env["claims"] = [{**c, "stale": True, "stale_reason": "this run failed; last supported value kept"} for c in previous.get("claims", [])]
        env["evidence"] = previous.get("evidence", [])
        env["history"] = previous.get("history", [])
        env["change_log"] = previous.get("change_log", [])
    return env


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
    text = f"{name}{f' (trading as {brand})' if brand else ''} is a{'n' if (form or 'x')[0].lower() in 'aeiouæøå' else ''} {form or 'registered entity'}{f' ({code})' if code else ''}"
    text += f" based in {v('identity.municipality').title()}" if v("identity.municipality") else ""
    text += f", founded {v('identity.founded')}" if v("identity.founded") else ""
    text += f", registered under industry {industry.get('code')} “{industry.get('description')}”." if industry.get("code") else "."
    say(text, ["identity.legal_name", "identity.public_brand", "identity.legal_form", "identity.municipality", "identity.founded", "identity.industry"])
    status = v("identity.status")
    if status and status != "active":
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
        period = next(iter(pick.values()))["reporting_period"]
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
