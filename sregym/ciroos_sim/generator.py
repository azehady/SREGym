"""Bind templates to inventory targets to produce novel problem variants.

Novelty is the point: the mechanism is a known-good public one, but the instance
(app, workload, container, key, value) must not be a pair that already exists in public
SREGym — otherwise a model that memorised the benchmark still has the answer.
"""

from __future__ import annotations

import hashlib
import itertools
import random
import re
from dataclasses import asdict, dataclass, field

from sregym.ciroos_sim.inventory import WorkloadTarget
from sregym.ciroos_sim.templates import FaultTemplate

# Values used to build a fault payload. Deliberately unremarkable so the symptom, not the
# value, is what gives the fault away.
SHADOW_POOL = ["localhost", "127.0.0.1", "0.0.0.0", "invalid.local", "unused.svc.cluster.local"]
UNUSED_PORTS = [9, 81, 1234, 8099, 9099, 18080, 28080, 39090]
OVERSIZED = ["cpu=64,memory=256Gi", "cpu=128,memory=512Gi", "memory=1Ti"]
# How much room over the measured base-load working set a squeezed limit keeps.
HEADROOMS = ["1.10", "1.15", "1.25"]


@dataclass
class Variant:
    id: str
    template_id: str
    mechanism: str
    category: str
    fault_level: str
    primitive: str
    app: str
    namespace: str
    workload: str
    container: str
    params: dict
    root_cause: str
    symptom_class: str
    difficulty_hint: str
    novel: bool
    provenance: dict = field(default_factory=dict)
    engine: str = "spec"
    checks: dict = field(default_factory=dict)   # declarative assertions for a primitive fault
    invoke: dict = field(default_factory=dict)   # extra args for the primitive, per action
    targets: list[dict] = field(default_factory=list)  # every workload this variant damages
    members: list[dict] = field(default_factory=list)  # sub-variants, when this is a composite
    trigger: dict = field(default_factory=dict)        # the transient push, for a metastable fault
    fidelity: str = ""                                 # "node-level" needs a cluster to itself


def _fits(template: FaultTemplate, target: WorkloadTarget, container_index: int) -> bool:
    """Static precondition check: can this template bind to this container at all."""
    c = target.containers[container_index]
    req = template.requires
    if req.get("has_liveness") and not c.has_liveness:
        return False
    if req.get("has_readiness") and not c.has_readiness:
        return False
    if req.get("has_resources") and not c.has_resources:
        return False
    if len(c.env_keys) < req.get("min_env_keys", 0):
        return False
    if len(c.ports) < req.get("min_ports", 0):
        return False
    if req.get("kind") and target.ref.split("/")[0] != req["kind"]:
        return False
    # A primitive often works on one specific dependency (valkey, the cart it serves), so a
    # template can name the workloads it is meaningful for.
    if req.get("workload_in") and target.name not in req["workload_in"]:
        return False
    return True


def _is_port_key(key: str) -> bool:
    return "PORT" in {t for t in re.split(r"[^A-Z0-9]+", key.upper()) if t}


def _payload(template: FaultTemplate, target: WorkloadTarget, c, rng: random.Random) -> dict:
    """Derive the concrete fault payload for this binding."""
    # A primitive carries its own payload; binding an env key here would only add noise.
    if template.engine == "sregym":
        return {"env_key": "", "bad_value": ""}
    source = template.mutation.get("value_from", "")
    env_key = rng.choice(c.env_keys) if c.env_keys else ""
    if source == "shadow_pool":
        # A PORT key must get a port, not a hostname, or the fault is incoherent.
        pool = UNUSED_PORTS if _is_port_key(env_key) else SHADOW_POOL
        return {"env_key": env_key, "bad_value": str(rng.choice(pool))}
    if source == "unused_port":
        return {"env_key": env_key, "bad_value": str(rng.choice(UNUSED_PORTS))}
    if source == "live_working_set":
        # No value to draw: the limit is measured on the cluster at inject time. Only how
        # tight the band is varies, which is what makes one binding harder than the next.
        return {"env_key": "", "bad_value": "", "headroom": rng.choice(HEADROOMS)}
    if source == "oversized_resources":
        return {"env_key": "", "bad_value": rng.choice(OVERSIZED)}
    return {"env_key": env_key, "bad_value": ""}


def _variant_id(template_id: str, target: WorkloadTarget, container: str, payload: dict) -> str:
    seed = f"{template_id}|{target.app}|{target.name}|{container}|{payload.get('env_key')}|{payload.get('bad_value')}"
    return f"sim_{template_id}_{target.name}_{hashlib.sha1(seed.encode()).hexdigest()[:6]}"


def _bind(block: dict, target: WorkloadTarget) -> dict:
    """Resolve {name}/{ref} in a template's checks or invoke args against its workload."""
    # Literal substitution, not str.format: a jsonpath like {.spec.replicas} is not a field.
    def sub(val):
        if not isinstance(val, str):
            return val
        return val.replace("{name}", target.name).replace("{ref}", target.ref)

    return {phase: ({k: sub(v) for k, v in spec.items()} if isinstance(spec, dict) else sub(spec))
            for phase, spec in (block or {}).items()}


def _target(template: FaultTemplate, target: WorkloadTarget) -> dict:
    """One workload a variant damages, with its checks already resolved against it."""
    return {"name": target.name, "ref": target.ref, "checks": _bind(template.checks, target)}


def _fanout(template: FaultTemplate, inventory: list[WorkloadTarget], apps, public, novel_only) -> list[Variant]:
    """One variant over several workloads: a correlated failure is a single cause with many
    victims, which a one-target binding cannot express. Sliding windows, so the sets differ."""
    out: list[Variant] = []
    groups: dict[tuple[str, str], list[WorkloadTarget]] = {}
    seen: set[str] = set()
    for t in sorted((t for t in inventory if (not apps or t.app in apps) and t.containers
                     and _fits(template, t, 0)), key=lambda t: t.name):
        # One app at a time, and each workload once: a set spanning two namespaces is not a
        # shared cause, and the same workload repeated is not a correlated failure at all.
        if t.ref in seen:
            continue
        seen.add(t.ref)
        groups.setdefault((t.app, t.namespace), []).append(t)
    windows = [g[i:i + template.fanout] for g in groups.values()
               for i in range(len(g) - template.fanout + 1)]
    for window in windows:
        head = window[0]
        novel = _is_novel(template, head, public)
        if novel_only and not novel:
            continue
        names = ", ".join(t.ref for t in window)
        root_cause = template.root_cause_template.format(
            container=head.containers[0].name, workload=names, name=head.name,
            env_key="", bad_value="")
        key = "|".join(t.name for t in window)
        out.append(Variant(
            id=f"sim_{template.id}_{head.name}_{hashlib.sha1(key.encode()).hexdigest()[:6]}",
            template_id=template.id, mechanism=template.mechanism, category=template.category,
            fault_level=template.fault_level, primitive=template.primitive, app=head.app,
            namespace=head.namespace, workload=head.ref, container=head.containers[0].name,
            params={"env_key": "", "bad_value": "", "workloads": names},
            root_cause=f"[fault_spec] component={names}; namespace={head.namespace} || {root_cause}",
            symptom_class=template.symptom_class, difficulty_hint=template.difficulty_hint,
            novel=novel, provenance={"source": "ciroos_sim", "fanout": template.fanout},
            engine=template.engine, checks=_bind(template.checks, head), invoke=template.invoke,
            targets=[_target(template, t) for t in window],
        ))
    return out


def _is_novel(template: FaultTemplate, target: WorkloadTarget, public_pairs: set[tuple[str, str]]) -> bool:
    """Novel when this (mechanism, workload) pair is not already a public SREGym problem."""
    return (template.id, target.name) not in public_pairs


def public_pairs_from(templates: list[FaultTemplate], inventory: list[WorkloadTarget]) -> set[tuple[str, str]]:
    """Approximate the public surface: a template's known instances, matched to workloads."""
    names = {t.name for t in inventory}
    pairs: set[tuple[str, str]] = set()
    for t in templates:
        for inst in t.public_instances:
            for n in names:
                if n and n.replace("-", "_") in inst.replace("-", "_"):
                    pairs.add((t.id, n))
    return pairs


def _bindings(template: FaultTemplate, inventory: list[WorkloadTarget], apps, public,
              novel_only: bool, rng: random.Random, seed: int) -> list[Variant]:
    """Every (target x container) binding of one template."""
    out: list[Variant] = []
    for target in inventory:
        if apps and target.app not in apps:
            continue
        # A spec op edits one container; a primitive acts on the workload, so binding it
        # per container would emit identical variants under different names.
        indices = [0] if template.engine == "sregym" else range(len(target.containers))
        for i in indices:
            if i >= len(target.containers):
                continue
            c = target.containers[i]
            if not _fits(template, target, i):
                continue
            novel = _is_novel(template, target, public)
            if novel_only and not novel:
                continue
            payload = _payload(template, target, c, rng)
            root_cause = template.root_cause_template.format(
                container=c.name, workload=target.ref, name=target.name,
                env_key=payload.get("env_key", ""), bad_value=payload.get("bad_value", ""),
            )
            out.append(Variant(
                id=_variant_id(template.id, target, c.name, payload),
                template_id=template.id, mechanism=template.mechanism,
                category=template.category, fault_level=template.fault_level,
                primitive=template.primitive, app=target.app, namespace=target.namespace,
                workload=target.ref, container=c.name, params=payload,
                root_cause=f"[fault_spec] component={target.ref}; namespace={target.namespace} || {root_cause}",
                symptom_class=template.symptom_class,
                difficulty_hint=template.difficulty_hint, novel=novel,
                provenance={"seed": seed, "source": "ciroos_sim"},
                engine=template.engine,
                checks=_bind(template.checks, target),
                # Left unbound: the renderer resolves {name} per target, which is the
                # only correct answer once a variant has more than one.
                invoke=template.invoke,
                targets=[_target(template, target)],
                trigger=template.trigger, fidelity=template.fidelity,
            ))
    return out


def _compose(template: FaultTemplate, pool: list[Variant], limit: int) -> list[Variant]:
    """Several unrelated faults in one incident, which is what makes them hard to tell apart.

    Members are drawn from variants that already bind on their own, so each keeps its own
    engine, namespace and checks — the renderer then runs each as its own sub-bundle.
    """
    # Imported here, not at module scope: the gate imports this module to read Variant.
    from sregym.ciroos_sim.validate import check

    wanted = template.compose.get("members") or []
    # Screen members BEFORE truncating. Taking the first twelve by id and rejecting the bad
    # ones afterwards threw away 22 of 24 composites on a four-app inventory, because the
    # alphabetically early workloads are the ones the gate most often refuses.
    lanes = [sorted((v for v in pool if v.template_id == tid and check(v)), key=lambda v: v.id)[:12]
             for tid in wanted]
    if not all(lanes):
        return []
    candidates: list[tuple[tuple, tuple[Variant, ...]]] = []
    for idx in itertools.product(*(range(len(lane)) for lane in lanes)):
        picks = tuple(lane[i] for lane, i in zip(lanes, idx))
        # Independence is the whole claim: two faults on one workload, or two instances of
        # one mechanism, are a single cause wearing two hats.
        if len({p.workload for p in picks}) < len(picks):
            continue
        if len({p.category for p in picks}) < len(picks):
            continue
        key = "|".join(sorted(f"{p.template_id}@{p.namespace}/{p.workload}" for p in picks))
        cross = len({p.namespace for p in picks}) == len(picks)
        # Cross-namespace first (one namespace invites a shared-cause explanation), then in
        # diagonal order, so a truncated list still draws evenly from every lane instead of
        # pairing the whole of one lane against the alphabetically first member of the next.
        candidates.append(((0 if cross else 1, max(idx), sum(idx), key), picks))
    out: list[Variant] = []
    for rank, picks in sorted(candidates, key=lambda c: c[0])[:limit]:
        key = rank[-1]
        head = picks[0]
        refs = "+".join(f"{p.namespace}/{p.workload}" for p in picks)
        story = "\n\n".join(
            f"Fault {i} ({p.template_id}): {p.root_cause.split('|| ', 1)[-1]}"
            for i, p in enumerate(picks, start=1))
        out.append(Variant(
            id=f"sim_{template.id}_{head.workload.split('/')[-1]}_"
               f"{hashlib.sha1(key.encode()).hexdigest()[:6]}",
            template_id=template.id, mechanism=template.mechanism, category=template.category,
            fault_level=template.fault_level, primitive=template.primitive, app=head.app,
            namespace=head.namespace, workload=head.workload, container=head.container,
            params={"env_key": "", "bad_value": "", "members": refs},
            root_cause=f"[fault_spec] component={refs}; namespace="
                       f"{','.join(sorted({p.namespace for p in picks}))} || "
                       f"{len(picks)} independent faults are active at once, with no common "
                       f"cause between them.\n\n{story}",
            symptom_class=template.symptom_class, difficulty_hint=template.difficulty_hint,
            novel=all(p.novel for p in picks),
            provenance={"source": "ciroos_sim", "compose": list(wanted),
                        "cross_namespace": len({p.namespace for p in picks}) == len(picks)},
            engine="composite", members=[asdict(p) for p in picks],
        ))
    return out


def generate(
    templates: list[FaultTemplate],
    inventory: list[WorkloadTarget],
    seed: int = 0,
    limit: int | None = None,
    apps: list[str] | None = None,
    novel_only: bool = True,
) -> list[Variant]:
    """Enumerate every valid (template x target x container) binding, deterministically."""
    rng = random.Random(seed)
    public = public_pairs_from(templates, inventory)
    out: list[Variant] = []
    composers: list[FaultTemplate] = []
    for template in templates:
        if template.compose:
            composers.append(template)
        elif template.fanout:
            out.extend(_fanout(template, inventory, apps, public, novel_only))
        else:
            out.extend(_bindings(template, inventory, apps, public, novel_only, rng, seed))
    # Composition runs last: it pairs variants, so it needs the pool already complete.
    pool = sorted(out, key=lambda v: v.id)
    for template in composers:
        out.extend(_compose(template, pool, int(template.compose.get("limit", 24))))
    out.sort(key=lambda v: v.id)
    return out[:limit] if limit else out


def variants_as_dicts(variants: list[Variant]) -> list[dict]:
    return [asdict(v) for v in variants]
