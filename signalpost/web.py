"""Company website discovery, exact-identity verification and company-owned extraction.

Candidates come only from official data or deterministic rules: the registry website, the registry e-mail
domain, then the legal name as a .no/.com domain. Search engines and social platforms are never scraped.
A candidate is published only after identity proof on the site itself (see verify()).
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import unicodedata
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from .net import Fetcher, Resp, resolve_public
from .profile import Builder

HTML = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"
SHARED = {}
for _line in (Path(__file__).with_name("shared_domains.txt")).read_text(encoding="utf-8").splitlines():
    if _line and not _line.startswith("#"):
        _domain, _count = _line.split("\t")
        SHARED[_domain] = int(_count)

FORM_SUFFIXES = {"as", "asa", "ans", "da", "sa", "ks", "ba", "nuf"}
PLACES = {line for line in Path(__file__).with_name("places.txt").read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")}
LEGAL_TOKENS = {"as", "asa", "ans", "da", "sa", "ba", "ks", "enk", "nuf", "sf", "iks", "kf", "bbl", "brl", "a/s", "ab",
                "ltd", "limited", "gmbh", "inc", "i", "likvidasjon", "konkursbo", "under", "avvikling"}
GENERIC = {"eiendom", "eiendommer", "holding", "holdings", "invest", "investering", "investment", "investments", "consult",
           "consulting", "service", "services", "norge", "norway", "norsk", "norske", "group", "gruppen", "og", "and", "the",
           "utvikling", "drift", "bygg", "transport", "handel", "regnskap", "management", "capital", "partners", "solutions",
           "prosjekt", "ventures", "tech", "technology", "entreprenor", "maskin", "installasjon", "elektro", "ror", "vvs",
           "bil", "borettslag", "sameiet", "stiftelsen", "stiftelse", "forening", "selskap", "selskapet", "firma",
           "nord", "sor", "vest", "ost", "nye", "ny", "vi", "co", "company", "eiendomsselskap", "utleie", "familie"}
SOCIAL = [
    ("linkedin", re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/(company|school|showcase)/([^/?#]+)", re.I)),
    ("facebook", re.compile(r"^https?://(www\.|m\.|business\.)?(facebook|fb)\.com/(?!sharer|share|dialog|plugins|tr\b|login|groups/|events/|photo|watch|hashtag|policies|privacy|help)([^?#]+)", re.I)),
    ("instagram", re.compile(r"^https?://(www\.)?instagram\.com/(?!p/|explore|reel|stories|accounts)([^/?#]+)", re.I)),
    ("youtube", re.compile(r"^https?://(www\.|m\.)?youtube\.com/((?:channel|c|user)/[^/?#]+|@[^/?#]+)", re.I)),
    ("x", re.compile(r"^https?://(www\.|mobile\.)?(twitter|x)\.com/(?!intent|share|home|search|hashtag|i/)([^/?#]+)", re.I)),
    ("tiktok", re.compile(r"^https?://(www\.)?tiktok\.com/(@[^/?#]+)", re.I)),
    ("vimeo", re.compile(r"^https?://(www\.)?vimeo\.com/(?!video|\d)([^/?#]+)", re.I)),
]
PLACEHOLDER = re.compile(r"(yourpage|your-page|username|example|pagename|profile\.php$|^#)", re.I)
CAREERS = re.compile(r"(karriere|ledige[ -_]?stillinger|stillinger|jobb|career|jobs|job-openings|rekrutter|join-us|bli-en-av-oss|jobbe-hos)", re.I)
CONTACT = re.compile(r"(kontakt|contact|om-oss|om oss|about|hvem-er-vi|firma|selskapet|company)", re.I)
ATS = re.compile(r"https?://([\w.-]+\.)?(webcruiter\.no|jobylon\.com|teamtailor\.com|recman\.(no|page)|finn\.no/(job|jobb)|arbeidsplassen\.nav\.no/stillinger/stilling|"
                 r"hrmanager\.no|easycruit\.com|reachmee\.com|varbi\.com|jobbnorge\.no|emply\.(com|net)|talentech\.com|simployer\.com|"
                 r"workbuster\.com|jobindex\.dk|smartrecruiters\.com|lever\.co|greenhouse\.io|workday\.com|jobs\.personio\.|homerun\.co)[^\s\"'<>]*", re.I)
NEWS_PATH = re.compile(r"/(nyhet|nyheter|news|aktuelt|artikkel|artikler|blogg?|presse|press|pressemelding|article|articles|innlegg|stories|insights)(/|-|$)|/20[12]\d/", re.I)


def fold(text: str) -> str:
    text = (text or "").lower().replace("æ", "ae").replace("ø", "o").replace("å", "a")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def name_tokens(legal_name: str) -> list[str]:
    return [t for t in fold(legal_name).split() if t not in LEGAL_TOKENS]


def valid_org(number: str) -> bool:
    if len(number) != 9 or not number.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(number[:8], (3, 2, 7, 6, 5, 4, 3, 2)))
    check = 11 - total % 11
    return (0 if check == 11 else check) == int(number[8]) and check != 10


def org_numbers(text: str) -> tuple[set[str], set[str]]:
    """(all checksum-valid 9-digit numbers, those introduced by an org-number label)."""
    compact = re.sub(r"(?<=\d)[ .  -](?=\d)", "", text)
    found = {m for m in re.findall(r"(?<!\d)\d{9}(?!\d)", compact) if valid_org(m)}
    labelled = {m for m in re.findall(r"(?:org|foretaks|mva|vat|company)[^\d]{0,25}(\d{9})(?!\d)", compact, re.I) if valid_org(m)}
    return found, labelled


def types(node: dict) -> set[str]:
    kind = node.get("@type")
    return {k for k in (kind if isinstance(kind, list) else [kind]) if isinstance(k, str)}


def digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


class Page(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.title = ""
        self.meta: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []
        self.ldjson: list[str] = []
        self.feeds: list[str] = []
        self.text_parts: list[str] = []
        self._stack: list[str] = []
        self._href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in {"script", "style", "noscript", "title"}:
            self._stack.append(tag if not (tag == "script" and "ld+json" in a.get("type", "")) else "ldjson")
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"].strip())
        elif tag == "a" and a.get("href"):
            self._href = urllib.parse.urljoin(self.base, a["href"].strip())
            self._anchor_text = [a.get("title", ""), a.get("aria-label", "")]
        elif tag == "link" and "alternate" in a.get("rel", "") and re.search(r"(rss|atom)", a.get("type", "")) and a.get("href"):
            self.feeds.append(urllib.parse.urljoin(self.base, a["href"]))
        elif tag in {"br", "p", "div", "li", "td", "footer", "section", "h1", "h2", "h3", "span"}:
            self.text_parts.append(" ")

    def handle_endtag(self, tag):
        if self._stack and tag in {"script", "style", "noscript", "title"}:
            self._stack.pop()
        elif tag == "a" and self._href:
            self.links.append((self._href, " ".join(t for t in self._anchor_text if t).strip()))
            self._href = None

    def handle_data(self, data):
        top = self._stack[-1] if self._stack else None
        if top == "title":
            self.title += data
        elif top == "ldjson":
            self.ldjson.append(data)
        elif top is None:
            self.text_parts.append(data)
            if self._href is not None:
                self._anchor_text.append(data.strip())

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self.text_parts)).strip()

    def jsonld(self) -> list[dict]:
        items = []
        for raw in self.ldjson:
            try:
                data = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            stack = [data]
            while stack:
                node = stack.pop()
                if isinstance(node, list):
                    stack.extend(node)
                elif isinstance(node, dict):
                    items.append(node)
                    stack.extend(v for k, v in node.items() if k == "@graph" or isinstance(v, (dict, list)))
        return items


def parse(resp: Resp) -> Page:
    page = Page(resp.final_url)
    try:
        page.feed(resp.text()[:2_000_000])
        page.close()
    except Exception:
        pass
    return page


def registrable(host: str) -> str:
    host = (host or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def candidates(entity: dict, legal_name: str) -> list[tuple[str, str]]:
    """Ordered (url, origin) candidates from official data first, then the legal name."""
    out, seen = [], set()

    def add(url, origin):
        host = registrable(urllib.parse.urlsplit(url).hostname or "")
        if host and host not in seen:
            seen.add(host)
            out.append((url, origin))

    site = (entity.get("hjemmeside") or "").strip()
    if site:
        add(site if re.match(r"https?://", site, re.I) else "https://" + site, "registry_website")
    email = (entity.get("epostadresse") or "").strip().lower()
    if "@" in email:
        domain = email.rsplit("@", 1)[1]
        if domain and SHARED.get(domain, 0) < 3:
            add(f"https://{domain}/", "registry_email_domain")
    tokens = name_tokens(legal_name)
    merged: list[str] = []  # "P.E. GAARUD" -> ["pe", "gaarud"]
    for t in tokens:
        if len(t) == 1 and merged and merged[-1].isalpha() and len(merged[-1]) <= 3 and all(len(x) == 1 for x in merged[-1]):
            merged[-1] += t
        else:
            merged.append(t)
    distinctive = [t for t in tokens if t not in GENERIC and len(t) >= 3 and not t.isdigit()]
    if distinctive and 1 <= len(tokens) <= 4:
        names = ["".join(tokens) + ".no", "-".join(tokens) + ".no", "-".join(merged) + ".no", "".join(tokens) + ".com"]
        if len(merged) >= 2 and merged[0] not in GENERIC | PLACES and len(merged[0]) >= 4:
            names.append(merged[0] + ".no")  # "LAPSEN & CO AS" -> lapsen.no; still needs on-site proof
        for domain in dict.fromkeys(names):
            if 4 <= len(domain) <= 63:  # shared domains allowed: the owning company proves itself on the site
                add(f"https://{domain}/", "name_domain_guess")
    return out


def dns_exists(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return resolve_public(host) or resolve_public("www." + host)


def fetch_home(url: str, fx: Fetcher) -> Resp:
    resp = fx.get(url, accept=HTML)
    if not resp.ok and not resp.blocked and resp.error != "budget_exhausted":
        host = urllib.parse.urlsplit(url).hostname or ""
        alt = f"https://www.{host}/" if not host.startswith("www.") else f"http://{host}/"
        resp2 = fx.get(alt, accept=HTML)
        if resp2.ok or resp2.blocked:
            return resp2
    return resp


def verify(entity: dict, legal_name: str, pages: list[tuple[Resp, Page]], origin: str, people: list[str] = ()) -> tuple[str, list[str], set[str]]:
    """Return (decision, proof list, other org numbers). decision: exact | related | weak."""
    org = entity["organisasjonsnummer"]
    all_text = " ".join(f"{p.title} {p.meta.get('og:site_name', '')} {p.text} " + " ".join(h for h, _ in p.links if h.startswith(("mailto:", "tel:")))
                        for _, p in pages)
    found, labelled = org_numbers(all_text)
    proof = []
    if org in found or any(org in href for _, p in pages for href, _ in p.links):
        proof.append("organisation_number_on_site")
    folded = f" {fold(all_text)} "
    tokens = name_tokens(legal_name)
    name_hit = bool(tokens) and f" {' '.join(tokens)} " in folded
    if name_hit:
        proof.append("legal_name_on_site")
    # Norwegian registered names are unique, so the full name including its form (e.g. "LIME FRISØR AS") is specific.
    full = fold(legal_name)
    if full.split()[-1:] and full.split()[-1] in FORM_SUFFIXES and len(full.split()) >= 2 and f" {full} " in folded:
        proof.append("exact_registered_name_on_site")
    page_digits = digits(all_text)
    for key in ("telefon", "mobil"):
        number = digits(entity.get(key))[-8:]
        if len(number) == 8 and number in page_digits:
            proof.append(f"registry_{key}_on_site")
    email = (entity.get("epostadresse") or "").lower()
    if email and email in all_text.lower():
        proof.append("registry_email_on_site")
    street = fold(", ".join((entity.get("forretningsadresse") or {}).get("adresse") or []))
    if len(street) >= 6 and re.search(r"\d", street) and f" {street} " in folded:
        proof.append("registry_address_on_site")
    for person in people:
        parts = fold(person).split()
        if len(parts) >= 2 and (f" {' '.join(parts)} " in folded or f" {parts[0]} {parts[-1]} " in folded):
            proof.append("registered_role_holder_on_site")
            break
    others = (labelled - {org})
    if "organisation_number_on_site" in proof:
        return "exact", proof, others
    no_domain = (urllib.parse.urlsplit(pages[0][0].final_url).hostname or "").endswith(".no")
    corroborated = [p for p in proof if p not in {"legal_name_on_site", "exact_registered_name_on_site"}
                    and not (origin == "registry_email_domain" and p == "registry_email_on_site")]
    if no_domain and "exact_registered_name_on_site" in proof:
        corroborated.append("exact_registered_name_on_no_domain")
    if others and not corroborated:
        return "related", proof, others
    if origin == "registry_website" and proof:
        return "exact", ["registry_declared_website", *proof], others
    domain_is_name = spells_name(pages[0][0].final_url, legal_name)
    # A register e-mail domain can belong to a parent, developer or accountant: the name alone counts only when the domain spells it.
    if origin == "registry_email_domain" and ((name_hit and domain_is_name) or corroborated):
        return "exact", ["registry_email_domain", *proof], others
    if origin == "name_domain_guess" and name_hit and corroborated:
        return "exact", proof, others
    return "weak", proof, others


def spells_name(url: str, legal_name: str) -> bool:
    label = fold((urllib.parse.urlsplit(url).hostname or "").removeprefix("www.").rsplit(".", 1)[0]).replace(" ", "")
    tokens = name_tokens(legal_name)
    return bool(tokens) and "".join(tokens) in label


def extra_pages(home: Page, root_host: str, pattern: re.Pattern, limit: int) -> list[str]:
    urls = []
    for href, text in home.links:
        parts = urllib.parse.urlsplit(href)
        if registrable(parts.hostname or "") != root_host or parts.path in {"", "/"}:
            continue
        if pattern.search(parts.path) or pattern.search(text or ""):
            url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
            if url not in urls:
                urls.append(url)
    return urls[:limit]


def social_links(pages: list[Page]) -> dict[str, str]:
    found: dict[str, str] = {}
    hrefs = [h for p in pages for h, _ in p.links]
    for p in pages:
        for node in p.jsonld():
            same = node.get("sameAs")
            hrefs.extend([same] if isinstance(same, str) else [s for s in same or [] if isinstance(s, str)])
    for href in hrefs:
        for platform, pattern in SOCIAL:
            m = pattern.match(href.strip())
            if m and platform not in found and not PLACEHOLDER.search(m.group(m.lastindex)):
                found[platform] = m.group(0).rstrip("/")
    return found


def research_website(entity: dict | None, fx: Fetcher, b: Builder, max_candidates: int = 4, prior_site: str | None = None) -> None:
    if not entity:
        for section in ("website", "social_profiles", "jobs"):
            b.state(section, "failed", "Identity record unavailable; website research skipped")
        return
    legal_name = entity.get("navn") or ""
    prior_host = registrable(urllib.parse.urlsplit(prior_site or "").hostname or "")
    transient = None  # a previously verified or registry-listed site that failed for a temporary reason
    people = [c["value"]["name"] for c in b.claims.values() if c["section"] == "leadership" and "organisation_number" not in c["value"]]
    tried = 0
    for url, origin in candidates(entity, legal_name):
        if tried >= max_candidates or fx.remaining() < 30:
            b.error("website", "Candidate or request budget reached")
            break
        if origin == "name_domain_guess" and not dns_exists(url):
            continue
        tried += 1
        home = fetch_home(url, fx)
        if not home.ok:
            reason = home.error or f"HTTP {home.status}"
            b.candidates.append({"url": url, "origin": origin, "decision": "unreachable", "reason": reason})
            temporary = not home.blocked and (home.status is None or home.status >= 500)
            if temporary and registrable(urllib.parse.urlsplit(url).hostname or "") == prior_host:
                transient = f"{url} (verified in the previous run) temporarily unreachable ({reason})"
            elif origin == "registry_website":
                b.state("website", "blocked" if home.blocked else "not_available",
                        f"Registry-listed website {url} could not be retrieved ({reason})")
            continue
        if "html" not in home.content_type.lower():
            b.candidates.append({"url": url, "origin": origin, "decision": "not_html", "reason": home.content_type})
            continue
        page = parse(home)
        pages = [(home, page)]
        root_host = registrable(urllib.parse.urlsplit(home.final_url).hostname or "")
        decision, proof, others = verify(entity, legal_name, pages, origin, people)
        if decision != "exact":
            for extra in extra_pages(page, root_host, CONTACT, 2):
                r = fx.get(extra, accept=HTML)
                if r.ok and "html" in r.content_type.lower():
                    pages.append((r, parse(r)))
                    decision, proof, others = verify(entity, legal_name, pages, origin, people)
                    if decision == "exact":
                        break
        if decision == "exact" and len(pages) == 1:  # same page set every run, so contact details do not flicker
            for extra in extra_pages(page, root_host, CONTACT, 1):
                r = fx.get(extra, accept=HTML)
                if r.ok and "html" in r.content_type.lower():
                    pages.append((r, parse(r)))
        related_registry_site = decision == "related" and origin == "registry_website"
        if decision != "exact" and not related_registry_site:
            b.candidates.append({"url": home.final_url, "origin": origin, "decision": "ambiguous" if decision == "weak" else "related_entity_site",
                                 "proof": proof, "other_organisation_numbers": sorted(others)})
            continue
        # A domain many entities list (group, franchise or manager) is attributed in full only to the entity that owns it.
        owner = bool({"organisation_number_on_site", "exact_registered_name_on_site"} & set(proof)) or spells_name(home.final_url, legal_name)
        publish_site(entity, legal_name, origin, proof, others, pages, root_host, fx, b,
                     related=related_registry_site or (SHARED.get(root_host, 0) >= 3 and not owner))
        return
    if transient:
        for section in ("website", "social_profiles", "jobs"):
            b.state(section, "failed", transient + "; last known values are kept")
        b.error("website", transient)
        return
    if "website" not in b.sections:
        ambiguous = [c for c in b.candidates if c["decision"] in {"ambiguous", "related_entity_site"}]
        if ambiguous:
            b.state("website", "ambiguous", "Candidate site(s) found but identity not proven: " + ", ".join(c["url"] for c in ambiguous))
        else:
            b.state("website", "not_available", "No website listed in the register and no verifiable site found from the register e-mail domain or legal name")
    for section in ("social_profiles", "jobs"):
        b.state(section, "not_available", "No verified company website to take company-owned links from")


def publish_site(entity, legal_name, origin, proof, others, pages, root_host, fx: Fetcher, b: Builder, related: bool = False) -> None:
    home, page = pages[0]
    evs = [b.ev(r, "company_owned_website", "html_static_fetch") for r, _ in pages]
    registry_ev = [eid for eid, ev in b.evidence.items() if ev["extraction_method"] == "brreg_enhetsregisteret_api_v2_json"]
    site = urllib.parse.urlunsplit(urllib.parse.urlsplit(home.final_url)._replace(path="/", query="", fragment=""))
    meta = {"discovered_via": origin, "identity_proof": proof or ["registry_declared_website"]}
    shared = SHARED.get(root_host, 0)
    if shared >= 3:
        meta["relationship"] = f"domain shared by {shared} registered entities (group, brand or managed site)"
    if others:
        meta["other_organisation_numbers_on_site"] = sorted(others)
    if related and others:
        meta["relationship"] = "registry-declared website that names another organisation number (group or related entity site)"
    b.claim("website.official_website", "website", "official_website", {"url": site, "domain": root_host},
            evs + (registry_ev if origin.startswith("registry") else []), basis=origin, locator="identity proof: " + ", ".join(meta["identity_proof"]), **meta)
    if related:
        note = ("Website is shared with or belongs to a related entity" + (f" ({', '.join(sorted(others))})" if others else "")
                + "; its profiles, jobs and news are not attributed to this company")
        for section in ("social_profiles", "jobs"):
            b.state(section, "ambiguous", note)
        return
    title = re.sub(r"\s+", " ", page.title).strip()
    if title:
        b.claim("website.title", "website", "page_title", title[:300], [evs[0]], locator="<title>", materiality="minor")
    description = page.meta.get("description") or page.meta.get("og:description")
    if description:
        b.claim("website.description", "website", "self_description", description[:600], [evs[0]], locator="meta[name=description]",
                basis="company_reported", materiality="minor")
    org_nodes = [n for _, p in pages for n in p.jsonld() if types(n) & {"Organization", "Corporation", "LocalBusiness"}]
    brand = page.meta.get("og:site_name") or next((n.get("name") for n in org_nodes if isinstance(n.get("name"), str)), None)
    if brand and fold(brand) != fold(legal_name):
        b.claim("identity.public_brand", "identity", "public_brand", brand.strip()[:200], [evs[0]], locator="og:site_name / JSON-LD Organization.name",
                basis="company_owned_website")

    socials = social_links([p for _, p in pages])
    for platform, url in socials.items():
        b.claim(f"social_profiles.{platform}", "social_profiles", "profile", {"platform": platform, "url": url}, evs,
                basis="linked_from_verified_official_website", locator=f"a[href^='{url}']")
    if not socials:
        b.state("social_profiles", "not_available", "The verified website links to no company social or video profiles", evs)

    emails = sorted({h[7:].split("?")[0].lower() for _, p in pages for h, _ in p.links if h.lower().startswith("mailto:") and "@" in h})[:5]
    phones = sorted({re.sub(r"[^\d+]", "", h[4:]) for _, p in pages for h, _ in p.links if h.lower().startswith("tel:")} - {""})[:5]
    for e in emails:
        b.claim(f"contact.site_email.{e}", "contact", "email", e, evs, basis="company_owned_website", locator="a[href^=mailto:]", materiality="minor")
    for ph in phones:
        b.claim(f"contact.site_phone.{ph}", "contact", "phone", ph, evs, basis="company_owned_website", locator="a[href^=tel:]", materiality="minor")

    research_jobs(page, root_host, evs[0], fx, b)
    research_activity(page, home, root_host, evs[0], fx, b)


def _job_links(page: Page, root_host: str) -> list[tuple[str, str]]:
    jobs = []
    for href, text in page.links:
        m = ATS.match(href)
        parts = urllib.parse.urlsplit(href)
        internal_ad = (registrable(parts.hostname or "") == root_host and re.search(r"/(stilling|stillinger|jobb|jobs|job|karriere|career|careers|ledige-stillinger)/[^/]{6,}", parts.path, re.I))
        if (m and re.search(r"/(job|jobs|stilling|stillinger|ad|annonse|vacanc|position|jobdetails|\d{4,})", href, re.I)) or internal_ad:
            title = (text or "").strip()
            if len(title) >= 4 and not CAREERS.fullmatch(fold(title).replace(" ", "-")):
                jobs.append((href.split("#")[0], title[:200]))
    return list(dict.fromkeys(jobs))[:25]


def research_jobs(home: Page, root_host: str, home_ev: str, fx: Fetcher, b: Builder) -> None:
    careers = extra_pages(home, root_host, CAREERS, 1)
    ats_links = [h for h, _ in home.links if ATS.match(h)]
    postings, sources = _job_links(home, root_host), [home_ev]
    for node in home.jsonld():
        if "JobPosting" in types(node) and node.get("title"):
            postings.append((node.get("url") or home.base, str(node["title"])[:200]))
    target = careers[0] if careers else (ats_links[0] if ats_links else None)
    if target and fx.remaining() > 40:
        r = fx.get(target, accept=HTML)
        if r.ok and "html" in r.content_type.lower():
            cp = parse(r)
            ev = b.ev(r, "company_owned_website" if registrable(urllib.parse.urlsplit(r.final_url).hostname or "") == root_host else "company_linked_recruiting_page",
                      "html_static_fetch")
            sources.append(ev)
            b.claim("jobs.careers_page", "jobs", "careers_page", r.final_url, [home_ev, ev], basis="linked_from_verified_official_website", materiality="minor")
            postings += _job_links(cp, root_host)
            for node in cp.jsonld():
                if "JobPosting" in types(node) and node.get("title"):
                    postings.append((node.get("url") or r.final_url, str(node["title"])[:200]))
            if not postings:
                for href in [h for h, _ in cp.links if ATS.match(h)][:1]:
                    r2 = fx.get(href, accept=HTML)
                    if r2.ok and "html" in r2.content_type.lower():
                        sources.append(b.ev(r2, "company_linked_recruiting_page", "html_static_fetch"))
                        postings += [(u, t) for u, t in _job_links(parse(r2), root_host)]
    seen = set()
    for url, title in postings:
        key = hashlib.sha256(url.encode()).hexdigest()[:12]
        if key in seen:
            continue
        seen.add(key)
        b.claim(f"jobs.posting.{key}", "jobs", "job_posting", {"title": title, "url": url}, sources[-1:] if len(sources) > 1 else sources,
                basis="linked_from_verified_official_website", locator=f"a[href='{url}']")
    if not seen:
        note = "Careers page checked; no individual job postings found" if careers or ats_links else "No careers page or job postings linked from the verified website"
        b.state("jobs", "not_available", note, sources)


def _date(value: str) -> str | None:
    m = re.search(r"(20\d\d-\d\d-\d\d)", value or "")
    if m:
        return m.group(1)
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(value.strip()).strftime("%Y-%m-%d")
    except (TypeError, ValueError, IndexError):
        return None


def _xml_items(xml: str, item_tag: str, fields: dict[str, str]) -> list[dict]:
    items = []
    for block in re.findall(rf"<{item_tag}[\s>](.*?)</{item_tag}>", xml, re.S | re.I)[:2000]:
        item = {}
        for out, tag in fields.items():
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S | re.I)
            if m:
                item[out] = re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1)).strip()
        if not item.get("link") and item_tag == "entry":
            m = re.search(r"<link[^>]+href=\"([^\"]+)\"", block)
            item["link"] = m.group(1) if m else None
        items.append(item)
    return items


def research_activity(home: Page, home_resp: Resp, root_host: str, home_ev: str, fx: Fetcher, b: Builder) -> None:
    items: list[dict] = []
    source_ev = None
    for node in home.jsonld():
        kind = types(node) & {"NewsArticle", "BlogPosting", "Article", "Event", "PressRelease"}
        if kind:
            date = _date(str(node.get("datePublished") or node.get("startDate") or ""))
            if date and (node.get("headline") or node.get("name")):
                items.append({"title": str(node.get("headline") or node.get("name"))[:200], "url": node.get("url") or home_resp.final_url,
                              "date": date, "date_basis": "json_ld_" + ("startDate" if kind == {"Event"} else "datePublished"), "ev": home_ev})
    feeds = [f for f in home.feeds if registrable(urllib.parse.urlsplit(f).hostname or "") == root_host][:1]
    if feeds and fx.remaining() > 40:
        r = fx.get(feeds[0], accept="application/rss+xml,application/atom+xml,application/xml,text/xml,*/*")
        if r.ok:
            source_ev = b.ev(r, "company_owned_feed", "rss_atom_regex")
            text = r.text()
            for it in _xml_items(text, "item", {"title": "title", "link": "link", "date": "pubDate"}) + \
                      _xml_items(text, "entry", {"title": "title", "date": "published"}):
                date = _date(it.get("date") or "")
                if date and it.get("title") and it.get("link"):
                    items.append({"title": re.sub(r"<[^>]+>", "", it["title"])[:200], "url": it["link"], "date": date,
                                  "date_basis": "feed_publication_date", "ev": source_ev})
    if not items and fx.remaining() > 40:
        parts = urllib.parse.urlsplit(home_resp.final_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        r = fx.get((fx.sitemaps(origin) or [origin + "/sitemap.xml"])[0], accept="application/xml,text/xml,*/*")
        if r.ok and b"<sitemapindex" in r.body[:3000]:
            children = re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text())
            pick = [c for c in children if re.search(r"(post|news|nyhet|artik|blog|aktuelt|article|press)", c, re.I)][:1] or children[:1]
            r = fx.get(pick[0], accept="application/xml,text/xml,*/*") if pick else r
        if r.ok and b"<urlset" in r.body[:5000]:
            source_ev = b.ev(r, "company_owned_sitemap", "sitemap_regex")
            for it in _xml_items(r.text(), "url", {"link": "loc", "date": "lastmod"}):
                url, date = it.get("link") or "", _date(it.get("date") or "")
                if date and NEWS_PATH.search(urllib.parse.urlsplit(url).path):
                    slug = [s for s in urllib.parse.urlsplit(url).path.split("/") if s][-1]
                    items.append({"title": re.sub(r"[-_]+", " ", urllib.parse.unquote(slug)).strip()[:200], "url": url, "date": date,
                                  "date_basis": "sitemap_lastmod", "title_basis": "url_slug", "ev": source_ev})
    items.sort(key=lambda i: i["date"], reverse=True)
    for it in list({i["url"]: i for i in items}.values())[:10]:
        key = hashlib.sha256(it["url"].encode()).hexdigest()[:12]
        ev = it.pop("ev")
        b.claim(f"activity.site.{key}", "activity", "company_news", it, [ev], effective_date=it["date"], basis="company_owned_website",
                locator=it["date_basis"], materiality="minor")
