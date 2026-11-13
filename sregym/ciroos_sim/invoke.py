"""Run one SREGym fault primitive standalone, so a generated bundle injects the real fault.

The 57 `inject_*`/`recover_*` primitives already span every fault type in the problem
catalogue. ciroos_sim's own op table only ever edits a container spec, which is why it
reaches two taxonomy categories; this entrypoint lets a variant drive a primitive instead,
with the variation coming from the target it is bound to.

Safety: evals_gke is shared, so this pins the kube context for its own process, refuses any
namespace outside the bench prefix, and runs only primitives reviewed as namespace-scoped.
"""

from __future__ import annotations

import argparse
import atexit
import json
import importlib
import inspect
import os
import pathlib
import subprocess
import sys
import tempfile

# Injectors whose constructor needs nothing but a namespace. Node/OS/hardware injectors are
# deliberately absent: they act on the node, which is shared with every other tenant.
INJECTORS = {
    "ApplicationFaultInjector": "sregym.generators.fault.inject_app",
    "K8SOperatorFaultInjector": "sregym.generators.fault.inject_operator",
    "VirtualizationFaultInjector": "sregym.generators.fault.inject_virtual",
}

# Primitives read line by line and confirmed to touch only objects inside the target
# namespace. Anything absent is refused rather than assumed safe — several primitives rewrite
# the kube-system coredns ConfigMap or bind a ClusterRole, which would hit other tenants.
NAMESPACE_SAFE = {
    "wrong_dns_policy",
    "wrong_service_selector",
    "valkey_auth_disruption",
    "valkey_memory_disruption",
    "incorrect_image",
    # Patches the `rpc` ConfigMap and rollout-restarts the deployments labelled for it, all
    # inside the target namespace.
    "rpc_timeout_retries_misconfiguration",
}

# Why each refused primitive is refused, so the message names the blast radius.
CLUSTER_SCOPED = {
    "service_dns_resolution_failure": "rewrites the kube-system coredns ConfigMap",
    "stale_coredns_config": "rewrites the kube-system coredns ConfigMap",
    "rbac_misconfiguration": "creates a ClusterRole and ClusterRoleBinding, and deletes the deployment",
    "kubelet_crash": "acts on the node, which is shared",
    "disk_outage": "acts on the node, which is shared",
    "disk_pressure": "acts on the node, which is shared",
}

# Syscall faults khaos delivers by attaching a BPF kprobe to a workload's host PID. The probe
# runs in the NODE's kernel, not the container's, so a bad one takes down every tenant on that
# node. Gated behind an explicit opt-in rather than listed as namespace-safe.
HOST_LEVEL = {
    "read_error": "read() returns EIO, as a failing storage controller would",
    "write_error": "write() fails, as a disk rejecting writes would",
    "mmap_fail": "anonymous mappings fail, as a defective DRAM module surfacing ECC errors would",
    "getaddrinfo_fail": "the node-local resolver path fails for every lookup",
}

# The demo's own app-level faults, flipped in the flagd ConfigMap. Listed apart from the
# others because the injector takes a FLAG name rather than a workload, and because flagd
# re-reads its mounted file, so a flag can change without restarting the service that reads it.
FEATURE_FLAG = "feature_flag"

NS_PREFIX = os.environ.get("CIROOS_SIM_NS_PREFIX", "orca-bench-")
KHAOS_NS = os.environ.get("CIROOS_SIM_KHAOS_NS", "khaos")


def pin_context(context: str) -> None:
    """Point this process at one context without touching the shared kubeconfig.

    The primitives build bare `kubectl ...` strings and KubeCtl calls load_kube_config(), so
    both follow whatever context is current. Switching it with `config use-context` would
    change it for every other session sharing this file.
    """
    out = subprocess.run(["kubectl", "config", "view", "--raw", "--minify",
                          f"--context={context}"], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"FAIL cannot read context {context!r}: {out.stderr.strip()}")
    fd, path = tempfile.mkstemp(prefix="ciroos-sim-kubeconfig-", suffix=".yaml")
    os.write(fd, out.stdout.encode())
    os.close(fd)
    os.chmod(path, 0o600)
    os.environ["KUBECONFIG"] = path
    atexit.register(lambda: pathlib.Path(path).unlink(missing_ok=True))


def find_owner(primitive: str):
    """The injector class that implements this primitive, with both halves of the pair."""
    for cls_name, module in INJECTORS.items():
        mod = importlib.import_module(module)
        cls = getattr(mod, cls_name, None)
        if cls and hasattr(cls, f"inject_{primitive}") and hasattr(cls, f"recover_{primitive}"):
            return cls
    return None


def call(injector, action: str, primitive: str, workload: str, params: dict) -> None:
    """Primitives disagree on how they name their target, so bind by parameter name.

    The first parameter is the target whatever it is called; `namespace` is the injector's
    own; anything else must come from the template, since a primitive like incorrect_image
    cannot recover without being handed the image it replaced.
    """
    method = getattr(injector, f"{action}_{primitive}")
    bound = {}
    for i, (name, p) in enumerate(inspect.signature(method).parameters.items()):
        if name == "microservices":
            bound[name] = [workload]
        elif name == "namespace":
            bound[name] = injector.namespace
        elif name in params:
            bound[name] = params[name]
        elif i == 0:
            bound[name] = workload
        elif p.default is inspect.Parameter.empty:
            sys.exit(f"FAIL {action}_{primitive} needs {name!r}; pass --param {name}=<value>")
    return method(**bound)


def _kubectl(*args: str) -> str:
    out = subprocess.run(["kubectl", *args], capture_output=True, text=True)
    return out.stdout or ""


def require_disposable(context: str, primitive: str) -> None:
    """Host-level faults need a cluster nobody else is using; nothing scopes them to a tenant."""
    allowed = [c.strip() for c in os.environ.get("CIROOS_SIM_DISPOSABLE_CLUSTER", "").split(",") if c.strip()]
    if context not in allowed:
        sys.exit(f"FAIL {primitive} attaches a BPF kprobe in the node's kernel ({HOST_LEVEL[primitive]}). "
                 f"A panic takes every pod on that node with it, and nothing confines it to "
                 f"{NS_PREFIX}*. Export CIROOS_SIM_DISPOSABLE_CLUSTER={context} only for a cluster "
                 f"you can afford to destroy.")


def require_khaos() -> list[str]:
    """Without the DaemonSet these primitives inject nothing and still report success."""
    pods = _kubectl("-n", KHAOS_NS, "get", "pods", "-l", "app=khaos",
                    "--field-selector=status.phase=Running",
                    "-o", "jsonpath={.items[*].metadata.name}").split()
    if not pods:
        sys.exit(f"FAIL no running khaos DaemonSet pod in namespace {KHAOS_NS!r}; a host-level "
                 f"fault would be a silent no-op. Deploy khaos first.")
    return pods


def host_pods(namespace: str, workload: str) -> list[str]:
    """A kprobe binds to a PID, so the target is the workload's pods, not the Deployment."""
    labels = _kubectl("-n", namespace, "get", "deployment", workload,
                      "-o", "jsonpath={.spec.selector.matchLabels}")
    sel = ",".join(f"{k}={v}" for k, v in json.loads(labels or "{}").items())
    if not sel:
        sys.exit(f"FAIL deployment/{workload} in {namespace} has no selector to resolve pods from")
    names = _kubectl("-n", namespace, "get", "pods", "-l", sel,
                     "--field-selector=status.phase=Running",
                     "-o", "jsonpath={.items[*].metadata.name}").split()
    if not names:
        sys.exit(f"FAIL no Running pods for deployment/{workload} in {namespace}")
    return [f"{namespace}/{n}" for n in names]


def call_host_level(action: str, primitive: str, namespace: str, workload: str, context: str) -> None:
    """Drive khaos directly: its injector is one inject/recover pair, not a method per fault."""
    from sregym.generators.fault.inject_hw import HWFaultInjector

    require_disposable(context, primitive)
    pin_context(context)
    require_khaos()
    pods = host_pods(namespace, workload)
    injector = HWFaultInjector(khaos_namespace=KHAOS_NS)
    (injector.inject if action == "inject" else injector.recover)(pods, primitive)


def call_feature_flag(action: str, namespace: str, context: str, params: dict) -> None:
    """Flip one flagd flag, optionally leaving every consumer running.

    Keeping the consumer alive is the whole point for a metastable fault: restarting it throws
    away the warm state the baseline was measured on, so the fault could never outlive its own
    trigger.
    """
    from sregym.generators.fault.inject_otel import OtelFaultInjector

    flag = params.get("flag")
    if not flag:
        sys.exit("FAIL feature_flag needs --param flag=<flagName>")
    pin_context(context)
    injector = OtelFaultInjector(namespace,
                                 restart_consumers=params.get("restart", "true") != "false")
    if action == "inject":
        injector.inject_fault(flag, variant=params.get("variant"))
    else:
        injector.recover_fault(flag)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ciroos-sim-invoke")
    ap.add_argument("action", choices=["inject", "recover", "list"])
    ap.add_argument("--primitive")
    ap.add_argument("--namespace")
    ap.add_argument("--workload", default="")
    ap.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                    help="extra argument for the primitive; repeatable")
    ap.add_argument("--context", default=os.environ.get("CIROOS_SIM_CONTEXT", "evals_gke"))
    args = ap.parse_args(argv)

    if args.action == "list":
        print(f"{FEATURE_FLAG:32s} flag     flips one flagd flag; --param flag=<name> "
              f"[--param variant=<v>] [--param restart=false]")
        for name, why in sorted(HOST_LEVEL.items()):
            print(f"{name:32s} host     {why}")
        for name in sorted(NAMESPACE_SAFE):
            owner = find_owner(name)
            print(f"{name:32s} {'ok' if owner else 'MISSING'}  {owner.__name__ if owner else ''}")
        for name, why in sorted(CLUSTER_SCOPED.items()):
            print(f"{name:32s} refused  {why}")
        return 0

    if not args.primitive or not args.namespace:
        sys.exit("FAIL --primitive and --namespace are required")
    if not args.namespace.startswith(NS_PREFIX):
        sys.exit(f"FAIL namespace {args.namespace!r} is outside {NS_PREFIX!r}; refusing to inject")
    if args.primitive == FEATURE_FLAG:
        call_feature_flag(args.action, args.namespace, args.context,
                          dict(kv.partition("=")[::2] for kv in args.param))
        print(f"{args.action}ed feature flag on {args.namespace} (flagd, context={args.context})")
        return 0
    if args.primitive in HOST_LEVEL:
        call_host_level(args.action, args.primitive, args.namespace, args.workload, args.context)
        print(f"{args.action}ed {args.primitive} on {args.workload} (khaos, context={args.context})")
        return 0
    if args.primitive in CLUSTER_SCOPED:
        sys.exit(f"FAIL {args.primitive} {CLUSTER_SCOPED[args.primitive]}; not runnable on a shared cluster")
    if args.primitive not in NAMESPACE_SAFE:
        sys.exit(f"FAIL {args.primitive} has not been reviewed as namespace-scoped; "
                 f"read it and add it to NAMESPACE_SAFE in {pathlib.Path(__file__).name}")

    owner = find_owner(args.primitive)
    if owner is None:
        sys.exit(f"FAIL no injector implements inject_{args.primitive}/recover_{args.primitive}")

    pin_context(args.context)
    params = dict(kv.partition("=")[::2] for kv in args.param)
    injector = owner(args.namespace)
    call(injector, args.action, args.primitive, args.workload, params)
    print(f"{args.action}ed {args.primitive} on {args.workload or args.namespace} "
          f"({owner.__name__}, context={args.context})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
