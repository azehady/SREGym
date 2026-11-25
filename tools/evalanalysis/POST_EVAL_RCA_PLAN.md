# Post-eval RCA pipeline: from an eval run to a merged fix

Status: plan for review. No code changes until this is agreed.
Owner: Andy. Reviewers: Guneet (plan, then PR), Abhinav (code-agent integration).

## 1. Goal

After every eval run, an agent should check the run instead of a person. It reports concrete
bugs in our tool calling or our setup, as **insights**. When a person agrees an insight is right,
the code agent writes the patch and opens a PR for normal review.

Every piece already exists in some form. Nothing connects them. This plan connects them and adds
two things that are missing: an insight step, and a human approval gate.

## 2. What already exists

### 2a. Eval analysis (SREGym `tools/evalanalysis`, built this month)

It knows each case's ground truth, so it can say *where* a run left the path to the right answer.

| Piece | What it does today |
|---|---|
| Data sync | Local eval DB plus prod0's eval campaigns, mirrored and de-duplicated. Indexes all 1,729 prod0 investigations. Re-syncs every 30 min, read-only. |
| Ground truth | Reads each SREGym problem's own root-cause definition. Covers 132 of 146 cases seen. |
| Failure taxonomy | Puts every failing run in one bucket, A–H plus N and M, using a mechanical test each. It traces the true cause through tool output → agent report → hypothesis → confirmed → conclusion. The layer where it drops out is the bucket. |
| Timeline + compare | Round-by-round timeline per run. Pass-vs-fail diff shows where the failing run diverged, and which decisive reads it never made. |
| Report grading | Grades the run's own HTML report (Joseph's reporter) against ground truth. Catches "blind spots": the run drew the root-cause component and called it healthy. |
| Scoped chat | An LLM with tools for one run or a pair: `round_detail`, `grep_trace`, and the timeline digest. |
| Dashboard | Overview, History, Taxonomy, Compare, Trace. Shareable URLs. Every id keyed by cluster. |

### 2b. Code agent (samay `gitmcp/src/server/coding_agents`, Pranav and Abhinav)

| Piece | What it does today |
|---|---|
| `code_question` (code_agent) | Read-only Claude Agent SDK session over the org's connected repos. Answers a question unchanged; 10 turns, 600 s cap. |
| `git_commit_analysis` | Ranks up to 3 suspect commits across all of an investigation's repos. |
| `generate_patch` | Input is an investigation's `rca_summary` plus repos. Finds the broken repo, edits it, and commits to branch `ciroos/{INV}`. Stores the diff on a PVC. **Does not open the PR.** |
| `POST /api/v2/investigations/{id}/patches/push` | Opens the PR for a stored patch, triggered by a person clicking "push". |
| `git-nudge` | Continues a finished patch run with a person's correction, as a new revision. |
| Repo discovery agent (#8825, this week) | Finds deployed repos and commits, and makes worktrees. |
| Gate | `CODE_AGENT_ORG_IDS` already includes **qa1 (Ciroos-Evals)**. |

Today the code agent is driven by **customer incidents**: sleuth calls it during an RCA to patch
the customer's application. In our case the "incident" is a defect in **our own harness**: sleuth
tools, prompts, BPs, the reporter, or the eval environment. So the repo to patch is ours
(`ciroos-ai/samay`, and sometimes `brahma` or `SREGym`).

## 3. The gap

1. **Nothing runs after an eval.** A batch finishes and its failures wait for a person.
2. **No insight step.** The code agent expects an RCA summary of a customer fault. We need a
   statement about our harness, backed by evidence across runs.
3. **No contract between them.** `generate_patch` is keyed by an investigation id. An insight
   spans many investigations and has no ixapi record.
4. **No review gate and no follow-up.** Nothing records whether an insight was right. Nothing
   re-runs the cases after a fix to confirm it worked.

## 4. Proposed pipeline

```
eval batch completes
  │
  ▼
S1  Triage (deterministic, no LLM)
      sync → trace extraction → A–H bucket per failing run
      → environment pre-checks (MELT returns data? kubectl alive? fault actually active?)
      → group failures into clusters: (bucket, case family, signature)
  │
  ▼
S2  Insight agent (LLM, uses the eval-analysis tools + code_question)
      for each cluster: compare passing vs failing runs, check claims in the raw tool output,
      find the code involved with code_question over samay
      → Insight record (claim, evidence, layer, where the fix goes, confidence, how to verify)
  │
  ▼
S3  Review gate (human)
      Insights tab + Slack digest → approve / reject / correct
      every verdict is stored (labels for later training)
  │ approved
  ▼
S4  Patch (gitmcp generate_patch on the samay repo)
      rca_summary = the insight; repos = where the insight says the fix goes
      → stored diff → person reviews → "push" → PR on branch ciroos/{insight-id}
  │ merged + deployed
  ▼
S5  Verify
      re-run the affected cases on the new build → re-bucket → insight marked
      verified or refuted; it re-opens if the bucket comes back
```

### S1 Triage: deterministic first

Most "model failures" have been environment defects. Recent examples: Prometheus was down for
7 weeks; MELT answered for the wrong cluster; the janitor scaled apps to 0; Loki's disk was full.
So triage rules those out before any LLM looks at the run:

- **Environment gate.** MELT returned data, kubectl tool calls succeeded, the fault was verified
  active, and the engine build is the same within the batch. A run that fails the gate becomes an
  *environment* insight. It is not counted as a model failure.
- **Clustering.** One bad run is not evidence: the same case on the same setup has swung by up to
  0.56. An insight needs either at least 2 failing runs with the same signature, or a pass-vs-fail
  contrast on the same case.
- Output: clusters with their member runs, bucket, and the decisive missing layer.

### S2 Insight agent

One agent call per cluster, with a fixed tool set:

- From eval analysis: `timeline`, `compare(pass, fail)`, `report_grade`, `grep_trace`,
  `round_detail`, `judge_questions`.
- From gitmcp: `code_question`, read-only over samay, to name the function or prompt involved.

The output is a typed record, so a person can check it in under a minute:

```json
{
  "id": "EI-2026-11-25-004",
  "claim": "describe_k8s_resources drops spec.internalTrafficPolicy, so the agent never sees it",
  "layer": "tool",            // tool | setup/env | prompt | bp | reporter | eval-harness
  "bucket": "A",
  "evidence": [{"ref": "evals:INV-…", "round": 2, "quote": "…"}, …],
  "counter_evidence": [ … ],  // passing runs that got it another way
  "fix_locus": [{"repo": "ciroos-ai/samay", "path": "common-agents/…/describe.py", "why": "…"}],
  "confidence": 0.7,
  "verify_by": "re-run internal_traffic_policy ×3; expect bucket A → none",
  "dedupe_key": "tool:describe:internalTrafficPolicy"
}
```

Rules for the agent:

- Every claim must cite a run and round, and quote the raw tool output it relies on.
- It must try to disprove the claim with passing runs.
- `dedupe_key` merges a repeat of a known insight into the open one, so each batch does not
  create a new duplicate.

### S3 Review gate

- An **Insights** tab on the existing dashboard and a short Slack digest per batch.
- Actions:
  - **Approve**: go to S4.
  - **Reject** with a reason: stored, and suppresses that `dedupe_key`.
  - **Correct**: edit the claim, then approve.
- Verdicts are kept. They become labelled data for judging insight quality, and later for training.

### S4 Patch: reuse `generate_patch` as-is where possible

- Register `ciroos-ai/samay` (and `brahma`, `SREGym` as needed) as git integrations in the
  **qa1 Ciroos-Evals** org, which is already in the code-agent gate.
- Call `generate_patch` with:
  - `rca_summary` = the insight's claim, evidence and `fix_locus`;
  - repos = the repos named in `fix_locus`.
- **Open question for Abhinav:** `generate_patch` and `patches/push` are keyed by an investigation
  id (branch `ciroos/{INV}`, a revision allocated by ixapi). Options:
  1. Create a lightweight **"eval insight" investigation** in ixapi per approved insight. Every
     existing path then works unchanged: the patch card, nudge, push.
  2. Add a gitmcp entry point that takes a free-form task id. This is cleaner, but means new code
     in gitmcp.

  I lean to option 1 to start: no gitmcp changes, and the existing UI, nudge and push flows
  work for free.
- A person reviews the diff, can nudge it, then clicks push → PR. **Never auto-merge.**

### S5 Verify

- After merge and deploy: re-run only the affected cases, with the same n the batch used. Do not
  quietly increase n.
- Re-bucket them. The insight is marked:
  - **verified**: the bucket clears on the cases it named;
  - **no effect**;
  - **regressed**: other cases got worse; this needs the full-suite check, because the goal is
    systemic fixes, not a higher pass rate on a handful of cases.

## 5. Where this has already worked by hand

Each of these was found by a person reading traces. Each is exactly the kind of insight S1–S2
should produce automatically:

| Found by hand | Layer | Would be caught by |
|---|---|---|
| `describe_k8s_resources` omits traffic-policy and env fields, which led to a hallucinated RCA | tool | S2 (bucket A, the fact never appears in any tool output) |
| Namespace-omission failures were caused by the tool schema; server-side resolution fixed them | tool | S2 (bucket F, error shape) |
| MELT answered for the wrong cluster; removing it took two cases to 1.00 | env | S1 environment gate |
| Prometheus unschedulable for 7 weeks (control-plane nodeSelector) | env | S1 environment gate |
| Our own analysis: investigation ids collide across clusters, so the wrong trace was read for 103 runs | eval-harness | S1 data checks |
| valkey: the report draws valkey-cart as "Ready" when it is the root cause | reporter / prompt | report grading: blind spot |
| cronjob / finalizer: the run confirms the true hypothesis, then the conclusion blames something else | ranking / reporter | bucket D cluster |

## 6. Guardrails

- **Deterministic before LLM.** Environment and data checks run first. The LLM only explains what
  survives them.
- **Read-only on prod0.** The public API only, one request at a time, so live investigations are
  never slowed down.
- **No auto-merge, no auto-push.** A person approves at S3 and pushes at S4.
- **Cost cap.** One insight call per cluster, not per run. A token budget per batch.
- **Evidence or nothing.** An insight without a cited run, round and raw-output quote is dropped
  automatically.

## 7. Phases

| Phase | Work | Done when |
|---|---|---|
| 0 | Agree this plan with Guneet; agree the gitmcp contract with Abhinav (option 1 or 2) | Plan approved |
| 1 | S1 triage as a post-batch job, plus an Insights tab listing the clusters | Each new batch shows clusters with no manual step |
| 2 | S2 insight agent, insight records, dedupe | Re-discovers at least 3 of the section 5 findings from historical runs, without being told about them |
| 3 | S3 review gate (tab + Slack) | Approve, reject and correct are stored |
| 4 | S4 through generate_patch in the Ciroos-Evals org | One approved insight becomes a stored patch and then a PR |
| 5 | S5 verify job | One insight goes end to end: verified or refuted after a re-run |

Phase 2 has a built-in test. Run the insight agent on historical batches and check whether it
finds the bugs we already know about. That measures the agent before we trust it on new batches.

## 8. Open questions

1. **Abhinav:** option 1 (an "eval insight" investigation in ixapi) or option 2 (a new gitmcp
   entry point)? And should the samay repo be connected in qa1 Ciroos-Evals, or in an
   evals_gke org?
2. **Guneet:** where should the insight records and the post-batch job live? Options: SREGym
   `tools/evalanalysis` (where the analysis is today), brahma's eval pipeline (next to
   universal-eval), or samay.
3. Which batches trigger the pipeline: all of them, or only campaigns marked `included`?
4. Who reviews the insights (S3)? One owner, or a rotation?

## 9. Not in scope

- Auto-merging anything.
- Changing how the code agent handles customer incidents.
- New eval cases. This is about learning more from the runs we already make.
