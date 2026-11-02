"""Validity gate: reject generated variants that cannot be a fair RCA problem.

A binding that type-checks is not automatically a usable problem. Three ways it fails:
semantically (shadowing a password does not cause wrong-upstream routing), evidentially
(breaking the telemetry stack blinds the very channel the agent needs), and trivially
(no observable symptom). Generating without this gate poisons the eval set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sregym.ciroos_sim.generator import Variant

# Keys that genuinely address a dependency — the only ones a routing/port fault can use.
ADDRESS_HINTS = ("ADDR", "HOST", "URL", "URI", "ENDPOINT", "PORT", "SERVICE", "SERVER", "DSN")
# Never mutate these: wrong mechanism, and rewriting credentials is its own hazard.
SECRET_HINTS = ("PASSWORD", "SECRET", "TOKEN", "APIKEY", "API_KEY", "CREDENTIAL", "PRIVATE")
# Telemetry/observability workloads. Breaking these removes the evidence the agent reasons
# from, so the problem becomes unsolvable by construction rather than hard.
EVIDENCE_WORKLOADS = (
    "otel", "opentelemetry", "collector", "prometheus", "grafana", "jaeger", "loki",
    "tempo", "alertmanager", "kube-state-metrics", "node-exporter", "beacon", "vector",
)
# Mutating these strands the cluster rather than the app under test.
INFRA_WORKLOADS = ("kube-", "coredns", "etcd", "operator", "cert-manager", "ingress-nginx")

ADDRESS_TEMPLATES = {"env_var_shadowing", "incorrect_port_assignment"}


@dataclass
class Verdict:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _tokens(key: str) -> set[str]:
    return {t for t in re.split(r"[^A-Z0-9]+", key.upper()) if t}


def _looks_addressy(key: str) -> bool:
    # Token-boundary match: a substring test lets OTEL_EXPORTER_... pass on "PORT".
    return bool(_tokens(key) & set(ADDRESS_HINTS))


def _is_secret(key: str) -> bool:
    k = key.upper()
    return any(h in k for h in SECRET_HINTS)


def _is_telemetry_key(key: str) -> bool:
    return bool(_tokens(key) & {"OTEL", "OTLP", "JAEGER", "PROMETHEUS", "TRACES", "METRICS"})


def _is_evidence_plane(workload: str) -> bool:
    w = workload.lower()
    return any(h in w for h in EVIDENCE_WORKLOADS)


def _is_infra(workload: str) -> bool:
    w = workload.lower()
    return any(h in w for h in INFRA_WORKLOADS)


def check(variant: Variant) -> Verdict:
    """Static gate. Live symptom confirmation is a separate, cluster-side step."""
    key = (variant.params or {}).get("env_key", "") or ""

    if _is_evidence_plane(variant.workload):
        return Verdict(False, "targets the telemetry plane; would blind the agent's evidence")
    if _is_infra(variant.workload):
        return Verdict(False, "targets cluster infrastructure rather than the app under test")
    if key and _is_secret(key):
        return Verdict(False, f"would mutate a credential ({key})")

    if key and _is_telemetry_key(key):
        return Verdict(False, f"`{key}` configures telemetry, not application behaviour")

    if variant.template_id in ADDRESS_TEMPLATES:
        if not key:
            return Verdict(False, "no environment key bound")
        if not _looks_addressy(key):
            return Verdict(False, f"`{key}` does not address a dependency; mechanism would not apply")

    if variant.template_id == "missing_env_variable" and key and not _looks_addressy(key):
        # Removing an arbitrary tuning flag usually produces no user-visible symptom.
        return Verdict(False, f"removing `{key}` has no reliable observable symptom")

    if not variant.root_cause.startswith("[fault_spec] "):
        return Verdict(False, "root cause is not in the structured fault_spec form the judge parses")

    return Verdict(True, "ok")


def partition(variants: list[Variant]) -> tuple[list[Variant], list[tuple[Variant, str]]]:
    """Split into accepted variants and (variant, reason) rejections."""
    ok: list[Variant] = []
    bad: list[tuple[Variant, str]] = []
    for v in variants:
        verdict = check(v)
        (ok.append(v) if verdict else bad.append((v, verdict.reason)))
    return ok, bad
