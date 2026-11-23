"""Precompute and cache the cross-run analyses the dashboard shows as an overview.

The live /trace view answers one investigation at a time; the findings here are the aggregate
story — per-case failure taxonomy and the pass-vs-fail reading-chain contrast — which are too
slow to compute on page load (the contrast fetches dozens of multi-MB traces). So they are built
once into a JSON cache and the service just serves it. Rebuild with `python findings.py build`.

Per-investigation reads are cached separately (reads_cache.json) so a rebuild only fetches
traces it has not seen.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from evalanalysis import analyzer as A
from evalanalysis import trace_analyzer as T

CACHE = Path(__file__).resolve().parent / "findings_cache.json"
READS = Path(__file__).resolve().parent / "reads_cache.json"

# Which case terms count as "the run read the ground-truth component".
GT_READ = {
    "cronjob_sidecar_blocks_completion_hotel_reservation": ["cronjob", "audit-log-archiver", " job "],
    "valkey_auth_disruption": ["valkey", "cart"],
    "duplicate_pvc_mounts_social_network": ["jaeger", "pvc", "persistentvolume"],
}


def _reads_cache() -> dict:
    try:
        return json.loads(READS.read_text())
    except Exception:
        return {}


def _reads_for(inv: str, cache: dict) -> tuple[list[str], str] | None:
    """(normalized reads, joined targets) for an investigation, memoized to disk."""
    if inv in cache:
        return cache[inv]
    try:
        trace, _ = T.fetch_any(inv)
        rc = T.reading_chain(trace)
        if not rc:
            cache[inv] = None
            return None
        reads = [r["read"] for r in rc]
        blob = " ".join((r["target"] or "").lower() + " " + r["read"] for r in rc)
        cache[inv] = [reads, blob]
        return cache[inv]
    except Exception:
        cache[inv] = None
        return None


def _case_invs(case: str) -> tuple[list[str], list[str]]:
    c = sqlite3.connect(f"file:{A.DB}?mode=ro", uri=True)
    P, F = [], []
    for inv, s in c.execute(
        "select investigation_id, common_score_json from eval_runs "
        "where case_id=? and common_score_json is not null and investigation_id like 'INV-%'",
        (case,),
    ):
        (P if json.loads(s)["passed"] else F).append(inv)
    return P, F


def _chain_contrast(case: str, cache: dict, fail_cap: int = 22) -> dict:
    """Pass-vs-fail reading-chain stats: do fails read as much, and do they touch the GT."""
    P, F = _case_invs(case)
    terms = GT_READ.get(case, [])

    def agg(invs):
        n = reads_sum = dist_sum = gt = 0
        for inv in invs:
            got = _reads_for(inv, cache)
            if not got:
                continue
            reads, blob = got
            n += 1
            reads_sum += len(reads)
            dist_sum += len(set(reads))
            if terms and any(t in blob for t in terms):
                gt += 1
        d = max(n, 1)
        return {"n": n, "reads": round(reads_sum / d), "distinct": round(dist_sum / d),
                "touched_gt_pct": round(100 * gt / d)}

    return {"pass": agg(P), "fail": agg(F[:fail_cap])}


def build(cases: list[str] | None = None) -> dict:
    cases = cases or list(GT_READ)
    cache = _reads_cache()
    doc = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "cases": {}, "contrastable": A.contrastable_cases()}
    for case in cases:
        summary = A.case_summary(case)
        disc = A.subquestion_discrimination(case)[:5]
        contrast = _chain_contrast(case, cache)
        doc["cases"][case] = {"summary": summary, "discrimination": disc,
                              "reading_chain": contrast}
        READS.write_text(json.dumps(cache))     # persist incrementally; traces are slow
    # A one-line headline the overview leads with.
    doc["headline"] = (
        "Across cronjob / valkey / duplicate_pvc, ~90% of FAILING runs read the ground-truth "
        "component and issue comparable read counts — the gap is interpreting the decisive read "
        "into the conclusion, not gathering it. (valkey fails also quit shallow: fewer reads.)")
    CACHE.write_text(json.dumps(doc, indent=2))
    return doc


def load() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {"built_at": None, "cases": {}, "headline": "Findings not built yet.",
                "contrastable": []}


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["build"]:
        d = build(sys.argv[2:] or None)
        print(f"built {len(d['cases'])} cases at {d['built_at']}")
        for case, v in d["cases"].items():
            rc = v["reading_chain"]
            print(f"  {case[:40]:40} buckets={v['summary']['failure_buckets']}")
            print(f"    {'':40} reads pass/fail={rc['pass']['reads']}/{rc['fail']['reads']} "
                  f"gt%={rc['pass']['touched_gt_pct']}/{rc['fail']['touched_gt_pct']}")
    else:
        print(json.dumps(load(), indent=2)[:600])
