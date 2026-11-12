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

NS_PREFIX = os.environ.get("CIROOS_SIM_NS_PREFIX", "orca-bench-")


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


def call(injector, action: str, primitive: str, workload: str) -> None:
    """Primitives disagree on how they name their target, so bind by parameter name."""
    method = getattr(injector, f"{action}_{primitive}")
    params = [p for p in inspect.signature(method).parameters]
    if not params:
        return method()
    if params[0] == "microservices":
        return method([workload])
    return method(workload)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ciroos-sim-invoke")
    ap.add_argument("action", choices=["inject", "recover", "list"])
    ap.add_argument("--primitive")
    ap.add_argument("--namespace")
    ap.add_argument("--workload", default="")
    ap.add_argument("--context", default=os.environ.get("CIROOS_SIM_CONTEXT", "evals_gke"))
    args = ap.parse_args(argv)

    if args.action == "list":
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
    if args.primitive in CLUSTER_SCOPED:
        sys.exit(f"FAIL {args.primitive} {CLUSTER_SCOPED[args.primitive]}; not runnable on a shared cluster")
    if args.primitive not in NAMESPACE_SAFE:
        sys.exit(f"FAIL {args.primitive} has not been reviewed as namespace-scoped; "
                 f"read it and add it to NAMESPACE_SAFE in {pathlib.Path(__file__).name}")

    owner = find_owner(args.primitive)
    if owner is None:
        sys.exit(f"FAIL no injector implements inject_{args.primitive}/recover_{args.primitive}")

    pin_context(args.context)
    injector = owner(args.namespace)
    call(injector, args.action, args.primitive, args.workload)
    print(f"{args.action}ed {args.primitive} on {args.workload or args.namespace} "
          f"({owner.__name__}, context={args.context})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
