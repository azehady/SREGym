"""A–H failure taxonomy: where each investigation strayed from the ground truth (see PLAN.md).

Each trace is reduced once to a compact feature record (which GT / distractor terms appear in each
funnel layer, tool health, BP, rounds), cached in features_cache.json. Classification is then a pure
function of that record + the case's GT terms, so re-tuning terms never re-fetches traces.

Funnel layers:  outputs (tool results seen by K8s/MELT agents) → ledger (the findings those
agents report back) → hypotheses (board lines) → summary (final RCA from the eval DB).
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evalanalysis import analyzer as A
from evalanalysis import trace_analyzer as T

FEATURES = Path(__file__).resolve().parent / "features_cache.json"

# Decisive-evidence terms per case, taken from each problem's build_structured_root_cause
# (vendor/sregym/sregym/conductor/problems). Mechanism-specific so a pod listing alone isn't "gathered".
GT_TERMS: dict[str, list[str]] = {
    "env_variable_shadowing_astronomy_shop": ["frontend_host", "shadow", "defined twice", "duplicate env"],
    "trainticket_f22_sql_column_name_mismatch_error": ["ts-contacts", "unknown column", "column name"],
    "cronjob_sidecar_blocks_completion_hotel_reservation": ["audit-log-archiver", "fluent-bit-sidecar", "jobtemplate", "sidecar"],
    "duplicate_pvc_mounts_social_network": ["persistentvolumeclaim", "pvc", "multi-attach", "readwriteonce", "anti-affinity"],
    "duplicate_pvc_mounts_astronomy_shop": ["persistentvolumeclaim", "pvc", "multi-attach", "readwriteonce", "anti-affinity"],
    "edge_request_filter_cpu_saturation": ["regex", "waf", "backtracking", "request filter", "request-filter"],
    "finalizer_deadlock_controller_hotel_reservation": ["finalizer", "cleanup-controller", "reservation-cleanup", "clusterrole"],
    "internal_traffic_policy_local_astronomy_shop": ["internaltrafficpolicy", "internal traffic policy", "traffic policy"],
    "kafka_poison_pill_hol_block": ["orders-fulfillment", "poison", "orders-validator", "consumer lag", "deserializ"],
    "mutating_webhook_resource_limits_social_network": ["gatekeeper-mutating", "mutatingwebhook", "mutating webhook", "16mi"],
    "namespace_memory_limit": ["resourcequota", "memory-limit-quota", "must specify memory", "resource quota"],
    "network_policy_block": ["networkpolicy", "network policy", "deny-all"],
    "rolling_update_misconfigured_social_network": ["maxunavailable", "max unavailable", "custom-service", "maxsurge"],
    "search_rate_retry_collapse_hotel_reservation": ["retry", "retries", "deadline exceeded", "queue"],
    "secret_rotation_stale_env_credentials_astronomy_shop": ["password authentication failed", "credential", "secret rotat", "rotated"],
    "service_dns_resolution_failure_social_network": ["coredns", "nxdomain"],
    "service_wrong_pod_selection_hotel_reservation": ["service-route", "selector"],
    "unschedulable_incorrect_port_assignment": ["product_catalog_addr", "8082", "unschedulable"],
    "valkey_auth_disruption": ["requirepass", "noauth", "authentication required", "wrongpass", "password"],
    "postgres_lock_contention_product_catalog": ["pg_locks", "access exclusive", "pg_stat_activity", "lock"],
    "calico_route_reflector_label_drift_hotel_reservation": ["bgppeer", "route reflector", "route-reflector", "routereflector"],
    "cumulative_admission_webhook_timeout_hotel_reservation": ["mutatingwebhook", "mutating webhook", "admission webhook", "webhook"],
    "wrong_dns_policy_social_network": ["dnspolicy", "dns policy", "8.8.8.8"],
    "wrong_dns_policy_hotel_reservation": ["dnspolicy", "dns policy", "8.8.8.8"],
    "wrong_dns_policy_astronomy_shop": ["dnspolicy", "dns policy", "8.8.8.8"],
    "wrong_service_selector_social_network": ["selector"],
    "admission_webhook_outage_hotel_reservation": ["validatingwebhook", "admission webhook", "webhook"],
    "readiness_probe_misconfiguration_social_network": ["readinessprobe", "readiness probe", "readiness"],
}

# Known environment/harness artifacts that capture investigations (bucket E).
# One-line ground truth per case (component: mechanism), condensed from build_structured_root_cause.
GT_DESC: dict[str, str] = {
    "env_variable_shadowing_astronomy_shop": "deployment/frontend-proxy: the container defines FRONTEND_HOST twice; the later value `localhost` shadows the intended one, so the edge proxy routes to the wrong upstream while the pod stays Running and Ready.",
    "trainticket_f22_sql_column_name_mismatch_error": "deployment/ts-contacts-service: SQL statements reference an incorrect column name, so queries fail at execution time.",
    "cronjob_sidecar_blocks_completion_hotel_reservation": "CronJob/audit-log-archiver: its jobTemplate has a short-lived `archiver` container plus a long-running `fluent-bit-sidecar` regular container that never exits, so no Job ever completes and pods pile up every minute.",
    "duplicate_pvc_mounts_social_network": "deployment/jaeger: scaled to multiple replicas that share one ReadWriteOnce PVC under required hostname anti-affinity, so only one replica can mount it and the other stays Pending.",
    "duplicate_pvc_mounts_astronomy_shop": "A deployment scaled to multiple replicas sharing one ReadWriteOnce PVC under required anti-affinity; one replica stays Pending.",
    "edge_request_filter_cpu_saturation": "deployment/frontend-proxy: a WAF request-filter rule update introduced the regex `^([a-zA-Z]+)*$`; long near-matching `waf` values cause catastrophic backtracking and CPU saturation.",
    "finalizer_deadlock_controller_hotel_reservation": "ClusterRole of the cleanup controller was made read-only: the controller gets 403 patching ConfigMaps, cannot remove its finalizer, and the ConfigMap is stuck Terminating.",
    "internal_traffic_policy_local_astronomy_shop": "service/recommendation has spec.internalTrafficPolicy: Local with a single replica, so callers on other nodes have connections silently dropped by kube-proxy.",
    "kafka_poison_pill_hol_block": "kafka topic orders-fulfillment: a poison-pill record cannot be deserialized by consumer group orders-validator, which never commits the offset; lag grows while pods stay Ready.",
    "mutating_webhook_resource_limits_social_network": "MutatingWebhookConfiguration/gatekeeper-mutating-webhook-configuration patches every new pod's memory request/limit to 16Mi; four look-alike webhooks are inert.",
    "namespace_memory_limit": "resourcequota/memory-limit-quota requires memory declarations the workloads lack; recreating deployment/search is rejected with `must specify memory`.",
    "network_policy_block": "NetworkPolicy deny-all-recommendation blocks all ingress and egress for the recommendation pods; dependent request paths fail while pods stay Running.",
    "rolling_update_misconfigured_social_network": "deployment/custom-service: rollout strategy maxUnavailable=100% / maxSurge=0 with a hanging init container, so the update drains every old replica and sticks.",
    "search_rate_retry_collapse_hotel_reservation": "search-to-rate RPC path: a burst fills rate's bounded queue; calls exceed their deadline and search retries them, so a timeout/retry/queue feedback loop sustains the overload after traffic normalizes.",
    "secret_rotation_stale_env_credentials_astronomy_shop": "deployment/product-catalog: PostgreSQL credentials and the Secret were rotated, but the running pod keeps the connection string captured at startup and cannot reconnect.",
    "service_dns_resolution_failure_social_network": "configmap/coredns (kube-system) contains an NXDOMAIN template for one service's cluster-local name, so lookups for that service fail.",
    "service_wrong_pod_selection_hotel_reservation": "service/frontend: selector broadened to `service-route=frontend`, a label also on search pods; endpoints include a search pod that listens on 8082, not 5000.",
    "unschedulable_incorrect_port_assignment": "deployment/checkout: PRODUCT_CATALOG_ADDR points at port 8082 instead of the right one, so checkout cannot reach product-catalog.",
    "valkey_auth_disruption": "service/valkey-cart: Valkey was reconfigured at runtime to require a password (CONFIG SET requirepass) its clients lack; cart's connections are rejected with NOAUTH and cart cannot start.",
    "postgres_lock_contention_product_catalog": "postgresql: one session holds an ACCESS EXCLUSIVE lock on catalog.products; every read queues behind it. Visible only in pg_locks / pg_stat_activity.",
    "calico_route_reflector_label_drift_hotel_reservation": "BGPPeer/cluster-peer-policy selects the deprecated node-role.kubernetes.io/master label; no route reflector is selected, so cross-node routes stop propagating.",
    "cumulative_admission_webhook_timeout_hotel_reservation": "A chain of mutating admission webhooks (failurePolicy: Ignore) whose summed timeouts exceed the apiserver's ~30s admission deadline, so pod creation times out.",
    "wrong_dns_policy_social_network": "deployment/user-service: dnsPolicy None with external resolver 8.8.8.8, so cluster-internal names do not resolve.",
    "wrong_dns_policy_hotel_reservation": "deployment/profile: dnsPolicy None with external resolver 8.8.8.8, so cluster-internal names do not resolve.",
    "wrong_dns_policy_astronomy_shop": "A deployment with dnsPolicy None and external resolver 8.8.8.8, so cluster-internal names do not resolve.",
}

# Patterns require the artifact framed as a failure, not merely cited as a data source.
ARTIFACTS: dict[str, str] = {
    "otel collector / DNS flood": r"otel.?collector.{0,80}(fail|error|dns|unreachable|crash|export)|dns (lookup )?flood",
    "Helm / deploy SIGTERM": r"sigterm|helm (upgrade|release|install)|rollout restart",
    "janitor scale-down": r"janitor|scaled (down )?to (0|zero)",
    "Consul churn": r"consul.{0,60}(crash|fail|restart|unavailable|error|churn)",
    "prometheus": r"prometheus.{0,60}(down|unavailable|fail|crash|error|not running|unreachable)",
}

_TOOL_ERR = re.compile(r"^\W*(error|forbidden|not found|guardrail|rejected|ambiguous|timed out|failed)", re.I)
_BP = re.compile(r"BP id: (k8s/[\w./-]+)")
_ALL_TERMS = sorted({t for v in GT_TERMS.values() for t in v})


def _msgs(li) -> list[dict]:
    rp = li.get("request_prompt")
    if isinstance(rp, str):
        try:
            rp = json.loads(rp)
        except ValueError:
            return [{"role": "user", "content": rp}]
    return rp if isinstance(rp, list) else []


def _hits(text: str) -> list[str]:
    t = text.lower()
    return [x for x in _ALL_TERMS if x in t]


def extract(inv: str) -> dict:
    """Trace → compact features. Layer hits are over the union of every case's terms."""
    trace, source = T.fetch_any(inv)
    spans = list(T._walk(trace.get("spans") or []))
    outputs, ledger, board = [], [], []
    tool_n = tool_err = melt_n = 0
    seen = set()
    bps: list[str] = []
    orch: list = []
    rounds = 0
    for s in spans:
        ag = s.get("agent_name")
        for li in s.get("llm_interactions") or []:
            ms = _msgs(li)
            if ag in ("K8sAgent", "MELTAgent"):
                # Ledger = the findings agents report back (generate_final_response, or plain final text).
                for ch in T._safe(li.get("response") or ""):
                    msg = ch.get("message", {})
                    if msg.get("content") and not msg.get("tool_calls"):
                        ledger.append(str(msg["content"]))
                    for tc in msg.get("tool_calls") or []:
                        if tc["function"]["name"] == "generate_final_response":
                            ledger.append(tc["function"].get("arguments") or "")
                for m in ms:
                    if m.get("role") != "tool":
                        continue
                    c = str(m.get("content") or "")
                    key = hash(c[:400])
                    if key in seen:          # prompts are cumulative; count each result once
                        continue
                    seen.add(key)
                    tool_n += 1
                    melt_n += ag == "MELTAgent"
                    tool_err += bool(_TOOL_ERR.match(c[:200]))
                    outputs.append(c)
            elif ag == "Orchestrator":
                rounds += 1
                orch.append((li.get("timestamp") or "", ms))
    orch.sort(key=lambda x: x[0])
    for _, ms in orch:
        for m in ms:
            if m.get("role") == "user":
                bps += _BP.findall(str(m.get("content")))
    ledger_txt = "\n".join(dict.fromkeys(ledger))
    view = T.analyze(trace, inv)
    hyps = [{"id": h.id, "text": h.text, "status": h.status, "traj": h.status_trajectory,
             "peak": h.peak_confidence, "round": h.first_round} for h in view.hypotheses]
    return {
        "source": source, "rounds": rounds, "tool_calls": tool_n, "tool_errors": tool_err,
        "melt_calls": melt_n, "bp": sorted(set(bps)),
        "out_hits": _hits("\n".join(outputs)), "ledger_hits": _hits(ledger_txt),
        "hyps": hyps, "reads": [r["read"] for r in T.reading_chain(trace)],
    }


def _cache() -> dict:
    try:
        return json.loads(FEATURES.read_text())
    except Exception:
        return {}


def build(workers: int = 4, limit: int | None = None) -> dict:
    cache = _cache()
    c = sqlite3.connect(f"file:{A.DB}?mode=ro", uri=True)
    invs = [r[0] for r in c.execute(
        "select distinct investigation_id from eval_runs where common_score_json is not null "
        "and investigation_id like 'INV-%' order by created_at desc")]
    todo = [i for i in invs if i not in cache][:limit]
    print(f"{len(invs)} investigations, {len(todo)} to extract", flush=True)

    def one(inv):
        try:
            return inv, extract(inv)
        except Exception as e:  # keep going; record the failure
            return inv, {"error": str(e)[:200]}

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for inv, feat in ex.map(one, todo):
            cache[inv] = feat
            done += 1
            if done % 20 == 0:
                FEATURES.write_text(json.dumps(cache))
                print(f"  {done}/{len(todo)}", flush=True)
    FEATURES.write_text(json.dumps(cache))
    return cache


# ---- classification (pure) -------------------------------------------------------------------

BUCKETS = {
    "A": ("Evidence never gathered", "add the probe to the unconditional first-round sweep"),
    "B": ("Gathered, not surfaced", "evidence-extraction prompt / round-1 budget"),
    "C": ("Surfaced, mis-ranked", "ranking discipline (prompt levers failed here twice)"),
    "D": ("Confirmed, then dropped from the conclusion", "final selection / reporter"),
    "E": ("Distractor capture", "noise suppression / local-knowledge BP"),
    "F": ("Tool / capability failure", "tool fixes"),
    "G": ("BP misdirection", "BP selection"),
    "H": ("Budget exhaustion", "budget"),
    "N": ("Named as origin, judged incomplete", "characterization depth (D2/D3), not localization"),
}

# BP families that fit a Misconfiguration fault; anything else on such a case is a misdirection.
_CONFIG_BP = re.compile(r"config|spec|env|deploy|rollout|webhook|admission|rbac|quota|policy|dns|selector", re.I)


def _artifacts(summary: str) -> list[str]:
    return [k for k, rx in ARTIFACTS.items() if re.search(rx, summary, re.I)]


def classify(run, feat: dict | None, fault_type: str = "", summary: str = "", inv_status: str = "COMPLETED") -> dict:
    """One run → primary bucket + secondary flags + the funnel strip (which layers held the GT)."""
    terms = GT_TERMS.get(run.case_id, [])
    summary = (summary or "").lower()
    in_sum = bool(terms) and any(t in summary for t in terms)
    arts = _artifacts(summary)
    res = {"primary": "", "flags": [], "funnel": None, "artifacts": arts}
    empty = feat is not None and not feat.get("tool_calls") and not feat.get("hyps") and not feat.get("rounds")
    if feat is None or "error" in feat or empty:   # trace store returned nothing usable
        res["flags"].append("no_trace")
        res["primary"] = ("" if run.passed else ("N" if run.d1 >= 0.5 else "C") if in_sum
                          else "E" if arts else "?")
        return res
    hit = lambda layer: bool(set(terms) & set(feat.get(layer) or []))
    gt_hyps = [h for h in feat["hyps"] if any(t in h["text"].lower() for t in terms)]
    funnel = {"outputs": hit("out_hits"), "ledger": hit("ledger_hits"),
              "hypothesis": bool(gt_hyps),
              "confirmed": any(h["status"] == "confirmed" for h in gt_hyps), "summary": in_sum}
    res["funnel"] = funnel
    err_rate = feat["tool_errors"] / max(feat["tool_calls"], 1)
    bp_mis = "Misconfiguration" in fault_type and feat["bp"] and not any(_CONFIG_BP.search(b) for b in feat["bp"])
    if feat["tool_calls"] == 0 or err_rate >= 0.5:
        res["flags"].append("F")
    if feat["melt_calls"] == 0:
        res["flags"].append("melt0")
    if bp_mis:
        res["flags"].append("G")
    if arts and not in_sum:
        res["flags"].append("E")
    if run.passed:
        return res
    status_bad = (inv_status or "COMPLETED") != "COMPLETED"
    if status_bad or not summary.strip():
        p = "H"
    elif not funnel["outputs"]:
        p = "F" if "F" in res["flags"] else "G" if bp_mis else "A"
    elif funnel["confirmed"] and (not in_sum or run.d1 < 0.5):
        p = "D"                              # confirmed on the board, not blamed in the conclusion
    elif in_sum:
        p = "N" if run.d1 >= 0.5 else "C"   # mentioned, but the judge says not blamed as origin
    elif arts:
        p = "E"
    elif funnel["ledger"] or funnel["hypothesis"]:
        p = "C"
    else:
        p = "B"
    res["primary"] = p
    return res


_LOADED: dict = {"data": None}


def load_cached(force: bool = False) -> dict:
    if force or _LOADED["data"] is None:
        _LOADED["data"] = _cache()
    return _LOADED["data"]


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        n = int(sys.argv[2]) if len(sys.argv) > 2 else None
        t0 = time.time()
        build(limit=n)
        print(f"done in {time.time()-t0:.0f}s")
