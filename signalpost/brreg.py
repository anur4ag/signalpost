"""Official Norwegian records from Brønnøysundregistrene (open data, NLOD 2.0).

Identity, roles, subunits, group structure and annual accounts. Birth dates in role data are discarded.
"""
from __future__ import annotations

import json
import threading
import time

from .net import Fetcher
from .profile import Builder, resp_failure_state

API = "https://data.brreg.no"
ENTITY = API + "/enhetsregisteret/api/enheter/{org}"
ROLES = API + "/enhetsregisteret/api/enheter/{org}/roller"
SUBUNITS = API + "/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=100&page={page}"
GROUP = API + "/enhetsregisteret/api/konsernstruktur/{org}"
ACCOUNTS = API + "/regnskapsregisteret/regnskap/{org}"
ACCOUNT_YEARS = API + "/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/aar"
ACCOUNT_PDF = API + "/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/{year}"
REGISTRY_PAGE = "https://virksomhet.brreg.no/nb/oppslag/enheter/{org}"
JSON = "application/json"

# Regnskapsregisteret's copy endpoint allows roughly 30 request starts per minute.
_years_lock = threading.Lock()
_years_last = [0.0]


def _get(value, *path):
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def fmt_address(address: dict | None) -> str | None:
    if not address:
        return None
    parts = [", ".join(address.get("adresse") or []), " ".join(filter(None, [address.get("postnummer"), address.get("poststed")]))]
    country = address.get("land")
    if country and country != "Norge":
        parts.append(country)
    return ", ".join(p for p in parts if p) or None


def fetch_entity(org: str, fx: Fetcher, b: Builder) -> dict | None:
    resp = fx.get(ENTITY.format(org=org), accept=JSON, retries=2)
    if not resp.ok:
        state = resp_failure_state(resp)
        note = "Entity deleted or absent from the live register (HTTP %s)" % resp.status if state == "not_available" else (resp.error or f"HTTP {resp.status}")
        b.state("identity", state, note)
        if state != "not_available":
            b.error("identity", note, resp.url)
        return None
    d = resp.json()
    e = b.ev(resp, "official_registry", "brreg_enhetsregisteret_api_v2_json")

    def c(field, value, locator, **kw):
        if value not in (None, "", [], {}):
            b.claim(f"identity.{field}", "identity", field, value, [e], locator=locator, **kw)

    c("organisation_number", d.get("organisasjonsnummer"), "/organisasjonsnummer")
    c("legal_name", d.get("navn"), "/navn")
    form = d.get("organisasjonsform") or {}
    c("legal_form", {"code": form.get("kode"), "description": form.get("beskrivelse")}, "/organisasjonsform")
    status, status_date = "active", None
    for flag, label, date_key in (("konkurs", "bankrupt", "konkursdato"), ("underAvvikling", "under_liquidation", "underAvviklingDato"),
                                  ("underTvangsavviklingEllerTvangsopplosning", "under_forced_dissolution", None)):
        if d.get(flag):
            status, status_date = label, d.get(date_key) if date_key else None
    c("status", status, "/konkurs,/underAvvikling,/underTvangsavviklingEllerTvangsopplosning", effective_date=status_date)
    c("founded", d.get("stiftelsesdato"), "/stiftelsesdato")
    c("registered_in_central_register", d.get("registreringsdatoEnhetsregisteret"), "/registreringsdatoEnhetsregisteret")
    industries = [{"code": n.get("kode"), "description": n.get("beskrivelse")} for n in (d.get(f"naeringskode{i}") for i in (1, 2, 3)) if n]
    c("industry", industries, "/naeringskode1..3")
    c("institutional_sector", _get(d, "institusjonellSektorkode", "beskrivelse"), "/institusjonellSektorkode")
    c("business_address", fmt_address(d.get("forretningsadresse")), "/forretningsadresse")
    c("postal_address", fmt_address(d.get("postadresse")), "/postadresse")
    c("municipality", _get(d, "forretningsadresse", "kommune") or _get(d, "postadresse", "kommune"), "/forretningsadresse/kommune")
    if d.get("harRegistrertAntallAnsatte") and d.get("antallAnsatte") is not None:
        c("registered_employees", d["antallAnsatte"], "/antallAnsatte",
          effective_date=d.get("registreringsdatoAntallAnsatteNAVAaregisteret") or d.get("registreringsdatoAntallAnsatteEnhetsregisteret"))
    else:
        b.state("identity", "not_available", "No employee count registered in the register (not converted to zero)")
    capital = d.get("kapital") or {}
    if capital.get("belop") is not None:
        c("share_capital", {"amount": capital.get("belop"), "currency": capital.get("valuta"), "type": capital.get("type"),
                            "shares": capital.get("antallAksjer")}, "/kapital", effective_date=capital.get("innfortDato"))
    c("statutory_purpose", " ".join(d.get("vedtektsfestetFormaal") or []), "/vedtektsfestetFormaal", materiality="minor")
    c("registered_activity", " ".join(d.get("aktivitet") or []), "/aktivitet", materiality="minor")
    c("former_names", [{"name": h.get("navn"), "from": h.get("fraDato"), "to": h.get("tilDato")} for h in d.get("historiskeNavn") or []], "/historiskeNavn")
    registers = {k: d.get(v) for k, v in (("vat", "registrertIMvaregisteret"), ("business_register", "registrertIForetaksregisteret"),
                                             ("voluntary_register", "registrertIFrivillighetsregisteret"), ("foundation_register", "registrertIStiftelsesregisteret")) if d.get(v) is not None}
    c("register_memberships", registers, "/registrertI*")
    c("latest_submitted_accounts_year", d.get("sisteInnsendteAarsregnskap"), "/sisteInnsendteAarsregnskap")
    c("part_of_group", d.get("erIKonsern"), "/erIKonsern")
    c("registry_website", d.get("hjemmeside"), "/hjemmeside")
    c("parent_unit", d.get("overordnetEnhet"), "/overordnetEnhet")
    b.claim("identity.registry_record_url", "identity", "registry_record_url", REGISTRY_PAGE.format(org=org), [e], locator="/organisasjonsnummer", materiality="minor")

    for field, source_key in (("email", "epostadresse"), ("phone", "telefon"), ("mobile", "mobil")):
        if d.get(source_key):
            b.claim(f"contact.registry_{field}", "contact", field, d[source_key], [e], locator=f"/{source_key}", basis="official_registry")

    # Dated official events: each is a filed/registered fact with its registry date.
    events = [("founded", "Company founded", d.get("stiftelsesdato")),
              ("articles_updated", "Articles of association dated", d.get("vedtektsdato")),
              ("share_capital_registered", "Current share capital registered", capital.get("innfortDato")),
              ("employee_count_reported", "Employee count reported to the register", d.get("registreringsdatoAntallAnsatteNAVAaregisteret")),
              ("bankruptcy_opened", "Bankruptcy proceedings opened", d.get("konkursdato")),
              ("liquidation_started", "Liquidation resolved", d.get("underAvviklingDato"))]
    for key, label, date in events:
        if date:
            b.claim(f"activity.registry.{key}", "activity", "registry_event", {"event": label, "date": date, "source": "Brønnøysundregistrene"},
                    [e], effective_date=date, locator=f"registry:{key}", materiality="minor")
    return d


def drop_birth_dates(body: bytes) -> bytes:
    def clean(node):
        if isinstance(node, dict):
            return {k: clean(v) for k, v in node.items() if k != "fodselsdato"}
        return [clean(v) for v in node] if isinstance(node, list) else node
    try:
        return json.dumps(clean(json.loads(body)), ensure_ascii=False, sort_keys=True).encode()
    except ValueError:
        return body


def fetch_roles(org: str, fx: Fetcher, b: Builder) -> None:
    resp = fx.get(ROLES.format(org=org), accept=JSON, retries=2, redact=drop_birth_dates)
    if not resp.ok:
        b.state("leadership", resp_failure_state(resp), resp.error or f"HTTP {resp.status}")
        return
    e = b.ev(resp, "official_registry", "brreg_roles_api_json")
    count = 0
    for group in resp.json().get("rollegrupper", []):
        changed = group.get("sistEndret")
        gcode = _get(group, "type", "kode")
        for role in group.get("roller", []):
            if role.get("avregistrert") or role.get("fratraadt"):
                continue
            person, entity = role.get("person") or {}, role.get("enhet") or {}
            name = " ".join(filter(None, [_get(person, "navn", "fornavn"), _get(person, "navn", "mellomnavn"), _get(person, "navn", "etternavn")]))
            if not name:
                name = " ".join(entity.get("navn") or []) or None
            if not name:
                continue
            code = _get(role, "type", "kode")
            value = {"name": name, "role": _get(role, "type", "beskrivelse"), "role_code": code, "group": _get(group, "type", "beskrivelse")}
            if entity.get("organisasjonsnummer"):
                value["organisation_number"] = entity["organisasjonsnummer"]
            if role.get("valgtAv"):
                value["elected_by"] = _get(role, "valgtAv", "beskrivelse")
            if person.get("erDoed"):
                value["deceased"] = True
            key = entity.get("organisasjonsnummer") or name.casefold()
            b.claim(f"leadership.{code}.{key}", "leadership", "role", value, [e], effective_date=changed, locator=f"/rollegrupper[{gcode}]")
            count += 1
        if changed and gcode in {"STYR", "DAGL"}:
            b.claim(f"activity.registry.roles_{gcode}", "activity", "registry_event",
                    {"event": "Board composition last changed" if gcode == "STYR" else "Managing director registration last changed",
                     "date": changed, "source": "Brønnøysundregistrene"}, [e], effective_date=changed, materiality="minor")
    if not count:
        b.state("leadership", "not_available", "The official roles register returned no active roles", [e])


def fetch_subunits(org: str, entity: dict | None, fx: Fetcher, b: Builder) -> None:
    for page in range(3):  # ponytail: first 300 workplaces only; very large chains are truncated and say so
        resp = fx.get(SUBUNITS.format(org=org, page=page), accept=JSON, retries=2)
        if not resp.ok:
            b.state("locations", resp_failure_state(resp), resp.error or f"HTTP {resp.status}")
            break
        e = b.ev(resp, "official_registry", "brreg_subunits_api_json")
        body = resp.json()
        for unit in _get(body, "_embedded", "underenheter") or []:
            value = {"name": unit.get("navn"), "organisation_number": unit.get("organisasjonsnummer"),
                     "address": fmt_address(unit.get("beliggenhetsadresse") or unit.get("postadresse")),
                     "municipality": _get(unit, "beliggenhetsadresse", "kommune"),
                     "industry": _get(unit, "naeringskode1", "beskrivelse"), "started": unit.get("oppstartsdato")}
            if unit.get("harRegistrertAntallAnsatte") and unit.get("antallAnsatte") is not None:
                value["registered_employees"] = unit["antallAnsatte"]
            if unit.get("hjemmeside"):
                value["website"] = unit["hjemmeside"]
            b.claim(f"locations.subunit.{unit.get('organisasjonsnummer')}", "locations", "registered_workplace",
                    {k: v for k, v in value.items() if v is not None}, [e], effective_date=unit.get("oppstartsdato"))
        total_pages = _get(body, "page", "totalPages") or 1
        if page + 1 >= total_pages:
            break
    else:
        b.error("locations", "More than 300 registered workplaces; list truncated")
    if entity and entity.get("forretningsadresse"):
        # The registered business address is itself a location fact from the entity record.
        src = [eid for eid, ev in b.evidence.items() if ev["extraction_method"] == "brreg_enhetsregisteret_api_v2_json"]
        b.claim("locations.business_address", "locations", "registered_business_address",
                {"address": fmt_address(entity["forretningsadresse"]), "municipality": _get(entity, "forretningsadresse", "kommune")}, src,
                locator="/forretningsadresse")


def fetch_group(org: str, entity: dict | None, fx: Fetcher, b: Builder) -> None:
    if entity is not None and not entity.get("erIKonsern"):
        b.state("group", "not_applicable", "The register reports that the company is not part of a group (erIKonsern=false)")
        return
    resp = fx.get(GROUP.format(org=org), accept=JSON, retries=1)
    if not resp.ok:
        b.state("group", resp_failure_state(resp), resp.error or f"HTTP {resp.status}")
        return
    e = b.ev(resp, "official_registry", "brreg_group_structure_api_json")
    root = resp.json()

    def find(node, parent=None):
        if node.get("organisasjonsnummer") == org:
            return node, parent
        for child in node.get("children") or []:
            hit = find(child, node)
            if hit[0]:
                return hit
        return None, None

    node, parent = find(root)
    if not node:
        b.state("group", "not_available", "Group structure returned without this company", [e])
        return
    if root.get("organisasjonsnummer") != org:
        b.claim("group.ultimate_parent", "group", "ultimate_parent", {"name": root.get("navn"), "organisation_number": root.get("organisasjonsnummer")}, [e])
    if parent:
        b.claim("group.parent", "group", "parent", {"name": parent.get("navn"), "organisation_number": parent.get("organisasjonsnummer"),
                                                    "relationship": _get(node, "knytningsform", "beskrivelse"), "basis": node.get("grunnlag")},
                [e], effective_date=node.get("dato"))
    for child in (node.get("children") or [])[:100]:
        if child.get("organisasjonsnummer"):
            b.claim(f"group.subsidiary.{child['organisasjonsnummer']}", "group", "subsidiary",
                    {"name": child.get("navn"), "organisation_number": child["organisasjonsnummer"],
                     "relationship": _get(child, "knytningsform", "beskrivelse"), "basis": child.get("grunnlag")}, [e], effective_date=child.get("dato"))


FIN_FIELDS = {
    "revenue": ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
    "operating_expenses": ("resultatregnskapResultat", "driftsresultat", "driftskostnad", "sumDriftskostnad"),
    "operating_result": ("resultatregnskapResultat", "driftsresultat", "driftsresultat"),
    "net_financial_items": ("resultatregnskapResultat", "finansresultat", "nettoFinans"),
    "profit_before_tax": ("resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"),
    "net_result": ("resultatregnskapResultat", "aarsresultat"),
    "total_assets": ("eiendeler", "sumEiendeler"),
    "fixed_assets": ("eiendeler", "anleggsmidler", "sumAnleggsmidler"),
    "current_assets": ("eiendeler", "omloepsmidler", "sumOmloepsmidler"),
    "equity": ("egenkapitalGjeld", "egenkapital", "sumEgenkapital"),
    "total_debt": ("egenkapitalGjeld", "gjeldOversikt", "sumGjeld"),
}


def fetch_accounts(org: str, fx: Fetcher, b: Builder) -> None:
    resp = fx.get(ACCOUNTS.format(org=org), accept=JSON, retries=4)
    if not resp.ok:
        state = resp_failure_state(resp)
        b.state("financials", state, "No normalized annual accounts returned (HTTP 404); not converted to zero" if state == "not_available"
                else f"Regnskapsregisteret unavailable: {resp.error or 'HTTP %s' % resp.status}")
        if state == "failed":
            b.error("financials", f"Regnskapsregisteret: {resp.error or 'HTTP %s' % resp.status}", resp.url)
        return
    e = b.ev(resp, "official_annual_accounts", "brreg_regnskapsregisteret_api_json")
    records = resp.json() if isinstance(resp.json(), list) else []
    for i, record in enumerate(records):
        period = record.get("regnskapsperiode") or {}
        year = (period.get("tilDato") or "")[:4]
        kind = (record.get("regnskapstype") or "SELSKAP").lower()
        meta = {"from": period.get("fraDato"), "to": period.get("tilDato")}
        currency = record.get("valuta")
        for field, path in FIN_FIELDS.items():
            value = _get(record, *path)
            if value is None:
                continue  # never zero-fill
            b.claim(f"financials.{kind}.{field}.{year}", "financials", field, {"amount": value, "currency": currency, "account_type": kind},
                    [e], period=meta, locator=f"[{i}]/" + "/".join(path))
        extra = {k: record.get(v) for k, v in (("audited", "revisjon"), ("small_company_rules", "oppstillingsplan")) if record.get(v) is not None}
        b.claim(f"financials.{kind}.statement.{year}", "financials", "statement",
                {"account_type": kind, "currency": currency, "period_end": period.get("tilDato"), "record_id": record.get("id"), **extra},
                [e], period=meta, locator=f"[{i}]")
    if not records:
        b.state("financials", "not_available", "Regnskapsregisteret returned an empty list", [e])


def fetch_account_years(org: str, fx: Fetcher, b: Builder) -> None:
    for _ in range(5):  # this endpoint answers 503 intermittently; every attempt keeps the 30-per-minute pace
        with _years_lock:
            wait = 2.1 - (time.monotonic() - _years_last[0])
            if wait > 0:
                time.sleep(wait)
            _years_last[0] = time.monotonic()
        resp = fx.get(ACCOUNT_YEARS.format(org=org), accept=JSON)
        if resp.ok or resp.status in {404, 410} or resp.error == "budget_exhausted":
            break
    if not resp.ok:
        b.state("financial_history", resp_failure_state(resp), resp.error or f"HTTP {resp.status}")
        return
    e = b.ev(resp, "official_annual_accounts", "brreg_annual_account_copies_json")
    body = resp.json()
    years = sorted({str(y) for y in body if str(y).isdigit()}) if isinstance(body, list) else []
    if not years:
        b.state("financial_history", "not_available", "No filed annual-account copies listed", [e])
        return
    b.claim("financial_history.filing_years", "financial_history", "filing_years", years, [e])
    for year in years:
        b.claim(f"financial_history.copy.{year}", "financial_history", "annual_account_copy",
                {"year": year, "url": ACCOUNT_PDF.format(org=org, year=year)}, [e], period={"year": year})
    b.claim(f"activity.registry.accounts_filed_{years[-1]}", "activity", "registry_event",
            {"event": f"Annual accounts for {years[-1]} available in Regnskapsregisteret", "date": years[-1], "source": "Regnskapsregisteret"},
            [e], effective_date=years[-1], materiality="minor")
