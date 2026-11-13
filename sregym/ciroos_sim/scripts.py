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
_READ_SNAPSHOT = ('import json;o=json.load(open("%s"))\n'
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
import time

SPEC = json.load(open("spec.json"))
CTX, NS, KIND, NAME = SPEC["context"], SPEC["namespace"], SPEC["kind"], SPEC["name"]
CONTAINER, OP, PARAMS = SPEC["container"], SPEC["op"], SPEC["params"]
TRIGGER = SPEC.get("trigger") or {}
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


def cpu_millicores():
    """What the target container is burning right now, summed across its pods."""
    sel = live()["spec"]["selector"]["matchLabels"]
    q = ",".join(f"{k}={v}" for k, v in sel.items())
    out = kubectl("top", "pods", "-l", q, "--containers", "--no-headers", check=False)
    total = 0
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 4 and cols[1] == CONTAINER and cols[2].endswith("m"):
            total += int(cols[2][:-1])
    return total


def working_set_mi():
    """What the target container is actually using right now, in MiB, across its pods."""
    sel = live()["spec"]["selector"]["matchLabels"]
    q = ",".join(f"{k}={v}" for k, v in sel.items())
    out = kubectl("top", "pods", "-l", q, "--containers", "--no-headers", check=False)
    peak = 0
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 4 and cols[1] == CONTAINER and cols[3].endswith("Mi"):
            peak = max(peak, int(cols[3][:-2]))
    return peak


def squeeze_patch(c, base):
    """Trim memory headroom to just above what the workload uses at base load.

    Calibrated on the cluster rather than guessed, because the band that makes a failure
    metastable is per-workload: a limit under the base-load working set is plain starvation,
    and one far above it survives the spike and produces nothing.
    """
    ws = working_set_mi()
    if ws <= 0:
        sys.exit("FAIL cannot read the working set; is metrics-server up?")
    limit = f"{max(16, int(ws * float(PARAMS.get('headroom', 1.15))))}Mi"
    json.dump({"memory_limit": limit, "working_set_mi": ws}, open("calibration.json", "w"))
    res = dict(c.get("resources") or {})
    lim, req = dict(res.get("limits") or {}), dict(res.get("requests") or {})
    lim["memory"] = limit
    # A request above its own limit is rejected at admission, so move it down too.
    if req.get("memory"):
        req["memory"] = limit
    res["limits"] = lim
    if req:
        res["requests"] = req
    return [{"op": "add", "path": f"{base}/resources", "value": res}]


def baseline():
    """Record the workload's state with the predisposition applied but the spike not yet run."""
    facts = pod_facts()
    facts["memory_mi"] = working_set_mi()
    facts["cpu_m"] = cpu_millicores()
    json.dump(facts, open("baseline.json", "w"))
    print(f"baseline={facts}")


def metastable(window):
    """Is the failure still running now that the spike is over and load is back to base?

    Two things must hold together or it is not metastable: the predisposition alone was
    harmless, and the symptom outlived the transient that started it.
    """
    try:
        base = json.load(open("baseline.json"))
    except FileNotFoundError:
        return False, "no baseline.json; simulate.sh never recorded the pre-spike state"
    if base.get("ready", 0) < 1:
        return False, f"predisposition alone already broke it ({base}): starvation, not metastable"
    if base.get("restarts", 0) > 0:
        # Readiness is a snapshot: a pod in a crash loop reads Ready between crashes. If it had
        # already restarted before the spike, the predisposition alone did it.
        return False, (f"workload had already restarted {base['restarts']}x before the spike "
                       f"({base}): starvation, not metastable")
    # Some loops show up as restarts; a leak or a backlog shows up as a number that keeps
    # climbing while the load is back to base. The template says which to watch.
    sustain = TRIGGER.get("sustain") or {}
    if sustain.get("metric") == "cpu":
        # The honest measure of retry amplification: the offered load is back to exactly what
        # the baseline was taken at, so CPU far above the baseline means the system is doing
        # work the clients never asked for, and is still doing it.
        factor = float(sustain.get("exceeds_baseline_by", 2.0))
        floor = int(sustain.get("baseline_floor_m", 50))
        was = int(base.get("cpu_m") or 0)
        # A ratio against a near-idle baseline is a measurement of noise: 2.5x of 1m is 3m.
        # Refuse to call that amplification rather than pass on it.
        if was < floor:
            return False, (f"baseline cpu was only {was}m, under the {floor}m floor; the base "
                           f"load is too small to measure amplification against")
        time.sleep(window)
        now = cpu_millicores()
        if now >= was * factor:
            return True, (f"cpu {now}m vs {was}m at baseline ({now / was:.1f}x) at the SAME "
                          f"base load, {window}s after the spike ended")
        return False, (f"cpu {now}m vs {was}m at baseline; under the {factor}x that would show "
                       f"amplification outliving the spike")

    if sustain.get("metric") == "memory":
        need = int(sustain.get("grows_by", 1))
        first = working_set_mi()
        time.sleep(window)
        second = working_set_mi()
        if second - first >= need:
            return True, (f"memory {first}->{second} MiB over {window}s with the spike over "
                          f"(baseline {base.get('memory_mi')} MiB)")
        return False, (f"memory {first}->{second} MiB over {window}s, short of +{need}; "
                       f"not still climbing once the spike stopped")

    first = pod_facts()
    time.sleep(window)
    second = pod_facts()
    if second["restarts"] > first["restarts"]:
        return True, f"restarts {first['restarts']}->{second['restarts']} with the spike over"
    if first["ready"] == 0 and second["ready"] == 0:
        return True, f"no Ready pod across {window}s with the spike over"
    return False, f"recovered once the spike stopped: {second}"


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

    if OP == "squeeze_memory_limit":
        return squeeze_patch(c, base)

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
    # A primitive-driven fault edits something other than this container spec, so the bundle's
    # declarative check is the authority and there is nothing to confirm here.
    if not OP:
        return True
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
    if OP == "squeeze_memory_limit":
        want = json.load(open("calibration.json"))["memory_limit"]
        return ((c.get("resources") or {}).get("limits") or {}).get("memory") == want
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
    elif cmd == "baseline":
        baseline()
    elif cmd == "assert-metastable":
        ok = applied()
        sym, why = metastable(int(sys.argv[2]))
        print(f"mutation_applied={ok} symptom_holds={sym} pods={why}")
        sys.exit(0 if (ok and sym) else 1)
    elif cmd == "restore":
        restore()
    elif cmd == "assert-recovered":
        ok = recovered()
        f = pod_facts()
        print(f"spec_restored={ok} pods={f}")
        sys.exit(0 if (ok and f["ready"] > 0) else 1)
    else:
        sys.exit("usage: _lib.py patch|assert-applied|baseline|assert-metastable|restore|assert-recovered")
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
    # A check may assert on another namespace: proof that a host-level probe is attached
    # lives with the injector's DaemonSet, not with the workload it damages.
    kube = f"kubectl --context={context} -n {spec.get('namespace', namespace)}"
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
    """Bundle that drives real SREGym primitives, rather than our own JSON patch.

    The primitive owns both halves of the lifecycle, so recovery calls its recover_* rather
    than restoring a snapshot. A correlated fault is the same primitive driven over several
    workloads at once, so every phase here is a loop over the variant's targets.
    """
    repo = "${CIROOS_SIM_REPO:-/workspace/home/ciroos/SREGym}"
    py = f'"{repo}/.venv/bin/python"'
    kube = f"kubectl --context={context} -n {v.namespace}"
    targets = v.targets or [{"name": v.workload.split("/")[-1], "ref": v.workload, "checks": v.checks}]
    multi = len(targets) > 1

    def snap(t: dict) -> str:
        return f"original-{t['name']}.json" if multi else "original.json"

    def sub(val, t: dict):
        return val.replace("{name}", t["name"]).replace("{ref}", t["ref"]) if isinstance(val, str) else val

    def invoke(action: str, t: dict, flags: str = "") -> str:
        # Built per action rather than formatted: the repo default holds shell ${VAR:-...}.
        return (f'{py} -m sregym.ciroos_sim.invoke {action} --primitive {v.primitive} '
                f'--namespace {v.namespace} --workload {t["name"]} '
                f'--context {context} {flags}'.rstrip())

    def params(action: str, t: dict) -> tuple[str, str]:
        """(preamble, --param flags) for one target's extra arguments.

        A primitive that overwrites a field cannot always invert it: recover_incorrect_image
        wants the image it replaced. `@snapshot:` reads that value back out of the snapshot
        simulate.sh took, so recovery replays captured state rather than guessing.
        """
        pre, flags = [], []
        for key, val in (v.invoke.get(action) or {}).items():
            var = f"{key.upper()}_{t['name'].replace('-', '_').upper()}" if multi else key.upper()
            if isinstance(val, str) and val.startswith("@snapshot:"):
                pre.append(f"{var}=$(python3 -c {shlex.quote(_READ_SNAPSHOT % (snap(t), val[10:]))})")
                flags.append(f'--param {key}="${var}"')
            else:
                flags.append("--param " + shlex.quote(f"{key}={sub(val, t)}"))
        return "\n".join(pre), " ".join(flags)

    def phase(action: str) -> str:
        """Snapshot reads happen in the bundle directory, the invokes in the repo."""
        pre = [p for p, _ in (params(action, t) for t in targets) if p]
        calls = [invoke(action, t, f) for t, (_, f) in ((t, params(action, t)) for t in targets)]
        return "\n".join([*pre, f'cd "{repo}"', *calls, "cd - >/dev/null"])

    def checks_for(t: dict, phase_name: str, label: str, negate: bool = False) -> str:
        tag = f"{label}[{t['name']}]" if multi else label
        return _check(t["checks"].get(phase_name, {}), context, v.namespace, tag, negate=negate)

    simulate = _preamble(v, "simulate", f"inject via the {v.primitive} primitive") + "\n" + "\n".join(
        [f"# Snapshot first: the primitive owns recovery, but a stranded fault needs a reference."]
        + [f"{kube} get {t['ref']} -o json > {snap(t)}" for t in targets]
        + [phase("inject"), f'echo "injected {v.id}; now run ./validate.sh"', ""])

    validate = _preamble(v, "validate", "prove the fault is live, not merely invoked") + "\n" + "\n".join(
        ["# The primitive reports success on its own terms; these assert the cluster changed."]
        + [checks_for(t, "applied", "mutation_applied") for t in targets]
        + [checks_for(t, "symptom", "symptom_holds") for t in targets]
        + ["# The runner parses this line; every check above exits non-zero before reaching it.",
           f'echo "mutation_applied=True symptom_holds=True pods=primitive:{v.primitive}x{len(targets)}"',
           f'echo "VALID {v.id}  symptom_class={v.symptom_class}"', ""])

    # Some primitives park the workload at zero to roll it; put the replica count back too.
    replicas = []
    for t in targets:
        if v.invoke.get("restore_replicas"):
            var = f"REPLICAS_{t['name'].replace('-', '_').upper()}"
            replicas += [f"{var}=$(python3 -c {shlex.quote(_READ_SNAPSHOT % (snap(t), 'spec.replicas'))})",
                         f'{kube} scale {t["ref"]} --replicas="${var}"']
    recover = _preamble(v, "recover", f"undo via the {v.primitive} primitive") + "\n" + "\n".join(
        [phase("recover"), *replicas]
        + [f"{kube} rollout status {t['ref']} --timeout=180s || true" for t in targets]
        + [f'echo "recovered {v.id}; now run ./verify_recovered.sh"', ""])

    verify_lines = ["# Every applied-check must now FAIL to hold, and each workload must serve again."]
    for t in targets:
        verify_lines.append(checks_for(t, "applied", "mutation_gone", negate=True))
        if t["checks"].get("recovered"):
            verify_lines.append(checks_for(t, "recovered", "dependency_healthy"))
        verify_lines += [
            f"""ready=$({kube} get {t['ref']} -o jsonpath='{{.status.readyReplicas}}' 2>/dev/null || true)""",
            f"""if [ -z "$ready" ] || [ "$ready" -lt 1 ]; then echo "spec_restored=False pods=not-ready";"""
            f""" echo "FAIL {t['ref']} has no Ready replica"; exit 1; fi""",
        ]
    verify = _preamble(v, "verify_recovered", "prove the cluster is clean again") + "\n" + "\n".join(
        verify_lines + ['echo "spec_restored=True pods=ready"', f'echo "CLEAN {v.id}"', ""])

    import json as _json
    spec = {
        "variant_id": v.id, "context": context, "namespace": v.namespace,
        "kind": v.workload.split("/")[0], "name": v.workload.split("/")[1],
        "container": v.container, "engine": "sregym", "primitive": v.primitive,
        "checks": v.checks, "invoke": v.invoke, "params": v.params, "targets": targets,
        "symptom_class": v.symptom_class, "root_cause": v.root_cause,
        "template_id": v.template_id, "category": v.category, "fault_level": v.fault_level,
    }
    return {
        "simulate.sh": simulate, "validate.sh": validate,
        "recover.sh": recover, "verify_recovered.sh": verify,
        "_lib.py": _LIB, "spec.json": _json.dumps(spec, indent=2) + "\n",
    }


def _render_composite(v: Variant, context: str, ops: dict) -> dict[str, str]:
    """Independent faults as one incident: each member keeps its own bundle, in its own directory.

    Delegating to real sub-bundles is the only way a composite can mix engines and namespaces
    without this renderer growing a second copy of either path.
    """
    dirs = [(f"member{i}-{m['id']}", m) for i, m in enumerate(v.members, start=1)]
    files: dict[str, str] = {}
    for name, m in dirs:
        sub = render(Variant(**m), context=context, op=(ops or {}).get(m["template_id"], ""), ops=ops)
        files.update({f"{name}/{fn}": body for fn, body in sub.items()})

    def steps(script: str, reverse: bool = False) -> list[str]:
        order = reversed(dirs) if reverse else dirs
        return [f'echo "== {n}"' + "\n" + f'( cd "{n}" && ./{script} )' for n, _ in order]

    # set -e in the preamble means a failing member aborts before any summary line is printed.
    files["simulate.sh"] = _preamble(v, "simulate", f"inject {len(dirs)} independent faults") + "\n" + "\n".join(
        steps("simulate.sh") + [f'echo "injected {v.id}; now run ./validate.sh"', ""])
    files["validate.sh"] = _preamble(v, "validate", "prove every member fault is live") + "\n" + "\n".join(
        steps("validate.sh")
        + ["# Only reached when every member validated; the runner parses the next line.",
           f'echo "mutation_applied=True symptom_holds=True pods=composite:{len(dirs)}"',
           f'echo "VALID {v.id}  symptom_class={v.symptom_class}"', ""])
    # Reverse order, so a member whose recovery depends on another's is undone first.
    files["recover.sh"] = _preamble(v, "recover", "undo every member fault") + "\n" + "\n".join(
        steps("recover.sh", reverse=True) + [f'echo "recovered {v.id}; now run ./verify_recovered.sh"', ""])
    files["verify_recovered.sh"] = _preamble(v, "verify_recovered", "prove the cluster is clean again") + "\n" + "\n".join(
        steps("verify_recovered.sh", reverse=True)
        + ['echo "spec_restored=True pods=ready"', f'echo "CLEAN {v.id}"', ""])

    import json as _json
    files["spec.json"] = _json.dumps({
        "variant_id": v.id, "context": context, "namespace": v.namespace,
        "engine": "composite", "symptom_class": v.symptom_class, "root_cause": v.root_cause,
        "template_id": v.template_id, "category": v.category, "fault_level": v.fault_level,
        "members": [{"dir": n, "id": m["id"], "template_id": m["template_id"],
                     "namespace": m["namespace"], "workload": m["workload"]} for n, m in dirs],
    }, indent=2) + "\n"
    return files


def _scale(step: dict, context: str, namespace: str) -> str:
    """A trigger step is a scale and nothing else: a template is data, not a shell script."""
    if not step:
        return "true"
    return f"kubectl --context={context} -n {namespace} scale {step['scale']} --replicas={step['replicas']}"


def _render_metastable(v: Variant, context: str, op: str) -> dict[str, str]:
    """A fault that outlives the thing that caused it.

    The protocol is what makes it metastable, not the mutation: run base load, prove the
    predisposition alone is harmless, spike, then drop back to the SAME base load. Withdrawing
    to zero would prove nothing — every load-driven loop dies when the load does.
    """
    t, ref = v.trigger, v.workload
    kube = f"kubectl --context={context} -n {v.namespace}"
    settle, hold, window = int(t.get("settle", 90)), int(t.get("hold", 120)), int(t.get("window", 90))
    repo = "${CIROOS_SIM_REPO:-/workspace/home/ciroos/SREGym}"

    def invoke(action: str) -> str:
        """The predisposition as a primitive call, for a fault that is not a container patch."""
        flags = " ".join("--param " + shlex.quote(f"{k}={val}")
                         for k, val in (v.invoke.get(action) or {}).items())
        # `|| true`: several primitives end by waiting for every pod in the namespace to be
        # Ready, which is a post-condition a fault like this one is designed to violate. Left
        # strict, the protocol aborts at inject and a later phase reads whatever files the
        # PREVIOUS run left behind. The applied check decides whether the mutation landed.
        return (f'cd "{repo}"\n'
                f'"{repo}/.venv/bin/python" -m sregym.ciroos_sim.invoke {action} '
                f'--primitive {v.primitive} --namespace {v.namespace} '
                f'--workload {ref.split("/")[-1]} --context {context} {flags} '
                f'|| echo "note: {action} primitive reported failure; checks are the authority"\n'
                f'cd - >/dev/null')

    primitive = getattr(v, "engine", "spec") == "sregym"
    # A primitive owns both halves of its own lifecycle; a spec op is a patch against a
    # snapshot. The protocol around them is identical either way.
    predispose = invoke("inject") if primitive else (
        f'python3 _lib.py patch\n'
        f'{kube} patch {ref} --type=json --patch-file patch.json\n'
        f'{kube} rollout status {ref} --timeout=180s || true')
    undo = invoke("recover") if primitive else (
        f'python3 _lib.py restore\n'
        f'{kube} patch {ref} --type=json --patch-file recover-patch.json\n'
        f'{kube} rollout status {ref} --timeout=180s')
    simulate = _preamble(v, "simulate", f"predispose, then spike and withdraw ({op})") + f"""
# Bundle directories are reused between runs, so a stale baseline.json or marker from an
# earlier run can satisfy a later run's precondition. One run's verdict came from exactly that.
rm -f baseline.json protocol-done.json calibration.json
{kube} get {ref} -o json > original.json
echo "snapshot: original.json"

# Base load first: the baseline has to be measured with traffic on, or it is not the
# state the spike knocks the system out of.
{_scale(t.get("warm"), context, v.namespace)}
{kube} rollout status {t["warm"]["scale"]} --timeout=180s || true
echo "warming {settle}s at base load"; sleep {settle}

{predispose}
echo "settling {settle}s before the baseline reading"; sleep {settle}

# The baseline is the control: if the workload is already unhealthy here, the predisposition
# alone did it, and what follows is starvation rather than a metastable failure.
python3 _lib.py baseline

{_scale(t.get("apply"), context, v.namespace)}
echo "spike held for {hold}s"; sleep {hold}
{_scale(t.get("withdraw"), context, v.namespace)}
echo "spike withdrawn; load is back to base"
# Only now is the protocol complete. validate.sh refuses to judge without this.
printf '{{"variant_id": "%s"}}\\n' "{v.id}" > protocol-done.json
echo "injected {v.id}; now run ./validate.sh"
"""
    # A primitive edits something other than this container spec, so _lib.py cannot confirm the
    # predisposition landed; the template's own check is the only authority.
    applied_check = (_check(v.checks.get("applied", {}), context, v.namespace, "mutation_applied")
                     if primitive else "true")
    validate = _preamble(v, "validate", "prove the failure outlived the spike") + f"""
grep -q '"{v.id}"' protocol-done.json 2>/dev/null || {{
  echo "FAIL simulate.sh did not finish the protocol for {v.id} (no marker); refusing to judge"
  exit 2
}}
test -f baseline.json || {{ echo "FAIL no baseline.json — simulate.sh has not run"; exit 2; }}
{applied_check}
# Samples twice across {window}s with the spike already over: a residue that is not still
# progressing is a system recovering slowly, not one stuck in a self-sustaining loop.
python3 _lib.py assert-metastable {window}
echo "VALID {v.id}  symptom_class={v.symptom_class}"
"""
    recover = _preamble(v, "recover", "stop the load, then put the snapshot back") + f"""
# Load off first: restoring the spec under a running spike just re-enters the loop.
{_scale(t.get("stop"), context, v.namespace)}
test -f original.json || {{ echo "FAIL no original.json — nothing to restore"; exit 2; }}
{undo}
echo "recovered {v.id}; now run ./verify_recovered.sh"
"""
    if primitive:
        gone = _check(v.checks.get("applied", {}), context, v.namespace, "mutation_gone", negate=True)
        verify = _preamble(v, "verify_recovered", "prove the cluster is clean again") + "\n" + "\n".join([
            gone,
            f"""ready=$({kube} get {ref} -o jsonpath='{{.status.readyReplicas}}' 2>/dev/null || true)""",
            f"""if [ -z "$ready" ] || [ "$ready" -lt 1 ]; then echo "spec_restored=False pods=not-ready";"""
            f""" echo "FAIL {ref} has no Ready replica"; exit 1; fi""",
            'echo "spec_restored=True pods=ready"', f'echo "CLEAN {v.id}"', ""])
    else:
        verify = _preamble(v, "verify_recovered", "prove the cluster is clean again") + f"""
python3 _lib.py assert-recovered
echo "CLEAN {v.id}"
"""
    import json as _json
    spec = {
        "variant_id": v.id, "context": context, "namespace": v.namespace,
        "kind": ref.split("/")[0], "name": ref.split("/")[1], "container": v.container,
        "op": op, "params": v.params, "trigger": t, "invoke": v.invoke,
        "primitive": v.primitive, "engine": v.engine, "checks": v.checks,
        "symptom_class": v.symptom_class,
        "root_cause": v.root_cause, "template_id": v.template_id, "category": v.category,
        "fault_level": v.fault_level,
    }
    return {"simulate.sh": simulate, "validate.sh": validate, "recover.sh": recover,
            "verify_recovered.sh": verify, "_lib.py": _LIB,
            "spec.json": _json.dumps(spec, indent=2) + "\n"}


def render(variant: Variant, context: str = "evals_gke", op: str = "",
           ops: dict | None = None) -> dict[str, str]:
    """Return {filename: contents} for the whole bundle, including spec.json and _lib.py."""
    v, ref = variant, variant.workload
    if getattr(v, "members", None):
        return _render_composite(v, context, ops or {})
    if getattr(v, "trigger", None):
        return _render_metastable(v, context, op)
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
