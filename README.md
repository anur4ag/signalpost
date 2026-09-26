# Signalpost agent

Give it Norwegian organisation numbers. It returns one evidence-backed company profile per number, and each fact links to the saved source bytes it came from. Run it again and it reports what changed while keeping the earlier evidence.

Built for the [Builderr Signalpost challenge](https://builderr.ai/challenges/signalpost).
- Python standard library only
- no API keys
- no LLM
- $0 third-party cost

## Run it (one command)

```bash
python3 -m signalpost --organisations <input-file> --output-dir <output-dir>
```

**Clean-machine check.** This is the same check the evaluator runs. There is no install step, because there are no dependencies.

```bash
git clone https://github.com/anur4ag/signalpost && cd signalpost && git checkout <commit>
python3 -m venv .venv && . .venv/bin/activate
python3 -m signalpost --organisations examples/batch-10.txt --output-dir out
```

**Requirements.** Python ≥ 3.10 and outbound HTTPS.

**Input.** The file lists organisation numbers, in any of these forms:
- `.txt` or `.csv`: one per line; the first 9-digit number on each line is used, and header or blank lines are skipped
- `.jsonl`: `{"organisation_number": "..."}` or `{"orgnr": ...}` per line
- `.json`: a list, or an object holding a list, such as `{"organisation_numbers": [...]}`

Spaces inside numbers are ignored and order is preserved. A JSON row without a valid number still gets its own `failed` envelope, so no row is ever dropped.

**Output.** Written to `<output-dir>`:

| File | Content |
|---|---|
| `envelopes.jsonl` | Exactly one terminal envelope per input, in input order |
| `snapshots/<sha256>.<ext>` | Raw bytes of every source response. Each evidence record's `content_sha256` is the hash of its snapshot file. |
| `report.json` | Runtime, request count, p50/p95 latency, bytes, cost (0), availability counts, validation (`exact_count`, `unique`, `all_states_valid`) |
| `index.html` | Offline viewer that works on desktop and mobile: search, filter, side-by-side compare, and per-claim source, date and saved copy |
| `manifest.txt` | The organisation numbers of this run |
| `history/<run_id>/envelopes.jsonl` | The previous run's envelopes, kept when a refresh replaces them |

The exit code is 0 when validation passes. Resume an interrupted run with `--run-id <id> --resume`.

## Refresh

Any of these runs a refresh. Earlier ones take precedence.
- **Explicit baseline:** `--previous <path/to/envelopes.jsonl>` compares against any earlier output. Its snapshots are copied over when needed.
- **Same folder:** run the same command again with the same `--output-dir`. The previous `envelopes.jsonl` becomes the baseline and is archived under `history/`.
- **State store (automatic):** every run also saves each company's latest envelope, and the snapshots it cites, to `.signalpost-state/` in the working directory (`--state-dir`, or `SIGNALPOST_STATE_DIR`; pass `''` to disable). A later run on the same checkout uses that as the baseline, even with a fresh output folder.

Every claim has a stable id, such as `leadership.DAGL.<name>`, `financials.selskap.revenue.2025` or `jobs.posting.<urlhash>`. A refresh compares claim values by id:
- **Changes.** `changes` lists this run's differences as `added`, `removed` or `changed`. Each carries a typed event (`new_role`, `role_ended`, `new_filing`, `new_job_posting`, `job_posting_closed`, `website_found`, …), materiality, old and new values, and evidence ids for both sides.
- **Change log.** `change_log` accumulates every change across runs, deduplicated by a deterministic `change_id`, so re-running the same sources adds nothing.
- **History.** `history` keeps every superseded or removed claim, and its evidence stays in `evidence` marked `"superseded": true`.
- **Complete read of the website:** every website candidate must have been read or be definitively gone (no DNS record, or HTTP 404/410), and every verification page must have loaded. A 403, a robots block, a crawl-delay skip, a network error, an untried candidate or a failed contact page all leave the read incomplete.
- **Windows and caps are never complete lists.** RSS/Atom feeds and homepage news show only recent items, and job, news, contact, careers-page and Nav lists have caps. A read is complete only when nothing was cut: a single uncapped sitemap with at most 10 news items, all postings listed, one careers page.
- **Identity is decided on every page anything is published from.** A contact page fetched after the homepage matched is re-verified together with it.
- **Each source reports `ok`, `partial` or `failed`.**
  - `ok`: a complete read, so a missing fact is removed.
  - `partial`: read, but a window or capped list, so new facts count as `added` while missing ones are kept as stale.
  - `failed`: nothing is concluded.

  News items record the sub-source they came from: homepage JSON-LD, feed or sitemap. A feed or homepage window that shrinks, even to empty, never removes an item. Neither does a fallback lookup in a different source.
- **Removals need a complete re-read of the same source.** Each claim maps to the one fetch that produced it (`sources` in the envelope):
  - Brreg: entity, roles, subunits, group, accounts, filing years;
  - company site: homepage, careers page, feed/sitemap;
  - Nav.

  A claim that is missing counts as `removed` only if that fetch completed in this run. For example, a 503 on the workplace list never closes a workplace, even when the entity record succeeds. A careers page that failed to load never closes a job, even when the homepage loads. A truncated list never removes its tail. Such claims are carried forward with `stale: true`, and the section says so.
- **Additions need a complete earlier read.** A fact is reported as `added` only if its source was fully read last time. Otherwise it is a first observation.
- **Failed refresh.** If a company's refresh fails entirely, every earlier claim, history entry and change is carried forward with its evidence. Nothing is erased.
- **Evidence is never dropped.** Every evidence record referenced by a current or stale claim, `history` or `change_log` stays in `evidence`, with its saved copy, through any number of refreshes and output folders.
- **What never counts as a change:** retrieval timestamps, and provenance details such as the identity proof list.

## Envelope

```jsonc
{
  "organisation_number": "810034882",
  "availability": "available",            // available | not_available | blocked | not_applicable | ambiguous | failed
  "terminal_status": "completed",
  "run": {"run_id": "...", "agent_version": "...", "started_at": "...", "completed_at": "...", "previous_run_id": "..."},
  "name": "SANDNES ELEKTRISKE AS",
  "sources": {"brreg_entity": "ok", "brreg_subunits": "failed", ...},   // which fetches completed in this run
  "sections": {                           // one explicit state per information family
    "identity": {"availability": "available", "claim_ids": [...]},
    "financials": ..., "financial_history": ..., "leadership": ..., "locations": ..., "group": ...,
    "website": ..., "social_profiles": ..., "contact": ..., "jobs": ..., "activity": ...
  },
  "claims": [{"id": "financials.selskap.revenue.2025", "section": "financials", "field": "revenue",
              "value": {"amount": 10012345.0, "currency": "NOK", "account_type": "selskap"},
              "availability": "available", "evidence_ids": ["ev-..."],
              "reporting_period": {"from": "2025-01-01", "to": "2025-12-31"},
              "locator": "[0]/resultatregnskapResultat/driftsresultat/driftsinntekter/sumDriftsinntekter",
              "first_observed_at": "...", "last_verified_at": "..."}],
  "evidence": [{"id": "ev-...", "source_url": "...", "requested_url": "...", "redirects": [], "source_class": "official_annual_accounts",
                "retrieved_at": "...", "http_status": 200, "content_sha256": "...", "snapshot_path": "snapshots/<sha256>.json",
                "extraction_method": "brreg_regnskapsregisteret_api_json"}],
  "summary": {"text": "...", "sentences": [{"text": "...", "claim_ids": [...]}], "changes": "...", "unknowns": [...]},
  "changes": [...], "change_log": [...], "history": [...],
  "website_candidates": [...],           // rejected or ambiguous website candidates, with the reason (audit trail)
  "errors": [...]
}
```

**States.**
- A `not_available` section means the source was checked and has nothing: a 404, no roles, or no site found.
- `not_applicable` is used, for example, for the group section when the register says the company is not part of a group.
- Missing numbers are omitted, never written as zero.

**Summary.** Every sentence is built only from claims and cites their ids. It covers:
- what the company is and does
- its latest accounts, leadership, workplaces and group
- its website, profiles, hiring and latest dated activity
- what changed since the last run
- what is not established

## Sources and rights

| Source | Used for | Access |
|---|---|---|
| Brønnøysundregistrene, Enhetsregisteret API (`data.brreg.no`) | Identity, status, industry, addresses, employees, capital, purpose, former names, contacts, roles, subunits, group structure | Open data, [NLOD 2.0](https://data.norge.no/nlod/en/2.0). Birth dates in role data are discarded. |
| Regnskapsregisteret API (`data.brreg.no/regnskapsregisteret`) | Latest filed annual accounts (reporting period, currency), filing years, official copy links | Open data. The copy endpoint is paced at ≤ 30 requests/min. |
| The company's own website | Verified official website, self-description, public brand, contact details, social/video profile links, careers page and job postings, dated news (RSS, sitemap or JSON-LD) | robots.txt obeyed with an RFC 9309 matcher:
- our product-token group, else `*`;
- `*` and `$` operators, longest match, Allow wins ties;
- §2.2.2 octet normalisation: `%2A`/`%24` are literal, unreserved escapes decode, non-ASCII is encoded, and a query must be allowed both literally and with reserved characters encoded.

A 5xx or unreachable robots.txt means no crawl. Honest user agent `SignalpostResearchAgent/1.0`. 1 request/s per host, or the site's crawl-delay if longer. A site asking for a delay over 10 s is skipped, never fetched faster than it asks. At most about 8 pages per site. |
| Nav's public job-vacancy feed ([pam-stilling-feed](https://navikt.github.io/pam-stilling-feed/)) | Active job ads. **Off by default:** an opt-in local experiment via `--nav-days N`, not used in the submission or any public artifact. | Nav's token for this is documented for experiments. Its [terms](https://arbeidsplassen.nav.no/vilkar-api) require republished ads to be updated or removed promptly, which a static public archive cannot do. When enabled, a candidate ad is published only if its employer organisation number is the company or one of its registered workplaces, and ad contact persons are never stored. |

**Personal data.** Snapshots are saved with two things removed:
- birth dates, from the Brreg roles response;
- contact persons, from Nav ads.

These evidence records say `snapshot_redacted`, and their `content_sha256` covers the saved, redacted copy.

**Evidence publication.**
- **Where snapshots live.** A run writes every snapshot into its own output folder, so an evaluator run has its full evidence locally.
- **The public viewer** links saved copies only for Brønnøysund open data (NLOD 2.0, birth dates removed). Every other claim shows its source URL, retrieval time and SHA-256.
- **Company web pages are kept private.** Their raw snapshots are never republished; we crawled them but have no right to redistribute them. For the submitted profiles they are available to Builderr privately on request.

**What it never does:**
- query or scrape a search engine, LinkedIn, Facebook, Instagram, Glassdoor, Indeed or any other platform;
- log in;
- use proxies or evade blocking.

Social and video profiles, contact details and the public brand are taken only from what the verified site presents as its own:
- header, navigation, footer and social-icon links;
- its contact/about pages;
- its own top-level JSON-LD organisation.

Links in body copy (customer or partner lists) and organisations nested in structured-data lists are ignored. A JSON-LD JobPosting counts only if its hiring organisation is this company, and news items must be on the company's own host.

`signalpost/shared_domains.txt` is a declared cache derived from the Brreg bulk register (NLOD 2.0). It lists the e-mail and web domains that 3 or more registered entities share, such as housing-co-op managers, franchise chains and generic mail providers. Such a domain is never treated as proof of a single company.

## Identity: when a website is published

A website is published only when the site itself proves identity. Candidates are tried in this order:
1. the website in the register;
2. the domain of the e-mail address in the register, unless that domain is shared;
3. the legal name as a domain, checked for DNS first:
   - `<name>.no`, `<name-with-hyphens>.no` and `<name>.com`, with initials merged ("P.E. GAARUD" becomes `pe-gaarud.no`);
   - the first word alone as `<word>.no`, unless it is a municipality name.

Candidates never come from search engines.

**Proof must come from where the site names its operator.** The name has to appear in one of two places:
- the site's self-identification: `<title>`, `og:site_name`, the copyright holder, or its own top-level JSON-LD Organization, or the WebSite publisher on the same host;
- the footer or a linked contact/about page, but only beside a registered phone, e-mail, address, role holder or org number.

**These never count:**
- developer and partner credits ("Nettside levert av …", "Website by …", partners, customers);
- the text of links to other domains;
- organisations nested in structured-data lists;
- body copy: a customer list, a news story or a partner page that mentions the company proves nothing.

**Proofs:**
- the organisation number (mod-11 validated) in that operator text (never in body copy, even when labelled, because customer lists label their customers);
- the registered phone, mobile, e-mail or street address;
- a registered role holder's full name, such as the managing director or a board member;
- the legal name, or the exact registered name including its form, such as "LIME FRISØR AS".

**Contradiction:** if the operator text carries a different labelled organisation number and not ours, the site is someone else's and we abstain.

**What counts as enough:**
- **Our org number:** exact, whichever route found the site.
- **Registry website:** any proof above.
- **Registry e-mail domain:** the name counts only if the domain spells it; otherwise a phone, address, org-number or role-holder proof is required. The site containing that same e-mail address does not count.
- **Name-derived domain:** the name plus one more proof. The exact registered name with its form counts as that extra proof on a `.no` domain, because Norwegian registered names are unique.

**Related, shared or unproven sites:**
- **Related-entity site:** the operator is another organisation. If the register lists that site as our website, only the URL is published, with a relationship label. Its profiles, jobs and news are not attributed to us. Otherwise nothing is published.
- **Shared-domain site** (3+ registered entities use it): the same label-only treatment applies, unless this company owns the domain. Ownership means its org number or exact registered name is in the operator text, or the domain spells its name.
- **Anything else:** `ambiguous`, which is not published, and the candidate stays in `website_candidates`.

Missing a website is preferred over publishing the wrong company's.

## Safe URL handling and budgets

- **Allowed URLs:** only `http`/`https` on ports 80 and 443, with no embedded credentials, and only hosts whose every resolved address is publicly routable. This prevents SSRF to private, loopback or link-local ranges.
- **Redirects:** followed manually (up to 4) and re-checked at every hop.
- **Response size:** capped at 4 MB.
- **Request budget:** a hard default of 1,900 per run (`--max-requests` or `SIGNALPOST_MAX_REQUESTS`). It is below the evaluator's 2,000, and every redirect hop, retry and robots.txt fetch counts. A typical 100-company batch uses about 750–1,100 requests, more when Regnskapsregisteret answers 503s that need retries.
- **Deadline:** a soft deadline of 38 minutes (`--deadline-minutes`). Any company not finished by then is still emitted, as `failed`.
- **Runtime:** a typical 100-company batch takes about 5–12 minutes on 12 worker threads. Companies whose official sources failed are retried in a second pass if time allows.
- **Cost:** third-party API cost $0. No model or LLM is used, and no secrets are needed.

## Tests

```bash
python3 tests/test_agent.py
```

This runs offline checks covering:
- org-number validation
- identity from operator text only, including the customer-list and contradicting-org-number cases
- RFC 9309 robots matching and crawl delays
- refresh idempotence and typed changes
- per-source removals, including a failed subunit list, careers page or Nav read
- evidence retained across three refreshes in different folders plus a failed refresh
- redaction
- input formats

## Limitations

- **JavaScript-only sites:** content rendered only by JavaScript is not seen, so a site like that usually ends up `ambiguous`.
- **Websites with no lead:** they are not found unless the register links them or the company's name is its domain.
- **Job postings:** limited to what the company's own verified site, or the recruiting page it links, lists.
- **Financials:** the latest normalized filing from Regnskapsregisteret. Older years are listed as official copy links and not parsed.
- **News items:** from sitemaps, the title is derived from the URL slug (`title_basis: url_slug`) and the date is the sitemap `lastmod`.

## Licence

MIT. Registry data is © Brønnøysundregistrene and used under NLOD 2.0.
