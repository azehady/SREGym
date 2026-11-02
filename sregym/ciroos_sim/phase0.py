"""Phase 0: matched-pair contamination test.

Pairs each public SREGym problem with a mechanism-identical but instance-novel variant.
Running the same model over both arms, interleaved in one healthy window, measures how
much of a score is memorisation rather than reasoning.

Interleaving is not optional. Our own sweeps show infra health moves scores more than
model choice, so running the arms days apart measures the cluster, not contamination.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass

from sregym.ciroos_sim.generator import Variant, generate
from sregym.ciroos_sim.inventory import build_inventory
from sregym.ciroos_sim.templates import FaultTemplate, load_templates
from sregym.ciroos_sim.validate import partition


@dataclass
class Pair:
    template_id: str
    category: str
    fault_level: str
    public_problem_id: str      # the public SREGym problem (arm A)
    novel_variant_id: str       # our generated counterpart (arm B)
    novel_app: str
    novel_workload: str
    novel_root_cause: str


def build_pairs(seed: int = 1337, per_template: int = 2) -> list[Pair]:
    """One or more novel counterparts per public problem, same mechanism."""
    templates: list[FaultTemplate] = load_templates()
    inventory = build_inventory("manifests")
    accepted, _ = partition(generate(templates, inventory, seed=seed))
    by_template: dict[str, list[Variant]] = {}
    for v in accepted:
        by_template.setdefault(v.template_id, []).append(v)

    rng = random.Random(seed)
    pairs: list[Pair] = []
    for t in templates:
        candidates = by_template.get(t.id, [])
        if not t.public_instances or not candidates:
            continue
        for public_id in t.public_instances:
            # Prefer a counterpart in a DIFFERENT app than the public one, so the model
            # cannot reuse a memorised app/service pairing.
            pool = [v for v in candidates if v.app.replace("-", "_") not in public_id] or candidates
            for v in rng.sample(pool, min(per_template, len(pool))):
                pairs.append(Pair(
                    template_id=t.id, category=t.category, fault_level=t.fault_level,
                    public_problem_id=public_id, novel_variant_id=v.id,
                    novel_app=v.app, novel_workload=v.workload, novel_root_cause=v.root_cause,
                ))
    return pairs


def write_plan(path: str, seed: int = 1337, per_template: int = 2) -> dict:
    """Emit the run plan: the two arms, interleaved, plus the analysis contract."""
    pairs = build_pairs(seed=seed, per_template=per_template)
    schedule: list[dict] = []
    for i, p in enumerate(pairs):
        # Interleave so both arms see the same infra window.
        schedule.append({"order": 2 * i, "arm": "public", "problem_id": p.public_problem_id})
        schedule.append({"order": 2 * i + 1, "arm": "novel", "variant_id": p.novel_variant_id})
    doc = {
        "seed": seed,
        "pairs": [asdict(p) for p in pairs],
        "schedule": schedule,
        "analysis": {
            "metric": "pass rate per arm, paired by template_id",
            "signal": "public >> novel implies memorisation advantage",
            "confounds": [
                "infra health dominates scores; arms must be interleaved, never run days apart",
                "MELT must return data in both arms or the novel arm is unfairly penalised",
                "difficulty is not identical across a pair; compare within template, not globally",
            ],
        },
    }
    json.dump(doc, open(path, "w"), indent=2)
    return doc
