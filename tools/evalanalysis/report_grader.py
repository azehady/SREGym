"""Grade the Reporter's own HTML report against the case's ground truth.

The agentic HTML reporter (samay common-agents reporting, served by ixapi
`/api/v2/investigations/{id}/rca/html?design=v3`) embeds a structured world model in each report:
`window.INV` (topology nodes with per-phase state, links, an event timeline), a graded hypotheses
table, and evidence cards that cite the workspace tool-output file they quote. Because every eval
case has a known ground truth, each of those elements can be marked right or wrong:

  - topology: did the report even draw the GT component, and what state did it believe it was in?
    "drawn and called healthy" is the sharpest failure there is: the run looked straight at it.
  - hypotheses: which row names the GT, its rating and status, and the run's stated reason.
  - evidence: which cards quote the GT, and which GT-bearing tool outputs the run read but never
    put in front of the reader.
  - conclusion: the judge's D1 verdict on the headline.

Reports are fetched once and cached (report_cache/); prod0 over the public API, evals_gke from
in-cluster ixapi. Older builds serve an untabbed layout: only the headline is graded then.
"""

from __future__ import annotations

import gzip
import html as H
import json
import os
import re
import urllib.request
from pathlib import Path

from evalanalysis import trace_analyzer as T

CACHE = Path(__file__).resolve().parent / "report_cache"
PROD0 = os.environ.get("CIROOS_PROD0_BASE", "https://prod0.ciroos.ai")
EVALS_IXAPI = os.environ.get("EVAL_IXAPI", "http://ixapi-service.ciroos.svc.cluster.local")
EVALS_ORG = "b589c349-b2be-459e-8bc6-43f2e045392e"
KEY_FILE = os.path.expanduser(os.environ.get("CIROOS_PROD0_KEY", "~/.config/ciroos/prod0-key"))
STATE_RANK = {"bad": 3, "warn": 2, "ok": 1, "": 0}


def fetch(ref: str, project: int | None = None) -> str:
    """Report HTML for plane:INV, cached gzip on disk ('' when the investigation has none)."""
    plane, inv = T.split_ref(ref)
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"{plane}_{inv}.html.gz"
    if f.exists():
        return gzip.decompress(f.read_bytes()).decode()
    if plane == "prod0":
        url = f"{PROD0}/api/v2/investigations/{inv}/rca/html?design=v3"
        hdr = {"Authorization": "Bearer " + open(KEY_FILE).read().strip(),
               "x-project-id": str(project or T.PROJECT)}
    else:
        url = f"{EVALS_IXAPI}/api/v2/investigations/{inv}/rca/html?design=v3"
        hdr = {"X-Organization-ID": EVALS_ORG, "X-Project-ID": "2", "x-user-id": "eval-analysis"}
    try:
        s = urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=90).read().decode()
    except Exception:
        s = ""
    f.write_bytes(gzip.compress(s.encode()))
    return s


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _section(s: str, sid: str) -> str:
    m = re.search(rf'<section[^>]*id="{sid}"', s)
    if not m:
        return ""
    nxt = re.search(r"<section[^>]*id=", s[m.end():])
    return s[m.start(): m.end() + (nxt.start() if nxt else len(s))]


def parse(s: str) -> dict:
    """Report HTML -> {layout, headline, lead, confidence, topology, hypotheses, evidence}."""
    if not s:
        return {"layout": "none"}
    out: dict = {"layout": "tabbed" if 'id="hypotheses"' in s else "legacy",
                 "title": _text((re.search(r"<title>(.*?)</title>", s, re.S) or [None, ""])[1])}
    ov = _section(s, "overview") or s
    m = re.search(r"<h1[^>]*>(.*?)</h1>", ov, re.S)
    out["headline"] = _text(m.group(1)) if m else out["title"]
    m = re.search(r'<p class="lead">(.*?)</p>', ov, re.S)
    out["lead"] = _text(m.group(1)) if m else ""
    m = re.search(r'aria-label="Confidence".*?<text[^>]*>([0-9.]+)</text>', ov, re.S)
    out["confidence"] = float(m.group(1)) if m else None
    out["kv"] = [(_text(a), _text(b)) for a, b in re.findall(
        r'<span class="lbl">(.*?)</span>\s*<b>(.*?)</b>', ov, re.S)][:12]
    i = s.find("window.INV=")
    out["topology"] = None
    if i >= 0:
        try:
            out["topology"] = json.JSONDecoder().raw_decode(s[i + len("window.INV="):])[0]
        except ValueError:
            pass
    hyps = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", _section(s, "hypotheses"), re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 3:
            continue
        # column order varies between report builds: locate stars / status by their markup
        star_td = next((t for t in tds if 'class="stars"' in t), "")
        lit = re.sub(r"<s>.*?</s>", "", star_td, flags=re.S)
        st = re.search(r'class="status ([\w-]+)"[^>]*>(.*?)<', row)
        rest = [t for t in tds[1:] if 'class="stars"' not in t and 'class="status' not in t]
        hyps.append({"text": _text(tds[0]), "stars": lit.count("&#9733;") + lit.count("★"),
                     "status": _text(st.group(2)) if st else "",
                     "evidence": _text(rest[-1]) if rest else ""})
    out["hypotheses"] = hyps
    ev = []
    for m in re.finditer(r'<div class="card[^"]*"\s+data-workspace-file="([^"]+)"', s):
        body = s[m.end(): m.end() + 6000]
        body = body[: body.find('data-workspace-file') if 'data-workspace-file' in body else len(body)]
        lbl = re.search(r'<div class="lbl">(.*?)</div>', body, re.S)
        pre = re.search(r"<pre>(.*?)</pre>", body, re.S)
        cap = re.search(r'<p class="xs[^"]*">(.*?)</p>', body, re.S)
        pre_t = H.unescape(re.sub(r"<[^>]+>", "", pre.group(1))) if pre else ""
        ev.append({"file": m.group(1), "label": _text(lbl.group(1)) if lbl else "",
                   "command": pre_t.split("\n", 1)[0][:200], "excerpt": pre_t[:1500],
                   "caption": _text(cap.group(1)) if cap else ""})
    out["evidence"] = ev
    return out


def _gt(text: str, terms: list[str]) -> bool:
    t = (text or "").lower()
    return any(x in t for x in terms)


def _norm_cmd(c: str) -> str:
    c = re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{5}\b|-[a-z0-9]{5}\b", "", (c or "").lower())
    return re.sub(r"\s+", " ", c.replace("kubectl ", "")).strip()[:80]


def _norm_topology(t):
    """Report builds encode phases as {t,n,l,c} objects or [t, {node: state}, {link: state}]
    lists, and a node state as [sev, label, detail] or a bare sev string. Normalize both."""
    if not isinstance(t, dict) or not isinstance(t.get("nodes"), dict):
        return None
    phases = []
    for p in t.get("phases") or []:
        if isinstance(p, list) and p:
            p = {"t": str(p[0]), "n": p[1] if len(p) > 1 and isinstance(p[1], dict) else {},
                 "l": p[2] if len(p) > 2 and isinstance(p[2], dict) else {}}
        if not isinstance(p, dict):
            continue
        n = {k: ([v, ""] if isinstance(v, str) else list(v or [])) for k, v in (p.get("n") or {}).items()}
        phases.append({**p, "t": str(p.get("t", "")), "n": n})
    return {**t, "phases": phases}


def grade(rep: dict, terms: list[str], d1: float | None = None, passed: bool | None = None,
          gt_reads: list[dict] | None = None, component: list[str] | None = None) -> dict:
    """Mark every report element against the GT; returns checks + annotated elements.
    `terms` are the decisive-fact terms; `component` the GT component's own names (for the
    topology, where a node is a component, not a fact)."""
    terms = [t.lower() for t in terms]
    comp = [c.lower() for c in (component or [])] or terms
    both = sorted(set(terms) | set(comp))
    g: dict = {"layout": rep.get("layout"), "checks": [], "headline": rep.get("headline", ""),
               "lead": rep.get("lead", ""), "confidence": rep.get("confidence"), "kv": rep.get("kv", [])}
    if rep.get("layout") in (None, "none"):
        return g
    topo = _norm_topology(rep.get("topology"))
    gt_nodes = []
    if topo:
        last: dict = {}
        for ph in sorted(topo.get("phases") or [], key=lambda p: p.get("t", "")):
            for nid, st in (ph.get("n") or {}).items():
                last[nid] = st
        worst: dict = {}
        for ph in topo.get("phases") or []:
            for nid, st in (ph.get("n") or {}).items():
                if st and STATE_RANK.get(st[0], 0) > STATE_RANK.get((worst.get(nid) or [""])[0], 0):
                    worst[nid] = st
        for nid, n in (topo.get("nodes") or {}).items():
            blob = " ".join([nid, n.get("label", ""), n.get("sub", ""), n.get("role", ""),
                             json.dumps(n.get("info") or [])])
            n["gt"] = _gt(n.get("label", "") + " " + nid, comp) or _gt(blob, terms)
            n["belief"] = (worst.get(nid) or last.get(nid) or ["", ""])[:3]
            if n["gt"]:
                gt_nodes.append({"id": nid, "label": n.get("label"), "belief": n["belief"],
                                 "note": n.get("note", "")})
        g["topology"] = topo
        g["gt_events"] = [e for e in topo.get("events") or [] if len(e) > 4 and _gt(e[4], both)]
    g["gt_nodes"] = gt_nodes
    hyps = []
    for h in rep.get("hypotheses") or []:
        h = dict(h, gt=_gt(h["text"], both))
        hyps.append(h)
    g["hypotheses"] = hyps
    ev = [dict(e, gt=_gt(e["label"] + " " + e["excerpt"] + " " + e["caption"], both))
          for e in rep.get("evidence") or []]
    g["evidence"] = ev
    cited = {_norm_cmd(e["command"]) for e in ev}
    g["missed_reads"] = [r for r in (gt_reads or []) if r.get("hit")
                         and _norm_cmd(r["target"]) not in cited][:10]
    gt_h = [h for h in hyps if h["gt"]]

    def chk(key, label, ok, detail=""):
        g["checks"].append({"key": key, "label": label, "ok": ok, "detail": detail})

    if topo:
        if not gt_nodes:
            chk("drawn", "Ground-truth component drawn in the topology", False, "not in the diagram at all")
        else:
            b = gt_nodes[0]["belief"]
            chk("drawn", "Ground-truth component drawn in the topology", True, gt_nodes[0]["label"])
            state = (b[0] if b else "") or "no state"
            chk("faulty", "…and shown as faulty", state in ("bad", "warn"),
                f"report shows it as {state}" + (f": {b[1]}" if len(b) > 1 and b[1] else ""))
    if rep.get("layout") == "tabbed":
        chk("hyp", "A hypothesis involves the ground truth", bool(gt_h),
            gt_h[0]["text"][:120] if gt_h else "no row names it")
        if gt_h:
            best = max(gt_h, key=lambda h: h["stars"])
            chk("hyp_conf", "…and it is confirmed", best["status"].lower() == "confirmed",
                f"{best['status']}, {best['stars']}★: {best['evidence'][:160]}")
        chk("evidence", "An evidence card shows the ground truth", any(e["gt"] for e in ev),
            f"{sum(e['gt'] for e in ev)} of {len(ev)} cards")
    blamed = (d1 is not None and d1 >= 0.5) if d1 is not None else _gt(g["headline"] + g["lead"], terms)
    blamed = blamed if d1 is not None else _gt(g["headline"] + g["lead"], both)
    chk("conclusion", "The conclusion blames it (judge D1)", bool(blamed),
        f"D1 = {d1:.2f}" if d1 is not None else "keyword match")
    g["score"] = f"{sum(c['ok'] for c in g['checks'])}/{len(g['checks'])}"
    g["blind_spot"] = bool(gt_nodes) and (gt_nodes[0]["belief"][:1] or [""])[0] in ("ok", "")
    return g


def belief_bucket(g: dict) -> str:
    """One word for what the report believed about the GT component."""
    if not g.get("topology"):
        return "no topology"
    if not g.get("gt_nodes"):
        return "not drawn"
    s = (g["gt_nodes"][0]["belief"][:1] or [""])[0]
    return {"bad": "failing", "warn": "degraded", "ok": "healthy"}.get(s, "no state")


GRADES = Path(__file__).resolve().parent / "report_grades.json"


def build_all(planes=("prod0", "evals")) -> dict:
    """Fetch (once) and grade the report of every scored run; sequential, so prod0 is never
    hit with more than one request at a time."""
    import time
    from evalanalysis import analyzer as A, taxonomy as X
    try:
        out = json.loads(GRADES.read_text())
    except Exception:
        out = {}
    runs = [r for r in A.load_runs() if r.ref and r.plane in planes]
    for i, r in enumerate(sorted(runs, key=lambda r: r.created_at, reverse=True)):
        rep = parse(fetch(r.ref, r.project_id))
        g = grade(rep, X.gt_terms(r.case_id), r.d1, r.passed, component=X.gt_component(r.case_id))
        out[r.ref] = {"layout": g["layout"], "score": g.get("score"), "belief": belief_bucket(g),
                      "checks": {c["key"]: c["ok"] for c in g.get("checks", [])},
                      "passed": r.passed, "case_id": r.case_id, "model": r.model}
        if i % 25 == 0:
            GRADES.write_text(json.dumps(out))
        time.sleep(0.2)
    GRADES.write_text(json.dumps(out))
    return out


def load_grades() -> dict:
    try:
        return json.loads(GRADES.read_text())
    except Exception:
        return {}
