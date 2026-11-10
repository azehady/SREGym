"""Target inventory: the workloads a fault template can be bound to.

Two sources. `cluster` is authoritative but needs the apps deployed; `manifests` works
offline (the eval janitor deletes app namespaces, so offline is the usual case).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

APPS_ROOT = Path(__file__).resolve().parents[2] / "SREGym-applications"

# Manifest roots per app. Helm charts are rendered; plain dirs are parsed directly.
APP_SOURCES: dict[str, dict] = {
    "astronomy-shop": {"namespace": "astronomy-shop", "chart": "astronomy-shop/charts/opentelemetry-demo"},
    "hotel-reservation": {"namespace": "hotel-reservation", "dirs": ["hotelReservation/kubernetes"]},
    "social-network": {"namespace": "social-network", "dirs": ["socialNetwork"]},
    "train-ticket": {"namespace": "train-ticket", "dirs": ["train-ticket/deployment/kubernetes-manifests"]},
}
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}


@dataclass
class Container:
    name: str
    image: str = ""
    env_keys: list[str] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)
    has_liveness: bool = False
    has_readiness: bool = False
    has_resources: bool = False


@dataclass
class WorkloadTarget:
    app: str
    namespace: str
    kind: str
    name: str
    containers: list[Container] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.kind.lower()}/{self.name}"


def _probeable(probe: dict | None) -> bool:
    """Only httpGet/tcpSocket probes have a port to repoint; an exec probe has none."""
    return bool(probe) and bool(probe.get("httpGet") or probe.get("tcpSocket"))


def _container_of(raw: dict) -> Container:
    probes = (raw.get("livenessProbe"), raw.get("readinessProbe"))
    return Container(
        name=raw.get("name", ""),
        image=raw.get("image", "") or "",
        env_keys=[e.get("name", "") for e in (raw.get("env") or []) if e.get("name")],
        ports=[p.get("containerPort") for p in (raw.get("ports") or []) if p.get("containerPort")],
        has_liveness=_probeable(probes[0]),
        has_readiness=_probeable(probes[1]),
        has_resources=bool(raw.get("resources")),
    )


def _targets_from_doc(doc: dict, app: str, namespace: str) -> WorkloadTarget | None:
    if not isinstance(doc, dict) or doc.get("kind") not in WORKLOAD_KINDS:
        return None
    name = (doc.get("metadata") or {}).get("name")
    spec = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
    containers = [_container_of(c) for c in (spec.get("containers") or []) if isinstance(c, dict)]
    if not name or not containers:
        return None
    return WorkloadTarget(app, namespace, doc["kind"], name, containers)


def _load_yaml_docs(text: str) -> list[dict]:
    # Helm-templated files are not valid YAML; skip them rather than fail the whole scan.
    try:
        return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError:
        return []


def _from_dirs(app: str, namespace: str, dirs: list[str]) -> list[WorkloadTarget]:
    out: list[WorkloadTarget] = []
    for rel in dirs:
        root = APPS_ROOT / rel
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.y*ml")):
            if "{{" in (text := path.read_text(errors="ignore")):
                continue
            for doc in _load_yaml_docs(text):
                if (t := _targets_from_doc(doc, app, namespace)) is not None:
                    out.append(t)
    return out


def _from_chart(app: str, namespace: str, chart: str) -> list[WorkloadTarget]:
    root = APPS_ROOT / chart
    if not (root / "Chart.yaml").is_file():
        charts = sorted(root.glob("Chart.yaml")) or sorted(root.glob("*/Chart.yaml"))
        if not charts:
            return []
        root = charts[0].parent
    proc = subprocess.run(
        ["helm", "template", app, str(root), "--namespace", namespace],
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        return []
    return [t for doc in _load_yaml_docs(proc.stdout) if (t := _targets_from_doc(doc, app, namespace))]


def _from_cluster(app: str, namespace: str, context: str) -> list[WorkloadTarget]:
    out: list[WorkloadTarget] = []
    for kind in sorted(WORKLOAD_KINDS):
        proc = subprocess.run(
            ["kubectl", f"--context={context}", "-n", namespace, "get", kind.lower(), "-o", "json"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            continue
        for doc in (json.loads(proc.stdout or "{}").get("items") or []):
            doc["kind"] = kind
            if (t := _targets_from_doc(doc, app, namespace)) is not None:
                out.append(t)
    return out


def build_inventory(
    source: str = "manifests",
    context: str = "evals_gke",
    namespaces: dict[str, str] | None = None,
    manifest_fallback: bool = True,
) -> list[WorkloadTarget]:
    """Collect bindable workloads. `namespaces` remaps app -> live namespace.

    A manifest inventory describes workloads that may not exist in any cluster, so every
    variant built from it is unrunnable until the app is deployed. With source="cluster",
    pass manifest_fallback=False to make an empty namespace an empty result instead of
    silently substituting the manifests — otherwise "cluster" can quietly mean "manifests".
    """
    targets: list[WorkloadTarget] = []
    for app, cfg in APP_SOURCES.items():
        ns = (namespaces or {}).get(app, cfg["namespace"])
        found: list[WorkloadTarget] = []
        if source == "cluster":
            found = _from_cluster(app, ns, context)
        if not found and (source != "cluster" or manifest_fallback):
            found = _from_chart(app, ns, cfg["chart"]) if "chart" in cfg else _from_dirs(app, ns, cfg.get("dirs", []))
        # One entry per workload name; manifest trees repeat the same workload across variants.
        seen: set[str] = set()
        for t in found:
            if t.name not in seen:
                seen.add(t.name)
                targets.append(t)
    return targets


def inventory_as_dicts(targets: list[WorkloadTarget]) -> list[dict]:
    return [asdict(t) for t in targets]
