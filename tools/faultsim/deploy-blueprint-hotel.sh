#!/usr/bin/env bash
# Deploy Blueprint Hotel Reservation for the Metastable fault family.
#
# This app is a dependency of rpc_retry_storm_metastable: it is the ONLY app whose services read
# GRPC_CLIENT_TIMEOUT / GRPC_CLIENT_RETRIES_ON_ERROR, which is the mechanism behind every one of
# SREGym's Metastable problems. Its shipped manifests need three fixups to run here, so this is
# a script rather than a note.
set -euo pipefail

NS="${1:-orca-bench-blueprint-hotel}"
CTX="${CIROOS_SIM_CONTEXT:-evals_gke}"
SRC="$(cd "$(dirname "$0")/../.." && pwd)/SREGym-applications/BlueprintHotelReservation"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cp -r "$SRC/kubernetes" "$SRC/wlgen" "$STAGE/"

# 1. The wlgen is the only manifest that names a namespace, in metadata and in the address it
#    dials the frontend on.
sed -i "s/namespace: blueprint-hotel-reservation/namespace: $NS/" "$STAGE/wlgen/wlgen_proc-deployment.yaml"
sed -i "s/frontend-service\.blueprint-hotel-reservation/frontend-service.$NS/" "$STAGE/wlgen/wlgen_proc-configmap.yaml"

# 2. Every service exports traces to otel-collector.observe, which does not exist on evals_gke.
#    Left alone, 18 services spam a failed DNS lookup per trace — enough kube-dns load to break
#    name resolution cluster-wide, and enough wasted CPU to pollute the amplification measurement
#    the metastable check depends on. This app ships its own jaeger on the same port.
sed -i "s|JAEGER_DIAL_ADDR: otel-collector.observe:14268|JAEGER_DIAL_ADDR: jaeger.$NS:14268|" \
  "$STAGE"/kubernetes/*/*-env-configmap.yaml "$STAGE/wlgen/wlgen_proc-configmap.yaml"

# 3. A flat generator, so the bundle's own trigger owns the spike rather than the generator's
#    built-in stable/trigger/revert cycle.
# TPUT is the base-load knob, and landing a metastable window with it is an open calibration
# problem, not a fixed value. Measured on this cluster with the 1Gi limit below: TPUT 300
# self-heals after the spike (the OOMKill wipes the retry queue), and 450 ALREADY starves at
# base load (the frontend restarts before any spike). So the window, if it exists, is below 450
# AND moves with the memory limit — the limit sets where the retry queue overflows. Finding it
# needs a joint TPUT x limit search, which is what SREGym's own calibration ladder does and
# what a validated variant here would require. 300 is the safe default; it does not validate.
sed -i "s|DURATION: 120s|DURATION: 3600s|; s|TPUT: '3000'|TPUT: '${BHOTEL_TPUT:-300}'|; s|MULTIPLIER: '6'|MULTIPLIER: '1'|" \
  "$STAGE/wlgen/wlgen_proc-configmap.yaml"

kubectl --context="$CTX" create ns "$NS" 2>/dev/null || true
# Or eval-app-janitor scales the whole namespace to 0 mid-protocol.
kubectl --context="$CTX" annotate ns "$NS" \
  "ciroos.eval/keep-until=$(( $(date -u +%s) + 86400 ))" --overwrite >/dev/null

kubectl --context="$CTX" -n "$NS" apply -R -f "$STAGE/kubernetes/"
kubectl --context="$CTX" -n "$NS" apply -f "$STAGE/wlgen/wlgen_proc-configmap.yaml" \
                                   -f "$STAGE/wlgen/wlgen_proc-deployment.yaml"
# The generator stays off: the bundle scales it up and back down around the spike.
kubectl --context="$CTX" -n "$NS" scale deploy bhotelwrk-wlgen --replicas=0

# A retry storm grows the client's queue without bound: measured at 8.5GB of a shared node's
# memory before the kernel killed the pod. A limit keeps the failure inside the pod, where it
# shows up as a fast OOMKill instead of as pressure on every other tenant.
for d in $(kubectl --context="$CTX" -n "$NS" get deploy -l configmap=rpc -o name); do
  kubectl --context="$CTX" -n "$NS" set resources "$d" \
    --limits=memory=1Gi --requests=memory=64Mi >/dev/null
done

kubectl --context="$CTX" -n "$NS" wait --for=condition=Available deploy -l configmap=rpc --timeout=300s
echo "deployed to $NS; generator parked at 0 replicas"
