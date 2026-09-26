"""Batch runner: N organisation numbers in, exactly N terminal envelopes out.

python3 -m signalpost --organisations batch.jsonl --output-dir out/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from . import viewer
from .envelope import AGENT_VERSION, assemble, failed_envelope, research
from .nav import NavJobs
from .net import Fetcher, utc_now
from .profile import STATES


ORG = re.compile(r"(?<!\d)(\d{3})[ . ]?(\d{3})[ . ]?(\d{3})(?!\d)")


def read_inputs(path: Path) -> list[str]:
    """Organisation numbers in file order from .json (list, or object holding a list), .jsonl or any line format
    (.txt/.csv/...). Each row contributes its first 9-digit number; rows without one (headers, blanks) are skipped,
    except that a JSON row with no valid number is kept so that it still gets its own failed envelope."""
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix in {".json", ".jsonl"}:
        body = json.loads(text) if path.suffix == ".json" else [json.loads(line) for line in text.splitlines() if line.strip()]
        if isinstance(body, dict):
            body = next((v for v in body.values() if isinstance(v, list)), [])
        rows = [str(v.get("organisation_number") or v.get("orgnr") or v.get("org") or "") if isinstance(v, dict) else str(v) for v in body]
        return list(dict.fromkeys("".join(m.groups()) if (m := ORG.search(r)) else r for r in rows))
    return list(dict.fromkeys("".join(m.groups()) for line in text.splitlines() if (m := ORG.search(line))))


def load_envelopes(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["organisation_number"]] = row
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Signalpost company research agent")
    ap.add_argument("--organisations", "--input", required=True, type=Path, help=".txt, .json or .jsonl list of organisation numbers")
    ap.add_argument("--output-dir", "--output", default=Path("out"), type=Path)
    ap.add_argument("--previous", type=Path, help="previous envelopes.jsonl to refresh against (default: <output-dir>/envelopes.jsonl if present)")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--max-requests", type=int, default=int(os.environ.get("SIGNALPOST_MAX_REQUESTS", 1900)))
    ap.add_argument("--deadline-minutes", type=float, default=float(os.environ.get("SIGNALPOST_DEADLINE_MINUTES", 38)))
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--resume", action="store_true", help="reuse envelopes already checkpointed for this run id")
    ap.add_argument("--nav-days", type=int, default=35, help="days of Nav's public job feed to index (0 disables)")
    args = ap.parse_args(argv)

    started = time.monotonic()
    deadline = started + args.deadline_minutes * 60
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    orgs = read_inputs(args.organisations)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    input_sha = hashlib.sha256(args.organisations.read_bytes()).hexdigest()

    prev_path = args.previous or (out / "envelopes.jsonl")
    previous = load_envelopes(prev_path) if prev_path.exists() else {}
    prev_dir = prev_path.parent if previous else None
    prev_run = next(iter(previous.values()))["run"]["run_id"] if previous else None
    if previous and prev_path == out / "envelopes.jsonl" and prev_run != run_id:
        keep = out / "history" / prev_run
        keep.mkdir(parents=True, exist_ok=True)
        if not (keep / "envelopes.jsonl").exists():
            (keep / "envelopes.jsonl").write_bytes(prev_path.read_bytes())
    if prev_run == run_id:  # re-running the same run id: compare against what that run started from
        previous, prev_dir = {}, None

    checkpoint = out / f"checkpoint-{run_id}.jsonl"
    done = load_envelopes(checkpoint) if args.resume else {}
    fx = Fetcher(out, max_requests=args.max_requests)
    nav = NavJobs(fx).start() if args.nav_days > 0 else None
    lock = threading.Lock()
    run_started_at = utc_now()

    def run_meta(org_started: str) -> dict:
        return {"run_id": run_id, "agent_version": AGENT_VERSION, "batch_started_at": run_started_at, "started_at": org_started,
                "completed_at": utc_now(), "previous_run_id": prev_run, "input_sha256": input_sha}

    def one(org: str) -> dict:
        org_started = utc_now()
        prev = previous.get(org)
        try:
            if not re.fullmatch(r"\d{9}", org):
                env = failed_envelope(org, run_meta(org_started), f"Invalid organisation number: {org!r}")
            else:
                b = research(org, fx, prev, deadline, nav)
                env = assemble(b, run_meta(org_started), prev, out, prev_dir)
        except Exception as error:  # a crash on one company must never drop its row
            env = failed_envelope(org, run_meta(org_started), f"{type(error).__name__}: {error}", prev)
        with lock, checkpoint.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(env, ensure_ascii=False, separators=(",", ":")) + "\n")
        return env

    pending = [org for org in orgs if org not in done]
    results = dict(done)
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = {pool.submit(one, org): org for org in pending}
    finished, unfinished = wait(futures, timeout=max(0, deadline - time.monotonic()) + 60)
    for future in finished:
        env = future.result()
        results[env["organisation_number"]] = env

    # Second pass: official sources are sometimes briefly unavailable; retry those companies once if time allows.
    failed = lambda env: sum(env["sections"][k]["availability"] == "failed" for k in ("identity", "financials", "financial_history", "leadership", "locations"))
    retry = [org for org in orgs if org in results and failed(results[org])]
    stragglers = bool(unfinished)
    if retry and not unfinished and deadline - time.monotonic() > 240 and fx.remaining() > 15 * len(retry):
        time.sleep(15)
        second = {pool.submit(one, org): org for org in retry}
        done2, not_done2 = wait(second, timeout=max(0, deadline - time.monotonic()))
        stragglers = stragglers or bool(not_done2)
        for future in done2:
            env = future.result()
            if failed(env) < failed(results[env["organisation_number"]]):
                results[env["organisation_number"]] = env
    if stragglers:
        fx.max_requests = 0  # stop the stragglers' network use; their rows are emitted as failed
    for future in unfinished:
        org = futures[future]
        results[org] = failed_envelope(org, run_meta(run_started_at), "Run deadline reached before this company finished", previous.get(org))
    pool.shutdown(wait=False, cancel_futures=True)

    envelopes = [results[org] for org in orgs]
    write_jsonl(out / "envelopes.jsonl", envelopes)
    checkpoint.unlink(missing_ok=True)
    (out / "manifest.txt").write_text("\n".join(orgs) + "\n", encoding="utf-8")
    lat = sorted(fx.latencies)
    counts = {state: sum(e["availability"] == state for e in envelopes) for state in STATES}
    section_counts = {}
    for e in envelopes:
        for key, s in e["sections"].items():
            section_counts.setdefault(key, {}).setdefault(s["availability"], 0)
            section_counts[key][s["availability"]] += 1
    validation = {
        "inputs": len(orgs), "envelopes": len(envelopes),
        "exact_count": len(envelopes) == len(orgs),
        "unique": len({e["organisation_number"] for e in envelopes}) == len(envelopes),
        "all_states_valid": all(e["availability"] in STATES and all(s["availability"] in STATES for s in e["sections"].values()) for e in envelopes),
    }
    validation["passed"] = all(v for k, v in validation.items() if isinstance(v, bool))
    report = {
        "run_id": run_id, "agent_version": AGENT_VERSION, "started_at": run_started_at, "completed_at": utc_now(),
        "runtime_seconds": round(time.monotonic() - started, 1), "previous_run_id": prev_run,
        "operations": {"requests": fx.requests, "request_budget": args.max_requests, "bytes": fx.bytes,
                       "p50_ms": lat[len(lat) // 2] if lat else None, "p95_ms": lat[int(len(lat) * 0.95)] if lat else None,
                       "requests_by_host_top": dict(sorted(fx.by_host.items(), key=lambda kv: -kv[1])[:15]),
                       "third_party_cost_usd": 0.0, "llm_calls": 0,
                       "nav_feed_pages": nav.pages if nav else 0, "nav_feed_error": nav.error if nav else None},
        "availability": counts, "sections": section_counts,
        "changes": sum(len(e["changes"]) for e in envelopes),
        "validation": validation,
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    viewer.write(out / "index.html", envelopes, report)
    print(json.dumps({k: report[k] for k in ("run_id", "runtime_seconds", "availability", "changes", "validation")} | {"requests": fx.requests}, ensure_ascii=False))
    sys.stdout.flush()
    if stragglers:
        os._exit(0 if validation["passed"] else 1)  # do not wait on stuck worker threads
    return 0 if validation["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
