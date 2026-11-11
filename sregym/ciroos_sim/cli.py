"""CLI for the Ciroos fault simulator.

    python -m sregym.ciroos_sim.cli inventory
    python -m sregym.ciroos_sim.cli generate --seed 1337 --app astronomy-shop
    python -m sregym.ciroos_sim.cli export --out simulator.json
    python -m sregym.ciroos_sim.cli scripts --index 0 --out-dir /tmp/bundle
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import sys

from sregym.ciroos_sim import registry, runner
from sregym.ciroos_sim.generator import generate, variants_as_dicts
from sregym.ciroos_sim.inventory import build_inventory, inventory_as_dicts
from sregym.ciroos_sim.scripts import CONTRACT, render
from sregym.ciroos_sim.templates import load_templates
from sregym.ciroos_sim.validate import partition


def _ns_map(pairs) -> dict:
    """--ns astronomy-shop=orca-bench-astronomy-shop, repeatable."""
    out = {}
    for item in pairs or []:
        app, _, ns = item.partition("=")
        if not ns:
            raise SystemExit(f"--ns expects app=namespace, got {item!r}")
        out[app] = ns
    return out


def _load(args):
    inventory = build_inventory(
        args.source,
        args.context,
        namespaces=_ns_map(getattr(args, "ns", None)),
        manifest_fallback=not getattr(args, "no_manifest_fallback", False),
    )
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


def _ops(templates) -> dict:
    """template_id -> mutation op, the only thing the script renderer needs from the template."""
    return {t.id: (t.mutation or {}).get("op", "") for t in templates}


def cmd_scripts(args) -> int:
    """Write the simulate/validate/recover/verify_recovered bundle for one variant."""
    templates, inventory = _load(args)
    accepted, _ = partition(generate(templates, inventory, seed=args.seed))
    chosen = next((v for v in accepted if v.id == args.id), None) if args.id else (
        accepted[args.index] if args.index is not None and args.index < len(accepted) else None)
    if chosen is None:
        print(f"no such variant; {len(accepted)} available, e.g. {accepted[0].id if accepted else '-'}")
        return 1
    bundle = render(chosen, context=args.context, op=_ops(templates).get(chosen.template_id, ""))
    out = args.out_dir or f"./bundles/{chosen.id}"
    os.makedirs(out, exist_ok=True)
    for name, body in bundle.items():
        path = os.path.join(out, name)
        with open(path, "w") as fh:
            fh.write(body)
        if name.endswith(".sh"):
            os.chmod(path, 0o755)
    print(f"wrote {out}/ ({len(bundle)} files)")
    print(f"  {chosen.root_cause[:140]}")
    print(f"  run: cd {out} && ./simulate.sh && ./validate.sh && ./recover.sh && ./verify_recovered.sh")
    return 0


def cmd_live_validate(args) -> int:
    """Inject each named variant for real, record what it actually did, then recover."""
    templates, inventory = _load(args)
    accepted, _ = partition(generate(templates, inventory, seed=args.seed))
    ops = _ops(templates)
    chosen = [v for v in accepted if v.id in set(args.id)]
    missing = set(args.id) - {v.id for v in chosen}
    if missing:
        print(f"unknown variant(s): {sorted(missing)}")
        return 1
    root = pathlib.Path(args.out_dir or "./bundles")
    rc = 0
    for v in chosen:
        bundle = root / v.id
        bundle.mkdir(parents=True, exist_ok=True)
        for name, body in render(v, context=args.context, op=ops.get(v.template_id, "")).items():
            path = bundle / name
            path.write_text(body)
            if name.endswith(".sh"):
                os.chmod(path, 0o755)
        spec = {"id": v.id, "template_id": v.template_id, "namespace": v.namespace,
                "workload": v.workload, "container": v.container, "params": v.params,
                "symptom_class": v.symptom_class}
        # A fault injected into an already-broken workload proves nothing.
        if not runner.wait_ready(args.context, v.namespace, v.workload):
            print(f"SKIP {v.id}: {v.workload} is not Ready before injection")
            registry.stamp(spec, verdict="failed", note="workload not Ready at baseline")
            rc = 1
            continue
        print(f"=== {v.id}  ({v.template_id} -> {v.symptom_class})")
        out = runner.lifecycle(bundle, spec)
        registry.stamp(spec, verdict=out["verdict"], mutation_applied=out["mutation_applied"],
                       symptom_holds=out["symptom_holds"],
                       discriminating=out["discriminating"],
                       spec_restored=out.get("spec_restored"), clean=out.get("clean"),
                       source="live-validate")
        print(f"--> {out['verdict']}  restored={out.get('spec_restored')} clean={out.get('clean')}\n")
    print(f"registry: {registry.DEFAULT}")
    print(f"runnable now: {len(registry.runnable(variants_as_dicts(accepted)))}")
    return rc


def cmd_export(args) -> int:
    templates, inventory = _load(args)
    accepted, rejected = partition(
        generate(templates, inventory, seed=args.seed, novel_only=not args.include_public)
    )
    ops = _ops(templates)
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
        # Scripts are rendered here, server-side, so the dashboard never re-implements the
        # mutation logic in TypeScript and drift becomes impossible.
        "contract": list(CONTRACT),
        "lib": render(accepted[0], context=args.context, op=ops.get(accepted[0].template_id, ""))["_lib.py"] if accepted else "",
        "variants": [
            dict(d, scripts={k: val for k, val in render(v, context=args.context,
                                                         op=ops.get(v.template_id, "")).items()
                             if k != "_lib.py"})
            for d, v in zip(variants_as_dicts(accepted), accepted)
        ],
        "rejected": [{"id": v.id, "reason": r} for v, r in rejected],
    }
    # Carry each variant's live verdict, so the dashboard never shows a stale gate.
    registry.annotate(doc["variants"])
    runnable = sum(1 for v in doc["variants"] if registry.is_runnable(v.get("live") or {}))
    # A cluster export silently falls back to manifests when workloads are parked at 0, which
    # yields a different, unvalidated catalogue. Refuse to trade live verdicts for it.
    out = pathlib.Path(args.out)
    if out.exists() and not args.force:
        prior = json.loads(out.read_text()).get("variants", [])
        had = sum(1 for v in prior if registry.is_runnable(v.get("live") or {}))
        if had > runnable:
            raise SystemExit(
                f"refusing to overwrite {args.out}: it has {had} live-validated variants, this "
                f"export has {runnable}. Scale the app up before a cluster export, or pass --force."
            )
    json.dump(doc, open(args.out, "w"), indent=2)
    print(f"wrote {args.out}: {len(accepted)} variants, {len(rejected)} rejected, "
          f"{runnable} live-validated")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ciroos-sim")
    p.add_argument("--source", default="manifests", choices=["manifests", "cluster"])
    p.add_argument("--ns", action="append", metavar="APP=NAMESPACE",
                   help="remap an app to the namespace it is actually deployed in")
    p.add_argument("--no-manifest-fallback", action="store_true",
                   help="with --source cluster, do not silently substitute manifests")
    p.add_argument("--context", default="evals_gke")
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--include-public", action="store_true", help="also emit non-novel bindings")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("inventory"); s.add_argument("--out"); s.set_defaults(fn=cmd_inventory)
    s = sub.add_parser("generate")
    s.add_argument("--app"); s.add_argument("--limit", type=int); s.add_argument("--show-rejects", action="store_true")
    s.set_defaults(fn=cmd_generate)
    s = sub.add_parser("live-validate")
    s.add_argument("--id", action="append", required=True, help="variant id; repeatable")
    s.add_argument("--out-dir")
    s.set_defaults(fn=cmd_live_validate)
    s = sub.add_parser("export"); s.add_argument("--out", required=True)
    s.add_argument("--force", action="store_true", help="overwrite even if it loses live verdicts")
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("scripts")
    s.add_argument("--id"); s.add_argument("--index", type=int); s.add_argument("--out-dir")
    s.set_defaults(fn=cmd_scripts)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
