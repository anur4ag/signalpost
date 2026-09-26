"""Local proxy score. NOT the official Builderr score, which uses hidden pooled labels.

- Evidence validity: every available claim cites evidence whose snapshot file exists and matches content_sha256
  (the public signalpost-citation-validator contract).
- Optional recall: pass --reference with Builderr's public 100-company sample (the DATA array embedded in
  https://builderr.ai/signalpost) to measure recall against their crawler's findings for those companies.

python3 eval/score.py <output-dir> [--reference sample100.json]
"""
import argparse
import hashlib
import json
import re
from pathlib import Path


def domain(url: str) -> str:
    host = re.sub(r"^https?://", "", (url or "").lower()).split("/")[0].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def handle(url: str) -> str:
    return re.sub(r"^https?://(www\.|m\.|[a-z]{2}\.(?=linkedin))?", "", (url or "").lower()).rstrip("/").replace("twitter.com", "x.com")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--reference", type=Path)
    args = ap.parse_args()
    envs = [json.loads(line) for line in (args.out / "envelopes.jsonl").read_text().splitlines() if line.strip()]

    cited = valid = 0
    for env in envs:
        evidence = {e["id"]: e for e in env["evidence"]}
        for claim in env["claims"]:
            if claim["availability"] != "available":
                continue
            for eid in claim["evidence_ids"] or [None]:
                cited += 1
                e = evidence.get(eid)
                path = args.out / e["snapshot_path"] if e and e.get("snapshot_path") else None
                ok = bool(e and re.match(r"https?://", e.get("source_url") or "") and e.get("retrieved_at", "").endswith("Z")
                          and path and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == e.get("content_sha256"))
                valid += ok
    with_summary = sum(len((e.get("summary") or {}).get("sentences", [])) >= 3 for e in envs)
    report = {
        "envelopes": len(envs),
        "evidence_citations": cited,
        "evidence_valid_rate": round(valid / cited, 4) if cited else None,
        "summaries_with_3plus_cited_sentences": with_summary,
        "sections_available": {k: sum(e["sections"][k]["availability"] == "available" for e in envs) for k in envs[0]["sections"]},
    }

    if args.reference:
        ref = {r["org"]: r for r in json.loads(args.reference.read_text())}
        ours = {e["organisation_number"]: e for e in envs if e["organisation_number"] in ref}
        fam = {}

        def add(name, company_hit, company_total, claim_hit, claim_total):
            f = fam.setdefault(name, [0, 0, 0, 0])
            f[0] += company_hit; f[1] += company_total; f[2] += claim_hit; f[3] += claim_total

        wrong_sites = []
        for org, r in ref.items():
            e = ours.get(org)
            if not e:
                continue
            claims = {c["id"]: c for c in e["claims"] if not c.get("stale")}
            our_site = domain((claims.get("website.official_website") or {}).get("value", {}).get("url"))
            if r["web"]["status"] == "available":
                ref_site = domain((r["web"].get("value") or {}).get("final_url"))
                add("website", our_site == ref_site, 1, our_site == ref_site, 1)
                if our_site and our_site != ref_site:
                    wrong_sites.append((org, e["name"], our_site, ref_site))
            ref_handles = {handle(h["url"]) for h in r["external"]["handles"] if h.get("rightsStatus") == "approved"}
            if ref_handles:
                our_handles = {handle(c["value"]["url"]) for c in claims.values() if c["section"] == "social_profiles"}
                add("social_profiles", bool(ref_handles & our_handles), 1, len(ref_handles & our_handles), len(ref_handles))
            if r["financial"]["status"] == "available":
                add("financials", any(c["section"] == "financials" for c in claims.values()), 1, 1, 1)
            nm = lambda n: (" ".join(n) if isinstance(n, list) else n or "").casefold()
            ref_roles = {(i["role_code"], nm(i["name"])) for i in r["roles"].get("items") or [] if not i.get("inactive")}
            if ref_roles:
                our_roles = {(c["value"]["role_code"], c["value"]["name"].casefold()) for c in claims.values() if c["section"] == "leadership"}
                add("leadership", bool(ref_roles & our_roles), 1, len(ref_roles & our_roles), len(ref_roles))
            ref_locs = {i["organisation_number"] for i in r["locations"].get("items") or []}
            if ref_locs:
                our_locs = {c["value"].get("organisation_number") for c in claims.values() if c["field"] == "registered_workplace"}
                add("locations", bool(ref_locs & our_locs), 1, len(ref_locs & our_locs), len(ref_locs))
        per = {k: round(0.7 * v[0] / v[1] + 0.3 * v[2] / v[3], 4) for k, v in fam.items()}
        report["reference_companies_matched"] = len(ours)
        report["recall_vs_builderr_sample"] = per
        report["recall_detail"] = {k: {"companies": f"{v[0]}/{v[1]}", "claims": f"{v[2]}/{v[3]}"} for k, v in fam.items()}
        report["website_disagreements"] = wrong_sites
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
