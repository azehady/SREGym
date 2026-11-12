#!/usr/bin/env python3
"""Loopback run service for the fault simulator: the Run-evaluation tab's missing backend.

Implements the four endpoints observer's EvalRunControls needs (/evals/targets, its resolve
probe, /evals/runs and the per-run get/cancel) and actually executes a run: it renders a
generated variant's four-script bundle, injects it, validates that a symptom really appeared,
posts an investigation to the deployed sleuth, then always recovers.

Loopback only, and proxied by the fault-sim dev server — it drives kubectl against a live
namespace, so it is never tailnet-exposed.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.environ.get("CIROOS_SIM_REPO", "/workspace/home/ciroos/SREGym"))
from sregym.ciroos_sim import registry, runner  # noqa: E402

PORT = int(os.environ.get("FAULTSIM_RUNS_PORT", "8095"))
HOME = pathlib.Path(os.environ.get("CIROOS_SIM_HOME", pathlib.Path.home() / ".ciroos-sim"))
CATALOGUE = HOME / "catalogue.json"
RUNS_DB = HOME / "runs.json"
BUNDLES = HOME / "bundles"

CONTEXT = os.environ.get("CIROOS_SIM_CONTEXT", "evals_gke")
ORG_ID = os.environ.get("CIROOS_SIM_ORG", "b589c349-b2be-459e-8bc6-43f2e045392e")
USER_ID = os.environ.get("CIROOS_SIM_USER", "azehady@ciroos.ai")
CLUSTER_NAME = os.environ.get("CIROOS_SIM_CLUSTER", "evals_gke")
BENCHMARK = "ciroos-sim"
POLL_TIMEOUT = int(os.environ.get("CIROOS_SIM_INV_TIMEOUT", "1500"))

# This pod is the executor; a stable UUID5 names it without inventing a VM registration.
SERVER_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://ciroos.ai/eval-workspace/faultsim-runs"))
SERVER_NAME = "eval-workspace (in-pod fault simulator)"

TERMINAL = {"COMPLETED", "COMPLETE", "ERRORED", "ERROR", "CANCELLED", "FAILED", "CLOSED"}
LOCK = threading.Lock()
RUNS: dict[str, dict] = {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clusterip(service: str, namespace: str = "ciroos") -> str:
    out = subprocess.run(["kubectl", f"--context={CONTEXT}", "-n", namespace, "get",
                          "svc", service, "-o", "jsonpath={.spec.clusterIP}"],
                         text=True, capture_output=True, timeout=30)
    return (out.stdout or "").strip()


def cluster_targets() -> list[dict]:
    """Ask megha which clusters are registered, so the run investigates a real one."""
    try:
        req = urllib.request.Request(
            f"http://{clusterip('megha')}/internal/api/v1/kubernetes/clusters",
            headers={"x-organization-id": ORG_ID, "x-user-id": USER_ID, "x-project-id": "2"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        return [{"resource_type": "kubernetes", "resource_id": c["id"], "resource_name": c["name"]}
                for c in data["data"]["clusters"] if c.get("status") == "active"]
    except (urllib.error.URLError, OSError, KeyError, ValueError, subprocess.SubprocessError):
        return []


def replicas(workload: str, namespace: str) -> int:
    out = subprocess.run(["kubectl", f"--context={CONTEXT}", "-n", namespace, "get", workload,
                          "-o", "jsonpath={.spec.replicas}"], text=True, capture_output=True,
                         timeout=30)
    return int((out.stdout or "0").strip() or 0)


def scale(workload: str, namespace: str, count: int) -> None:
    subprocess.run(["kubectl", f"--context={CONTEXT}", "-n", namespace, "scale", workload,
                    f"--replicas={count}"], capture_output=True, timeout=60)


def catalogue() -> dict[str, dict]:
    if not CATALOGUE.exists():
        return {}
    doc = json.loads(CATALOGUE.read_text())
    lib = doc.get("lib") or ""
    return {v["id"]: dict(v, _lib=lib) for v in doc.get("variants", [])}


def cases_for(suite: str) -> list[tuple[str, bool]]:
    """(variant id, score it?) for each case this suite runs."""
    known = catalogue()
    if suite == "generated":
        # The sweep that PRODUCES stamps: inject and validate only, nothing to score yet.
        return [(vid, False) for vid in sorted(known)]
    valid = registry.runnable(list(known.values()))
    return [(vid, True) for vid in (valid[:1] if suite == "smoke" else valid)]


def cases_from_ids(ids: list[str]) -> list[tuple[str, bool]]:
    """Hand-picked variants. An unvalidated one still runs, but unscored: a mutation with no
    proven symptom would score the investigation against a fault that may never have appeared."""
    known = catalogue()
    return [(vid, registry.is_runnable(registry.stamp_for(known[vid]))) for vid in ids]


def load_runs() -> None:
    if RUNS_DB.exists():
        try:
            RUNS.update(json.loads(RUNS_DB.read_text()))
        except (OSError, ValueError):
            pass
    # A run cannot survive a restart of this process; nothing is left driving it.
    for run in RUNS.values():
        if run["state"] in ("running", "cancelling"):
            run.update(state="interrupted", phase="failed", can_start=True,
                       error={"error_type": "service_restart",
                              "message": "The run service restarted while this run was active."})


def save_runs() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = RUNS_DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(RUNS, indent=2))
    tmp.replace(RUNS_DB)


def touch(run: dict, **fields) -> None:
    with LOCK:
        run.update(fields)
        run["last_update_at"] = now()
        save_runs()


def post_investigation(spec: dict, log, on_created=None) -> dict:
    """Hand the live fault to the deployed sleuth and wait for its verdict."""
    ix = f"http://{clusterip('ixapi-service')}"
    headers = {"Content-Type": "application/json", "X-Organization-ID": ORG_ID,
               "X-Project-ID": str(spec["project_id"]), "x-user-id": USER_ID}
    workload = spec["workload"].split("/")[-1]
    body = {
        "summary": f"{workload} in {spec['namespace']} is degraded; service impact reported.",
        "description": f"Alert on {spec['workload']} in namespace {spec['namespace']}. "
                       "Investigate the root cause from the observed symptoms.",
        "severity": "HIGH",
        "resource_type": spec["investigation_target"]["resource_type"],
        "resource_id": spec["investigation_target"]["resource_id"],
        "resource_name": spec["investigation_target"].get("resource_name"),
    }
    req = urllib.request.Request(f"{ix}/api/v2/investigations", headers=headers,
                                 data=json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=40) as resp:
        created = json.load(resp)
    made = created.get("data") if isinstance(created.get("data"), dict) else created
    inv = made.get("investigation_id") or made.get("id")
    log(f"  investigation {inv} created ({made.get('status')})")
    # Record it before the wait, so the run panel can link to it while it is still running.
    if on_created:
        on_created(inv)
    deadline = time.time() + POLL_TIMEOUT
    status, report = "UNKNOWN", {}
    while time.time() < deadline:
        time.sleep(20)
        try:
            get = urllib.request.Request(f"{ix}/api/v2/investigations/{inv}", headers=headers)
            with urllib.request.urlopen(get, timeout=30) as resp:
                doc = json.load(resp)
        except (urllib.error.URLError, OSError, ValueError):
            continue
        # ixapi returns the investigation unwrapped on GET but enveloped on POST; accept both.
        data = doc.get("data") if isinstance(doc.get("data"), dict) else doc
        status = (data.get("status") or "").upper()
        if status in TERMINAL:
            report = data
            break
    log(f"  investigation {inv} finished status={status}")
    # A heuristic D1 only: whether the report names the faulted workload and namespace.
    text = json.dumps(report).lower()
    return {"investigation_id": inv, "status": status,
            "names_workload": workload.lower() in text,
            "names_namespace": spec["namespace"].lower() in text}


def run_case(run: dict, spec: dict, scored: bool) -> dict:
    """One variant: bring it up if parked, inject, validate, maybe investigate, recover, park."""
    lines: list[str] = []

    def log(line: str) -> None:
        lines.append(line)
        touch(run, log=lines[-40:])

    def record(inv: str) -> None:
        with LOCK:
            run.setdefault("investigation_ids", []).append(inv)
            save_runs()

    def investigate(sp, lg):
        return post_investigation(
            dict(sp, project_id=run["selection"]["project_id"],
                 investigation_target=run["selection"]["investigation_target"]),
            lg, on_created=record)

    # The namespace is parked at 0 between runs; bring just this workload up and put it back.
    # Scaling changes no pod template, so it adds no revision to the workload's history.
    parked = replicas(spec["workload"], spec["namespace"]) == 0
    try:
        if parked:
            scale(spec["workload"], spec["namespace"], 1)
        if not runner.wait_ready(CONTEXT, spec["namespace"], spec["workload"]):
            log(f"SKIP {spec['id']}: {spec['workload']} is not Ready before injection")
            return {"variant_id": spec["id"], "verdict": "failed",
                    "note": "workload not Ready at baseline"}
        out = runner.lifecycle(bundle_for(spec), spec, log=log,
                               phase=lambda name: touch(run, phase=name),
                               investigate=investigate if scored else None)
        registry.stamp(spec, verdict=out["verdict"], mutation_applied=out["mutation_applied"],
                       symptom_holds=out["symptom_holds"],
                       discriminating=out["discriminating"],
                       spec_restored=out.get("spec_restored"), clean=out.get("clean"),
                       source="dashboard-run")
        return out
    finally:
        # Park it again whatever happened: an abandoned workload costs the next run its baseline.
        if parked:
            scale(spec["workload"], spec["namespace"], 0)


def bundle_for(spec: dict) -> pathlib.Path:
    return runner.render_bundle(spec, BUNDLES / spec["id"])


def execute(run: dict) -> None:
    """Drive every case in the run's suite, recovering after each one."""
    known = catalogue()
    picked = run["selection"].get("case_ids")
    cases = cases_from_ids(picked) if picked else cases_for(run["selection"]["suite"])
    outcomes: list[dict] = []
    try:
        for vid, scored in cases:
            if run.get("stop_requested"):
                touch(run, state="cancelled", phase="ready", can_start=True)
                return
            touch(run, current_case_id=vid, phase="initializing")
            outcomes.append(run_case(run, known[vid], scored))
    except Exception as exc:  # noqa: BLE001 - any failure must still leave a readable run
        touch(run, state="failed", phase="failed", can_start=True,
              error={"error_type": type(exc).__name__, "message": str(exc)[:300]})
        return
    finally:
        with LOCK:
            run["outcomes"] = outcomes
            save_runs()

    dirty = [o for o in outcomes if o.get("spec_restored") is False]
    errors = [o for o in outcomes if o.get("verdict") == "failed"]
    state = "completed"
    if dirty:
        state = "dirty"
    elif errors:
        state = "completed_with_errors"
    touch(run, state=state, phase="ready", can_start=True,
          cleanup_verified=not dirty, current_case_id=None)


def public(run: dict) -> dict:
    """The run as the dashboard's schema expects it; internals stay on our side."""
    keep = ("run_id", "state", "batch_id", "stop_requested", "stop_supported", "phase",
            "current_case_id", "last_update_at", "cleanup_verified", "can_start", "error",
            "selection", "created_at", "created_by", "investigation_ids")
    return {k: run[k] for k in keep if k in run}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def ok(self, data: dict) -> None:
        self.reply(200, {"status": "success", "data": data})

    def guard(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in {"localhost", "127.0.0.1"}:
            self.reply(403, {"error": "Local access only"})
            return False
        return True

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        if not self.guard():
            return
        url = urlsplit(self.path)
        path = url.path.rstrip("/")
        if path == "/api/v1/evals/targets":
            project = (parse_qs(url.query).get("project_id") or ["2"])[0]
            try:
                pid = int(project)
            except ValueError:
                return self.reply(422, {"error": "Invalid project_id"})
            return self.ok({
                "targets": [{"server_id": SERVER_ID, "name": SERVER_NAME,
                             "hostname": os.uname().nodename, "project_ids": [pid]}],
                "investigation_targets": cluster_targets(),
            })
        if path == "/api/v1/evals/runs":
            with LOCK:
                runs = sorted(RUNS.values(), key=lambda r: r.get("created_at") or "", reverse=True)
                return self.ok({"runs": [public(r) for r in runs]})
        if path.startswith("/api/v1/evals/runs/"):
            run = RUNS.get(path.rsplit("/", 1)[-1])
            if not run:
                return self.reply(404, {"error": "No such run"})
            return self.ok(public(run))
        if path == "/api/v1/faultsim/runs/health":
            return self.ok({"port": PORT, "catalogue": len(catalogue()),
                            "runnable": len(registry.runnable(list(catalogue().values())))})
        self.reply(404, {"error": "Endpoint not found"})

    def do_POST(self):
        if not self.guard():
            return
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/api/v1/evals/targets/resolve":
            payload = self.body()
            target = str(payload.get("target") or "").lower()
            if target != SERVER_ID:
                return self.reply(404, {"error": "Unknown evaluation target"})
            return self.ok({"target": {"organization_id": ORG_ID, "server_id": SERVER_ID,
                                       "project_id": payload.get("project_id"),
                                       "status": "Active", "beacon_version": "in-pod"}})
        if path == "/api/v1/evals/runs":
            return self.start_run(self.body())
        if path.endswith("/cancel") and path.startswith("/api/v1/evals/runs/"):
            run = RUNS.get(path.split("/")[-2])
            if not run:
                return self.reply(404, {"error": "No such run"})
            touch(run, stop_requested=True,
                  state="cancelling" if run["state"] == "running" else run["state"])
            return self.ok(public(run))
        self.reply(404, {"error": "Endpoint not found"})

    def start_run(self, payload: dict):
        with LOCK:
            active = [r for r in RUNS.values() if r["state"] in ("running", "cancelling")]
        if active:
            return self.reply(409, {"error": "A run is already active"})
        if payload.get("benchmark") != BENCHMARK:
            return self.reply(422, {"error": f"This service only runs {BENCHMARK}"})
        picked = payload.get("case_ids") or []
        if not isinstance(picked, list) or any(not isinstance(v, str) for v in picked):
            return self.reply(422, {"error": "case_ids must be a list of variant ids"})
        known = catalogue()
        unknown = [v for v in picked if v not in known]
        if unknown:
            return self.reply(422, {"error": f"Unknown variant(s): {', '.join(unknown[:3])}"})
        suite = "custom" if picked else (payload.get("suite") or "validated")
        cases = cases_from_ids(picked) if picked else cases_for(suite)
        if not cases:
            return self.reply(422, {"error": f"Suite {suite!r} has no live-validated variants"})
        target = payload.get("investigation_target") or {}
        # Only a scored case posts an investigation, so only it needs a target to investigate.
        if any(scored for _, scored in cases) and not target.get("resource_id"):
            return self.reply(422, {"error": "Choose an investigation target"})
        run = {
            "run_id": uuid.uuid4().hex,
            "state": "running", "batch_id": None, "stop_requested": False,
            "stop_supported": True, "phase": "starting", "cleanup_verified": False,
            "can_start": False, "created_at": now(), "created_by": USER_ID,
            "investigation_ids": [],
            "selection": {"benchmark": BENCHMARK, "target": payload.get("target"),
                          "suite": suite, "project_id": payload.get("project_id"),
                          "case_ids": picked or None, "investigation_target": target},
        }
        with LOCK:
            RUNS[run["run_id"]] = run
            save_runs()
        threading.Thread(target=execute, args=(run,), daemon=True).start()
        self.ok(public(run))


if __name__ == "__main__":
    HOME.mkdir(parents=True, exist_ok=True)
    load_runs()
    print(f"faultsim-runs on 127.0.0.1:{PORT} | catalogue={len(catalogue())} "
          f"runnable={len(registry.runnable(list(catalogue().values())))} "
          f"server_id={SERVER_ID}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
