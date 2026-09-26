"""Claim/evidence accumulation for one company, plus the terminal envelope."""
from __future__ import annotations

import hashlib

from .net import Resp

STATES = ("available", "not_available", "blocked", "not_applicable", "ambiguous", "failed")

# Information families, in the order of the brief's required sections.
SECTIONS = {
    "identity": "Legal identity and public brand",
    "financials": "Latest annual accounts",
    "financial_history": "Annual-account history",
    "leadership": "Leadership and roles",
    "locations": "Registered workplaces and addresses",
    "group": "Group relationships",
    "website": "Verified official website",
    "social_profiles": "Company-owned social and video profiles",
    "contact": "Contact details",
    "jobs": "Hiring",
    "activity": "Dated public activity",
}


def resp_failure_state(resp: Resp) -> str:
    """Map a non-200 response to an availability state (a 404 is 'not_available', never zero)."""
    if resp.blocked:
        return "blocked"
    if resp.status in {404, 410}:
        return "not_available"
    return "failed"


class Builder:
    def __init__(self, org: str):
        self.org = org
        self.claims: dict[str, dict] = {}
        self.evidence: dict[str, dict] = {}
        self.sections: dict[str, dict] = {}
        self.errors: list[dict] = []
        self.candidates: list[dict] = []  # rejected/ambiguous website candidates, kept for audit
        self.sources: dict[str, str] = {}  # fetch -> "ok" | "partial" | "failed"
        self.withheld: dict[str, str] = {}  # source -> why it was deliberately not re-read (earlier claims kept as stale)

    def source(self, name: str, state: bool | str) -> None:
        """Record how completely a source was read; the worst report wins.
        ok: complete, so an absent item is gone. partial: read, but windowed or capped, so absence proves nothing
        (new items still count as added). failed: nothing can be concluded."""
        state = {True: "ok", False: "failed"}.get(state, state)
        rank = {"ok": 0, "partial": 1, "failed": 2}
        if name not in self.sources or rank[state] > rank[self.sources[name]]:
            self.sources[name] = state

    def ev(self, resp: Resp, source_class: str, method: str) -> str:
        eid = "ev-" + hashlib.sha256(f"{resp.final_url}|{resp.sha256}".encode()).hexdigest()[:16]
        self.evidence.setdefault(eid, {
            "id": eid,
            "source_url": resp.final_url,
            "requested_url": resp.url,
            "redirects": resp.redirects,
            "source_class": source_class,
            "retrieved_at": resp.retrieved_at,
            "http_status": resp.status,
            "content_type": resp.content_type,
            "content_sha256": resp.sha256,
            "snapshot_path": resp.snapshot_path,
            "extraction_method": method,
            **({"snapshot_redacted": "personal data (birth dates / contact persons) removed before saving"} if resp.redacted else {}),
        })
        return eid

    def claim(self, cid: str, section: str, field: str, value, evidence_ids: list[str], *, locator: str | None = None,
              period: dict | None = None, effective_date: str | None = None, basis: str | None = None,
              materiality: str = "material", **meta) -> None:
        """Publish one supported fact. Values are never defaulted: callers skip missing data instead."""
        claim = {"id": cid, "section": section, "field": field, "value": value, "availability": "available",
                 "evidence_ids": sorted(set(evidence_ids)), "materiality": materiality}
        if locator:
            claim["locator"] = locator
        if period:
            claim["reporting_period"] = period
        if effective_date:
            claim["effective_date"] = effective_date
        if basis:
            claim["basis"] = basis
        claim.update(meta)  # provenance details kept outside "value" so they never register as a change
        self.claims[cid] = claim
        self.sections.setdefault(section, {"availability": "available"})["availability"] = "available"

    def state(self, section: str, availability: str, note: str | None = None, evidence_ids: list[str] | None = None) -> None:
        """Record an explicit section state. An 'available' section is never downgraded by a later note."""
        assert availability in STATES, availability
        current = self.sections.get(section)
        if current and current["availability"] == "available" and availability != "available":
            current.setdefault("notes", []).append(note)
            return
        self.sections[section] = {"availability": availability, **({"note": note} if note else {}),
                                  **({"evidence_ids": sorted(set(evidence_ids))} if evidence_ids else {})}

    def error(self, section: str, message: str, url: str | None = None) -> None:
        self.errors.append({"section": section, "message": message, **({"url": url} if url else {})})
