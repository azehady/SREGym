"""Round-by-round timeline of one investigation, and a ground-truth-anchored diff of two.

A round starts at each orchestrator dispatch (assign_task). Within it we keep what the orchestrator
intended, what the agents read, what they reported, what the orchestrator wrote down, and how the
hypothesis board moved. Every item is flagged against the case's ground-truth terms, so the GT
fact's path through the run is a set of milestones: first tool output containing it, first agent
finding, first orchestrator note, first hypothesis, confirmation, final summary.

compare(pass, fail) lines those milestones up: the first one the pass reaches and the fail does
not is where the failing run left the path, and the diff says what it did instead.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from evalanalysis import trace_analyzer as T

CACHE_DIR = Path(__file__).resolve().parent / "timeline_cache"
LAYERS = [
    ("output", "a tool output contains the GT fact"),
    ("finding", "an agent reports it back"),
    ("note", "the orchestrator writes it down"),
    ("hypothesis", "a hypothesis names it"),
    ("confirmed", "that hypothesis is confirmed"),
    ("summary", "the conclusion blames it"),
]
_DISPATCH = {"assign_task"}
_RAW: dict[str, dict] = {}      # small in-memory LRU of raw traces, for chat grep


def _ts(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "").replace(" ", "T")[:26])
    except ValueError:
        return None


def _msgs(li) -> list[dict]:
    rp = li.get("request_prompt")
    if isinstance(rp, str):
        try:
            rp = json.loads(rp)
        except ValueError:
            return []
    return rp if isinstance(rp, list) else []


def _has(text, terms) -> bool:
    t = str(text or "").lower()
    return any(x in t for x in terms)


def raw_trace(ref: str) -> tuple[dict, str]:
    """ref = '<plane>:<INV>' (INV ids collide across clusters, so the plane picks the source)."""
    if ref not in _RAW:
        if len(_RAW) >= 4:
            _RAW.pop(next(iter(_RAW)))
        _RAW[ref] = dict(zip(("trace", "source"), T.fetch(ref)))
    return _RAW[ref]["trace"], _RAW[ref]["source"]


def _note_kind(text: str) -> str:
    t = text.lstrip()
    if t.startswith("{") or t.startswith("```"):
        return ""                      # hypothesis / review JSON: the board already carries it
    if "Current assessment" in t[:60]:
        return "assessment"
    if "Candidate" in t[:60]:
        return "candidates"
    if t.startswith("- "):
        return "evidence"
    return "thinking"


def build(ref: str, terms: list[str], summary: str = "") -> dict:
    """Investigation -> ordered rounds with GT flags and milestones."""
    terms = [t.lower() for t in terms]
    trace, source = raw_trace(ref)
    inv = T.split_ref(ref)[1]
    spans = list(T._walk(trace.get("spans") or []))
    events = sorted(((li.get("timestamp") or "", s.get("agent_name"), li)
                     for s in spans for li in (s.get("llm_interactions") or [])),
                    key=lambda e: e[0])
    if not events:
        return {"investigation_id": inv, "ref": ref, "source": source, "rounds": [], "milestones": {},
                "hypotheses": [], "final_summary": summary, "terms": terms, "empty": True}
    t0 = _ts(events[0][0])
    mins = lambda ts: round(((_ts(ts) or t0) - t0).total_seconds() / 60, 1) if t0 else 0

    outputs: dict[str, str] = {}       # tool_call_id -> result text
    for _, ag, li in events:
        if ag in ("K8sAgent", "MELTAgent"):
            for m in _msgs(li):
                if m.get("role") == "tool" and m.get("tool_call_id"):
                    outputs.setdefault(m["tool_call_id"], str(m.get("content") or ""))

    rounds: list[dict] = [{"n": 0, "t": 0, "intent": "planning and first hypotheses", "tasks": [],
                           "reads": [], "findings": [], "notes": [], "board": []}]
    prev_board: dict = {}
    final = ""
    for ts, ag, li in events:
        cur = rounds[-1]
        resp = T._safe(li.get("response") or "")
        if ag == "Orchestrator":
            calls, content = [], ""
            for ch in resp:
                msg = ch.get("message", {})
                calls += msg.get("tool_calls") or []
                content = msg.get("content") if isinstance(msg.get("content"), str) else content
            names = {c["function"]["name"] for c in calls}
            if names & _DISPATCH:
                board = T._parse_board(li.get("request_prompt") or "")
                changes = []
                for hid, (st, txt, stars) in board.items():
                    old = prev_board.get(hid)
                    if not old or old[0] != st:
                        changes.append({"id": hid, "text": txt, "status": st, "stars": stars,
                                        "was": old[0] if old else None, "gt": _has(txt, terms)})
                prev_board = board or prev_board
                cur = {"n": len(rounds), "t": mins(ts), "intent": (content or "")[:400], "tasks": [],
                       "reads": [], "findings": [], "notes": [], "board": changes}
                rounds.append(cur)
            for c in calls:
                fn = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                if fn == "assign_task":
                    ins = str(args.get("instruction") or "")
                    cur["tasks"].append({"agent": args.get("agent_name"), "instruction": ins[:500],
                                         "gt": _has(ins, terms)})
                elif fn == "complete_investigation":
                    final = args.get("summary") or final
                    cur["notes"].append({"kind": "complete", "text": str(final)[:600],
                                         "gt": _has(final, terms)})
                elif fn == "switch_behavior_pattern":
                    cur["notes"].append({"kind": "bp switch", "text": json.dumps(args)[:200], "gt": False})
            if content and not names & _DISPATCH:
                kind = _note_kind(content)
                if kind:
                    cur["notes"].append({"kind": kind, "text": content[:700], "gt": _has(content, terms)})
        elif ag in ("K8sAgent", "MELTAgent"):
            for ch in resp:
                for c in ch.get("message", {}).get("tool_calls") or []:
                    fn = c["function"]["name"]
                    try:
                        args = json.loads(c["function"].get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    if fn == "generate_final_response":
                        r = args.get("response") if isinstance(args.get("response"), dict) else args
                        text = " ".join(str(r.get(k) or "") for k in ("summary", "answer"))
                        cur["findings"].append({"agent": ag, "summary": str(r.get("summary") or "")[:300],
                                                "answer": str(r.get("answer") or "")[:700],
                                                "gt": _has(text, terms)})
                        continue
                    q = args.get("query")
                    target = args.get("cmd") or (q.get("raw_query") if isinstance(q, dict) else q) \
                        or args.get("pod_name") or args.get("name") or ""
                    out = outputs.get(c.get("id") or "", "")
                    cur["reads"].append({"agent": ag, "tool": fn, "read": T.normalize_read(ag, fn, args),
                                         "target": str(target)[:150], "probe": _has(target, terms),
                                         "hit": _has(out, terms)})

    view = T.analyze(trace, inv, terms)
    hyps = []
    for h in view.hypotheses:
        d = {"id": h.id, "text": h.text, "status": h.status, "traj": h.status_trajectory,
             "peak": h.peak_confidence, "gt": bool(h.is_ground_truth), "reason": h.final_reason}
        hyps.append(d)
    final = final or view.final_summary
    summary = summary or final

    def first(pred):
        for r in rounds:
            if pred(r):
                return {"round": r["n"], "t": r["t"]}
        return None

    gt_h = [h for h in hyps if h["gt"]]
    ms = {
        "output": first(lambda r: any(x["hit"] for x in r["reads"])),
        "finding": first(lambda r: any(x["gt"] for x in r["findings"])),
        "note": first(lambda r: any(x["gt"] for x in r["notes"] if x["kind"] != "complete")),
        "hypothesis": first(lambda r: any(b["gt"] for b in r["board"])),
        "confirmed": first(lambda r: any(b["gt"] and b["status"] == "confirmed" for b in r["board"])),
        "summary": {"round": rounds[-1]["n"], "t": rounds[-1]["t"]} if _has(summary, terms) else None,
    }
    if not ms["hypothesis"] and gt_h:               # board line seen only after the last dispatch
        ms["hypothesis"] = {"round": rounds[-1]["n"], "t": rounds[-1]["t"]}
    if not ms["confirmed"] and any(h["status"] == "confirmed" for h in gt_h):
        ms["confirmed"] = {"round": rounds[-1]["n"], "t": rounds[-1]["t"]}
    return {"investigation_id": inv, "ref": ref, "source": source, "terms": terms,
            "duration_min": rounds[-1]["t"] if rounds else 0, "rounds": rounds,
            "hypotheses": hyps, "final_summary": str(summary)[:3000], "milestones": ms,
            "totals": {"rounds": len(rounds) - 1, "reads": sum(len(r["reads"]) for r in rounds),
                       "gt_reads": sum(1 for r in rounds for x in r["reads"] if x["hit"] or x["probe"]),
                       "findings": sum(len(r["findings"]) for r in rounds)}}


def get(ref: str, terms: list[str], summary: str = "") -> dict:
    """Disk-cached build; the key includes plane and terms, so re-tuned terms rebuild."""
    key = hashlib.sha1((ref + "|" + ",".join(sorted(terms))).encode()).hexdigest()[:10]
    CACHE_DIR.mkdir(exist_ok=True)
    f = CACHE_DIR / f"{ref.replace(':', '_')}-{key}.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except ValueError:
            pass
    tl = build(ref, terms, summary)
    f.write_text(json.dumps(tl))
    return tl


# ---- comparison ---------------------------------------------------------------------------------

def _norm_target(t: str) -> str:
    t = re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{5}\b|-[a-z0-9]{5}\b(?=\s|$)", "", t.lower())   # pod hashes
    return re.sub(r"\s+", " ", t).strip()


def compare(ok: dict, bad: dict) -> dict:
    """`ok` reached the ground truth, `bad` did not: where did `bad` leave the path, and why."""
    rows, div = [], None
    for key, label in LAYERS:
        a, b = ok["milestones"].get(key), bad["milestones"].get(key)
        rows.append({"layer": key, "label": label, "ok": a, "bad": b})
        if div is None and a and not b:
            div = key
    # Equivalent read = same kind of read that also touches the GT (same term named, if any);
    # exact command strings differ run to run (pod hashes, flags), so they are not compared.
    terms = ok.get("terms") or []
    def key(x):
        t = x["target"].lower()
        return x["read"] + "|" + next((m for m in terms if m in t), "")
    bad_keys = {key(x) for r in bad["rounds"] for x in r["reads"] if x["hit"] or x["probe"]}
    seen, decisive = set(), []
    for r in ok["rounds"]:
        for x in r["reads"]:
            k = key(x)
            if (x["hit"] or x["probe"]) and k not in seen:
                seen.add(k)
                decisive.append({"round": r["n"], "agent": x["agent"], "target": x["target"] or x["read"],
                                 "hit": x["hit"], "also_in_fail": k in bad_keys})
    gt_h = [h for h in bad["hypotheses"] if h["gt"]]
    instead = [h for h in bad["hypotheses"] if h["status"] == "confirmed" and not h["gt"]]
    label = dict(LAYERS)
    if div is None:
        story = ("Both runs carry the GT fact through every layer the terms can see; the difference is in "
                 "how the summary characterizes it (read the judge panel).")
    else:
        prior = [r for r in rows if r["bad"] and r["layer"] != "summary"]
        got = (f"The failing run got as far as: {label[prior[-1]['layer']]} (round {prior[-1]['bad']['round']}). "
               if prior else "The failing run never touched the GT fact at all. ")
        story = got + f"It leaves the path where {label[div]}: the passing run does this in round " \
                      f"{ok['milestones'][div]['round']}, the failing run never does."
        rej = [h for h in gt_h if h["status"] in ("rejected", "refuted", "dismissed")]
        if rej:
            story += f" It raised the true cause and then rejected it" + \
                     (f": \"{rej[0]['reason'][:220]}\"" if rej[0]["reason"] else ".")
        if instead:
            story += f" It confirmed instead: \"{_clean(instead[0]['text'])[:160]}\"."
    missing = [d for d in decisive if not d["also_in_fail"]]
    return {"ok": ok.get("ref") or ok["investigation_id"], "bad": bad.get("ref") or bad["investigation_id"], "rows": rows,
            "divergence": div, "story": story, "decisive_reads": decisive[:14],
            "reads_missing_in_fail": len(missing), "fail_gt_hypotheses": gt_h,
            "fail_confirmed_instead": instead, "mermaid": mermaid(ok, bad, rows, div)}


def _mm(s: str, n: int = 70) -> str:
    return re.sub(r'["\[\]{}()<>|#;`]', " ", str(s))[:n].strip()


def mermaid(ok: dict, bad: dict, rows: list[dict], div: str | None) -> str:
    """Two lanes of GT milestones; the failing lane turns red where it leaves the path."""
    out = ["flowchart TB", "  classDef hit fill:#173a27,stroke:#4bb972,color:#e8e6e0",
           "  classDef miss fill:#3a1a17,stroke:#e0574f,color:#e8e6e0",
           "  classDef wrong fill:#3a2c14,stroke:#d99a4e,color:#e8e6e0"]
    for side, tl, tag in (("ok", ok, "PASS"), ("bad", bad, "FAIL")):
        out.append(f'  subgraph {side}["{tag} {tl["investigation_id"]}"]')
        out.append("    direction LR")
        ids = []
        for r in rows:
            m = r[side]
            nid = f"{side}_{r['layer']}"
            txt = f"{r['layer']}<br/>round {m['round']} · {m['t']} min" if m else f"{r['layer']}<br/>never"
            out.append(f'    {nid}["{txt}"]:::{"hit" if m else "miss"}')
            ids.append(nid)
        out.append("    " + " --> ".join(ids))
        if side == "bad":
            for i, h in enumerate([h for h in tl["hypotheses"] if h["status"] == "confirmed" and not h["gt"]][:2]):
                out.append(f'    bad_w{i}["confirmed instead:<br/>{_mm(h["text"])}"]:::wrong')
                out.append(f"    {ids[-1]} -.-> bad_w{i}")
        out.append("  end")
        out.append(f"  style {side} fill:#1c2026,stroke:#2a2f37,color:#e8e6e0")
    out.append("  ok ~~~ bad")
    return "\n".join(out)


def path(tl: dict) -> dict:
    """The run's reasoning path toward the GT: per layer, the round it happened and the concrete
    step (the read, the finding, the hypothesis...). Missing layers are None."""
    out = {}
    rounds = {r["n"]: r for r in tl.get("rounds", [])}
    ms = tl.get("milestones", {})
    gt_h = [h for h in tl.get("hypotheses", []) if h.get("gt")]
    for key, _ in LAYERS:
        m = ms.get(key)
        if not m:
            out[key] = None
            continue
        r = rounds.get(m["round"], {})
        if key == "output":
            x = next((x for x in r.get("reads", []) if x["hit"]), None)
            text = f"{x['agent']} ran `{x['target'] or x['read']}` and the output held the fact" if x else ""
        elif key == "finding":
            x = next((f for f in r.get("findings", []) if f["gt"]), None)
            text = f"{x['agent']} reported: {x['summary'] or x['answer'][:240]}" if x else ""
        elif key == "note":
            x = next((n for n in r.get("notes", []) if n["gt"] and n["kind"] != "complete"), None)
            text = f"orchestrator {x['kind']}: {x['text'][:260]}" if x else ""
        elif key in ("hypothesis", "confirmed"):
            text = f"[{gt_h[0]['id']}] {gt_h[0]['text'][:220]}" if gt_h else ""
        else:
            text = (tl.get("final_summary") or "")[:300]
        out[key] = {**m, "text": text}
    return out


def _clean(t: str) -> str:
    return re.sub(r"\s*\((\\u260[56])+\)|\\u260[56]", "", t or "")


def doing_instead(tl: dict, round_n: int | None) -> str:
    """What a run was pursuing around a given round: its intent and the hypotheses it moved."""
    rs = [r for r in tl.get("rounds", []) if r["n"]]
    if not rs:
        return ""
    r = min(rs, key=lambda r: abs(r["n"] - (round_n or 1)))
    bits = []
    if r.get("intent"):
        bits.append(f"round {r['n']} intent: {r['intent'][:200]}")
    moved = [b for b in r.get("board", []) if not b.get("gt")][:2]
    bits += [f"{b['was'] or 'new'} → {b['status']}: {b['text'][:140]}" for b in moved]
    return _clean(" · ".join(bits))


def mermaid_many(tls: list[dict], ref_idx: int = 0) -> str:
    """One lane of GT milestones per run; the reference lane first, misses in red."""
    out = ["flowchart TB", "  classDef hit fill:#173a27,stroke:#4bb972,color:#e8e6e0",
           "  classDef miss fill:#3a1a17,stroke:#e0574f,color:#e8e6e0",
           "  classDef wrong fill:#3a2c14,stroke:#d99a4e,color:#e8e6e0"]
    order = [ref_idx] + [i for i in range(len(tls)) if i != ref_idx]
    lanes = []
    for k, i in enumerate(order):
        tl, side = tls[i], f"r{k}"
        tag = "REFERENCE" if k == 0 else f"RUN {k + 1}"
        out.append(f'  subgraph {side}["{tag} {tl["investigation_id"]}"]')
        out.append("    direction LR")
        ids = []
        for key, _ in LAYERS:
            m = tl["milestones"].get(key)
            nid = f"{side}_{key}"
            txt = f"{key}<br/>round {m['round']} · {m['t']} min" if m else f"{key}<br/>never"
            out.append(f'    {nid}["{txt}"]:::{"hit" if m else "miss"}')
            ids.append(nid)
        out.append("    " + " --> ".join(ids))
        if k:
            for j, h in enumerate([h for h in tl["hypotheses"] if h["status"] == "confirmed" and not h["gt"]][:1]):
                out.append(f'    {side}_w{j}["confirmed instead:<br/>{_mm(h["text"])}"]:::wrong')
                out.append(f"    {ids[-1]} -.-> {side}_w{j}")
        out.append("  end")
        out.append(f"  style {side} fill:#1c2026,stroke:#2a2f37,color:#e8e6e0")
        lanes.append(side)
    if len(lanes) > 1:
        out.append("  " + " ~~~ ".join(lanes))
    return "\n".join(out)


def digest(tl: dict, cap: int = 9000) -> str:
    """Compact text rendering of a timeline, for the chat model's context."""
    L = [f"Investigation {tl['investigation_id']}: {tl.get('totals', {}).get('rounds', 0)} rounds, "
         f"{tl.get('totals', {}).get('reads', 0)} reads, {tl.get('duration_min', 0)} min.",
         "GT milestones (round reached, or never): " + ", ".join(
             f"{k}={'R' + str(v['round']) if v else 'never'}" for k, v in tl["milestones"].items())]
    for r in tl["rounds"]:
        L.append(f"\nRound {r['n']} (+{r['t']} min) intent: {r['intent'][:220]}")
        for t in r["tasks"]:
            L.append(f"  task -> {t['agent']}: {t['instruction'][:200]}")
        gt = [x["target"] for x in r["reads"] if x["hit"] or x["probe"]]
        L.append(f"  reads: {len(r['reads'])}" + (f"; touching GT: {'; '.join(gt[:4])}" if gt else ""))
        for f in r["findings"]:
            L.append(f"  finding[{f['agent']}]{' (GT)' if f['gt'] else ''}: {f['summary'][:220]}")
        for n in r["notes"]:
            L.append(f"  orchestrator {n['kind']}{' (GT)' if n['gt'] else ''}: {n['text'][:260]}")
        for b in r["board"]:
            L.append(f"  board: [{b['id']}] {b['was'] or 'new'} -> {b['status']}{' (GT)' if b['gt'] else ''}: {b['text'][:140]}")
    L.append("\nFinal hypotheses:")
    for h in tl["hypotheses"]:
        L.append(f"  [{h['id']}] {'→'.join(h['traj'])}{' (GT)' if h['gt'] else ''}: {h['text'][:160]}"
                 + (f" | reason: {h['reason'][:200]}" if h["reason"] else ""))
    L.append("\nFinal summary: " + tl["final_summary"][:1200])
    text = "\n".join(L)
    return text if len(text) <= cap else text[:cap] + "\n…(truncated; use round_detail / grep_trace)"


def grep(ref: str, pattern: str, limit: int = 12) -> list[dict]:
    """Search an investigation's tool outputs and agent findings; returns short snippets."""
    trace, _ = raw_trace(ref)
    rx = re.compile(pattern, re.I)
    hits, seen = [], set()
    for s in T._walk(trace.get("spans") or []):
        if s.get("agent_name") not in ("K8sAgent", "MELTAgent"):
            continue
        for li in s.get("llm_interactions") or []:
            for m in _msgs(li):
                if m.get("role") != "tool":
                    continue
                c = str(m.get("content") or "")
                for mt in rx.finditer(c):
                    snip = c[max(0, mt.start() - 160):mt.end() + 200]
                    if snip[:80] in seen:
                        continue
                    seen.add(snip[:80])
                    hits.append({"agent": s["agent_name"], "snippet": snip})
                    if len(hits) >= limit:
                        return hits
    return hits
