"""Bring prod0's investigations into the analysis, read-only, over the public API.

Three things are pulled, each incrementally and gently (one request at a time; a live prod0
investigation must never be slowed down):

1. Scored eval runs from prod0's own campaign snapshots (`/api/v1/evals/campaigns/<id>`). They
   carry case_id + judge scores, so they join the ground-truth analysis. Runs already in the local
   eval DB (same correlation_id) are skipped. They land in prod0_evals.sqlite3, which has the same
   `eval_runs` schema and is UNION-ed in by analyzer.connect().
2. An index of every prod0 investigation (all visible projects). Ones no scored run covers become
   "no ground truth" history entries: production alerts and unscored eval runs.
3. investigation_id -> MLflow trace id, per plane, for the deep links.

Investigation ids are per-cluster counters: INV-0000001741 on evals_gke and on prod0 are different
investigations. Everything here is therefore keyed by plane ("prod0" | "evals"), never by bare id.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIRROR = HERE / "prod0_evals.sqlite3"
INDEX = HERE / "prod0_investigations.json"
MLMAP = HERE / "mlflow_map.json"

PROD0 = os.environ.get("CIROOS_PROD0_BASE", "https://prod0.ciroos.ai")
PROD0_ORG = "fa2f0f5e-d35e-42c8-b7c9-018075de2340"
PROJECTS = [int(p) for p in os.environ.get("CIROOS_PROD0_PROJECTS", "1190,1125,1").split(",")]
KEY_FILE = os.path.expanduser(os.environ.get("CIROOS_PROD0_KEY", "~/.config/ciroos/prod0-key"))
EVALS_MLFLOW = os.environ.get("EVAL_MLFLOW", "http://sleuth-service.ciroos.svc.cluster.local:5000")
MLFLOW_PATH = "/ciroos/dev-tools/mlflow"


def _get(path: str, project: int = PROJECTS[0], body: dict | None = None, base: str = PROD0,
         auth: bool = True, timeout: int = 120):
    h = {"Content-Type": "application/json"}
    if auth:
        h["Authorization"] = "Bearer " + open(KEY_FILE).read().strip()
        h["x-project-id"] = str(project)
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body else None,
                                 headers=h)
    for attempt in range(3):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


# ---- 1. scored eval runs from prod0 campaigns ---------------------------------------------------

def _local_db() -> Path:
    from evalanalysis import analyzer as A
    return A.DB


def _score(cs: dict | None) -> str | None:
    """Campaign snapshot score -> the local common_score_json shape the analyzer reads."""
    if not cs or cs.get("value") is None:
        return None
    dims = cs.get("dimensions") or {}
    return json.dumps({
        "d1_localization": dims.get("D1", 0.0), "d2_characterization": dims.get("D2", 0.0),
        "d3_scope_and_impact": dims.get("D3", 0.0), "value": cs.get("value"),
        "passed": bool(cs.get("passed")), "identity": cs.get("identity"),
        "explanation": cs.get("explanation") or "",
        "question_results": [{"question_id": q.get("question_id"), "dimension": q.get("dimension"),
                              "answer": q.get("answer"), "confidence": q.get("confidence"),
                              "evidence": q.get("evidence") or ""} for q in cs.get("questions") or []],
    })


def sync_campaigns() -> dict:
    local = sqlite3.connect(f"file:{_local_db()}?mode=ro", uri=True)
    schema = local.execute("select sql from sqlite_master where name='eval_runs'").fetchone()[0]
    known = {r[0] for r in local.execute("select correlation_id from eval_runs")}
    m = sqlite3.connect(MIRROR)
    m.execute(schema.replace("REFERENCES eval_batches(id)", "").replace("CREATE TABLE eval_runs", "CREATE TABLE IF NOT EXISTS eval_runs"))
    m.execute("create table if not exists run_source (run_id text primary key, campaign_id text, "
              "project_id text, label text, synced_at text)")
    cats = _get("/api/v1/evals/campaigns")
    cats = cats.get("data", cats)
    added = seen = 0
    for camp in cats.get("campaigns") or []:
        snap = _get(f"/api/v1/evals/campaigns/{camp['id']}")
        snap = snap.get("data", snap)
        proj = str(snap.get("batch", {}).get("project_id") or camp.get("project_id"))
        for r in snap.get("runs") or []:
            seen += 1
            if not r.get("investigation_id") or r.get("correlation_id") in known:
                continue
            score = _score(r.get("common_score"))
            meta = {"investigation": {"organization_id": PROD0_ORG, "project_ids": [int(proj)]},
                    "plane": "prod0"}
            m.execute(
                "insert or replace into eval_runs (id, batch_id, dataset, dataset_version, case_id, "
                "pass_number, trial_number, behavior_pattern_id, requested_model, "
                "resolved_behavior_pattern_id, resolved_behavior_pattern_ids_json, resolved_model, "
                "correlation_id, investigation_id, outcome_status, investigation_status, "
                "safe_issue_json, rca_json, investigation_metadata_json, common_score_json, "
                "cleanup_status, created_at, updated_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], camp["id"], r.get("dataset") or "sre-gym", r.get("dataset_version") or "",
                 r["case_id"], r.get("pass_number") or 1, r.get("trial_number") or 1,
                 r.get("behavior_pattern_id") or "auto", r.get("requested_model") or "",
                 r.get("resolved_behavior_pattern_id"),
                 json.dumps(r.get("resolved_behavior_pattern_ids") or []), r.get("resolved_model"),
                 r["correlation_id"], r["investigation_id"], r.get("outcome_status") or "",
                 r.get("investigation_status"), json.dumps(r.get("safe_issue")),
                 json.dumps(r.get("rca")) if r.get("rca") else None, json.dumps(meta), score,
                 r.get("cleanup_status") or "", r.get("created_at") or "", r.get("updated_at") or ""))
            m.execute("insert or replace into run_source values (?,?,?,?,?)",
                      (r["id"], camp["id"], proj, camp.get("label"), time.strftime("%Y-%m-%dT%H:%M:%SZ")))
            known.add(r["correlation_id"])
            added += 1
        time.sleep(0.5)
    m.commit()
    n = m.execute("select count(*), sum(common_score_json is not null) from eval_runs").fetchone()
    return {"campaigns": len(cats.get("campaigns") or []), "runs_seen": seen, "added": added,
            "mirror_runs": n[0], "mirror_scored": n[1]}


# ---- 2. index of every prod0 investigation ---------------------------------------------------

def sync_investigations() -> dict:
    out = {}
    for p in PROJECTS:
        page = 1
        while True:
            d = _get(f"/api/v2/investigations?page={page}&page_size=100", project=p)
            for r in d.get("data") or []:
                md, ut = r.get("metadata") or {}, r.get("user_tags") or {}
                out[r["investigation_id"]] = {
                    "investigation_id": r["investigation_id"], "project_id": p,
                    "summary": r.get("summary"), "status": r.get("status"),
                    "resource_name": r.get("resource_name"),
                    "created_at": r.get("create_timestamp"), "completed_at": r.get("completed_timestamp"),
                    "model": md.get("ciroos_api_model") or ut.get("ci-custom-rca-model"),
                    "orchestrator_model": md.get("ciroos_orchestrator_model"),
                    "eval_correlation_id": md.get("eval_correlation_id"),
                    "bp": ut.get("c-ubp"), "rca_description": str(r.get("rca_description") or "")[:4000],
                }
            if not (d.get("pagination") or {}).get("has_more"):
                break
            page += 1
            time.sleep(0.3)
    INDEX.write_text(json.dumps(out))
    return {"investigations": len(out)}


def load_index() -> dict:
    try:
        return json.loads(INDEX.read_text())
    except Exception:
        return {}


# ---- 3. MLflow trace ids, per plane -------------------------------------------------------------

def _mlflow_scan(plane: str, max_pages: int = 60) -> dict:
    out: dict[str, list] = {}
    base, auth = (PROD0, True) if plane == "prod0" else (EVALS_MLFLOW, False)
    exps = _get(f"{MLFLOW_PATH}/ajax-api/2.0/mlflow/experiments/search", base=base, auth=auth,
                body={"max_results": 100}).get("experiments") or []
    for e in exps:
        token = None
        for _ in range(max_pages):
            body = {"locations": [{"type": "MLFLOW_EXPERIMENT",
                                   "mlflow_experiment": {"experiment_id": e["experiment_id"]}}],
                    "max_results": 500, "order_by": ["timestamp_ms DESC"]}
            if token:
                body["page_token"] = token
            page = _get(f"{MLFLOW_PATH}/ajax-api/3.0/mlflow/traces/search", base=base, auth=auth,
                        body=body)
            for t in page.get("traces") or []:
                inv = (t.get("tags") or {}).get("investigation_id")
                if inv and inv not in out:          # newest first wins
                    out[inv] = [e["experiment_id"], t["trace_id"]]
            token = page.get("next_page_token")
            if not token:
                break
            time.sleep(0.2)
    return out


def sync_mlflow() -> dict:
    res = {}
    for plane in ("prod0", "evals"):
        try:
            res[plane] = _mlflow_scan(plane)
        except Exception as e:
            print(f"mlflow {plane} failed: {e}", file=sys.stderr)
            res[plane] = load_mlflow().get(plane, {})
    MLMAP.write_text(json.dumps(res))
    return {k: len(v) for k, v in res.items()}


def load_mlflow() -> dict:
    try:
        return json.loads(MLMAP.read_text())
    except Exception:
        return {}


def sync_all() -> dict:
    t0 = time.time()
    out = {"campaigns": sync_campaigns(), "investigations": sync_investigations(),
           "mlflow": sync_mlflow()}
    out["seconds"] = round(time.time() - t0)
    return out


if __name__ == "__main__":
    sys.path.insert(0, str(HERE.parent))
    print(json.dumps(sync_all(), indent=1))
