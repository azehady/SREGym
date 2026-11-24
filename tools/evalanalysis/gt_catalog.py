"""Ground truth for every SREGym case, read from the benchmark's own source.

Each problem builds its expected root cause with `build_structured_root_cause(component=...,
description=...)`, usually from f-strings over class constants (`CRONJOB_NAME = "..."`) and
`self.x = ...` assignments, with per-variant kwargs supplied in the registry lambda. This module
statically evaluates those expressions (no imports, no cluster), so the analysis gets a GT
description and matching terms for any case the eval DBs mention, not only hand-curated ones.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

SREGYM = Path(os.environ.get(
    "SREGYM_SRC", "/workspace/home/ciroos/brahma/oracle/vendor/sregym/sregym/conductor/problems"))
CACHE = Path(__file__).resolve().parent / "gt_catalog.json"
_STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "service", "deployment",
         "namespace", "pod", "pods", "its", "are", "not", "but", "while", "when", "every", "all",
         "which", "their", "them", "only", "even", "still", "than", "has", "have", "been"}


class _Eval(ast.NodeVisitor):
    """Evaluate a string expression against known names; unknown parts become ''."""

    def __init__(self, env: dict):
        self.env = env

    def ev(self, n) -> str:
        if isinstance(n, ast.Constant):
            return n.value if isinstance(n.value, str) else str(n.value)
        if isinstance(n, ast.JoinedStr):
            return "".join(self.ev(v.value) if isinstance(v, ast.FormattedValue) else self.ev(v)
                           for v in n.values)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self":
            return str(self.env.get(n.attr, ""))
        if isinstance(n, ast.Name):
            return str(self.env.get(n.id, ""))
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            return self.ev(n.left) + self.ev(n.right)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "join":
            return ""
        return ""


def _registry() -> dict[str, tuple[str, dict]]:
    """case_id -> (class name, constructor kwargs) from registry.py and every app's ports.py
    (whose entries are `source_case: ("<case_id>", ProblemClass)`)."""
    out = {}
    for f in [SREGYM / "registry.py", *SREGYM.glob("apps/*/ports.py")]:
        out.update(_scan(ast.parse(f.read_text())))
    return out


def _scan(tree) -> dict[str, tuple[str, dict]]:
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Tuple) and len(node.elts) == 2 and isinstance(node.elts[0], ast.Constant) \
                and isinstance(node.elts[0].value, str) and isinstance(node.elts[1], (ast.Name, ast.Lambda, ast.Call)):
            v = node.elts[1]
            call = v.body if isinstance(v, ast.Lambda) else v
            if isinstance(call, ast.Name):
                out[node.elts[0].value] = (call.id, {})
            elif isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                out[node.elts[0].value] = (call.func.id, {a.arg: a.value.value for a in call.keywords
                                                          if isinstance(a.value, ast.Constant)})
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                continue
            call = v.body if isinstance(v, ast.Lambda) else v
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                kw = {a.arg: a.value.value for a in call.keywords
                      if isinstance(a.value, ast.Constant)}
                out[k.value] = (call.func.id, kw)
            elif isinstance(call, ast.Name):
                out.setdefault(k.value, (call.id, {}))
    return out


def _classes() -> dict[str, tuple[ast.ClassDef, dict]]:
    """class name -> (ClassDef, module-level string constants)."""
    out = {}
    for f in SREGYM.rglob("*.py"):
        try:
            tree = ast.parse(f.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        mod = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
        for n in tree.body:
            if isinstance(n, ast.ClassDef):
                out[n.name] = (n, mod)
    return out


def _resolve(cls: ast.ClassDef, mod: dict, kwargs: dict, classes: dict, depth=0) -> dict | None:
    env = dict(mod)
    for base in cls.bases:                       # inherit constants from a parent problem
        if isinstance(base, ast.Name) and base.id in classes and depth < 3:
            pc, pm = classes[base.id]
            env.update({k: v for k, v in _consts(pc, pm).items()})
    env.update(_consts(cls, mod))
    init = next((f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == "__init__"), None)
    if init:
        for a, d in zip(reversed(init.args.args), reversed(init.args.defaults)):
            if isinstance(d, ast.Constant):
                env.setdefault(a.arg, d.value)
        env.update(kwargs)
    ev = _Eval(env)
    found = None
    chain = [cls]
    for base in cls.bases:
        if isinstance(base, ast.Name) and base.id in classes and depth < 3:
            chain.insert(0, classes[base.id][0])
    for node in (n for c in chain for n in ast.walk(c)):
        if isinstance(node, ast.Assign):           # self.x = <str expr> (in source order)
            for t in node.targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                    val = ev.ev(node.value)
                    if val:
                        env[t.attr] = val
        if isinstance(node, ast.Call) and {"component", "description"} <= {k.arg for k in node.keywords}:
            found = node                     # build_structured_root_cause / configure_port / ...
    if not found:
        return _from_docstrings(cls, classes)
    args = {k.arg: ev.ev(k.value) for k in found.keywords}
    return {"component": args.get("component", ""), "namespace": args.get("namespace", ""),
            "description": args.get("description", "")}


def _consts(cls: ast.ClassDef, mod: dict) -> dict:
    env, ev = {}, _Eval(mod)
    for n in cls.body:
        if isinstance(n, ast.Assign) and isinstance(n.value, (ast.Constant, ast.JoinedStr, ast.BinOp)):
            val = n.value.value if isinstance(n.value, ast.Constant) else ev.ev(n.value)
            for t in n.targets:
                if isinstance(t, ast.Name):
                    env[t.id] = val
    return env


def _from_docstrings(cls: ast.ClassDef, classes: dict) -> dict | None:
    """Incident-Arena style problems carry no structured root cause: the GT is the problem's
    docstring plus each fault leg's docstring and `component` (legs come from build_legs())."""
    parts, comps = [ast.get_docstring(cls) or ""], []
    for fn in cls.body:
        if isinstance(fn, ast.FunctionDef) and fn.name == "build_legs":
            for call in ast.walk(fn):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in classes:
                    leg = classes[call.func.id][0]
                    parts.append(ast.get_docstring(leg) or "")
                    comps += [n.value.value for n in leg.body if isinstance(n, ast.Assign)
                              and isinstance(n.value, ast.Constant) and any(
                                  isinstance(t, ast.Name) and t.id == "component" for t in n.targets)]
    desc = " ".join(p.replace("\n", " ") for p in parts if p).strip()
    if not desc or not comps:
        return None
    return {"component": ", ".join(comps), "namespace": "", "description": re.sub(r"\s+", " ", desc)}


def terms_for(gt: dict) -> list[str]:
    """Distinctive terms only: resource names from the component, backticked identifiers, and
    hyphenated/dotted/underscored names. Plain words ("site", "message") would match any trace."""
    ident = re.compile(r"[a-z0-9]+(?:[-._][a-z0-9]+)+")
    comp = gt.get("component", "").lower()
    names = [p.split("/")[-1].strip() for p in re.split(r"[,()]| and ", comp) if "/" in p]
    desc = gt.get("description", "")
    cands = names + [t.lower().strip(".:") for t in re.findall(r"`+([^`]{3,60})`+", desc)]
    cands += ident.findall(desc.lower())
    seen, out = set(), []
    for t in cands:
        t = t.strip()
        if not t or not t[0].isalnum() or len(t.split()) > 4 or "<" in t or len(t) < 4:
            continue
        if t in _STOP or t.startswith(("non-", "read-", "long-", "short-", "cross-", "in-", "re-",
                                       "already-", "e.g", "i.e", "per-", "well-", "pre-")):
            continue
        if " " not in t and not ident.fullmatch(t) and t not in names and not re.search(r"\d", t):
            continue                    # a single plain word
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:8]


def build(case_ids: list[str] | None = None) -> dict:
    reg, classes = _registry(), _classes()
    out = {}
    for case, (cname, kw) in reg.items():
        if case_ids and case not in case_ids:
            continue
        if cname not in classes:
            continue
        try:
            gt = _resolve(*classes[cname], kw, classes)
        except Exception:
            gt = None
        if gt and gt["description"]:
            gt["terms"] = terms_for(gt)
            out[case] = gt
    CACHE.write_text(json.dumps(out, indent=1))
    return out


def load() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return build() if SREGYM.exists() else {}


if __name__ == "__main__":
    d = build()
    print(len(d), "cases with ground truth")
    for k in list(d)[:6]:
        print(k, "|", d[k]["component"], "|", d[k]["terms"], "|", d[k]["description"][:120])
