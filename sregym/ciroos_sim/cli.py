"""CLI for the Ciroos fault simulator.

    python -m sregym.ciroos_sim.cli inventory
    python -m sregym.ciroos_sim.cli generate --seed 1337 --app astronomy-shop
    python -m sregym.ciroos_sim.cli export --out simulator.json
"""

from __future__ import annotations

import argparse
import collections
import json
import sys

from sregym.ciroos_sim.generator import generate, variants_as_dicts
from sregym.ciroos_sim.inventory import build_inventory, inventory_as_dicts
from sregym.ciroos_sim.templates import load_templates
from sregym.ciroos_sim.validate import partition


def _load(args):
    inventory = build_inventory(args.source, args.context)
    templates = load_templates()
    return templates, inventory


def cmd_inventory(args) -> int:
    _, inventory = _load(args)
    by_app = collections.Counter(t.app for t in inventory)
    print(f"{len(inventory)} workloads from source={args.source}")
    for app, n in by_app.most_common():
        print(f"  {app:22} {n}")
    if args.out:
        json.dump(inventory_as_dicts(inventory), open(args.out, "w"), indent=2)
        print(f"wrote {args.out}")
    return 0


def cmd_generate(args) -> int:
    templates, inventory = _load(args)
    apps = [args.app] if args.app else None
    variants = generate(templates, inventory, seed=args.seed, apps=apps, novel_only=not args.include_public)
    accepted, rejected = partition(variants)
    print(f"generated {len(variants)} | accepted {len(accepted)} | rejected {len(rejected)}")
    for cat, n in collections.Counter(v.category for v in accepted).most_common():
        print(f"  {cat:34} {n}")
    if args.show_rejects:
        for v, reason in rejected[: args.limit or 10]:
            print(f"  REJECT {v.id}: {reason}")
    for v in accepted[: args.limit or 5]:
        print(f"  {v.id}\n    {v.root_cause[:160]}")
    return 0


def cmd_export(args) -> int:
    templates, inventory = _load(args)
    accepted, rejected = partition(
        generate(templates, inventory, seed=args.seed, novel_only=not args.include_public)
    )
    doc = {
        "source": "ciroos_sim",
        "seed": args.seed,
        "counts": {
            "templates": len(templates),
            "workloads": len(inventory),
            "accepted": len(accepted),
            "rejected": len(rejected),
        },
        "templates": [
            {
                "id": t.id, "mechanism": t.mechanism, "category": t.category,
                "fault_level": t.fault_level, "primitive": t.primitive,
                "symptom_class": t.symptom_class, "difficulty": t.difficulty_hint,
            }
            for t in templates
        ],
        "apps": sorted({t.app for t in inventory}),
        "variants": variants_as_dicts(accepted),
        "rejected": [{"id": v.id, "reason": r} for v, r in rejected],
    }
    json.dump(doc, open(args.out, "w"), indent=2)
    print(f"wrote {args.out}: {len(accepted)} variants, {len(rejected)} rejected")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ciroos-sim")
    p.add_argument("--source", default="manifests", choices=["manifests", "cluster"])
    p.add_argument("--context", default="evals_gke")
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--include-public", action="store_true", help="also emit non-novel bindings")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("inventory"); s.add_argument("--out"); s.set_defaults(fn=cmd_inventory)
    s = sub.add_parser("generate")
    s.add_argument("--app"); s.add_argument("--limit", type=int); s.add_argument("--show-rejects", action="store_true")
    s.set_defaults(fn=cmd_generate)
    s = sub.add_parser("export"); s.add_argument("--out", required=True); s.set_defaults(fn=cmd_export)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
