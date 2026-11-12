#!/usr/bin/env python3
"""Chat-to-editor backend for the fault builder: a prompt in, a validated facet patch out.

The dashboard is a static asset with no API of its own, so this is the one endpoint that
gives the builder a real model. It holds the litellm address (never the browser), binds
loopback only, and returns a PATCH over the wizard's facets rather than free text — the
client re-validates every value against the gated catalogue before applying it, so a
hallucinated workload name changes nothing.

  POST /api/v1/faultsim/chat
    {"prompt": str, "selection": {facet: value}, "space": {facet: [legal values]},
     "history": [{"role": "user"|"assistant", "content": str}]}
  ->  {"patch": {facet: value}, "clear": [facet], "reply": str, "model": str}
"""

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LITELLM = os.environ.get("FAULTSIM_LITELLM", "http://litellm-service.ciroos.svc.cluster.local")
MODEL = os.environ.get("FAULTSIM_CHAT_MODEL", "anthropic/claude-sonnet-5.5")
PORT = int(os.environ.get("FAULTSIM_CHAT_PORT", "8094"))
FACETS = ["faultLevel", "category", "template", "app", "workload", "container", "variantId"]
MAX_BODY = 512 * 1024
MAX_PROMPT = 2000
MAX_TURNS = 12

SYSTEM = """You edit a fault-injection spec for an SRE evaluation harness by adjusting seven facets:
faultLevel, category (the SREGym failure TYPE, e.g. "Misconfiguration Failure"),
template (the MECHANISM — the concrete mutation, e.g. "missing_env_variable"),
app, workload, container, variantId.

category and faultLevel are the same vocabulary the problem catalogue uses; template is narrower
and the catalogue does not name it. A user saying "fault type" usually means `category`, but if
their words name a mechanism, set `template`.

You do NOT invent faults. Every legal value is listed in `space` below; the catalogue was already
gated, so any combination reachable from those values is a fault that can actually be injected.

Reply with ONE JSON object and nothing else:
  {"patch": {"<facet>": "<value from space>"}, "clear": ["<facet>"], "reply": "<one sentence>"}

Rules:
- Only facets the user's message actually constrains go in `patch`. Leave the rest alone.
- Every value in `patch` must appear verbatim in that facet's list in `space`. Never guess a name.
- `clear` removes facets the user asked to widen or undo ("any app", "forget the container").
- If nothing in `space` fits the request, return an empty patch and say so plainly in `reply`.
- `reply` names what you changed and why, in one sentence. No preamble, no markdown."""


def ask(prompt, selection, space, history):
    """One litellm round-trip; returns the model's parsed patch."""
    context = json.dumps({"selection": selection, "space": space}, separators=(",", ":"))
    messages = [{"role": "system", "content": f"{SYSTEM}\n\nCurrent state:\n{context}"}]
    messages += history[-MAX_TURNS:]
    messages.append({"role": "user", "content": prompt})
    # No `temperature`: the newer Anthropic models reject it outright (400 "deprecated").
    body = json.dumps({"model": MODEL, "messages": messages, "max_tokens": 700}).encode()
    req = urllib.request.Request(
        f"{LITELLM}/v1/chat/completions", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    text = data["choices"][0]["message"]["content"].strip()
    # Models occasionally wrap JSON in a fence even when told not to.
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    return json.loads(text)


def clean(out, space):
    """Keep only facets that exist and values the catalogue actually offers."""
    patch, dropped = {}, []
    for facet, value in (out.get("patch") or {}).items():
        if facet not in FACETS or not isinstance(value, str):
            continue
        if value in (space.get(facet) or []):
            patch[facet] = value
        else:
            dropped.append(f"{facet}={value}")
    clear = [f for f in (out.get("clear") or []) if f in FACETS]
    reply = str(out.get("reply") or "")[:600]
    if dropped:
        reply = f"{reply} (ignored values not in the catalogue: {', '.join(dropped)})".strip()
    return {"patch": patch, "clear": clear, "reply": reply, "model": MODEL}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Loopback only, and no CORS: the dev server proxies to it from inside the pod.
        if self.headers.get("Host", "").split(":")[0] not in {"localhost", "127.0.0.1"}:
            return self.reply(403, {"error": "Local access only"})
        if self.path.rstrip("/") != "/api/v1/faultsim/chat":
            return self.reply(404, {"error": "Endpoint not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self.reply(400, {"error": "Body must be 1..512KB of JSON"})
        try:
            req = json.loads(self.rfile.read(length))
            prompt = str(req.get("prompt") or "").strip()[:MAX_PROMPT]
            space = {f: list(req.get("space", {}).get(f) or []) for f in FACETS}
            if not prompt:
                return self.reply(400, {"error": "prompt is required"})
            out = ask(prompt, req.get("selection") or {}, space, req.get("history") or [])
            return self.reply(200, clean(out, space))
        except json.JSONDecodeError:
            return self.reply(502, {"error": "The model did not return JSON; try rephrasing."})
        except urllib.error.HTTPError as exc:
            # Surface litellm's own body — "invalid model name" and "deprecated param" land here.
            return self.reply(502, {"error": f"litellm {exc.code}: {exc.read()[:400].decode(errors='replace')}"})
        except urllib.error.URLError as exc:
            return self.reply(502, {"error": f"litellm unreachable: {exc}"})
        except (KeyError, TypeError, ValueError) as exc:
            return self.reply(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self):
        if self.path.rstrip("/") == "/api/v1/faultsim/health":
            return self.reply(200, {"ok": True, "model": MODEL, "litellm": LITELLM})
        return self.reply(404, {"error": "Endpoint not found"})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print(f"faultsim-chat on 127.0.0.1:{PORT} -> {LITELLM} model={MODEL}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
