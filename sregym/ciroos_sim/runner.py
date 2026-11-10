"""Run one variant's four-script contract end to end, reporting each phase as it happens.

The CLI and the dashboard's run service both call `lifecycle`, so the sequence exists once:
a second copy in TypeScript or in a shell wrapper is exactly how the injection path and the
recovery path drift apart.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import time

from sregym.ciroos_sim import registry

# Long enough for `kubectl rollout status --timeout=120s` to give up and for the script to
# finish reporting; an unschedulable pod never rolls out, which is the point.
STEP_TIMEOUT = 420

APPLIED = re.compile(r"mutation_applied=(\w+)\s+symptom_holds=(\w+)")
RESTORED = re.compile(r"spec_restored=(\w+)")


def _run(bundle: pathlib.Path, script: str, log) -> tuple[int, str]:
    log(f"$ ./{script}")
    proc = subprocess.run(["bash", f"./{script}"], cwd=bundle, text=True,
                          capture_output=True, timeout=STEP_TIMEOUT)
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.splitlines():
        log(f"  {line}")
    return proc.returncode, out


def lifecycle(bundle: pathlib.Path, spec: dict, *, investigate=None, log=print,
              phase=lambda name: None, recover: bool = True) -> dict:
    """Inject, validate, optionally investigate, then always put the cluster back.

    `investigate` is called only when the symptom actually holds — scoring an inert mutation
    measures nothing. Recovery runs in a finally block: a stranded fault blocks every later
    run in the namespace.
    """
    result = {"variant_id": spec["id"], "symptom_class": spec.get("symptom_class")}
    try:
        phase("initializing")
        code, _ = _run(bundle, "simulate.sh", log)
        # simulate.sh exits non-zero when the rollout cannot complete, which is expected for
        # pods-pending-unschedulable; validate.sh is the authority on whether it worked.
        result["simulate_exit"] = code

        phase("activating")
        code, out = _run(bundle, "validate.sh", log)
        found = APPLIED.search(out)
        result["mutation_applied"] = bool(found and found.group(1) == "True")
        result["symptom_holds"] = bool(found and found.group(2) == "True")
        result["verdict"] = registry.verdict_for(result["mutation_applied"],
                                                 result["symptom_holds"])
        result["discriminating"] = spec.get("symptom_class") not in registry.WEAK_SYMPTOMS
        if not result["discriminating"]:
            log("  NOTE symptom check only asserts the pod is Ready; it cannot prove a fault")

        if investigate and result["verdict"] == registry.VALID:
            phase("investigating")
            result["investigation"] = investigate(spec, log)
    finally:
        if recover:
            phase("cleaning")
            _run(bundle, "recover.sh", log)
            code, out = _run(bundle, "verify_recovered.sh", log)
            found = RESTORED.search(out)
            result["spec_restored"] = bool(found and found.group(1) == "True")
            result["clean"] = code == 0
    phase("ready")
    return result


def render_bundle(spec: dict, out_dir: pathlib.Path) -> pathlib.Path:
    """Materialize a catalogue variant's scripts on disk so they can be executed."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (spec.get("scripts") or {}).items():
        path = out_dir / name
        path.write_text(body)
        if name.endswith(".sh"):
            os.chmod(path, 0o755)
    lib = spec.get("_lib")
    if lib:
        (out_dir / "_lib.py").write_text(lib)
    return out_dir


def wait_ready(context: str, namespace: str, workload: str, timeout: int = 240) -> bool:
    """Baseline gate: a fault injected into an already-broken workload proves nothing."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        proc = subprocess.run(
            ["kubectl", f"--context={context}", "-n", namespace, "get", workload,
             "-o", "jsonpath={.status.readyReplicas}"],
            text=True, capture_output=True)
        if (proc.stdout or "").strip() not in ("", "0"):
            return True
        time.sleep(5)
    return False
