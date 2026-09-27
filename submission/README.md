# Submission v1 artifact

- **Profiles:** 1000 envelopes, 1000 with `terminal_status: completed` (0 failed, still emitted with their failure state).
- **Organisation numbers:** `manifest.txt`, a seeded random sample (seed 20261017) of the public 411,160-company universe.
- **Produced by** commit `657e6e9ddcd09639f51849ef056433809619eea6`, run `submission-v1`, 2026-09-26T23:13:23Z to 2026-09-27T00:49:13Z.
- **Operations:** 12093 requests, third-party cost $0.0, no LLM.
- **Sections available:** identity 1000, financials 935, financial_history 945, leadership 998, locations 998, group 66, website 173, social_profiles 79, contact 389, jobs 21, activity 998.

**Evidence.** Every claim cites evidence records with source URL, retrieval time and the SHA-256 of the saved response.
- **Public viewer:** https://anur4ag.github.io/signalpost/. It includes the saved Brønnøysund open-data (NLOD 2.0) copies.
- **Packaged code:** also includes the post-crawl JSONL reader fix, preserving Unicode separators inside strings during refresh/resume. Crawl output bytes are unchanged.
- **Company web pages:** their raw snapshots are not republished; they are available to Builderr privately on request for verification.
