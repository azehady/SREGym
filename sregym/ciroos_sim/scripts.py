"""Render the four-script contract for a variant: simulate / validate / recover / verify_recovered.

Recovery is the hard half, and it is why only 2 of maya's 400 generated faults were usable: the
rest had no deterministic reset plan. So simulate.sh SNAPSHOTS the container spec before it
mutates anything, and recover.sh replaces the live spec with that snapshot. Reset is therefore a
replay of a captured state, never an inferred inverse patch.

Every script is self-contained except for `_lib.py`, which does the JSON work bash is bad at.
"""

from __future__ import annotations

import shlex

from sregym.ciroos_sim.generator import Variant

CONTRACT = ("simulate.sh", "validate.sh", "recover.sh", "verify_recovered.sh")

# Pull one value out of the pre-injection snapshot, for a recovery argument.
_READ_SNAPSHOT = ('import json;o=json.load(open("original.json"))\n'
                  'for k in "%s".split("."): o = o[int(k)] if k.isdigit() else o[k]\n'
                  'print(o)')

# How each mutation op becomes a JSON patch, and what proves it landed. Keyed by template.mutation.op.
_OPS = {
    "duplicate_env":         "append a second env entry with the same name, so the later value wins",
    "remove_env":            "delete the env entry by index",
    "rewrite_env_port":      "replace the env entry's value with an unused port",
    "repoint_liveness_port": "repoint the liveness probe at an unused port",
    "repoint_readiness_port": "repoint the readiness probe at an unused port",
    "inflate_requests":      "raise resource requests above any node's allocatable",
}

_LIB = '''\
"""JSON surgery for the fault bundle. One place, so the shell scripts stay thin."""

import json
import subprocess
import sys

SPEC = json.load(open("spec.json"))
CTX, NS, KIND, NAME = SPEC["context"], SPEC["namespace"], SPEC["kind"], SPEC["name"]
CONTAINER, OP, PARAMS = SPEC["container"], SPEC["op"], SPEC["params"]
REF = f"{KIND}/{NAME}"


def kubectl(*args, check=True):
    cmd = ["kubectl", f"--context={CTX}", "-n", NS, *args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        sys.exit(f"FAIL kubectl {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def live():
    return json.loads(kubectl("get", REF, "-o", "json"))


def container_index(obj):
    names = [c["name"] for c in obj["spec"]["template"]["spec"]["containers"]]
    if CONTAINER not in names:
        sys.exit(f"FAIL container {CONTAINER!r} not in {REF} (have {names})")
    return names.index(CONTAINER)


def env_index(container, key):
    for i, e in enumerate(container.get("env") or []):
        if e.get("name") == key:
            return i
    return None


def probe_port_path(container, probe):
    p = container.get(probe) or {}
    for handler in ("httpGet", "tcpSocket"):
        if handler in p:
            return f"/{probe}/{handler}/port"
    return None


def build_patch():
    """Emit the JSON patch for this op, computed against the LIVE spec, not the manifest."""
    obj = live()
    i = container_index(obj)
    c = obj["spec"]["template"]["spec"]["containers"][i]
    base = f"/spec/template/spec/containers/{i}"
    key, bad = PARAMS.get("env_key", ""), PARAMS.get("bad_value", "")

    if OP == "duplicate_env":
        # A true append: a strategic-merge patch would merge on `name` and overwrite instead.
        if not (c.get("env") or []):
            return [{"op": "add", "path": f"{base}/env", "value": [{"name": key, "value": bad}]}]
        return [{"op": "add", "path": f"{base}/env/-", "value": {"name": key, "value": bad}}]

    if OP in ("remove_env", "rewrite_env_port"):
        j = env_index(c, key)
        if j is None:
            sys.exit(f"FAIL env key {key!r} not present on {CONTAINER}; the fault cannot bind")
        if OP == "remove_env":
            return [{"op": "remove", "path": f"{base}/env/{j}"}]
        return [{"op": "replace", "path": f"{base}/env/{j}/value", "value": str(bad)}]

    if OP in ("repoint_liveness_port", "repoint_readiness_port"):
        probe = "livenessProbe" if "liveness" in OP else "readinessProbe"
        path = probe_port_path(c, probe)
        if path is None:
            sys.exit(f"FAIL {CONTAINER} has no httpGet/tcpSocket {probe}; cannot repoint a port")
        return [{"op": "replace", "path": base + path, "value": int(bad)}]

    if OP == "inflate_requests":
        res = dict(c.get("resources") or {})
        req, lim = dict(res.get("requests") or {}), dict(res.get("limits") or {})
        for pair in str(bad).split(","):
            k, _, v = pair.partition("=")
            k, v = k.strip(), v.strip()
            req[k] = v
            # A request above the container's OWN limit is rejected at admission, not
            # scheduled and left Pending, so raise the limit with it.
            if k in lim:
                lim[k] = v
        res["requests"] = req
        if lim:
            res["limits"] = lim
        return [{"op": "add", "path": f"{base}/resources", "value": res}]

    sys.exit(f"FAIL unknown mutation op {OP!r}")


def applied():
    """True when the live spec actually carries the mutation."""
    obj = live()
    c = obj["spec"]["template"]["spec"]["containers"][container_index(obj)]
    key, bad = PARAMS.get("env_key", ""), PARAMS.get("bad_value", "")
    env = c.get("env") or []
    if OP == "duplicate_env":
        return [e.get("name") for e in env].count(key) > 1 and (env[-1].get("value") == str(bad))
    if OP == "remove_env":
        return env_index(c, key) is None
    if OP == "rewrite_env_port":
        j = env_index(c, key)
        return j is not None and env[j].get("value") == str(bad)
    if OP in ("repoint_liveness_port", "repoint_readiness_port"):
        probe = "livenessProbe" if "liveness" in OP else "readinessProbe"
        p = c.get(probe) or {}
        return str((p.get("httpGet") or p.get("tcpSocket") or {}).get("port")) == str(bad)
    if OP == "inflate_requests":
        req = (c.get("resources") or {}).get("requests") or {}
        return all(req.get(k.split("=")[0].strip()) == v.strip()
                   for k, _, v in (p.partition("=") for p in str(bad).split(",")))
    return False


def pods():
    sel = live()["spec"]["selector"]["matchLabels"]
    q = ",".join(f"{k}={v}" for k, v in sel.items())
    return json.loads(kubectl("get", "pods", "-l", q, "-o", "json")).get("items", [])


def pod_facts():
    ready = restarts = pending = 0
    for p in pods():
        st = p.get("status") or {}
        if st.get("phase") == "Pending":
            pending += 1
        for cs in st.get("containerStatuses") or []:
            if cs.get("name") != CONTAINER:
                continue
            restarts += cs.get("restartCount") or 0
            if cs.get("ready"):
                ready += 1
    return {"ready": ready, "restarts": restarts, "pending": pending, "total": len(pods())}


# Each symptom class says what the cluster must look like for the fault to be REAL, not just
# present in the spec. A fault that patched cleanly but produced nothing is not a problem.
def symptom_holds(kind):
    f = pod_facts()
    if kind == "continuous-restart-loop":
        return f["restarts"] > 0, f
    if kind == "service-has-no-endpoints":
        return f["ready"] == 0 and f["total"] > 0, f
    if kind == "pods-pending-unschedulable":
        return f["pending"] > 0, f
    if kind in ("wrong-upstream-while-Ready", "connection-refused-to-dependency"):
        # Deliberately invisible at the k8s layer; the spec mutation IS the symptom surface.
        return f["ready"] > 0, f
    if kind == "startup-or-dependency-failure":
        return f["ready"] == 0 or f["restarts"] > 0, f
    return True, f


def restore():
    """Replace the live container spec with the snapshot taken before injection."""
    snap = json.load(open("original.json"))
    saved = snap["spec"]["template"]["spec"]["containers"]
    by_name = {c["name"]: c for c in saved}
    if CONTAINER not in by_name:
        sys.exit("FAIL snapshot has no such container; refusing to guess")
    obj = live()
    i = container_index(obj)
    patch = [{"op": "replace", "path": f"/spec/template/spec/containers/{i}",
              "value": by_name[CONTAINER]}]
    json.dump(patch, open("recover-patch.json", "w"), indent=2)
    print("wrote recover-patch.json")


def recovered():
    """True when the live container spec matches the snapshot exactly."""
    snap = json.load(open("original.json"))
    want = {c["name"]: c for c in snap["spec"]["template"]["spec"]["containers"]}[CONTAINER]
    obj = live()
    got = obj["spec"]["template"]["spec"]["containers"][container_index(obj)]
    return json.dumps(want, sort_keys=True) == json.dumps(got, sort_keys=True)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "patch":
        json.dump(build_patch(), open("patch.json", "w"), indent=2)
        print("wrote patch.json")
    elif cmd == "assert-applied":
        ok = applied()
        sym, facts = symptom_holds(SPEC["symptom_class"])
        print(f"mutation_applied={ok} symptom_holds={sym} pods={facts}")
        sys.exit(0 if (ok and sym) else 1)
    elif cmd == "restore":
        restore()
    elif cmd == "assert-recovered":
        ok = recovered()
        f = pod_facts()
        print(f"spec_restored={ok} pods={f}")
        sys.exit(0 if (ok and f["ready"] > 0) else 1)
    else:
        sys.exit(f"usage: _lib.py patch|assert-applied|restore|assert-recovered")
'''


def _preamble(v: Variant, phase: str, note: str) -> str:
    return f"""#!/usr/bin/env bash
# {v.id} — {phase}: {note}
# Generated by ciroos_sim. Run from the bundle directory; spec.json holds every parameter.
set -euo pipefail
cd "$(dirname "$0")"
"""


def _source(spec: dict, context: str, namespace: str) -> str:
    """The shell that fetches what a check asserts on: a field, a log tail, or an exec."""
    kube = f"kubectl --context={context} -n {namespace}"
    if spec.get("logs"):
        since = spec.get("since", "3m")
        return (f"{kube} logs {spec['logs']} --all-containers --tail=400 --since={since} "
                f"2>/dev/null | grep -Ei '{spec['matches']}' | head -3 || true")
    if spec.get("exec"):
        return f"{kube} exec {spec['exec']} -- {spec['command']} 2>/dev/null || true"
    return f"{kube} get {spec['get']} -o jsonpath='{spec['jsonpath']}' 2>/dev/null || true"


def _condition(spec: dict, negate: bool) -> str:
    """The shell test that means this assertion passes."""
    if spec.get("empty") is not None or spec.get("logs") or spec.get("matches"):
        want_empty = bool(spec.get("empty", False)) != negate
        return '[ -z "$got" ]' if want_empty else '[ -n "$got" ]'
    if spec.get("contains"):
        bang = "! " if negate else ""
        return f"""{bang}printf "%s" "$got" | grep -qiE '{spec["contains"]}'"""
    op = "!=" if negate else "="
    return f"""[ "$got" {op} "{spec.get("equals", "")}" ]"""


def _check(spec: dict, context: str, namespace: str, label: str, negate: bool = False) -> str:
    """One declarative assertion as shell. An empty spec asserts nothing, and says so.

    Retried, because the things these assert on settle at their own pace: a flood job needs
    time to fill a cache, and a foreground delete is still Terminating when it returns.
    """
    if not spec:
        return f'echo "skip {label}: template declares no check"'
    what = spec.get("get") or spec.get("logs") or spec.get("exec")
    tries, delay = int(spec.get("retries", 1)), int(spec.get("delay", 10))
    lines = [
        'ok=""',
        f"for _ in $(seq 1 {tries}); do",
        f"  got=$({_source(spec, context, namespace)})",
        f"  if {_condition(spec, negate)}; then ok=1; break; fi",
        f"  sleep {delay}",
        "done",
        f"""if [ -z "$ok" ]; then echo "FAIL {label}: {what} never satisfied the check (last: '$got')"; exit 1; fi""",
        f'echo "ok {label}: {what}"',
    ]
    return "\n".join(lines)


def _render_sregym(v: Variant, context: str) -> dict[str, str]:
    """Bundle that drives a real SREGym primitive, rather than our own JSON patch.

    The primitive owns both halves of the lifecycle, so recovery calls its recover_* rather
    than restoring a snapshot; the snapshot is still taken as a backstop for inspection.
    """
    repo = "${CIROOS_SIM_REPO:-/workspace/home/ciroos/SREGym}"
    py = f'"{repo}/.venv/bin/python"'
    def invoke(action: str, flags: str = "") -> str:
        # Built per action rather than formatted: the repo default holds shell ${VAR:-...}.
        return (f'{py} -m sregym.ciroos_sim.invoke {action} --primitive {v.primitive} '
                f'--namespace {v.namespace} --workload {v.workload.split("/")[-1]} '
                f'--context {context} {flags}'.rstrip())

    def params(action: str) -> tuple[str, str]:
        """(preamble, --param flags) for one action's extra arguments.

        A primitive that mutates a field cannot always invert it: recover_incorrect_image
        wants the image it replaced. `@snapshot:` reads that value back out of the snapshot
        simulate.sh took, so recovery replays captured state rather than guessing.
        """
        pre, flags = [], []
        for key, val in (v.invoke.get(action) or {}).items():
            if isinstance(val, str) and val.startswith("@snapshot:"):
                var = key.upper()
                pre.append(f"{var}=$(python3 -c {shlex.quote(_READ_SNAPSHOT % val[10:])})")
                flags.append(f'--param {key}="${var}"')
            else:
                flags.append("--param " + shlex.quote(f"{key}={val}"))
        return "\n".join(pre), " ".join(flags)

    inject_pre, inject_flags = params("inject")
    recover_pre, recover_flags = params("recover")
    applied = v.checks.get("applied", {})
    symptom = v.checks.get("symptom", {})
    # Recovery is proven by the template's own check when it names one, because the workload
    # a primitive damages is not always the one the variant is bound to.
    recovered = v.checks.get("recovered", {})

    simulate = _preamble(v, "simulate", f"inject via the {v.primitive} primitive") + f"""
# Snapshot first: the primitive owns recovery, but a stranded fault needs a reference.
kubectl --context={context} -n {v.namespace} get {v.workload} -o json > original.json
{inject_pre}
cd "{repo}" && {invoke("inject", inject_flags)}
echo "injected {v.id}; now run ./validate.sh"
"""

    validate = _preamble(v, "validate", "prove the fault is live, not merely invoked") + f"""
# The primitive reports success on its own terms; these assert the cluster actually changed.
{_check(applied, context, v.namespace, "mutation_applied")}
{_check(symptom, context, v.namespace, "symptom_holds")}
# The runner parses this line; both checks above exit non-zero before reaching it.
echo "mutation_applied=True symptom_holds=True pods=primitive:{v.primitive}"
echo "VALID {v.id}  symptom_class={v.symptom_class}"
"""

    # Some primitives park the workload at zero to roll it; put the replica count back too.
    replicas = (f'REPLICAS=$(python3 -c {shlex.quote(_READ_SNAPSHOT % "spec.replicas")})\n'
                f'kubectl --context={context} -n {v.namespace} scale {v.workload} '
                f'--replicas="$REPLICAS"') if v.invoke.get("restore_replicas") else ""
    recover = _preamble(v, "recover", f"undo via the {v.primitive} primitive") + f"""
{recover_pre}
cd "{repo}" && {invoke("recover", recover_flags)}
cd - >/dev/null
{replicas}
kubectl --context={context} -n {v.namespace} rollout status {v.workload} --timeout=180s || true
echo "recovered {v.id}; now run ./verify_recovered.sh"
"""

    verify = _preamble(v, "verify_recovered", "prove the cluster is clean again") + f"""
# The applied-check must now FAIL to hold, and the workload must be serving again.
{_check(applied, context, v.namespace, "mutation_gone", negate=True)}
{_check(recovered, context, v.namespace, "dependency_healthy") if recovered else 'echo "skip dependency_healthy: no extra check"'}
ready=$(kubectl --context={context} -n {v.namespace} get {v.workload} -o jsonpath='{{.status.readyReplicas}}' 2>/dev/null || true)
if [ -z "$ready" ] || [ "$ready" -lt 1 ]; then echo "spec_restored=False pods=not-ready"; echo "FAIL {v.workload} has no Ready replica"; exit 1; fi
echo "spec_restored=True pods=ready:$ready"
echo "CLEAN {v.id}"
"""
    import json as _json
    spec = {
        "variant_id": v.id, "context": context, "namespace": v.namespace,
        "kind": v.workload.split("/")[0], "name": v.workload.split("/")[1],
        "container": v.container, "engine": "sregym", "primitive": v.primitive,
        "checks": v.checks, "invoke": v.invoke, "params": v.params,
        "symptom_class": v.symptom_class, "root_cause": v.root_cause,
        "template_id": v.template_id, "category": v.category, "fault_level": v.fault_level,
    }
    return {
        "simulate.sh": simulate, "validate.sh": validate,
        "recover.sh": recover, "verify_recovered.sh": verify,
        "_lib.py": _LIB, "spec.json": _json.dumps(spec, indent=2) + "\n",
    }


def render(variant: Variant, context: str = "evals_gke", op: str = "") -> dict[str, str]:
    """Return {filename: contents} for the whole bundle, including spec.json and _lib.py."""
    v, ref = variant, variant.workload
    if getattr(v, "engine", "spec") == "sregym":
        return _render_sregym(v, context)
    simulate = _preamble(v, "simulate", f"inject the fault ({_OPS.get(op, op)})") + f"""
# Snapshot FIRST. This file is the only reset plan recover.sh will trust.
kubectl --context={context} -n {v.namespace} get {ref} -o json > original.json
echo "snapshot: original.json"

python3 _lib.py patch
kubectl --context={context} -n {v.namespace} patch {ref} --type=json --patch-file patch.json

# Some faults (unschedulable pods) never finish a rollout by design, so this is not the gate.
kubectl --context={context} -n {v.namespace} rollout status {ref} --timeout=120s || \\
  echo "rollout did not complete — expected for symptom_class={v.symptom_class}"
echo "injected {v.id}; now run ./validate.sh"
"""

    validate = _preamble(v, "validate", "prove the fault is live, not merely patched") + f"""
# Two assertions: the spec carries the mutation, AND the cluster shows the symptom.
# A fault that patched cleanly but produced no symptom is not a problem — it is a no-op.
test -f original.json || {{ echo "FAIL no original.json — simulate.sh has not run"; exit 2; }}
python3 _lib.py assert-applied
echo "VALID {v.id}  symptom_class={v.symptom_class}"
"""

    recover = _preamble(v, "recover", "put the snapshot back") + f"""
test -f original.json || {{ echo "FAIL no original.json — nothing to restore"; exit 2; }}
python3 _lib.py restore
kubectl --context={context} -n {v.namespace} patch {ref} --type=json --patch-file recover-patch.json
kubectl --context={context} -n {v.namespace} rollout status {ref} --timeout=180s
echo "recovered {v.id}; now run ./verify_recovered.sh"
"""

    verify = _preamble(v, "verify_recovered", "prove the cluster is clean again") + f"""
# Byte-compares the live container spec against the snapshot and requires a Ready pod.
# Without this the next fault inherits the previous one's damage — the contamination that
# makes a whole eval run unreadable.
python3 _lib.py assert-recovered
echo "CLEAN {v.id}"
"""

    spec = {
        "variant_id": v.id, "context": context, "namespace": v.namespace,
        "kind": v.workload.split("/")[0], "name": v.workload.split("/")[1],
        "container": v.container, "op": op, "params": v.params,
        "symptom_class": v.symptom_class, "root_cause": v.root_cause,
        "template_id": v.template_id, "category": v.category, "fault_level": v.fault_level,
    }
    import json as _json
    return {
        "simulate.sh": simulate, "validate.sh": validate,
        "recover.sh": recover, "verify_recovered.sh": verify,
        "_lib.py": _LIB, "spec.json": _json.dumps(spec, indent=2) + "\n",
    }
