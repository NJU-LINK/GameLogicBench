# GameLogicBench — Result Field Dictionary (solve `status.json` + judge `result.json`)

> Answers "**when reading one result cell, what does each field and enum value mean**". The
> directory layout is in [`structure.md`](./structure.md#result-layout), running in
> [`running.md`](./running.md).
> **This file is a reading reference; the authority is the code's docstrings** — change the
> convention in code first, then sync this file. References give symbol names, never line numbers,
> because line numbers drift:
> the solve payload = the `_write_status` call in
> [`geb/pipeline/solve.py`](../geb/pipeline/solve.py),
> the session `outcome` vocabulary = `AgentResult`'s docstring in
> [`geb/schema.py`](../geb/schema.py) plus
> [`geb/scaffold/common.py`](../geb/scaffold/common.py) (the `_CAUSE_OUTCOME` map and the
> `_SOLVE_UNUSABLE` set), judge normalisation = `normalize_from_raw` and `_build_seed_result` in
> [`geb/pipeline/judge.py`](../geb/pipeline/judge.py), the container `cause` and rc map =
> `ContainerRunResult` in [`geb/schema.py`](../geb/schema.py) and `_discriminate_exit` in
> [`geb/infra/container.py`](../geb/infra/container.py).

## Mental model: three orthogonal axes, and no status field

One result cell — a solve session or a judge verdict alike — answers three orthogonal questions,
one field each, and **there is no status field**:

| Axis | Answers | Field | Values |
|---|---|---|---|
| **What happened** | the **single source of truth** for this cell | `outcome` | the full vocabularies below; each word has one globally fixed `usable` |
| **Is it usable** | is this a usable data point (does it enter the denominator, does it need a rerun) | `usable` | `true` / `false` |
| **Verdict / delivery** | judge: did it pass; solve: was a controller delivered | judge `passed` / solve `produced` | `true` / `false` / `null` |

- **`outcome` is the only "what happened" source**, with the same word in code and in JSON. There is
  no `ending` at the session layer, no `status` or `timed_out` at the container layer, and no
  `status` at the result layer — all of those were derivable from `outcome` / `usable` and were
  removed.
- **Each `outcome` word has one globally fixed `usable`**: reading the outcome tells you usability,
  and a reader can just read the `usable` field rather than inferring it.
- Metrics are not flattened into the top level: judge metrics go in a `metrics` sub-object, solve
  metrics in a `session` sub-object.

---

## solve `status.json`

One per model-solution sample, at
`result/<model>@<scaffold>@<effort>@<net>/<task>/sample_<i>/solve/status.json` (the four-segment
slug is described in [`structure.md`](./structure.md#result-layout)). It records **how this agent
session ended**, not how it scored — the actual PASS/FAIL lives alongside it in
`test/<scenario>_<n>/result.json`.

| Field | Source | Meaning |
|---|---|---|
| `task` / `model` / `scaffold` / `effort` / `sample` | run parameters | this cell's coordinates. `scaffold` = agent backend (`cc` = vendored claude-code / `codex` / `opencode`); `effort` = reasoning tier |
| `outcome` | `AgentResult.outcome` | **how the session ended** (7 values, table below) — the central field of this document |
| `usable` | derived from outcome (`solve_usable`) | **is this session usable**: `false` = the workspace is untrustworthy (`aborted` / `daemon_error` / `stream_broken`), so judge is skipped and the cell is flagged for a rerun; `true` = usable |
| `produced` | `_controller_written` | **was a controller actually delivered**: `logic/controller.gd` exists AND its content differs from the default stub. The gate into judge is `usable && produced` |
| `session` | execution signals and metrics (sub-object) | `rc` (the process exit code under the `timeout` wrapper), `subtype` (terminal-event subtype, null when there was no terminal event), `detail` (human-readable diagnosis), `num_turns` (the official value when a terminal event exists, otherwise estimated from the trajectory for cc — see below), `duration_ms` / `cost_usd` / `usage` (**only populated when a terminal event exists**) |
| `tasks_commit` | the tasks/ repository | the exact library version of the judge and task statement (with a `-dirty` suffix when dirty), so a matrix stays traceable |
| `finished_at` | write time | UTC ISO8601 |

### The seven solve `outcome` values (each with one globally fixed usable)

`outcome` is decided by **whether a terminal event arrived**: with one, the CLI's own word is used;
without one, the container's `cause` maps in (see `cause_to_outcome`).

| `outcome` | When | `usable` | Notes |
|---|---|---|---|
| `completed` | terminal event + success subtype (codex/opencode: turn.completed / stop) | **true** | the agent finished cleanly on its own. **The only normal ending** |
| `agent_error` | terminal event + any non-success subtype (cc only) | true | the CLI reported an error itself, the turn cap (`error_max_turns`) included. Still the agent's own problem, so it is judged if it produced |
| `stream_broken` | no terminal event + `cause==exit` | **false** | the container exited normally but the trajectory has no terminal event (gateway drop / 429 retry exhaustion / CLI crash). The session never reached `turn.completed`, the workspace is untrustworthy, and the break often lands before any controller is written — **rerun**. (This was once treated as usable, which left such cells neither judged nor re-queued: silently missing.) |
| `timeout` | no terminal event + `cause==inner_timeout` / `wall_timeout` | true | the session was too slow (the in-container `timeout` gate or the outer wall clock); whatever it produced is still judged |
| `oom` | no terminal event + `cause==oom` | true | the model ran the container into `mem_limit` (rc 137 + OOMKilled); a real behavioural signal, judged if it produced |
| `aborted` | no terminal event + `cause==abnormal_exit` (segfault / exec error / external kill, not distinguished) | **false** | the agent CLI process died abnormally = a tool or environment failure — **rerun** |
| `daemon_error` | no terminal event + `cause==daemon_error` | **false** | the Docker API itself failed (not the workload) — **rerun** |

> **`produced` is orthogonal to `outcome`**: `outcome != completed` does not void the cell. As long
> as `usable && produced`, the delivered controller **goes to judge anyway** — even from a session
> that timed out, broke its stream or ran out of memory. solve is only responsible for producing a
> controller; the verdict belongs to the judge. Only `usable==false`
> (`aborted` / `daemon_error` / `stream_broken`, see `_SOLVE_UNUSABLE`) skips judge and flags a
> rerun; `produced==false` genuinely means there is nothing to judge. Timeouts and OOM are real
> capability signals and are judged.

### The num_turns convention, by backend

`num_turns` = **the number of model speaking/acting turns** — not tool calls, not total events. All
three backends share that meaning but obtain it differently:

| scaffold | Source | Notes |
|---|---|---|
| `cc` | the CLI's own `num_turns` when a terminal event exists; otherwise (timeout / oom / stream_broken) estimated by `count_turns` in `geb/scaffold/claude_code.py` as the number of `user` events in the trajectory plus one | with a terminal event this is claude-code's official figure and the most trustworthy. The estimator agreed with the official value exactly on 129 of 129 completed cc sessions (MAE 0); on a timeout it counts the turns completed before the kill |
| `codex` | counts `item.completed` events with `item.type=="agent_message"` itself (`count_turns` in `geb/scaffold/codex.py`) | codex-exec emits `turn.completed` once per whole session, so its value of 1 is useless; the real iteration is agent_message |
| `opencode` | counts `step_finish` itself (`count_steps` in `geb/scaffold/opencode.py`) | one model utterance per step |

> Tool-call count is a different quantity and is not in this field.

## rc to container `cause`

The actual logic of `_discriminate_exit(container, rc)` in `geb/infra/container.py`. `cause` is the
container layer's **single source of truth**; both solve's `outcome` and judge's container-death
attribution derive from it:

| rc | cause | Notes |
|---|---|---|
| `0` / `1` | `exit` | 0 = judge PASS, 1 = judge FAIL. Both are valid judge results, not failures |
| `124` | `inner_timeout` | `timeout --signal=TERM` fired and sent SIGTERM |
| `137` | `oom` **or** `inner_timeout` | `oom` when `State.OOMKilled=true`; otherwise attributed to `inner_timeout` (read as `--kill-after` escalating to SIGKILL). **See the pitfall below** |
| everything else (126/127/139/130/143…) | `abnormal_exit` | exec failure / segfault / external kill |

> The solve session container's `timeout` wrapper (from `conf/config.yaml`) is
> `timeout --signal=TERM --kill-after=20s 3600s`; the outer `wall` clock is a backstop poll, and
> exceeding it yields `wall_timeout`.

### Known pitfalls when reading results

- **An early rc=137 can be misattributed to `inner_timeout`.** That branch assumes SIGKILL only ever
  comes from a `--kill-after` escalation, i.e. that the container time limit was already exhausted.
  But if `State.OOMKilled` under-reports — cgroup killed a **child process** inside the container
  while Docker's `OOMKilled` only reflects PID 1 — a genuine OOM lands in the `inner_timeout`
  fallback. **How to tell: look at the seconds in `session.detail`**; an rc=137 far short of the
  limit is most likely an OOM. (To make the statistics clean, either fix `_was_oom_killed` or split
  out rc137-plus-early-exit as its own case.)

---

## judge `result.json`

One per cell, at `test/<scenario>_<seed>/result.json`. judge.gd emits a flat raw dict which the
Python side normalises in `normalize_from_raw`: task metrics and run values are collected into
`metrics`, and the `status` judge.gd writes for itself is **discarded** (outcome is the single
source).

| Field | Meaning |
|---|---|
| `solution` / `task` / `scenario` / `seed` / `seed_set` | this cell's coordinates. `seed_set` = the scenario tier: `public` (baseline) / `hidden` |
| `outcome` | **the judged phenomenon** (single source of truth, table below). Judge signatures are task-specific (clipping / overtime / breach …) |
| `usable` | **is this cell usable**: `false` = excluded from the denominator and automatically re-run next time; `true` = counted |
| `passed` | **the verdict**: `true` / `false` = a verdict was reached; `null` = unusable (N/A) |
| `broken_link` | composite-task attribution: which armed axis the failure landed on (empty for atoms) |
| `metrics` | task metrics and run values (controller / frames / the various margins …) as a sub-object |
| `error` | a one-line human-readable diagnosis |

### judge `outcome` and `usable` (each word with one globally fixed usable)

| Source | `outcome` | `usable` | `passed` |
|---|---|---|---|
| judge.gd reached a verdict | `pass` / `clipping` / `overtime` / … (the judge signature, passed through verbatim) | true | true/false |
| the submitted controller is invalid (fails to load or compile, missing `on_tick`) | `controller_invalid` (judge.gd writes `build_error`; normalisation maps it to this clearer word) | true | false |
| a task-authoring or command misconfiguration (fail-fast) | `unknown_scenario` / `no_press_axis` / `unknown_press_axis` | **false** | null |
| the container died with no result: the solution ran itself out of memory | `killed_oom` | true | false |
| the container died with no result: the solution ran out of time | `killed_timeout` | true | false |
| the container died with no result: the solution crashed the engine (rc 139 segfault) | `crashed` | true | false |
| the container died with no result: genuine infrastructure (daemon / wall / exec 126,127 / kill 130,143) | `infra_error` | **false** | null |

- The `usable==false` set is `{infra_error, unknown_scenario, no_press_axis, unknown_press_axis}`:
  excluded from the denominator, counted as `excluded` (the summary shows `[N excl]`), and
  automatically re-run by the next `run`. Everything else is `usable==true` and enters the
  denominator.
- **pass@k**: `solved` requires `total>0 and excluded==0 and pass==total`. Any unusable cell awaiting
  a re-judge means not solved — the evidence is incomplete, which is "undetermined", not "fail".
- **The two sides never share a word**: `crashed` (a judge-side segfault, i.e. the solution crashed
  the engine — a real failure, usable) and solve's `aborted` (a CLI tool failure, unusable) are
  **different words**, which is what keeps every outcome word's usable globally unique. Container
  `cause=daemon_error` is not an overlap either: solve records outcome `daemon_error`, while the
  judge side goes through `_is_infra_death` to `infra_error` — different words, both
  `usable:false`.

### Why a death inside the container can safely be blamed on the solution — the reference invariant

The judge container is a fixed image with fixed resources, `network=none` and a deterministic
simulation, and task authoring guarantees that `proper` and `naive` **always reach a verdict**
inside it (measured: zero infrastructure failures). So the environment does not kill containers
unprompted, and a death inside one (OOM, simulation timeout, segfault) can only be the solution
under test blowing up a healthy container = **a solution failure**
(`killed_oom` / `killed_timeout` / `crashed`, `usable:true`, counted FAIL, the same character as
`controller_invalid`). Genuine infrastructure failure is **non-selective** — it comes in clusters
and takes `proper` down with it, hence `infra_error` and `usable:false`. A solution failure is
**selective**: it only hits a particular solution on a particular hard scenario.

> Worked example: one model's hidden cell OOMed on a composite task = **a solution failure** (an
> unbounded BFS exhausted the 1g limit, while baseline passed and proper/naive were all usable —
> unmistakably the solution's fault) -> `outcome:killed_oom, usable:true, passed:false`, counted
> FAIL. Not a misjudgement, and not flaky infrastructure.
> Note: archived data from before this schema used `status:infra_error/solution_error` plus flat
> metrics and was not rewritten retroactively. To see it in the current fields, **re-run** the task
> under the current judge (it will still OOM, but will be recorded honestly as `killed_oom` and
> counted FAIL).

## The judge summary, `solve/judge.json` (one per task and sample)

After judging every cell of a task, a **scenario-level pass summary** is written next to `solve/`
(`_write_judge_summary` in `geb/pipeline/judge.py`) so a run can be eyeballed without running the
scoring script:

```json
{ "task":"atom_boids","solution":"proper","sample":null,
  "scenarios": {"baseline":{"pass":5,"total":5}, "sharp_turns":{"pass":5,"total":5}, ...},
  "overall": {"pass":20,"total":20,"excluded":0},
  "judged_at":"..." }
```

- Granularity is per scenario (`pass/total` each, with `excluded` only when that scenario has an
  unusable cell). Per-cell detail lives in the cells themselves, and finer public/hidden/press
  breakdowns come from `python scripts/score.py`.
- Model solutions write `sample_<i>/solve/judge.json` (one per sample, beside `status.json`);
  reference solutions write `<task>/solve/judge.json`. It is rewritten on every judge of that task,
  so it reflects the current grading state including idempotently skipped cells.

## Quick statistics

```bash
# outcome distribution across all samples of one model (anything but completed is an abnormal end)
find result/<model>@cc@high@egress -name status.json | xargs -I{} jq -r '.outcome' {} | sort | uniq -c

# unusable judge cells (awaiting a re-judge)
find result -path '*/test/*/result.json' | xargs -I{} sh -c \
  'jq -e ".usable==false" "$1" >/dev/null && jq -r ".task+\" \"+.scenario+\" \"+.outcome" "$1"' _ {}
```
