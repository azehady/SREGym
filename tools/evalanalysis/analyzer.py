"""Compare RCA eval runs to find WHY a run missed the root cause, mechanically.

The eval DB stores, per run, the final RCA summary, the two judge verdicts (gpt-4.1 common
score + the native checklist), and an investigation_id. That is enough for a two-layer evidence
funnel — did the final summary name the ground-truth component, and did the judge confirm it —
over all ~500 scored runs without touching the agentops trace store. The upper funnel layers
(tool outputs, evidence ledger, hypotheses) live in agentops ClickHouse keyed by
investigation_id, and are pulled on demand for a single run rather than for the whole corpus.

Nothing here interprets prose freely: a run's failure is placed in a bucket by a mechanical
test, so the same run always lands in the same bucket and two runs are compared field by field.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path

DB = Path(
    __import__("os").environ.get(
        "CIROOS_EVAL_DB", "/workspace/home/ciroos/brahma/oracle/.artifacts/evals.sqlite3"
    )
)

# Ground-truth component per case: the fault origin the judge rubric scores against, plus the
# terms a correct diagnosis uses for it. Seeded from the judge's own D1 evidence on passing
# runs (which quotes the rubric), so it is the rubric's language, not ours. `distractors` are
# the wrong-but-plausible components failing runs blame instead — a named distractor in the
# summary is bucket E, capture.
GROUND_TRUTH: dict[str, dict] = {
    "cronjob_sidecar_blocks_completion_hotel_reservation": {
        "component": "cronjob/audit-log-archiver",
        "terms": ["cronjob", "sidecar", "audit-log-archiver", "jobtemplate", "restartpolicy"],
        "distractors": ["telemetry", "timeout", "request latency", "missing metric"],
    },
    "duplicate_pvc_mounts_social_network": {
        "component": "deployment/jaeger",
        "terms": ["jaeger", "duplicate pvc", "persistentvolumeclaim", "volume", "multi-attach"],
        "distractors": ["write-home-timeline", "compose-post-redis", "missing service", "rabbitmq"],
    },
    "valkey_auth_disruption": {
        "component": "deployment/cart (valkey auth)",
        "terms": ["valkey", "cart", "auth", "password", "requirepass", "noauth"],
        "distractors": ["network policy", "dns", "image"],
    },
    "finalizer_deadlock_controller_hotel_reservation": {
        "component": "cleanup-controller / reservation-cleanup-token",
        "terms": ["finalizer", "cleanup-controller", "reservation-cleanup-token", "clusterrole", "rbac"],
        "distractors": ["deadlock", "timeout", "resource limit"],
    },
}

# Judge sub-questions whose answer most separates pass from fail are discovered, not assumed;
# this is only the fallback label when a case has no curated GT yet.
_WORD = re.compile(r"[a-z0-9][a-z0-9.-]*")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _summary_text(rca_json: str | None) -> str:
    if not rca_json:
        return ""
    d = json.loads(rca_json)
    return ((d.get("rca_description") or "") + " " + (d.get("summary") or "")).lower()


def _names_any(text: str, terms: list[str]) -> list[str]:
    return [t for t in terms if t.lower() in text]


@dataclass
class RunView:
    """One run reduced to the fields every comparison needs."""

    run_id: str
    case_id: str
    model: str | None
    bp_id: str | None
    bp_reselected: bool
    batch_id: str
    created_at: str
    passed: bool
    d1: float
    d2: float
    d3: float
    composite: float
    investigation_id: str | None
    summary_names_gt: bool | None          # did the final summary name the GT component
    gt_terms_hit: list[str] = field(default_factory=list)
    distractors_hit: list[str] = field(default_factory=list)
    failing_subquestions: list[dict] = field(default_factory=list)
    bucket: str = ""                        # the failure bucket, "" when passed


def _bucket(v: RunView, gt: dict | None) -> str:
    """Mechanical failure classification. Ordered so the cheapest-to-fix cause wins a tie.

    Only the layers this DB carries are decided here (summary + judge); A/B/C — whether the
    evidence was gathered but drowned vs never gathered — need the agentops trace and are
    resolved by funnel_trace() on demand, which refines NOT_NAMED into A or B.
    """
    if v.passed:
        return ""
    if gt is None:
        return "UNCLASSIFIED_NO_GT"
    if v.distractors_hit and not v.summary_names_gt:
        return "E_DISTRACTOR_CAPTURE"       # blamed a known wrong component
    if not v.summary_names_gt:
        return "NOT_NAMED"                   # GT never reached the summary (→ A or B via trace)
    return "CD_NAMED_BUT_FAILED"             # named it, still scored fail → ranking or reporter


def load_runs(case_id: str | None = None, model: str | None = None) -> list[RunView]:
    """Every scored run, reduced to RunViews. Optional case / model filter."""
    q = ("select id, case_id, resolved_model, behavior_pattern_id, "
         "resolved_behavior_pattern_ids_json, batch_id, created_at, common_score_json, "
         "rca_json, investigation_id from eval_runs where common_score_json is not null")
    args: list = []
    if case_id:
        q += " and case_id = ?"; args.append(case_id)
    out: list[RunView] = []
    for r in _conn().execute(q, args):
        score = json.loads(r["common_score_json"])
        model_r = r["resolved_model"]
        if model and (model_r or "") != model:
            continue
        gt = GROUND_TRUTH.get(r["case_id"])
        text = _summary_text(r["rca_json"])
        names = _names_any(text, gt["terms"]) if gt else []
        distr = _names_any(text, gt["distractors"]) if gt else []
        reselected = False
        if r["resolved_behavior_pattern_ids_json"]:
            try:
                reselected = len(json.loads(r["resolved_behavior_pattern_ids_json"])) > 1
            except Exception:
                pass
        v = RunView(
            run_id=str(r["id"]), case_id=r["case_id"], model=model_r,
            bp_id=r["behavior_pattern_id"], bp_reselected=reselected,
            batch_id=str(r["batch_id"]), created_at=r["created_at"] or "",
            passed=bool(score.get("passed")),
            d1=score.get("d1_localization", 0.0), d2=score.get("d2_characterization", 0.0),
            d3=score.get("d3_scope_and_impact", 0.0), composite=score.get("value", 0.0),
            investigation_id=r["investigation_id"],
            summary_names_gt=(bool(names) if gt else None),
            gt_terms_hit=names, distractors_hit=distr,
            failing_subquestions=[{"id": q_["question_id"], "dim": q_["dimension"],
                                   "evidence": q_["evidence"]}
                                  for q_ in score.get("question_results", [])
                                  if not q_["answer"]],
        )
        v.bucket = _bucket(v, gt)
        out.append(v)
    return out


def case_summary(case_id: str) -> dict:
    """Pass/fail split, per-model breakdown, and failure-bucket distribution for one case."""
    runs = load_runs(case_id)
    fails = [r for r in runs if not r.passed]
    by_model: dict[str, dict] = {}
    for r in runs:
        m = (r.model or "unknown").split("/")[-1]
        g = by_model.setdefault(m, {"n": 0, "pass": 0, "named_gt": 0})
        g["n"] += 1
        g["pass"] += r.passed
        g["named_gt"] += bool(r.summary_names_gt)
    buckets: dict[str, int] = {}
    for r in fails:
        buckets[r.bucket] = buckets.get(r.bucket, 0) + 1
    return {
        "case_id": case_id,
        "has_ground_truth": case_id in GROUND_TRUTH,
        "n": len(runs), "passed": sum(r.passed for r in runs), "failed": len(fails),
        "by_model": by_model,
        "failure_buckets": dict(sorted(buckets.items(), key=lambda kv: -kv[1])),
    }


def subquestion_discrimination(case_id: str) -> list[dict]:
    """For each judge sub-question, the pass-rate gap between passing and failing runs.

    A big gap means that sub-question IS the case's failure: the specific fact the misses miss.
    """
    runs = load_runs(case_id)
    P = [r for r in runs if r.passed]
    F = [r for r in runs if not r.passed]
    if not P or not F:
        return []
    import collections
    yes_p, yes_f, dim = collections.Counter(), collections.Counter(), {}
    for grp, acc in ((P, yes_p), (F, yes_f)):
        for r in grp:
            # failing_subquestions holds the NOs; recover YESes from the raw score
            pass
    # Recompute from raw, since RunView only kept the NOs.
    for r in _conn().execute(
        "select common_score_json from eval_runs where case_id=? and common_score_json is not null",
        (case_id,),
    ):
        d = json.loads(r["common_score_json"])
        acc = yes_p if d.get("passed") else yes_f
        for q in d.get("question_results") or []:
            dim[q["question_id"]] = q["dimension"]
            if q["answer"]:
                acc[q["question_id"]] += 1
    out = []
    for qid, dm in dim.items():
        pp = 100 * yes_p[qid] / len(P)
        fp = 100 * yes_f[qid] / len(F)
        out.append({"question_id": qid, "dimension": dm, "pass_pct": round(pp),
                    "fail_pct": round(fp), "gap": round(pp - fp)})
    return sorted(out, key=lambda x: -x["gap"])


def contrastable_cases() -> list[dict]:
    """Cases with both passing and failing runs — where within-case comparison is possible."""
    import collections
    agg = collections.defaultdict(lambda: {"P": 0, "F": 0})
    for r in _conn().execute(
        "select case_id, common_score_json from eval_runs where common_score_json is not null"
    ):
        agg[r["case_id"]]["P" if json.loads(r["common_score_json"]).get("passed") else "F"] += 1
    out = [{"case_id": c, "pass": v["P"], "fail": v["F"], "has_ground_truth": c in GROUND_TRUTH}
           for c, v in agg.items() if v["P"] and v["F"]]
    return sorted(out, key=lambda x: -(x["pass"] + x["fail"]))


def compare(run_a: str, run_b: str) -> dict:
    """Two runs side by side: scores, who named the GT, and each one's failing sub-questions."""
    idx = {r.run_id: r for r in load_runs()}
    a, b = idx.get(run_a), idx.get(run_b)
    if not a or not b:
        return {"error": f"unknown run(s): {[r for r in (run_a, run_b) if r not in idx]}"}
    return {"a": asdict(a), "b": asdict(b),
            "same_case": a.case_id == b.case_id,
            "named_gt_delta": f"{a.summary_names_gt} vs {b.summary_names_gt}"}


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "weakness"
    if cmd == "weakness":
        for case in GROUND_TRUTH:
            s = case_summary(case)
            print(f"\n=== {case}  ({s['passed']}/{s['n']} pass) ===")
            print("  buckets:", s["failure_buckets"])
            disc = subquestion_discrimination(case)[:3]
            print("  top discriminating sub-Qs:",
                  [f"{d['question_id']}(+{d['gap']})" for d in disc])
    elif cmd == "cases":
        for c in contrastable_cases():
            print(f"  {c['case_id'][:52]:52} {c['pass']:3}P {c['fail']:3}F  gt={c['has_ground_truth']}")
