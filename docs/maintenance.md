# GameLogicBench — Maintenance Guide

> Answers "**where do I start when changing code or adding a feature**". Code organisation and
> mechanisms are in [`structure.md`](./structure.md), environment and running in
> [`running.md`](./running.md), result fields in [`status-fields.md`](./status-fields.md). The full
> task-authoring discipline (the three-layer information boundary, judge health criteria) and the
> acceptance gates are documented in the paper's appendix — this repository carries only the
> harness-side engineering docs.

## Change map (where to change X)

| To change | Go to | Knock-on effects |
|---|---|---|
| Judge behaviour / add a hidden scenario | `tasks/<task>/judge/` (judge.gd · level.gd · sim_core.gd · assertions.gd) + the `scenarios:` table in `task.yaml` | Changing sim_core or level **requires updating the game/ twin**, or the F5 preview stops matching the judge |
| A task's visuals | `tasks/<task>/game/view.gd` (the single source for preview and recording) | Visual logic the preview never uses belongs in `viz/`; putting it in game/ is a category-2 leak |
| Reference solutions | `tasks/<task>/solutions/{proper,naive}/` | proper must pass everything, naive must pass baseline and break on hidden, or the calibration does not hold |
| The recording layer | `tasks/<task>/viz/` (record.gd + record.tscn + a windowed project.godot) | A task is recordable exactly when `viz/record.tscn` exists |
| Harness logic (solve/judge, overlay and judge commands) | `geb/pipeline/`, `geb/harness/godot.py` | Editing a task file needs no image rebuild; changing the Dockerfile, Godot or the agent CLI does |
| An agent backend (scaffold) | `geb/scaffold/` | The `@<scaffold>` segment of the four-part result slug separates runs automatically |
| Result fields / the outcome vocabulary | `geb/schema.py`, `geb/scaffold/common.py`, `geb/pipeline/judge.py` | Changing the convention means updating [`status-fields.md`](./status-fields.md) to match |
| Models | `conf/models/default.yaml` (the registry, gitignored) | `solution=<name>` picks it up |
| Concurrency / timeouts / network / record window | `conf/config.yaml` (overridable as `a.b=c` on the command line) | — |

## How to add things

- **A new task**: `game/` + `judge/` + `solutions/{proper,naive}` + `viz/` + `task.yaml` (with its
  `scenarios:` table), passing **acceptance gates 0-8** (blueprint-stage double machine check /
  black-box review / information-boundary audit / independent recomputation / red-team probes /
  determinism and host-container agreement / shape review / difficulty discipline / degenerate-
  solution record; repo-scale producer tasks have additional gates). The gate table and every
  decision rule are in the paper's task-authoring appendix. A new task ships with `view.gd` and
  `viz/` from the start.
- **A new model**: add an entry to `conf/models/default.yaml` (`api_type` / `id` / `base_url` /
  `api_key` / `defaults`) shaped like the existing ones; `solution=<name>` then runs it.
- **A new scaffold**: add a backend under `geb/scaffold/` (follow the shape of `claude_code.py` /
  `codex.py` / `opencode.py`, sharing the outcome vocabulary in `common.py`). The `@<scaffold>`
  dimension of the result path separates it automatically.
- **pass@k**: `sample_num=<k>` runs several samples; `sample_idx=<i>` targets or re-runs one.

## Testing and verification

- **Unit tests**: `tests/` (pytest).
- **Three-way calibration**: proper passes everything, and naive passes baseline while breaking on
  hidden along the armed press axis (`armed == broken`, zero off-axis). After changing a judge, all
  three have to be re-judged before the change counts as established.
- **Determinism**: the same (scenario, seed) must be bit-identical across runs (checksum/MD5). Cells
  involving navigation-mesh rebakes on door closure carry an inherent jitter of about one frame per
  rebake (see the pitfalls below), so regression checks must run **serially**.
- **Container equals host**: a host Godot binary of the same version runs the same judge.gd logic as
  the container, so a judge change can be checked on the host first and confirmed in the container
  after — command in [`running.md`](./running.md#quick-host-check-no-container).
- **Recording cross-check**: a finished recording is automatically cross-checked key by key against
  that cell's `result.json` (`[cross-check OK]`). Note that **cross-check OK does not prove the
  video has more than one frame** — acceptance must also check the frame count (ffprobe
  `nb_read_frames`).

## Pitfalls

- **Missing image** (`ImageNotFound` / `No such image`): without `geb-all` nothing runs. Rebuild per
  [`running.md`](./running.md#docker-image-geb-all) (which needs the vendored Godot binary and agent
  CLI tarball under `third_party/`).
- **Navigation rebake is not perfectly deterministic**: closing a door triggers a NavServer rebake
  with an inherent jitter of about one frame each (a two-door scenario accumulates about two). It
  does not flip a verdict; the recording cross-check relaxes the frames and time keys in proportion
  to the rebake count. **Regression runs must be serial** — running them in parallel adds further
  jitter.
- **Nested subagents inside a container**: an agent calling a nested subagent inside the solve
  container is very slow and can hit the container timeout (`solve.timeout.container`, 3600s by
  default).
- **rc=137 can be misattributed to `inner_timeout`**: see
  [`status-fields.md`](./status-fields.md#known-pitfalls-when-reading-results).
- **Four task-authoring lessons**: (1) judges need **tightrope margin** — zero construction margin
  kills correct solutions; (2) ORCA determinism requires avoidance multithreading to be off
  (`project.godot` pins this); (3) units arriving at a station should dock in place travelling
  straight, and keep feeding the simulation; (4) assertions must **bind to consequences, not to
  ceremonial labels** — asserting from frame 0 kills legitimate divisions of labour.
