"""Which generated variants have actually been injected, and what they did when they were.

Static acceptance only proves a variant's patch is well-formed against a real workload. It
cannot tell a fault from a no-op: `sim_missing_env_variable_cart_2186c6` applied cleanly and
produced no symptom at all. Only a live run separates the two, so nothing is runnable until
it carries a stamp written here.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib

DEFAULT = pathlib.Path(os.environ.get("CIROOS_SIM_REGISTRY",
                                      pathlib.Path.home() / ".ciroos-sim" / "registry.json"))

# Verdicts a live run can reach.
VALID = "valid"      # mutation applied AND the symptom holds
INERT = "inert"      # mutation applied, no symptom — the app tolerates it
FAILED = "failed"    # the mutation could not be applied at all

# Symptom classes whose check cannot distinguish a real fault from a no-op: they assert only
# that the pod is still Ready, because the mutation's effect is invisible at the k8s layer.
WEAK_SYMPTOMS = {"wrong-upstream-while-Ready", "connection-refused-to-dependency"}


def fingerprint(spec: dict) -> str:
    """Stable key for what a variant actually mutates.

    Deliberately excludes `bad_value`: it is an RNG draw from a pool of equivalent-effect
    values (any unused port refuses connections, any oversized request is unschedulable), and
    including it made every stamp expire whenever a generator change shifted the RNG stream.
    The generated id is a label, not an identity.
    """
    params = spec.get("params") or {}
    return "|".join([spec.get("template_id", ""), spec.get("namespace", ""),
                     spec.get("workload", ""), spec.get("container", ""),
                     params.get("env_key", "") or ""])


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def load(path: pathlib.Path | None = None) -> dict:
    path = path or DEFAULT
    if not path.exists():
        return {"schema": 1, "variants": {}}
    doc = json.loads(path.read_text())
    doc.setdefault("variants", {})
    return doc


def save(doc: dict, path: pathlib.Path | None = None) -> pathlib.Path:
    path = path or DEFAULT
    path.parent.mkdir(parents=True, exist_ok=True)
    doc["updated_at"] = _now()
    path.write_text(json.dumps(doc, indent=2, sort_keys=True))
    return path


def stamp(spec: dict, *, path: pathlib.Path | None = None, **fields) -> dict:
    """Record one live run's outcome, merging over any earlier stamp for the same mutation."""
    key = fingerprint(spec)
    doc = load(path)
    entry = dict(doc["variants"].get(key) or {})
    entry.update({k: v for k, v in fields.items() if v is not None})
    entry.update({"variant_id": spec.get("id"), "template_id": spec.get("template_id"),
                  "workload": spec.get("workload"), "namespace": spec.get("namespace"),
                  "container": spec.get("container"),
                  "symptom_class": spec.get("symptom_class"), "validated_at": _now()})
    doc["variants"][key] = entry
    save(doc, path)
    return entry


def verdict_for(mutation_applied: bool, symptom_holds: bool) -> str:
    if not mutation_applied:
        return FAILED
    return VALID if symptom_holds else INERT


def is_runnable(entry: dict) -> bool:
    """Live-validated, by a check that can actually tell a fault from a no-op."""
    return entry.get("verdict") == VALID and entry.get("discriminating") is not False


def runnable(variants: list[dict], path: pathlib.Path | None = None) -> list[str]:
    """Ids from THIS catalogue whose mutation has a valid stamp; nothing else is scoreable."""
    stamps = load(path)["variants"]
    return sorted(v["id"] for v in variants if is_runnable(stamps.get(fingerprint(v)) or {}))


def annotate(variants: list[dict], path: pathlib.Path | None = None) -> list[dict]:
    """Attach each variant's live stamp to its catalogue dict, for the dashboard to show."""
    stamps = load(path)["variants"]
    for v in variants:
        v["live"] = stamps.get(fingerprint(v)) or {"verdict": "untested"}
    return variants
