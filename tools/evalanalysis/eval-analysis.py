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

PORT = int(os.environ.get("EVAL_ANALYSIS_PORT", "8096"))
# The old keyless litellm, same path the fault chat uses; model is overridable per deploy.
LITELLM = os.environ.get("EVAL_CHAT_LITELLM", "http://litellm.ciroos.svc.cluster.local:4000")
CHAT_MODEL = os.environ.get("EVAL_CHAT_MODEL", "anthropic/claude-sonnet-4.6")
LITELLM_KEY = os.environ.get("EVAL_CHAT_LITELLM_KEY", "")

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
        if host not in {"localhost", "127.0.0.1"}:
            self.reply(403, {"error": "Local access only"})
            return False
        return True

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
        self.reply(404, {"error": "Endpoint not found"})

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
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
