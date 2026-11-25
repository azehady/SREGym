#!/usr/bin/env python3
"""Loopback service behind the Evaluations > Analysis tab, and its Ask-Ciroos eval chat.

Two surfaces over the same analyzer:
  - Read endpoints the Analysis page renders (contrastable cases, a case's funnel + buckets,
    sub-question discrimination, a two-run compare).
  - A chat endpoint that hands those same functions to litellm as tools, so "why did valkey
    fail" is answered from the eval DB rather than from the model's imagination. The agent
    gets named tools, not free-text SQL, so every answer is grounded and auditable — the same
    reason the fault builder emits a spec patch, not raw YAML.

Loopback only, proxied by the fault-sim dev server. It reads evals.sqlite3 read-only and never
writes, so it is safe to run anywhere the DB is mounted.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evalanalysis import analyzer as A  # noqa: E402
from evalanalysis import trace_analyzer as T  # noqa: E402
from evalanalysis import findings as FD  # noqa: E402
from evalanalysis import taxonomy as X  # noqa: E402
from evalanalysis import timeline as TL  # noqa: E402
from evalanalysis import prod0_sync as P0  # noqa: E402
from evalanalysis import report_grader as RG  # noqa: E402


def _gt_for_investigation(inv: str, override: str = "") -> tuple[list[str], str]:
    """Ground-truth terms for a trace: an explicit override, else the case's curated terms
    found by looking the investigation up in the local eval DB."""
    if override:
        return [t.strip() for t in override.split(",") if t.strip()], ""
    try:
        import sqlite3
        c = A._conn()
        row = c.execute("select case_id from eval_runs where investigation_id=? limit 1",
                        (inv,)).fetchone()
        if row and row[0] in X.GT_TERMS:
            return X.GT_TERMS[row[0]], row[0]
        return [], (row[0] if row else "")
    except Exception:
        return [], ""

PORT = int(os.environ.get("EVAL_ANALYSIS_PORT", "8096"))
# The old keyless litellm, same path the fault chat uses; model is overridable per deploy.
LITELLM = os.environ.get("EVAL_CHAT_LITELLM", "http://litellm-service.ciroos.svc.cluster.local")
CHAT_MODEL = os.environ.get("EVAL_CHAT_MODEL", "anthropic/claude-sonnet-4.6")
LITELLM_KEY = os.environ.get("EVAL_CHAT_LITELLM_KEY", "")
BIND = os.environ.get("EVAL_ANALYSIS_BIND", "127.0.0.1")
ALLOW_TUNNEL = os.environ.get("EVAL_ANALYSIS_ALLOW_TUNNEL", "").lower() in ("1", "true", "yes")

# The tools the chat agent may call. Each maps to an analyzer function; args are validated here
# so a hallucinated case id returns an empty result rather than an error the model loops on.
TOOLS = {
    "list_contrastable_cases": lambda **_: A.contrastable_cases(),
    "case_summary": lambda case_id, **_: A.case_summary(case_id),
    "subquestion_discrimination": lambda case_id, **_: A.subquestion_discrimination(case_id),
    "compare_runs": lambda run_a, run_b, **_: A.compare(run_a, run_b),
    "list_runs": lambda case_id=None, model=None, **_: [
        {"run_id": r.run_id, "model": (r.model or "").split("/")[-1], "passed": r.passed,
         "bucket": r.bucket, "composite": round(r.composite, 2), "names_gt": r.summary_names_gt}
        for r in A.load_runs(case_id, model)][:200],
}

TOOLS["round_detail"] = lambda investigation_id, round, **_: next(
    (r for r in _inv_payload(investigation_id)["timeline"]["rounds"] if r["n"] == int(round)),
    {"error": "no such round"})
TOOLS["grep_trace"] = lambda investigation_id, pattern, **_: TL.grep(_resolve(investigation_id), pattern)

TOOL_SCHEMA = [
    {"type": "function", "function": {
        "name": "round_detail",
        "description": "Full detail of one round of an investigation: tasks, every read with its "
                       "target, agent findings, orchestrator notes, board changes.",
        "parameters": {"type": "object", "properties": {
            "investigation_id": {"type": "string", "description": "ref like prod0:INV-0000002351"},
            "round": {"type": "integer"}},
            "required": ["investigation_id", "round"]}}},
    {"type": "function", "function": {
        "name": "grep_trace",
        "description": "Regex-search the raw tool outputs of an investigation (kubectl / logs / "
                       "metrics results). Use to check whether a fact was actually in the evidence.",
        "parameters": {"type": "object", "properties": {
            "investigation_id": {"type": "string", "description": "ref like evals:INV-0000001974"},
            "pattern": {"type": "string"}},
            "required": ["investigation_id", "pattern"]}}},
    {"type": "function", "function": {
        "name": "list_contrastable_cases",
        "description": "Cases that have both passing and failing runs, with counts. Start here.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "case_summary",
        "description": "Pass/fail split, per-model breakdown, and failure-bucket distribution "
                       "for one case. Buckets: NOT_NAMED (GT never reached the summary), "
                       "E_DISTRACTOR_CAPTURE (blamed a wrong component), CD_NAMED_BUT_FAILED "
                       "(named GT, still failed -> ranking or characterization).",
        "parameters": {"type": "object", "properties": {
            "case_id": {"type": "string"}}, "required": ["case_id"]}}},
    {"type": "function", "function": {
        "name": "subquestion_discrimination",
        "description": "Per judge sub-question, the pass-vs-fail answer gap for a case. A big "
                       "gap is the specific fact the failing runs miss.",
        "parameters": {"type": "object", "properties": {
            "case_id": {"type": "string"}}, "required": ["case_id"]}}},
    {"type": "function", "function": {
        "name": "list_runs",
        "description": "Runs for a case and/or model, each with its score, bucket, and whether "
                       "the summary named the ground-truth component.",
        "parameters": {"type": "object", "properties": {
            "case_id": {"type": "string"}, "model": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "compare_runs",
        "description": "Two runs side by side by run_id: scores, who named the GT, each one's "
                       "failing sub-questions.",
        "parameters": {"type": "object", "properties": {
            "run_a": {"type": "string"}, "run_b": {"type": "string"}},
            "required": ["run_a", "run_b"]}}},
]

SYSTEM = (
    "You analyze Ciroos RCA evaluation runs. Every eval case has a KNOWN ground truth; use it. "
    "Answer only from the tools and the supplied context; never invent run ids, scores, or case "
    "names. The judge scores localization (D1), characterization (D2) and scope (D3). Failures are "
    "classified by where the ground-truth fact drops out of the run: A never gathered, B gathered "
    "but not reported by the agent, C surfaced but mis-ranked, D confirmed then dropped from the "
    "conclusion, E distractor capture (environment artifact blamed), F tool failure, G BP "
    "misdirection, H budget exhaustion, N named but judged incomplete, M miscellaneous (no "
    "mechanical test applies: no usable trace or no GT terms). When asked why something "
    "failed, name the layer, quote the run's own reasoning, and say which harness change addresses "
    "it (first-round sweep, evidence extraction, completion gate, ranking, reporter, noise "
    "suppression, tools). Be concrete and brief. Prometheus was down on the eval cluster through "
    "2026-11-23, so metric reads before then return nothing."
)

DASHBOARD_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RCA Trace Analysis</title>
<style>
 :root{--bg:#14171b;--panel:#1c2026;--line:#2a2f37;--ink:#e8e6e0;--mut:#9aa0a8;--faint:#6b7179;
  --green:#4bb972;--red:#e0574f;--amber:#d99a4e;--blue:#6ea8d8;--mono:ui-monospace,Menlo,Consolas,monospace}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
 .wrap{max-width:1000px;margin:0 auto;padding:22px 20px 80px}
 h1{font-size:1.3rem;margin:0 0 4px}.sub{color:var(--mut);font-size:.85rem;margin-bottom:18px}
 .bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}
 input{background:var(--panel);border:1px solid var(--line);color:var(--ink);border-radius:6px;
  padding:9px 11px;font:14px var(--mono)}
 input#inv{flex:1;min-width:180px}input#gt{flex:2;min-width:160px}
 button{background:var(--blue);color:#0b0e11;border:0;border-radius:6px;padding:9px 16px;font-weight:600;cursor:pointer}
 button:disabled{opacity:.5;cursor:wait}
 .meta{display:flex;gap:18px;flex-wrap:wrap;color:var(--mut);font-size:.82rem;font-family:var(--mono);margin:6px 0 18px}
 .badge{display:inline-block;padding:3px 11px;border-radius:20px;font-size:.78rem;font-weight:700;letter-spacing:.03em}
 .SOLVED{background:#173a27;color:var(--green)}.REJECTED_TRUTH{background:#3a1a17;color:var(--red)}
 .NEVER_HYPOTHESIZED{background:#30181f;color:#e07a9a}.WRONG_CONCLUSION{background:#3a2c14;color:var(--amber)}
 .UNRESOLVED,.UNKNOWN_NO_GT{background:#23272e;color:var(--mut)}
 .sect{font-family:var(--mono);font-size:.7rem;letter-spacing:.12em;text-transform:uppercase;
  color:var(--faint);margin:26px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
 .hyp{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--line);
  border-radius:7px;padding:12px 14px;margin-bottom:10px}
 .hyp.gt-conf{border-left-color:var(--green)}.hyp.gt-rej{border-left-color:var(--red)}
 .hyp.conf{border-left-color:var(--amber)}
 .hrow{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
 .hid{font-family:var(--mono);font-size:.75rem;color:var(--faint)}
 .traj{display:flex;gap:4px;align-items:center;font-family:var(--mono);font-size:.72rem}
 .st{padding:2px 7px;border-radius:4px;background:#23272e;color:var(--mut)}
 .st.confirmed{background:#173a27;color:var(--green)}.st.rejected,.st.refuted{background:#3a1a17;color:var(--red)}
 .st.not_investigated{background:#23272e;color:var(--faint)}.st.investigating{background:#1c2b38;color:var(--blue)}
 .arrow{color:var(--faint)}.tag{font-size:.68rem;font-weight:700;padding:2px 7px;border-radius:4px}
 .tag.truth{background:#173a27;color:var(--green)}.tag.distractor{background:#30181f;color:#e07a9a}
 .htext{margin:7px 0 0;font-size:.92rem}.reason{margin:6px 0 0;font-size:.82rem;color:var(--mut);font-style:italic}
 .stars{color:var(--amber);font-size:.8rem;letter-spacing:1px}
 .summary{background:var(--panel);border:1px solid var(--line);border-radius:7px;padding:14px;
  font-size:.88rem;color:var(--mut);white-space:pre-wrap;max-height:240px;overflow:auto}
 .err{color:var(--red);font-family:var(--mono);font-size:.85rem}
 .hint{color:var(--faint);font-size:.8rem}
 .ov{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px 18px;margin-bottom:20px}
 .ov .head{font-size:.92rem;color:var(--ink);margin-bottom:12px;line-height:1.5}
 .ov .built{font-family:var(--mono);font-size:.68rem;color:var(--faint);float:right}
 table.f{border-collapse:collapse;width:100%;font-size:.82rem;margin:6px 0 14px}
 table.f th{font-family:var(--mono);font-size:.64rem;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);text-align:left;padding:6px 10px;border-bottom:1px solid var(--line)}
 table.f td{padding:6px 10px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
 table.f td.c{font-family:var(--mono);color:var(--mut)}
 .pil{display:inline-block;font-family:var(--mono);font-size:.68rem;padding:1px 6px;border-radius:4px;margin:1px 3px 1px 0;background:#23272e;color:var(--mut)}
 .pil.E{background:#30181f;color:#e07a9a}.pil.CD{background:#3a2c14;color:var(--amber)}.pil.NOT{background:#1c2b38;color:var(--blue)}
 .ovh{font-family:var(--mono);font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);margin:16px 0 4px}
</style></head><body><div class="wrap">
 <h1>RCA Trace Analysis</h1>
 <div class="sub">Any investigation &rarr; failure taxonomy + hypothesis thinking-sequence (correct vs incorrect).</div>
 <div id="overview"></div>
 <div class="bar">
  <input id="inv" placeholder="INV-0000002349" value="INV-0000002349">
  <input id="gt" placeholder="ground-truth terms (comma-sep), or leave blank for eval cases">
  <button id="go" onclick="go()">Analyze</button>
 </div>
 <div id="out"><div class="hint">Enter an investigation id. Eval cases auto-load their ground truth; for others, type the GT component terms to get a correct/incorrect verdict.</div></div>
</div>
<script>
const E=(h)=>{const d=document.createElement('div');d.innerHTML=h;return d.firstElementChild}
async function go(){
 const inv=document.getElementById('inv').value.trim(), gt=document.getElementById('gt').value.trim();
 const out=document.getElementById('out'), btn=document.getElementById('go');
 if(!inv)return; btn.disabled=true; out.innerHTML='<div class="hint">fetching trace&hellip; (large traces take ~10s)</div>';
 try{
  const r=await fetch(`/api/v1/evalanalysis/trace?investigation_id=${encodeURIComponent(inv)}`+(gt?`&gt=${encodeURIComponent(gt)}`:''));
  const j=await r.json(); if(j.status!=='success')throw new Error(j.error||'failed');
  render(j.data);
 }catch(e){out.innerHTML=`<div class="err">${e.message}</div>`}
 btn.disabled=false;
}
function render(d){
 const dur=(d.duration_ms/1000/60).toFixed(1), tok=(d.total_tokens/1e6).toFixed(1);
 const agents=Object.entries(d.agents||{}).map(([k,v])=>`${k}:${v}`).join('  ');
 let h=`<div class="hrow" style="gap:12px"><h1 style="margin:0">${d.investigation_id}</h1>`+
   `<span class="badge ${d.outcome}">${d.outcome.replace(/_/g,' ')}</span></div>`;
 if(d.case_id)h+=`<div class="hint" style="margin-top:4px">case: <span style="font-family:var(--mono)">${d.case_id}</span>`+
   (d.gt_terms&&d.gt_terms.length?` &middot; GT: ${d.gt_terms.join(', ')}`:' &middot; no ground truth (type terms for a verdict)')+`</div>`;
 h+=`<div class="meta"><span>${d.rounds} orchestrator rounds</span><span>${d.hypotheses.length} hypotheses</span>`+
   `<span>${dur} min</span><span>${tok}M tokens</span><span>${agents}</span></div>`;
 h+=`<div class="sect">Hypothesis thinking-sequence</div>`;
 for(const hy of d.hypotheses){
  const conf=hy.status==='confirmed', rej=['rejected','refuted'].includes(hy.status);
  let cls='hyp'; if(hy.is_ground_truth&&conf)cls+=' gt-conf'; else if(hy.is_ground_truth&&rej)cls+=' gt-rej'; else if(conf)cls+=' conf';
  const traj=hy.status_trajectory.map((s,i)=>(i?'<span class="arrow">&rarr;</span>':'')+`<span class="st ${s}">${s}</span>`).join('');
  let tag=''; if(hy.is_ground_truth===true)tag='<span class="tag truth">GROUND TRUTH</span>';
  else if(hy.is_ground_truth===false&&conf)tag='<span class="tag distractor">DISTRACTOR CONFIRMED</span>';
  const stars=hy.peak_confidence?`<span class="stars">${'★'.repeat(hy.peak_confidence)}${'☆'.repeat(5-hy.peak_confidence)}</span>`:'';
  h+=`<div class="${cls}"><div class="hrow"><span class="hid">[${hy.id}]</span><div class="traj">${traj}</div>${stars}${tag}</div>`+
    `<div class="htext">${esc(hy.text)}</div>`+(hy.final_reason?`<div class="reason">&#8866; ${esc(hy.final_reason)}</div>`:'')+`</div>`;
 }
 if(d.final_summary){h+=`<div class="sect">Final root-cause summary</div><div class="summary">${esc(d.final_summary)}</div>`}
 document.getElementById('out').innerHTML=h;
}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
const shortCase=(c)=>c.split('_').slice(0,2).join('_');
function bucketPills(b){const m={E_DISTRACTOR_CAPTURE:'E',CD_NAMED_BUT_FAILED:'CD',NOT_NAMED:'NOT'};
 return Object.entries(b||{}).map(([k,v])=>`<span class="pil ${m[k]||''}">${(m[k]||k)} ${v}</span>`).join('')}
async function loadOverview(){
 const el=document.getElementById('overview');
 try{
  const r=await fetch('/api/v1/evalanalysis/findings'); const j=await r.json();
  const d=j.data||{}; const cases=d.cases||{};
  if(!d.built_at){el.innerHTML='<div class="ov"><div class="hint">No cached findings yet. Run <span class="mono">python findings.py build</span>.</div></div>';return}
  let taxRows='', chainRows='';
  for(const [cid,v] of Object.entries(cases)){
   const s=v.summary||{}; const rc=v.reading_chain||{};
   taxRows+=`<tr><td class="c">${shortCase(cid)}</td><td>${s.passed}/${s.n}</td><td>${bucketPills(s.failure_buckets)}</td></tr>`;
   const p=rc.pass||{},f=rc.fail||{};
   chainRows+=`<tr><td class="c">${shortCase(cid)}</td><td>${p.reads}/${f.reads}</td><td>${p.distinct}/${f.distinct}</td>`+
     `<td>${p.touched_gt_pct}% / <b>${f.touched_gt_pct}%</b></td></tr>`;
  }
  el.innerHTML=`<div class="ov"><span class="built">built ${d.built_at||'—'}</span>`+
   `<div class="head">${esc(d.headline||'')}</div>`+
   `<div class="ovh">Failure taxonomy &middot; per case (pass/total, failure buckets)</div>`+
   `<table class="f"><tr><th>case</th><th>pass</th><th>fail buckets</th></tr>${taxRows}</table>`+
   `<div class="ovh">Reading-chain contrast &middot; pass / fail (the losers read the GT just as much)</div>`+
   `<table class="f"><tr><th>case</th><th>reads/run</th><th>distinct</th><th>touched GT (pass/fail)</th></tr>${chainRows}</table>`+
   `<div class="hint">E = distractor confirmed &middot; CD = named GT but failed &middot; NOT = GT never named. Enter an investigation below to drill into one trace.</div></div>`;
 }catch(e){el.innerHTML=`<div class="ov"><div class="err">findings: ${e.message}</div></div>`}
}
loadOverview();
document.getElementById('inv').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
</script></body></html>"""


# Deep links beside every investigation id. INV ids are per-cluster counters, so every link and
# lookup is keyed by ref = "<plane>:<INV>" (plane "prod0" | "evals", shown as prod0 / evals_gke).
UI_BASE = os.environ.get("EVAL_UI_BASE", "https://evals-dev.ruffe-bowfin.ts.net:8443")
UI_PROJECT = os.environ.get("EVAL_UI_PROJECT", "2")
PROD0_UI = os.environ.get("EVAL_PROD0_UI", "https://prod0.ciroos.ai")
MLFLOW_PATH = "/ciroos/dev-tools/mlflow"
PLANE_LABEL = {"prod0": "prod0", "evals": "evals_gke"}


def _links(ref: str, project: int | None = None) -> dict:
    plane, inv = T.split_ref(ref)
    if not inv.startswith("INV-") or not plane:
        return {}
    ml = (P0.load_mlflow().get(plane) or {}).get(inv)
    if plane == "prod0":
        base, proj = PROD0_UI, project or T.PROJECT
    else:
        base, proj = UI_BASE, UI_PROJECT
    return {"investigation": f"{base}/investigations/{inv}?projectId={proj}",
            "mlflow": (f"{base}{MLFLOW_PATH}/#/experiments/{ml[0]}/traces?workflowType=genai"
                       f"&traceId={ml[1]}") if ml else None,
            "plane": plane, "plane_label": PLANE_LABEL[plane]}


def _row(r, full: bool = False) -> dict:
    t = _tax().get(r.run_id, {})
    d = {"run_id": r.run_id, "ref": r.ref, "investigation_id": r.investigation_id,
         "plane": r.plane, "plane_label": PLANE_LABEL.get(r.plane, r.plane), "case_id": r.case_id,
         "model": (r.model or "unknown").split("/")[-1], "passed": r.passed, "scored": True,
         "composite": round(r.composite, 2), "d1": round(r.d1, 2), "d2": round(r.d2, 2),
         "d3": round(r.d3, 2), "stage": r.stage, "tax": t.get("primary", ""),
         "flags": t.get("flags", []), "funnel": t.get("funnel"),
         "names_gt": r.summary_names_gt, "bp": r.bp_id, "created_at": r.created_at[:16],
         "links": _links(r.ref, r.project_id)}
    if full:  # training/export record: what the judge said was missed, the funnel, and the trace
        d["failing_subquestions"] = r.failing_subquestions
        d["ground_truth"] = {"terms": X.gt_terms(r.case_id), "description": X.gt_desc(r.case_id)}
        d["artifacts"] = t.get("artifacts", [])
        f = X.load_cached().get(r.ref) or {}
        d["reads"] = f.get("reads")
        d["hypotheses"] = f.get("hyps")
        d["picked_bp"] = f.get("bp")
    return d


def _unscored_row(m: dict, full: bool = False) -> dict:
    """A prod0 investigation with no scored eval run: production alert or unscored eval."""
    ref = f"prod0:{m['investigation_id']}"
    f = X.load_cached().get(ref) or {}
    d = {"run_id": "", "ref": ref, "investigation_id": m["investigation_id"], "plane": "prod0",
         "plane_label": "prod0", "case_id": "", "kind": "eval, no score" if m.get("eval_correlation_id")
         else "production", "summary": m.get("summary"), "status": m.get("status"),
         "model": (m.get("model") or "default").replace("litellm:", "").split("/")[-1],
         "passed": None, "scored": False, "composite": None, "d1": None, "d2": None, "d3": None,
         "tax": "", "flags": [], "funnel": None, "created_at": (m.get("created_at") or "")[:16],
         "has_trace": bool(f.get("rounds")), "links": _links(ref, m.get("project_id"))}
    if full:
        d["reads"], d["hypotheses"], d["picked_bp"] = f.get("reads"), f.get("hyps"), f.get("bp")
        d["rca_description"] = m.get("rca_description")
    return d


def _unscored() -> list[dict]:
    """prod0 investigations no scored run covers. OPEN alerts were never investigated: skipped."""
    _tax()
    covered = set(_TAX["by_ref"])
    corr = _TAX["corr"]
    return [m for inv, m in P0.load_index().items()
            if f"prod0:{inv}" not in covered and m.get("status") in ("COMPLETED", "ERRORED")
            and m.get("eval_correlation_id") not in corr]


_TAX: dict = {"mtime": None, "runs": {}, "meta": {}, "by_ref": {}, "corr": set()}


def _tax() -> dict:
    """run_id -> A–H classification; recomputed when features_cache.json changes."""
    try:
        mt = (os.path.getmtime(X.FEATURES), os.path.getmtime(A.MIRROR) if A.MIRROR.exists() else 0)
    except OSError:
        mt = (0, 0)
    if _TAX["mtime"] == mt:
        return _TAX["runs"]
    c = A._conn()
    meta, corr = {}, set()
    for rid, rca, st, cid in c.execute(
            "select id, rca_json, investigation_status, correlation_id from eval_runs"):
        meta[rid] = (rca, st)
        corr.add(cid)
    try:
        types = json.load(open(os.path.expanduser(
            "~/ciroos/eval-runs/bp-ab/taxonomy.json")))["cases"]
    except Exception:
        types = {}
    feats = X.load_cached(force=True)
    runs = A.load_runs()
    out = {}
    for r in runs:
        rca, st = meta.get(r.run_id, (None, None))
        out[r.run_id] = X.classify(r, feats.get(r.ref), (types.get(r.case_id) or {}).get("type", ""),
                                   A._summary_text(rca), st or "COMPLETED")
    by_ref = {r.ref: r for r in sorted(runs, key=lambda r: r.created_at) if r.ref}
    _TAX.update(mtime=mt, runs=out, meta=meta, by_ref=by_ref, corr=corr)
    return out


def _resolve(ref: str) -> str:
    """Accept a bare INV id from old links: pick the scored run with that id (evals first)."""
    if T.split_ref(ref)[0]:
        return ref
    _tax()
    for plane in ("evals", "prod0"):
        if f"{plane}:{ref}" in _TAX["by_ref"]:
            return f"{plane}:{ref}"
    return f"prod0:{ref}"


def _inv_payload(ref: str) -> dict:
    """Everything the timeline / compare / chat views need for one investigation."""
    _tax()
    ref = _resolve(ref)
    r = _TAX["by_ref"].get(ref)
    case = r.case_id if r else ""
    terms = X.gt_terms(case)
    if r:
        summary = A._summary_text(_TAX["meta"].get(r.run_id, (None,))[0])
    else:
        summary = (P0.load_index().get(T.split_ref(ref)[1]) or {}).get("rca_description") or ""
    tl = dict(TL.get(ref, terms, summary))
    # Ground truth beats keywords: a summary that mentions the component but that the judge says
    # does not blame it (D1 < 0.5) has NOT reached the summary milestone.
    if r and not r.passed and r.d1 < 0.5 and tl["milestones"].get("summary"):
        tl["milestones"] = {**tl["milestones"], "summary": None}
        tl["summary_mentions_only"] = True
    m = None if r else P0.load_index().get(T.split_ref(ref)[1])
    return {"ref": ref, "case_id": case,
            "run": _row(r) if r else (_unscored_row(m) if m else None),
            "gt": {"terms": terms, "description": X.gt_desc(case)},
            "judge": r.failing_subquestions if r else [], "timeline": tl}


def _partners(ref: str, n: int = 8) -> list[dict]:
    """Same-case runs with the opposite outcome, same model first, then nearest in time."""
    _tax()
    r = _TAX["by_ref"].get(_resolve(ref))
    if not r:
        return []
    feats = X.load_cached()
    c = [o for o in _TAX["by_ref"].values() if o.case_id == r.case_id and o.passed != r.passed
         and (feats.get(o.ref) or {}).get("rounds")]
    from datetime import datetime as _dt
    when = lambda x: _dt.fromisoformat(x.created_at[:19])
    c.sort(key=lambda o: (o.model != r.model, abs((when(o) - when(r)).total_seconds())))
    return [_row(o) for o in c[:n]]


def _compare(a: str, b: str) -> dict:
    pa, pb = _inv_payload(a), _inv_payload(b)
    # the run that reached the ground truth is the reference, whichever order they came in
    if pb["run"] and pb["run"].get("passed") and not (pa["run"] and pa["run"].get("passed")):
        pa, pb = pb, pa
    diff = TL.compare(pa["timeline"], pb["timeline"])
    if pb["timeline"].get("summary_mentions_only"):
        d1 = next((j for j in pb["judge"] if str(j.get("dim")) == "D1"), None)
        diff["story"] += (" Its summary mentions the ground-truth component but does not blame it"
                          + (f"; the judge: \"{str(d1.get('evidence'))[:260]}\"" if d1 else "."))
    return {"ok": pa, "bad": pb, "diff": diff, "same_case": pa["case_id"] == pb["case_id"]}


def _report(ref: str) -> dict:
    """The run's own HTML report (Reporter-built), every element graded against ground truth."""
    _tax()
    ref = _resolve(ref)
    r = _TAX["by_ref"].get(ref)
    m = None if r else P0.load_index().get(T.split_ref(ref)[1])
    case = r.case_id if r else ""
    rep = RG.parse(RG.fetch(ref, r.project_id if r else (m or {}).get("project_id")))
    tl = TL.get(ref, X.gt_terms(case), "") if case else None
    reads = [x for rd in (tl or {}).get("rounds", []) for x in rd["reads"]] if tl else []
    g = RG.grade(rep, X.gt_terms(case), r.d1 if r else None, r.passed if r else None,
                 gt_reads=reads, component=X.gt_component(case))
    g["belief"] = RG.belief_bucket(g)
    g["has_gt"] = bool(case)
    g["ref"] = ref
    plane, inv = T.split_ref(ref)
    g["source_url"] = _links(ref, r.project_id if r else (m or {}).get("project_id")).get("investigation")
    return g


def _reports_summary() -> dict:
    """Across graded reports: what did the report believe about the GT component, and which
    report element separates passing from failing runs."""
    grades = RG.load_grades()
    belief: dict = {}
    checks: dict = {}
    layouts: dict = {}
    for ref, g in grades.items():
        side = "pass" if g.get("passed") else "fail"
        layouts[g["layout"]] = layouts.get(g["layout"], 0) + 1
        if g["layout"] in ("none", None):
            continue
        b = belief.setdefault(g["belief"], {"pass": 0, "fail": 0})
        b[side] += 1
        for k, ok in (g.get("checks") or {}).items():
            c = checks.setdefault(k, {"pass": [0, 0], "fail": [0, 0]})
            c[side][0] += bool(ok)
            c[side][1] += 1
    return {"belief": belief, "checks": checks, "layouts": layouts, "graded": len(grades)}


MAX_COMPARE = 5


def _compare_many(refs: list[str]) -> dict:
    """N investigations side by side. The first run that reached the ground truth is the
    reference (else the first given); every other run is diffed against it."""
    refs = list(dict.fromkeys(r for r in refs if r))[:MAX_COMPARE]
    ps = [_inv_payload(r) for r in refs]
    # Reference = the run closest to the truth: passed first, then the highest judge score.
    def rank(i):
        run = ps[i]["run"] or {}
        return (bool(run.get("passed")), run.get("composite") or 0, run.get("d1") or 0)
    ref_idx = max(range(len(ps)), key=rank)
    ref = ps[ref_idx]
    diffs = {}
    for i, p in enumerate(ps):
        if i == ref_idx:
            continue
        d = TL.compare(ref["timeline"], p["timeline"])
        if p["timeline"].get("summary_mentions_only"):
            d1 = next((j for j in p["judge"] if str(j.get("dim")) == "D1"), None)
            d["story"] += (" Its summary mentions the ground-truth component but does not blame it"
                           + (f"; the judge: \"{str(d1.get('evidence'))[:260]}\"" if d1 else "."))
        d.pop("mermaid", None)
        diffs[p["ref"]] = d
    cases = {p["case_id"] for p in ps}
    # The correct path (reference) step by step, and for each other run what it did at that step.
    ref_path = TL.path(ref["timeline"])
    paths = {}
    for p in ps:
        own = TL.path(p["timeline"])
        steps = {}
        for k, _ in TL.LAYERS:
            if own.get(k):
                steps[k] = {"reached": True, **own[k]}
            else:
                at = (ref_path.get(k) or {}).get("round")
                steps[k] = {"reached": False, "instead": TL.doing_instead(p["timeline"], at)}
        paths[p["ref"]] = steps
    # Each run's own report, graded; plus a component x run matrix of what each report believed.
    reports = {}
    for p in ps:
        try:
            g = _report(p["ref"])
        except Exception as e:
            g = {"layout": "error", "error": str(e)[:200]}
        reports[p["ref"]] = g
    comps: dict = {}
    for r, g in reports.items():
        for nid, n in ((g.get("topology") or {}).get("nodes") or {}).items():
            key = (n.get("label") or nid).lower()
            row = comps.setdefault(key, {"label": n.get("label") or nid, "gt": False, "cells": {}})
            names = X.gt_component(ps[0]["case_id"]) if ps else []
            row["gt"] = row["gt"] or any(c in key or c in nid.lower() for c in names)
            b = n.get("belief") or []
            row["cells"][r] = {"state": (b[0] if b else ""), "label": (b[1] if len(b) > 1 else ""),
                               "note": n.get("note", "")[:200]}
    matrix = sorted(comps.values(), key=lambda c: (not c["gt"], -len(c["cells"]), c["label"]))
    return {"runs": ps, "reference": ref["ref"], "diffs": diffs, "paths": paths,
            "reports": reports, "belief_matrix": matrix[:14],
            "layers": [{"key": k, "label": l} for k, l in TL.LAYERS],
            "same_case": len(cases) == 1, "cases": sorted(c for c in cases if c),
            "mermaid": TL.mermaid_many([p["timeline"] for p in ps], ref_idx)}


def _history(q: dict, full: bool = False) -> list[dict]:
    """Every scored run plus unscored prod0 investigations, newest first, with filters."""
    one = lambda k: (q.get(k) or [""])[0]
    out = []
    for r in A.load_runs(one("case_id") or None):
        row = _row(r, full)
        if one("model") and row["model"] != one("model"):
            continue
        if one("stage") and row["stage"] != one("stage"):
            continue
        if one("tax") and row["tax"] != one("tax"):
            continue
        if one("plane") and row["plane"] != one("plane"):
            continue
        if one("outcome") in ("nogt",) or (one("outcome") == "pass" and not r.passed) \
                or (one("outcome") == "fail" and r.passed):
            continue
        out.append(row)
    if not one("case_id") and not one("tax") and one("outcome") in ("", "nogt") \
            and one("plane") in ("", "prod0") and not one("stage"):
        for m in _unscored():
            row = _unscored_row(m, full)
            if not one("model") or row["model"] == one("model"):
                out.append(row)
    return sorted(out, key=lambda x: x["created_at"], reverse=True)


def _taxonomy() -> dict:
    """A–H: failing runs by primary bucket, a case x bucket matrix, and the funnel per bucket."""
    buckets: dict[str, list] = {}
    matrix: dict[str, dict] = {}
    recall: dict[str, list] = {}
    models: dict[str, dict] = {}
    for r in A.load_runs():
        row = _row(r)
        mm = models.setdefault(row["model"], {"pass": 0, "fail": 0})
        mm["pass" if r.passed else "fail"] += 1
        if not r.passed:
            mm[row["tax"] or "?"] = mm.get(row["tax"] or "?", 0) + 1
        m = matrix.setdefault(r.case_id, {"pass": 0, "fail": 0})
        m["pass" if r.passed else "fail"] += 1
        if r.passed:   # null test: do passing summaries actually contain the GT terms?
            if row["funnel"]:
                recall.setdefault(r.case_id, []).append(row["funnel"]["summary"])
            continue
        b = row["tax"] or "?"
        m[b] = m.get(b, 0) + 1
        buckets.setdefault(b, []).append(row)
    for cid, v in recall.items():
        matrix[cid]["term_recall"] = round(100 * sum(v) / len(v))
    layers = ("outputs", "ledger", "hypothesis", "confirmed", "summary")
    sep: dict[str, dict] = {}
    for r in A.load_runs():
        f = _tax().get(r.run_id, {}).get("funnel")
        if not f:
            continue
        side = sep.setdefault(r.case_id, {"pass": [], "fail": []})["pass" if r.passed else "fail"]
        side.append(f)
    funnel = {cid: {k: {"n": len(v), **{l: round(100 * sum(f[l] for f in v) / len(v)) for l in layers}}
                    for k, v in d.items() if v} for cid, d in sep.items()}
    return {"buckets": {k: sorted(v, key=lambda x: x["created_at"], reverse=True)
                        for k, v in buckets.items()},
            "matrix": matrix, "defs": X.BUCKETS, "funnel": funnel, "models": models,
            "gt": {c: X.gt_desc(c) for c in matrix},
            "coverage": {"with_trace": sum(1 for t in _tax().values() if t.get("funnel")),
                         "runs": len(_tax()), "unscored": len(_unscored()),
                         "planes": {p: sum(1 for r in _TAX["by_ref"].values() if r.plane == p)
                                    for p in ("evals", "prod0")}}}


def _chat_context(invs: list[str]) -> str:
    """Ground the chat in specific investigations: GT, taxonomy, judge misses, timeline digest."""
    invs = [i for i in invs if i][:4]
    if not invs:
        return ""
    ps = [_inv_payload(i) for i in invs]
    out = ["\n\nYou are discussing specific investigation(s). The ground truth is KNOWN; use it. "
           "Explain where the run's reasoning left the path to the ground truth, citing rounds, "
           "reads, hypotheses and the orchestrator's own words. Use round_detail and grep_trace "
           "to verify before claiming a fact was or was not in the evidence."]
    for p in ps:
        run = p["run"] or {}
        out.append(f"\n=== {p['ref']} | case {p['case_id'] or 'none (no ground truth)'} | "
                   f"{'PASS' if run.get('passed') else 'FAIL'} score {run.get('composite')} | "
                   f"model {run.get('model')} | taxonomy bucket {run.get('tax') or '-'} "
                   f"({(X.BUCKETS.get(run.get('tax') or '', ('',))[0])})")
        out.append(f"GROUND TRUTH: {p['gt']['description']} (terms: {', '.join(p['gt']['terms'])})"
                   if p["gt"]["description"] else "GROUND TRUTH: unknown for this investigation "
                   "(production alert or unscored run); judge the reasoning on its own evidence.")
        if p["judge"]:
            out.append("Judge sub-questions failed: " + " | ".join(
                f"{j.get('question_id')}: {str(j.get('evidence'))[:200]}" for j in p["judge"][:6]))
        out.append(TL.digest(p["timeline"], {1: 9000, 2: 6500, 3: 4500}.get(len(ps), 3500)))
    if len(ps) >= 2:
        cm = _compare_many(invs)
        out.append(f"\n=== COMPARISON (reference {cm['reference']}, the run that reached the ground truth) ===")
        for r, d in cm["diffs"].items():
            out.append(f"vs {r}: {d['story']}\n  Milestones: " + "; ".join(
                f"{x['layer']}: ref={'R%s' % x['ok']['round'] if x['ok'] else 'never'} "
                f"this={'R%s' % x['bad']['round'] if x['bad'] else 'never'}" for x in d["rows"]))
    return "\n".join(out)


def run_chat(messages: list[dict], rounds: int = 6, invs: list[str] | None = None) -> dict:
    """Drive a short tool-use loop against litellm and return the final answer plus a trace."""
    convo = [{"role": "system", "content": SYSTEM + _chat_context(invs or [])}, *messages]
    trace: list[dict] = []
    headers = {"Content-Type": "application/json"}
    if LITELLM_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_KEY}"
    for _ in range(rounds):
        req = urllib.request.Request(
            f"{LITELLM}/v1/chat/completions",
            data=json.dumps({"model": CHAT_MODEL, "messages": convo,
                             "tools": TOOL_SCHEMA}).encode(),
            headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp:
            msg = json.loads(resp.read())["choices"][0]["message"]
        convo.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"answer": msg.get("content") or "", "trace": trace}
        for call in calls:
            fn = call["function"]["name"]
            args = json.loads(call["function"].get("arguments") or "{}")
            try:
                result = TOOLS[fn](**args) if fn in TOOLS else {"error": f"unknown tool {fn}"}
            except Exception as e:  # a bad arg returns an error the model can recover from
                result = {"error": str(e)}
            trace.append({"tool": fn, "args": args})
            convo.append({"role": "tool", "tool_call_id": call["id"],
                          "content": json.dumps(result, default=str)[:12000]})
    return {"answer": "(stopped: tool loop did not converge)", "trace": trace}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def reply(self, code, payload):
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def ok(self, data):
        self.reply(200, {"status": "success", "data": data})

    def guard(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host in {"localhost", "127.0.0.1"}:
            return True
        # Read-only analysis over an internal tailnet; opt in to the eval-workspace tunnel hosts.
        if ALLOW_TUNNEL and (host.endswith(".ts.net") or host.endswith(".ciroos.local")
                             or host == "gke.eval.ciroos.local" or not host):
            return True
        self.reply(403, {"error": "Local access only"})
        return False

    def do_GET(self):
        if not self.guard():
            return
        url = urlsplit(self.path)
        path, q = url.path.rstrip("/"), parse_qs(url.query)
        if path == "/api/v1/evalanalysis/health":
            cases = A.contrastable_cases()
            return self.ok({"port": PORT, "contrastable_cases": len(cases),
                            "with_ground_truth": sum(c["has_ground_truth"] for c in cases),
                            "chat_model": CHAT_MODEL})
        if path == "/api/v1/evalanalysis/cases":
            return self.ok({"cases": A.contrastable_cases()})
        if path == "/api/v1/evalanalysis/findings":
            return self.ok(FD.load())
        if path == "/api/v1/evalanalysis/history":
            return self.ok({"runs": _history(q)})
        if path == "/api/v1/evalanalysis/taxonomy":
            return self.ok(_taxonomy())
        if path == "/api/v1/evalanalysis/export":
            body = "\n".join(json.dumps(r) for r in _history({}, full=True)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Disposition", "attachment; filename=rca-eval-runs.jsonl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        one = lambda k: (q.get(k) or [""])[0].strip()
        try:
            if path == "/api/v1/evalanalysis/timeline":
                return self.ok(_inv_payload(one("ref") or one("investigation_id")))
            if path == "/api/v1/evalanalysis/partners":
                return self.ok({"partners": _partners(one("ref") or one("investigation_id"))})
            if path == "/api/v1/evalanalysis/report":
                return self.ok(_report(one("ref")))
            if path == "/api/v1/evalanalysis/reports_summary":
                return self.ok(_reports_summary())
            if path == "/api/v1/evalanalysis/compare_many":
                return self.ok(_compare_many(one("refs").split(",")))
            if path == "/api/v1/evalanalysis/compare_traces":
                return self.ok(_compare(one("a"), one("b")))
        except Exception as e:
            return self.reply(502, {"error": f"trace fetch/parse failed: {e}"})
        if path == "/api/v1/evalanalysis/case":
            case = (q.get("case_id") or [""])[0]
            return self.ok({"summary": A.case_summary(case),
                            "discrimination": A.subquestion_discrimination(case),
                            "runs": [{"run_id": r.run_id, "model": (r.model or "").split("/")[-1],
                                      "passed": r.passed, "bucket": r.bucket,
                                      "composite": round(r.composite, 2),
                                      "names_gt": r.summary_names_gt,
                                      "investigation_id": r.investigation_id}
                                     for r in A.load_runs(case)]})
        if path == "/api/v1/evalanalysis/compare":
            return self.ok(A.compare((q.get("run_a") or [""])[0], (q.get("run_b") or [""])[0]))
        if path == "/api/v1/evalanalysis/trace":
            inv = (q.get("investigation_id") or [""])[0].strip()
            if not inv:
                return self.reply(422, {"error": "investigation_id required"})
            terms, case = _gt_for_investigation(inv, (q.get("gt") or [""])[0])
            try:
                view = T.analyze_investigation(inv, terms)
            except Exception as e:
                return self.reply(502, {"error": f"trace fetch/parse failed: {e}"})
            runs = [r for r in A.load_runs(case or None) if r.investigation_id == inv]
            tax = _tax().get(runs[0].run_id, {}) if runs else {}
            return self.ok({"case_id": case, "tax": tax,
                            "fail_terms": X.GT_TERMS.get(case, []), **T.as_dict(view)})
        if path in ("", "/dashboard", "/eval-analysis", "/api/v1/evalanalysis",
                    "/api/v1/evalanalysis/dashboard"):
            return self._page()
        self.reply(404, {"error": "Endpoint not found"})

    def _page(self):
        page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
        html = open(page, "rb").read() if os.path.exists(page) else DASHBOARD_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def do_POST(self):
        if not self.guard():
            return
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/api/v1/evalanalysis/chat":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self.reply(422, {"error": "bad JSON"})
            msgs = payload.get("messages") or (
                [{"role": "user", "content": payload["prompt"]}] if payload.get("prompt") else [])
            if not msgs:
                return self.reply(422, {"error": "messages or prompt required"})
            try:
                ctx = payload.get("context") or {}
                return self.ok(run_chat(msgs, invs=ctx.get("refs") or ctx.get("investigation_ids") or []))
            except Exception as e:
                return self.reply(502, {"error": f"chat failed: {e}"})
        self.reply(404, {"error": "Endpoint not found"})


SYNC_EVERY = int(os.environ.get("EVAL_PROD0_SYNC_S", "1800"))


def _sync_loop():
    """Keep prod0 current: campaigns, the investigation index and MLflow ids, then extract only
    traces not yet cached. Two workers, so a live prod0 investigation is never crowded."""
    import threading, time

    def run():
        while True:
            time.sleep(SYNC_EVERY)
            try:
                res = P0.sync_all()
                X.build(workers=2)
                print(f"prod0 sync: {json.dumps(res)}", file=sys.stderr)
            except Exception as e:
                print(f"prod0 sync failed: {e}", file=sys.stderr)
    if SYNC_EVERY > 0:
        threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    _sync_loop()
    cases = A.contrastable_cases()
    print(f"eval-analysis on :{PORT}  "
          f"{len(cases)} contrastable cases, "
          f"{sum(c['has_ground_truth'] for c in cases)} with ground truth, "
          f"chat={CHAT_MODEL}", file=sys.stderr)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
