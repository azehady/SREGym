# Eval analysis plan: why RCA runs stray from ground truth

Goal: use eval runs, which have ground truth, to find where the orchestrator's reasoning goes shallow
or gets misled. Then turn that into harness changes, not model swaps. The same records double as
training data for rltrain.

## Assets (verified in `brahma/oracle/.artifacts/evals.sqlite3`)
- `common_score_json.question_results` is stored on every scored run. It holds each D1/D2/D3
  sub-question with its answer, evidence quote and confidence, so the judge already says which fact
  was missed. Caveat: it is a gpt-4.1 output, so check sub-question agreement on re-scored runs.
- 18 cases have both passing and failing runs: cronjob_sidecar 15/45, valkey 5/43,
  duplicate_pvc 5/32, finalizer 8/34, rolling_update 19/36, env_var_shadowing 29/26.
  No new runs are needed to start.
- Ground truth per case: `build_structured_root_cause(component, description)` in
  `vendor/sregym/sregym/conductor/problems/*.py`.

Do not compare one pass against one fail. Within-case swing reaches 0.89 on the same arm.
Compare all passing runs of a case against all failing runs of that case.

## Part 1: the evidence funnel
Track the ground-truth fact through four layers. The layer where it drops out is the failure mode.

    tool output → evidence ledger → hypothesis → final RCA summary

| Drop point | Fix lives in |
|---|---|
| never in any tool output | tools / the first-round sweep |
| in output, not in the ledger | evidence extraction / round-1 volume |
| in ledger, not in a hypothesis | ranking / hypothesis generation |
| in hypothesis, not in the summary | reporter |

## Part 2: failure taxonomy (one mechanical test per bucket)
| # | Bucket | Test | Implied harness change |
|---|---|---|---|
| A | Evidence never gathered | GT keywords absent from all tool outputs | add the probe to the first-round sweep, which runs unconditionally |
| B | Gathered, not surfaced | in tool outputs, absent from the ledger | evidence-extraction prompt / round-1 budget |
| C | Surfaced, mis-ranked | in the ledger, but the summary names a distractor as the cause | ranking discipline (prompt levers already failed here twice) |
| D | Lost in the reporter | a CONFIRMED hypothesis names it, the summary doesn't | reporter |
| E | Distractor capture | summary names a known artifact (otel DNS flood, Helm SIGTERM, janitor scale-down, Consul churn, prometheus) | noise suppression / local-knowledge BP |
| F | Tool or capability failure | MELT tool_count=0, describe eliding spec, guardrail rejection, namespace ambiguity | tool fixes |
| G | BP misdirection | wrong BP picked for a Misconfiguration-type fault | BP selection (ties to the −0.11 Misconfiguration finding) |
| H | Budget exhaustion | hit the round or time cap before reaching it | budget |

Prior: across 6 past interventions, tool and evidence fixes moved scores and prompt fixes did not.
Expect the returns in A, B, E and F, and little from C.

## Part 3: per-trace feature schema
One extractor turns each trace into a fixed record:
- identity: case, model per role (RCA, orchestrator, reporter), BP picked and whether it was
  re-selected, engine image, batch, date
- effort: tool calls per round, distinct commands, unique k8s objects, rounds, tokens, wall-clock
- coverage: did the run touch the GT object, namespace or field, and in which round
- funnel: the four-layer trace of the GT fact
- noise census: non-fault anomalies in round-1 evidence, and which known distractors appear
- outcome: D1/D2/D3 plus the failing `question_results` and the judge's evidence

## Part 4: environmental-noise census
Count the non-fault anomalies in each run's evidence and correlate the count with score. If noise
predicts failure, the fix is suppression, not prompting. Precedent: wrong-cluster MELT crowded out
kubectl, and removing it took two cases to 1.00.

## Part 5: transferable-path experiment
From opus's passing traces, extract the minimal set of probes that produced the decisive evidence.
Force those probes as a first-round sweep for the weak model, then re-score.
- If the weak model then passes, the capability has moved from the model into the harness.
- If both models already gather the same facts, the gap is ranking or reporting.

## Part 6: dashboard (renders the schema, not raw logs)
1. Pair view: round-by-round diff of tools, ledger entries and hypothesis transitions.
2. Funnel strip: the drop point of the GT fact in each run.
3. Judge panel: `question_results` next to the trace.
4. Aggregate: A–H bucket distribution per case and per model.
5. Case picker: defaults to the 18 contrastable cases.

## Part 7: sequencing
| Phase | Work | Output |
|---|---|---|
| 1 | extractor + funnel on cronjob_sidecar (60 runs) | go/no-go: does the funnel separate passes from fails? |
| 2 | extend to the 4 weakness cases | where the losses actually are |
| 3 | noise census + model-effort comparison | do small models make fewer calls? |
| 4 | dashboard over the computed features | shareable view; makes phase 5 inspectable |
| 5 | transferable-path experiment | a harness change with a pre-registered prediction |

## Threats to validity
- Engine drift: runs span multiple sleuth builds. Record the engine image per run and compare only
  within a build.
- Judge noise: `question_results` is itself a model output. Treat the funnel as the primary
  instrument and the judge as corroboration.

## Harness levers beyond BPs and prompts
- A typed evidence ledger.
- A completion gate: refuse to finish while anomalies are unexplained or hypotheses are still
  marked "not investigated".
- Hypotheses generated from the evidence each round.
- Deterministic "what's unusual" tools: spec vs last-applied diff, managedFields writers, change
  timeline.
- Label harness noise relative to the alert time.
- A coverage-based stop (valkey fails quit after about a third of the reads).
- Model allocation by role.
- Verdict voting.

## Status (2026-11-24)
- Built: History, Trace and Export tabs.
- In progress: the Failure-taxonomy tab currently groups by judge dimension
  (LOCALIZATION / CHARACTERIZATION / SCOPE). It must classify every run into A–H using the tests
  above.
