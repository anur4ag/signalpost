"""Job postings from Nav's public job-vacancy feed (arbeidsplassen.no), matched by exact organisation number.

Terms (https://arbeidsplassen.nav.no/vilkar-api): free for everyone; ads may be republished or analysed if inactive
ads are dropped when they close (a refresh does that). Contact persons in ads are never stored.
Feed pages list only the employer's display name, so names generate candidates; each candidate ad is then fetched
and published only when its employer organisation number is the company or one of its registered workplaces.
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from .net import Fetcher
from .profile import Builder
from .web import name_tokens

FEED = "https://pam-stilling-feed.nav.no"
JSON = "application/json"


def drop_contacts(body: bytes) -> bytes:
    try:
        data = json.loads(body)
        (data.get("ad_content") or {}).pop("contactList", None)
        return json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
    except (ValueError, AttributeError):
        return body


def key(name: str | None) -> str:
    return " ".join(name_tokens(name or ""))


class NavJobs:
    def __init__(self, fx: Fetcher, days: int = 35, max_pages: int = 110):
        self.fx, self.days, self.max_pages = fx, days, max_pages
        self.token: str | None = None
        self.by_name: dict[str, list[str]] = {}
        self.error: str | None = None
        self.partial = False
        self.pages = 0
        self._thread = threading.Thread(target=self._build, daemon=True)

    def start(self) -> "NavJobs":
        self._thread.start()
        return self

    def _build(self) -> None:
        r = self.fx.get(FEED + "/api/publicToken", accept="text/plain", save=False)
        if not r.ok:
            self.error = f"Nav job feed token unavailable ({r.error or r.status})"
            return
        self.token = r.text().strip().splitlines()[-1].strip()
        since = format_datetime(datetime.now(timezone.utc) - timedelta(days=self.days), usegmt=True)
        headers = {"Authorization": f"Bearer {self.token}", "If-Modified-Since": since}
        url, latest, seen = FEED + "/api/v1/feed", {}, set()
        for page_number in range(self.max_pages + 1):
            if page_number == self.max_pages:
                self.partial = True  # feed longer than max_pages: absent ads are unknown, not closed
                break
            r = self.fx.get(url, accept=JSON, headers=headers, save=False, retries=1)
            if not r.ok:
                self.error = f"Nav job feed page failed ({r.error or r.status}); index is partial"
                break
            self.pages += 1
            page = r.json()
            for item in page.get("items") or []:
                entry = item.get("_feed_entry") or {}
                if entry.get("uuid"):
                    latest[entry["uuid"]] = entry
            next_id = page.get("next_id")
            if not page.get("items") or not next_id or next_id in seen or next_id == page.get("id"):
                break
            seen.add(next_id)
            url, headers = FEED + page["next_url"], {"Authorization": f"Bearer {self.token}"}
        by_name = defaultdict(list)
        for uuid, entry in latest.items():
            if entry.get("status") == "ACTIVE" and key(entry.get("businessName")):
                by_name[key(entry["businessName"])].append(uuid)
        self.by_name = dict(by_name)

    def research(self, b: Builder, legal_name: str) -> None:
        self._thread.join()
        if not self.token or not self.by_name:
            b.error("jobs", self.error or "Nav job feed index empty")
            b.state("jobs", "failed", "Nav job feed could not be read in this run; earlier postings are kept")
            b.source("nav", False)
            return
        b.source("nav", "failed" if self.error else "partial" if self.partial else "ok")  # a partial index cannot prove an ad closed
        units = {b.org} | {c["value"].get("organisation_number") for c in b.claims.values() if c["field"] == "registered_workplace"}
        names = {legal_name, (b.claims.get("identity.public_brand") or {}).get("value")}
        names |= {c["value"].get("name") for c in b.claims.values() if c["field"] == "registered_workplace"}
        candidates = list(dict.fromkeys(u for n in names if key(n) for u in self.by_name.get(key(n), [])))
        if len(candidates) > 15:
            b.source("nav", "partial")  # more candidate ads than are checked: absent ads are unknown, not closed
            candidates = candidates[:15]
        found = 0
        for uuid in candidates:
            r = self.fx.get(f"{FEED}/api/v1/feedentry/{uuid}", accept=JSON, headers={"Authorization": f"Bearer {self.token}"}, retries=1,
                            redact=drop_contacts)
            if not r.ok:
                b.source("nav", r.status == 404)
                continue
            body = r.json()
            ad = body.get("ad_content") or {}
            employer = (ad.get("employer") or {}).get("orgnr")
            if body.get("status") != "ACTIVE" or employer not in units:
                continue
            e = b.ev(r, "official_job_feed", "nav_pam_stilling_feed_entry_json")
            places = sorted({(w.get("city") or w.get("municipal") or "").title() for w in ad.get("workLocations") or []} - {""})
            value = {"title": ad.get("title"), "url": ad.get("link") or f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}",
                     "employer_name": (ad.get("employer") or {}).get("name"), "employer_organisation_number": employer,
                     "published": (ad.get("published") or "")[:10] or None, "expires": (ad.get("expires") or "")[:10] or None,
                     "locations": places, "positions": ad.get("positioncount"), "source": "Nav arbeidsplassen"}
            b.claim(f"jobs.posting.nav.{uuid}", "jobs", "job_posting", {k: v for k, v in value.items() if v not in (None, [], "")}, [e],
                    effective_date=value["published"], basis="official_job_feed_exact_organisation_number",
                    locator="/ad_content/employer/orgnr")
            found += 1
        if not found:
            b.state("jobs", "not_available", "No active Nav job ad for this company or its registered workplaces"
                    + (" (checked the last %d days of the Nav feed)" % self.days))
