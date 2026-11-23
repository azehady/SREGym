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


def _gt_for_investigation(inv: str, override: str = "") -> tuple[list[str], str]:
    """Ground-truth terms for a trace: an explicit override, else the case's curated terms
    found by looking the investigation up in the local eval DB."""
    if override:
        return [t.strip() for t in override.split(",") if t.strip()], ""
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{A.DB}?mode=ro", uri=True)
        row = c.execute("select case_id from eval_runs where investigation_id=? limit 1",
                        (inv,)).fetchone()
        if row and row[0] in A.GROUND_TRUTH:
            return A.GROUND_TRUTH[row[0]]["terms"], row[0]
        return [], (row[0] if row else "")
    except Exception:
        return [], ""

PORT = int(os.environ.get("EVAL_ANALYSIS_PORT", "8096"))
# The old keyless litellm, same path the fault chat uses; model is overridable per deploy.
LITELLM = os.environ.get("EVAL_CHAT_LITELLM", "http://litellm.ciroos.svc.cluster.local:4000")
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

TOOL_SCHEMA = [
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
    "You analyze Ciroos RCA evaluation runs. Answer only from the tools; never invent run ids, "
    "scores, or case names. The eval measures whether the RCA agent's final summary localizes "
    "(D1), characterizes (D2) and scopes (D3) the injected fault against a ground truth. When "
    "asked why a case fails, call case_summary for the bucket split and "
    "subquestion_discrimination for the missed fact, then say which harness layer the fix lives "
    "in: NOT_NAMED -> evidence gathering or first-round sweep; E_DISTRACTOR_CAPTURE -> noise "
    "suppression; CD_NAMED_BUT_FAILED -> ranking or the reporter. Be concrete and cite the "
    "numbers the tools return. Note when metrics were unavailable (Prometheus was down through "
    "2026-11-23), since cloud-management cases depend on them."
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


def run_chat(messages: list[dict], rounds: int = 5) -> dict:
    """Drive a short tool-use loop against litellm and return the final answer plus a trace."""
    convo = [{"role": "system", "content": SYSTEM}, *messages]
    trace: list[dict] = []
    headers = {"Content-Type": "application/json"}
    if LITELLM_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_KEY}"
    for _ in range(rounds):
        req = urllib.request.Request(
            f"{LITELLM}/v1/chat/completions",
            data=json.dumps({"model": CHAT_MODEL, "messages": convo,
                             "tools": TOOL_SCHEMA, "temperature": 0}).encode(),
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
            return self.ok({"case_id": case, **T.as_dict(view)})
        if path in ("", "/dashboard", "/eval-analysis", "/api/v1/evalanalysis",
                    "/api/v1/evalanalysis/dashboard"):
            return self._page()
        self.reply(404, {"error": "Endpoint not found"})

    def _page(self):
        html = DASHBOARD_HTML.encode()
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
                return self.ok(run_chat(msgs))
            except Exception as e:
                return self.reply(502, {"error": f"chat failed: {e}"})
        self.reply(404, {"error": "Endpoint not found"})


if __name__ == "__main__":
    cases = A.contrastable_cases()
    print(f"eval-analysis on :{PORT}  "
          f"{len(cases)} contrastable cases, "
          f"{sum(c['has_ground_truth'] for c in cases)} with ground truth, "
          f"chat={CHAT_MODEL}", file=sys.stderr)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
