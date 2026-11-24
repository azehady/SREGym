"""Turn one investigation trace into a failure taxonomy and a hypothesis thinking-sequence.

Given an investigation id (pulled from the prod0 agentops public API) or a saved trace file,
this extracts the orchestrator's hypothesis board as it evolved round by round: every
hypothesis it raised, how its status and confidence moved, which it confirmed, which it
rejected, and — when the case's ground truth is known — whether the one it confirmed was right,
whether it rejected the truth, or whether the truth was never hypothesized at all.

The board lives in the orchestrator's own request_prompt each round, in a fixed text shape:
    - [investigating] [hc2ed2f] an out-of-band kubectl-patch injected init container hang-init (★★☆☆☆)
         ⊢ predicts: ...
so parsing is deterministic, not an LLM re-read of the trace.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field, asdict

AGENTOPS = os.environ.get("CIROOS_AGENTOPS_BASE", "https://prod0.ciroos.ai/api/v1/agentops")
PROJECT = os.environ.get("CIROOS_AGENTOPS_PROJECT", "1190")
KEY_FILE = os.path.expanduser(os.environ.get("CIROOS_PROD0_KEY", "~/.config/ciroos/prod0-key"))
# evals_gke agentops ClickHouse (no auth); the weakness-case traces live here, not prod0.
CLICKHOUSE = os.environ.get("CIROOS_AGENTOPS_CLICKHOUSE", "http://34.118.231.230:8123")

# A board line: "- [status] [id] text (★★☆☆☆)". Stars are optional.
_HYP = re.compile(
    r"-\s*\[(investigating|confirmed|rejected|refuted|pending|new|dismissed)\]\s*"
    r"\[(\w+)\]\s*(.*?)\s*(\([★☆]{1,5}\))?\s*$",
    re.I,
)
_CONFIRMED = {"confirmed"}
_REJECTED = {"rejected", "refuted", "dismissed"}


def _token() -> str:
    try:
        return open(KEY_FILE).read().strip()
    except OSError:
        return ""


def fetch_trace(investigation_id: str) -> dict:
    """Full span tree with content, from the agentops public API (no SSO, no ClickHouse)."""
    tid = investigation_id if ":" in investigation_id else f"{investigation_id}:v1"
    url = f"{AGENTOPS}/traces?trace_id={tid}&include_content=true"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {_token()}", "x-project-id": PROJECT})
    with urllib.request.urlopen(req, timeout=120) as r:
        doc = json.loads(r.read())
    return doc.get("data", doc)


def fetch_trace_clickhouse(investigation_id: str) -> dict:
    """Reconstruct the trace from evals_gke agentops ClickHouse, where kind-plane eval runs land.

    agent_tool_calls is empty on this cluster, so the reads are in each span's `response`; the
    flat span rows are folded into the same {spans:[{agent_name, llm_interactions}]} shape the
    prod0 API returns, so one analyzer path serves both sources.
    """
    import urllib.parse
    tid = investigation_id if ":" in investigation_id else f"{investigation_id}:v1"
    sql = ("SELECT agent_name, request_prompt, response, model, timestamp, execution_time "
           f"FROM agentops.agent_session_spans WHERE trace_id='{tid}' ORDER BY timestamp "
           "FORMAT JSONEachRow")
    url = f"{CLICKHOUSE}/?query=" + urllib.parse.quote(sql)
    raw = urllib.request.urlopen(url, timeout=60).read().decode()
    spans = []
    for line in raw.strip().split("\n"):
        if not line:
            continue
        r = json.loads(line)
        spans.append({
            "agent_name": r.get("agent_name"),
            "llm_interactions": [{"request_prompt": r.get("request_prompt"),
                                  "response": r.get("response"), "model": r.get("model"),
                                  "timestamp": r.get("timestamp"), "total_tokens": 0}],
            "total_tokens": 0, "children": [],
        })
    return {"spans": spans, "total_tokens": 0, "total_duration_ms": 0}


def split_ref(ref: str) -> tuple[str | None, str]:
    """'prod0:INV-1' -> ('prod0', 'INV-1'); a bare id has no plane."""
    if ":" in ref and not ref.startswith("INV-"):
        plane, inv = ref.split(":", 1)
        return plane, inv
    return None, ref


def fetch(ref: str) -> tuple[dict, str]:
    """Fetch from the cluster the investigation ran on. INV ids collide across clusters, so a
    bare id falls back to fetch_any's guess, which can return a DIFFERENT investigation."""
    plane, inv = split_ref(ref)
    if plane == "prod0":
        return fetch_trace(inv), "prod0"
    if plane == "evals":
        return fetch_trace_clickhouse(inv), "clickhouse"
    return fetch_any(inv)


def fetch_any(investigation_id: str) -> tuple[dict, str]:
    """Try prod0 first; fall back to the evals_gke ClickHouse for kind-plane runs."""
    try:
        t = fetch_trace(investigation_id)
        if list(_walk(t.get("spans") or [])):
            return t, "prod0"
    except Exception:
        pass
    return fetch_trace_clickhouse(investigation_id), "clickhouse"


def _walk(spans):
    for s in spans:
        yield s
        yield from _walk(s.get("children") or [])


def _lines(prompt) -> list[str]:
    """Board text may arrive with real or escaped newlines depending on the span encoding."""
    s = prompt if isinstance(prompt, str) else json.dumps(prompt)
    return s.replace("\\n", "\n").split("\n")


def _parse_board(prompt) -> dict[str, tuple[str, str, int]]:
    out: dict[str, tuple[str, str, int]] = {}
    for line in _lines(prompt):
        m = _HYP.match(line.strip())
        if m:
            stars = (m.group(4) or "").count("★")
            out[m.group(2)] = (m.group(1).lower(), m.group(3).strip()[:200], stars)
    return out


@dataclass
class Hypothesis:
    id: str
    text: str
    status: str                       # final status
    status_trajectory: list[str]      # collapsed status over rounds
    peak_confidence: int              # most stars it ever reached (0-5)
    first_round: int                  # board snapshot index it first appeared in
    is_ground_truth: bool | None = None   # does its text match the case GT (when known)
    is_conclusion: bool = False       # did the agent confirm/conclude it
    final_reason: str = ""            # the review's reason for the final status


@dataclass
class TraceView:
    investigation_id: str
    rounds: int                       # orchestrator board snapshots
    agents: dict                      # agent -> span count
    total_tokens: int
    duration_ms: int
    final_summary: str
    hypotheses: list[Hypothesis]
    # the thinking-sequence verdict, set when ground truth is known:
    outcome: str = ""                 # SOLVED | REJECTED_TRUTH | NEVER_HYPOTHESIZED | WRONG_CONCLUSION | UNKNOWN_NO_GT
    gt_terms: list[str] = field(default_factory=list)
    reads: list[dict] = field(default_factory=list)   # the reading chain (ordered agent reads)
    source: str = ""                  # prod0 | clickhouse


def _gt_match(text: str, terms: list[str]) -> bool:
    t = text.lower()
    return any(term.lower() in t for term in terms)


def analyze(trace: dict, investigation_id: str, gt_terms: list[str] | None = None) -> TraceView:
    spans = list(_walk(trace.get("spans") or []))
    import collections
    agents = dict(collections.Counter(s.get("agent_name") for s in spans))

    orch = [s for s in spans if s.get("agent_name") == "Orchestrator"]
    inter = sorted(
        ((li.get("timestamp"), li) for s in orch for li in (s.get("llm_interactions") or [])),
        key=lambda x: x[0] or "")

    # Board snapshot per orchestrator round, plus the final verdict (summary) and the final
    # per-hypothesis status the review step assigns in the last interaction's response.
    snaps: list[dict] = []
    summary = ""
    final_status: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for _, li in inter:
        b = _parse_board(li.get("request_prompt") or "")
        if b:
            snaps.append(b)
        resp = li.get("response")
        if isinstance(resp, str):
            for ch in _safe(resp):
                msg = ch.get("message", {})
                # complete_investigation carries the final summary.
                for tc in (msg.get("tool_calls") or []):
                    if tc["function"]["name"] == "complete_investigation":
                        try:
                            summary = json.loads(tc["function"]["arguments"]).get("summary", summary)
                        except ValueError:
                            pass
                # The final review emits {"updates":[{"id","status","reason"}]} as content.
                content = msg.get("content")
                if isinstance(content, str) and '"updates"' in content:
                    try:
                        for u in json.loads(content).get("updates", []):
                            if u.get("id") and u.get("status"):
                                final_status[u["id"]] = u["status"].lower()
                                if u.get("reason"):
                                    reasons[u["id"]] = u["reason"]
                    except ValueError:
                        pass

    # Fold snapshots into per-hypothesis trajectories.
    hyps: dict[str, Hypothesis] = {}
    for i, b in enumerate(snaps):
        for hid, (st, txt, stars) in b.items():
            h = hyps.get(hid)
            if not h:
                h = hyps[hid] = Hypothesis(id=hid, text=txt, status=st,
                                           status_trajectory=[], peak_confidence=stars,
                                           first_round=i)
            h.text = txt or h.text
            h.status = st
            h.peak_confidence = max(h.peak_confidence, stars)
            if not h.status_trajectory or h.status_trajectory[-1] != st:
                h.status_trajectory.append(st)
    for hid, st in final_status.items():
        if hid in hyps:
            hyps[hid].status = st
            hyps[hid].final_reason = reasons.get(hid, "")
            if hyps[hid].status_trajectory[-1:] != [st]:
                hyps[hid].status_trajectory.append(st)

    terms = gt_terms or []
    for h in hyps.values():
        h.is_conclusion = h.status in _CONFIRMED
        h.is_ground_truth = _gt_match(h.text, terms) if terms else None

    hlist = sorted(hyps.values(), key=lambda h: h.first_round)
    view = TraceView(
        investigation_id=investigation_id, rounds=len(snaps), agents=agents,
        total_tokens=trace.get("total_tokens") or sum(s.get("total_tokens") or 0 for s in spans),
        duration_ms=trace.get("total_duration_ms") or 0,
        final_summary=summary, hypotheses=hlist, gt_terms=terms,
    )
    view.outcome = _outcome(hlist, summary, terms)
    return view


def _safe(resp: str) -> list:
    try:
        v = json.loads(resp)
        return v if isinstance(v, list) else [v]
    except ValueError:
        return []


def _outcome(hyps: list[Hypothesis], summary: str, terms: list[str]) -> str:
    """The thinking-sequence verdict, grounded in the case's ground truth when we have it."""
    if not terms:
        return "UNKNOWN_NO_GT"
    summary_names_gt = _gt_match(summary, terms)
    gt_hyps = [h for h in hyps if h.is_ground_truth]
    concluded = [h for h in hyps if h.is_conclusion]
    if summary_names_gt and any(h.is_conclusion for h in gt_hyps):
        return "SOLVED"                       # confirmed the right hypothesis
    if gt_hyps and all(h.status in _REJECTED for h in gt_hyps):
        return "REJECTED_TRUTH"               # raised the truth, then threw it away
    if not gt_hyps:
        return "NEVER_HYPOTHESIZED"           # never put the real cause on the board
    if concluded and not summary_names_gt:
        return "WRONG_CONCLUSION"             # confirmed a distractor
    return "UNRESOLVED"


# Normalize a tool call to a comparable "read": verb + resource kind, dropping the instance
# name, namespace and flags, so the same read in two runs compares equal.
_KUBECTL = re.compile(r"\b(get|describe|logs|top|exec)\b\s+([a-z.]+)", re.I)


def normalize_read(agent: str, tool: str, args: dict) -> str:
    cmd = args.get("cmd") or ""
    if cmd:
        m = _KUBECTL.search(cmd)
        if m:
            kind = m.group(2).split("/")[0].rstrip("s") or m.group(2)
            return f"{m.group(1).lower()} {kind}"
        return cmd.split()[0] if cmd.split() else tool
    if "pod_name" in args or args.get("tail_lines"):
        return "logs pod"
    if "query" in args:
        return "melt query"
    if args.get("kinds") or args.get("resources"):
        return "get multi"
    return tool


def reading_chain(trace: dict) -> list[dict]:
    """The ordered reads the investigating agents issued: the evidence-gathering path."""
    out: list[dict] = []
    for s in _walk(trace.get("spans") or []):
        ag = s.get("agent_name")
        if ag not in ("K8sAgent", "MELTAgent"):
            continue
        for li in (s.get("llm_interactions") or []):
            for ch in _safe(li.get("response") or ""):
                for tc in (ch.get("message", {}).get("tool_calls") or []):
                    tool = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"].get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    target = args.get("cmd") or args.get("query") or args.get("pod_name") or ""
                    out.append({"agent": ag, "tool": tool,
                                "read": normalize_read(ag, tool, args),
                                "target": str(target)[:160]})
    return out


def analyze_investigation(investigation_id: str, gt_terms: list[str] | None = None) -> TraceView:
    trace, source = fetch_any(investigation_id)
    v = analyze(trace, investigation_id, gt_terms)
    v.reads = reading_chain(trace)
    v.source = source
    return v


def as_dict(v: TraceView) -> dict:
    d = asdict(v)
    d["hypotheses"] = [asdict(h) for h in v.hypotheses]
    return d


if __name__ == "__main__":
    import sys
    inv = sys.argv[1] if len(sys.argv) > 1 else "INV-0000002349"
    terms = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    v = analyze_investigation(inv, terms)
    print(f"{inv}: {v.rounds} rounds, {len(v.hypotheses)} hypotheses, outcome={v.outcome}")
    for h in v.hypotheses:
        mark = {True: "GT", False: "  ", None: "? "}[h.is_ground_truth]
        print(f"  [{mark}] [{h.id}] {'→'.join(h.status_trajectory):28} {h.peak_confidence}* {h.text[:62]}")
