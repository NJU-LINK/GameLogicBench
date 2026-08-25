# GameLogicBench — Code Structure

> Answers "**how the code is organised and how the mechanisms work**". Environment and running are
> in [`running.md`](./running.md), changing the code in [`maintenance.md`](./maintenance.md), result
> fields in [`status-fields.md`](./status-fields.md). The task-authoring discipline (the three-layer
> information boundary, judge health criteria) is documented in the paper's appendix.

## Overview

```
geb/          # harness source
│  cli.py         # Hydra entry point; command= dispatches to a phase
│  schema.py      # data contracts: TaskSpec / AgentResult / ContainerRunResult
│  pipeline/      # solve.py · judge.py · record.py · run.py (orchestration) + common.py (slug/layout/runner)
│  harness/       # godot.py: overlay assembly + judge/record command construction
│  scaffold/      # agent backends: claude_code.py · codex.py · opencode.py + common.py
│  infra/         # container.py (docker) · egress.py (egress whitelist) · scheduler.py (concurrency pool)
conf/         # Hydra configuration (config.yaml + the models/ registry)
tasks/        # the task library (its own git repository, untracked here)
result/       # grading output (gitignored)
logs/         # per-run resolved-config snapshot and logs
third_party/  # vendor/godot binary + packages/ agent CLI tgz (for the image build, gitignored)
tests/        # pytest
scripts/      # on-demand analysis (score.py scoring · cost.py spend · leaderboard.py · pricing/)
docs/         # documentation
Dockerfile    # the geb-all image (headless judge and recording via xvfb/software rendering/ffmpeg)
```

## Data and call flow: `solve → judge` (scoring on demand via `scripts/score.py`)

GameLogicBench is an agent benchmark for the game domain. A solution under test completes a
generation or development task inside a deterministic Godot engine; the harness grades it in an
**isolated container** using **black-box behavioural assertions** — zero LLM judging — and re-judges
along **perturbation axes** (new seeds, larger scale, out-of-distribution scenarios).

- **solve** (agent-in-container): game/ is copied to a writable duplicate and mounted into the
  container, the agent is given an open-ended BRIEF, and it writes the controller in place; the
  workspace IS the artefact. judge/ and hidden seeds **never enter the solve container**.
- **judge**: one `--network=none` container per seed — overlay the project root, build the level from
  the seed, load the controller under test, and assert on observable behaviour frame by frame (wall
  collisions, arrival, timeout, re-pathing through a dynamic door). Each cell writes its own
  `result/<sol>/<task>/…/result.json`.
- **Scoring** is a separate on-demand step, not part of the pipeline: `python scripts/score.py` reads
  the result tree live and prints the PASS/FAIL matrix plus the public/hidden pass-rate gap. It
  **persists and accumulates nothing** — a score is always computed from the tree as it stands.

`solution` naming a registry model -> solve then judge; `proper` / `naive` -> judge the calibration
reference directly.

## One task's directory

```
tasks/<name>/                        # prefix atom_* (capability atom) / combo_* (composite) / repo_* (real upstream repo)
├── game/       # the F5-runnable Godot project given to the agent: main.tscn + runtime/enemies
│               #   + view.gd (the single visual implementation) + ai/<brain>.gd (the deliverable,
│               #   a stub by default) + level/sim_core/assertions twins + CC0 assets + README
├── judge/      # the frozen black-box judge, never seen by the agent: judge.gd + level.gd
│               #   (the geometry authority) + sim_core.gd + assertions.gd
├── solutions/{proper,naive}/…       # calibration references, carrying only their differences
└── viz/        # the recording layer (optional, never seen by the agent): record.gd (a thin shell
                #   extending judge.gd) + record.tscn + a windowed project.godot. Visual logic the
                #   preview never uses — e.g. skinning a hidden-only event — also lives here
```

- **game/** is the runnable project handed to the agent, which implements its controller in
  `ai/<brain>.gd` (across several files if it likes). `view.gd` is the **single implementation** of
  that task's visuals, shared by the F5 preview and the recording (canvas `_draw` or node/Sprite
  based, per task).
- **judge/** is the frozen judge, invisible to the agent. judge.gd holds an `_on_frame` hook gated by
  `_record_mode`, so the grading path pays nothing, and viz/record.gd inherits from it to attach
  rendering.
- **solutions/** are the calibration references: `proper` should pass public and hidden; `naive`
  should pass public and break on hidden.
- **viz/** existing is what makes a task recordable (`has_viz` tests for `viz/record.tscn`).
  Recording is a **presentation channel only**: it does not grade and carries no consistency
  contract (its cross-check output is advisory). **Art layering**: atom and combo tasks are
  **always vector visuals** (zero assets in the agent's environment, and better video legibility);
  **repo-scale tasks record their real art** — an upstream renderer may share the global RNG with
  the logic, so its recorded trajectory is not co-generated with the headless grading even though
  each is deterministic, which is noted per task in the record comment of its task.yaml. Tasks whose
  upstream art may not be redistributed get no recording.
- `tasks/` is **its own git repository** (gitignored here, so the nesting does not conflict; the
  library version is traceable through each result's `tasks_commit`).

## Overlay assembly (how the project root is built for judge and record)

### The judge overlay
The judge `cp`s out a project root inside the container in the **order
`game → solution → judge` (judge last)**:

```
cp game/ → /tmp/proj      # base: the runnable project
overlay solution/         # the controller under test (or a reference)
overlay judge/ (last)     # the authoritative frozen judge goes on top
```

`judge.tscn` then runs with `/tmp/proj` as the project root, and the controller is loaded through
`load("res://logic/controller.gd")` (its preloaded sibling scripts resolve normally). This order
gives **tamper resistance for free**: an agent editing a frozen file has no effect, since the file
is overwritten last, and hidden seeds travel by argv and never touch the filesystem. The host-side
quick check is in [`running.md`](./running.md#quick-host-check-no-container).

### Boundary inversion (repo-scale producer tasks)

A producer task's deliverable lives at a real `res://src/**` path — an engine module the agent
implements — which "judge overlays last" would wrongly overwrite. The **opt-in** task.yaml field
`judge: {deliverable_paths: [res://…]}` declares the paths the agent owns: the judge snapshots them
before the overlay and restores them after, so **the agent's engine survives the authoritative
overlay while the rest of the world is still overwritten by it**. Without the field the overlay
shell is byte-identical to before, so existing tasks are unaffected; `--controller` receives
`deliverable_paths[0]`. judge/ should ship a **stub** at each deliverable path rather than leaving a
hole: with inversion disabled the stub runs and proper fails, which proves the inversion is actually
load-bearing.

### rendered judging (the modality for visual tasks)
task.yaml declares `judge: {mode: rendered, window: {width, height}}` (headless by default, so
existing tasks are unaffected). Overlay assembly is identical to headless; only the godot invocation
changes (the rendered branch of `build_judge_command`):

```
godot --headless --import        # warm the render cache (as record does; its exit does not gate)
xvfb-run -s "-screen 0 WxHx24" godot --rendering-driver opengl3 res://judge.tscn -- <same argv>
```

- **window must equal the scene viewport** (a probe red line). The judge pipeline pins
  `LIBGL_ALWAYS_SOFTWARE=1` and `LP_NUM_THREADS` into the environment, part of the bit-exact
  determinism contract — the same llvmpipe stack record uses.
- **The key difference from record: there is no authoritative visual overlay.** The agent's lighting
  and occlusion ARE the object under test; what is graded is the image it renders.
- After waiting for its settle frame the judge scene grabs a single frame via
  `get_viewport().get_texture().get_image()` and makes **pixel-relation assertions**: behind a wall
  is darker than open ground at the same distance, brightness falls monotonically with distance,
  beyond range returns to ambient. Never a golden-image comparison. Sample coordinates are always
  derived from that seed's spec geometry, and geometric grey zones (shadow-cone edges, annuli) are
  exempt — the line-of-sight grey-zone discipline carried into pixel space. Tolerance is spent on
  geometric grey zones, never on render noise: under the pinned software stack, re-running a cell is
  bit-identical at the pixel level.
- A rendered task still gets a viz/ layer (record.gd still extends judge.gd; record still goes
  through `--write-movie`).

### The record overlay
`build_record_command` (`geb/harness/godot.py`) adds two layers on top of the judge overlay:

```
cp game/ (or the workspace) → /tmp/proj    # base
overlay solution/ (reference mode)
overlay judge/ (the authoritative judge)
overlay the official game/ view.gd|visual/|assets/  # workspace mode: authoritative visuals are
                                                    # force-overlaid over the agent's edits
overlay viz/                               # record.tscn becomes the main scene
godot --headless --import                  # uniform pre-import (texture cache; a no-op for vector tasks)
xvfb-run godot --write-movie …             # windowed + Movie Maker, rendering the judge's simulation
ffmpeg transcode → /out/run.mp4            # not gated by godot's PASS/FAIL exit code
```

A finished recording is automatically cross-checked key by key against that seed's judge
`result.json` (`[record] … [cross-check OK]`, `_crosscheck` in `geb/pipeline/record.py`). The window
defaults to `record.window` in `conf/config.yaml` and is overridden per task through
`record.window.{width,height}` in its task.yaml, which must match the viewport in
`viz/project.godot`.

## Result layout

```
result/
├── <solution>/<task>/test/<scenario>_<n>/result.json        # one cell's judge result
│                                                            #   passed/usable/outcome + broken_link + metrics
└── <solution>/<task>/test/<scenario>_<n>/record/run.mp4     # the recording (optional, kept separate)
```

- The `<solution>` segment: references use their bare name, `proper` / `naive`. **A model solution
  gets a four-segment slug**, `<model>@<scaffold>@<effort>@<net>` (`result_slug` in
  `geb/pipeline/common.py`) — scaffold = agent backend (`cc` = vendored claude-code / `codex` /
  `opencode`), effort = reasoning tier, net = solve network regime (`egress` / `bridge`). Any of the
  four differing lands in its own subtree, so runs never overwrite each other.
- Model solutions carry a sample dimension for pass@k:
  `result/<four-segment slug>/<task>/sample_<i>/{workspace,solve,test}/…`, one independent subtree
  per sample.
- Scores are never persisted: `python scripts/score.py` reads those result.json files live and
  computes the matrix, printing per-sample detail plus a task-level `pass_at_k` (a sample solves a
  task when every one of its cells passes; pass@k = any sample solved it).
- `logs/<timestamp>_<solution>/` holds the resolved-config snapshot and logs.
- Each sample's `solve/status.json` records **how the session ended** (`outcome` / `usable` /
  `produced` plus rc/turns/cost in the `session` sub-object), and each task-sample pair also gets a
  `solve/judge.json` scenario-level pass summary. Field and enum meanings are in
  [`status-fields.md`](./status-fields.md).

## Core mechanisms (and why they are built this way)

### Why the overlay resists tampering
The project root is assembled by `cp` in the order `game → solution → judge (last)`, and that order
is not arbitrary: an agent editing a frozen file achieves nothing (the authoritative judge version
lands on top of it, and the judge does not run main.tscn anyway), and hidden seeds travel by argv and
never touch the filesystem. **Tamper resistance comes from copy order, not permissions.**
Multi-file dependencies resolve naturally, since brain and helpers all sit at real `res://` paths.

### One geometry authority ("the secret is the seed, not the algorithm")
`level.gd` — walls built in code, driven by a seed — is the sole geometry authority. game/'s preview
calls it with a public seed and the judge calls it with a hidden one: same algorithm, different seed,
perturbation axis intact. **What is withheld is the seed value, never the algorithm**: level.gd's
comments in game/ are sanitised and leak no strategy. **Geometry is never built from a TileMap** (a
PNG only provides the visual skin, generated at runtime from the spec rects, so visuals can never
drift from geometry). `sim_core.gd` — bake parameters, make_state, door-closing choreography,
constants — is twinned between judge and game, which is what makes **the F5 preview behave exactly
as the judge grades**.

### The scenario table is what creates a real OOD gap
The judge tiers world difficulty through the `scenarios:` table in task.yaml (`--scenario <name>`
travels by argv, and `level.gd` dispatches on the scenario name to **hand-designed** constructors;
seeds only perturb within a safe numeric band):
- **baseline** (a reserved name, equivalent to the public tier) is a static, simple map — the twin of
  game/level.gd, bit-identical on a bare seed. The agent develops and previews against it.
- **hidden scenarios** (every other row) are harder, often dynamic, hand-built structures applied
  only at judge time. Their RNG mixes in a hash of the scenario name so each scenario is an
  independent stream. Seeds are numbered locally within a scenario, the numbers carry no meaning, and
  the table is the authority.
- **The interface is identical between public and hidden** (a `nav_map`, for instance, is provided on
  both sides), which is what makes it fair: naive fails because it did not write a robust solution,
  not because the harness trapped it.

So naive — planning once — passes baseline and breaks on hidden, while proper — querying the world
each frame — holds up without being told anything. That is the **ideal OOD gap**: held-out scenarios
within an already-disclosed rule family, times parameter perturbation. The three-layer information
boundary discipline is in the paper's task-authoring appendix.

### The press instrument for composite tasks: explicit arming, so a first failure is attributable
A composite task's hidden scenarios mostly arm ONE axis at a time — arming everything at once
collapses first-failure attribution, so **the single-axis matrix is the foundation**. Each hidden
scenario **arms a designated** trap among the composed links and defuses the rest back to the
coincidental-compliance shape they have in public. Once the single-axis matrix is filled in, an
**all-press cell** can be layered on top: everything armed at once, which only yields new information
for columns that were green across every single axis. For those, a failure means load degradation,
and `broken_link` names which link went first. A column with a known single-axis defect merely
re-fails that known defect here, carrying zero new information. Attribution does not rest on this
cell (it comes from the single-axis matrix); this cell reads load. In detail:

- **press is an experimental configuration, not a random variable.** The scenario table is explicit:
  every hidden scenario of a composite task must carry a `press: {axis: tier}` mapping, and the
  harness refuses to load an absent one, a list form, or the reserved `default` tier. The harness
  passes `--press axis:tier[,axis:tier]` to the judge; each scenario has its own RNG stream and
  nothing is drawn from a placeholder. Balanced coverage (at least 3 cells per axis) and "the trap
  actually bites" (recalibration changes the assignment, not the scenario set) are both controlled by
  the table directly. An unknown or missing scenario name makes the judge fail fast with
  `unknown_scenario`, written as `usable:false` so it is excluded and re-run — not a genuine
  infra_error.
- **One vocabulary, plus a tier dimension.** An axis name is the atom task name minus its `atom_`
  prefix, which is also the `broken_link` value and the `press` value in result.json — zero mapping.
  **Tier names are anchored to atoms**: a tier on an atom axis must equal a hidden scenario that
  really exists in that atom (the trap's lineage; recalibrating its numeric band or geometry for the
  composite world is normal and expected). Where a composite construction has no counterpart, either
  backfill the atom or change the composite to align — never invent a tier the atom does not have.
  Axes original to a composite task (with no counterpart atom) name their own tiers. This is what
  makes "which atom tier did this composite absorb" machine-readable: it comes from each scenario's
  `press`, `scripts/score.py` prints that value per scenario for composite tasks, and an atom-to-
  composite cross-analysis can join on it directly with every join target guaranteed to exist. Tier
  names never enter the RNG stream, so renaming one is a mechanical re-judge with zero change to the
  world. `meta.composes` lists the atoms combined; a pass is always `outcome: "pass"`. Orchestration-
  glue failures (completion / engagement / return) do not occupy axis names.
- **The gating criterion**: an axis that can be defused without trivialising the task **must** enter
  the press rotation, because an always-on axis swallows every first-failure attribution for
  solutions carrying that defect. Only an axis that cannot be turned off is ambient (static
  narrow-gate geometry, say). Ambient pressure catching a real defect is still legitimate
  attribution.
- **Coupled cells (several axes armed together)**: expressed in one line as
  `press: {axisA: tier, axisB: tier}`, they measure whether a state-reading axis's discipline holds
  during the multi-frame committed manoeuvre a structural-depth axis forces. When a single model
  passes both axes separately, arming them together is a genuinely blank measurement — load
  degradation is real. The attribution contract relaxes to **broken_link within the armed set**,
  while off-axis remains a task-authoring error; acceptance adds a **per-axis probe** beyond the
  regular gates (one probe per axis, naive on that axis and correct on the others, which must break
  on its own axis). Two-axis cells are only built for combinations with a behavioural hypothesis,
  never as an axis-by-tier Cartesian product. The all-press cell is the exception: it selects no
  combination, it is simply the single "everything on" cell — a load reading, not an attribution
  instrument.
- **Calibration closes with an axis reconciliation**: for single-axis cells, armed == broken with zero
  off-axis (both naive and each axis's dedicated probe must satisfy this); for coupled cells,
  broken_link within the armed set, with the two probes each landing on their own axis.

### The record pipeline: one source per axis, plus authoritative visuals
`record` renders the judge's simulation into real gameplay video, for the paper and for human
verification. The central design rule is that **neither axis is allowed a second implementation**:

- **One simulation source**: `viz/record.gd` is a thin shell that `extends judge.gd`. judge.gd holds
  an `_on_frame(vs)` hook gated by `_record_mode` (false by default), so the grading path pays
  nothing and stays bit-identical; the record subclass flips the switch and implements the hook. The
  simulation loop exists in exactly one place, judge.gd, which makes **the video faithful to the
  judge by construction** — it runs that loop, on that scenario and seed. The finished recording is
  then cross-checked key by key against the judge's result.json.
- **One visual source**: each task's drawing lives only in `game/view.gd`, shared by the F5 preview
  and the recording, so the video shows exactly the preview's visuals with no imitation that could
  drift. **Art layering**: atom and combo tasks are always vector (zero assets in the agent's
  environment, structural uniformity across the library, and instruments like HP bars and lock-on
  rings read best on video). Real textures belong to **repo-scale tasks**, which bring their own art
  for the recording to render faithfully. Such tasks need the "authoritative visual overlay" boundary
  reconsidered: if the art itself is the deliverable, the recording should preserve the agent's
  visual changes and the tamper-resistance boundary should be redrawn around judge-related files
  instead.
- **The visual boundary rule**: **visual logic the preview never uses belongs in viz/, never in
  game/.** Skinning a hidden-only event — putting a texture on a door after it closes, say — is
  record-only knowledge, and writing it into a comment or function name in game/ is a category-2
  leak (the scenario-difficulty layer).
- **Authoritative visuals resist tampering**: when recording in workspace mode, the official `game/`
  visual layer (`view.gd` / `visual/` / `assets/`, listed in `VISUAL_PATHS` in
  `geb/harness/godot.py`) is force-overlaid on top of the agent's workspace after the judge layer, so
  an agent breaking or deliberately tampering with the visuals cannot undermine the credibility of a
  human-verification video. Same principle as the overlay: copy order, not permissions.
- **Uniform pre-import**: one `godot --headless --import` pass before rendering builds the texture
  cache (needed by tasks with assets, a no-op for vector tasks) — one command for every task, with no
  per-task branching.
- **The single-frame trap for re-exec judges**: if a judge's `_ready` performs a bootstrap re-exec
  (wipe `.godot`, `--import`, restart as a subprocess), Movie Maker in record mode captures only the
  one frame belonging to the parent process before the re-exec. The fix is for the judge to declare a
  `_record_mode` flag (viz/record.gd sets it in `_init`), add `and not _record_mode` to the re-exec
  condition in `_ready`, and run the simulation inline in record mode (the record pipeline's
  pre-import plus Movie Maker's pinned frame rate replace `--fixed-fps`). **Acceptance must check the
  mp4's frame count** (ffprobe `nb_read_frames`); a cross-check OK does not prove more than one
  frame.

### The solve phase (agent-in-container)
`geb/scaffold/claude_code.py` runs the vendored CLI with
`-p --output-format stream-json --model <id> --permission-mode bypassPermissions`, judging completion
from the last line's subtype rather than trusting rc. `geb/pipeline/solve.py` copies game/ to a
writable duplicate, supplies the BRIEF, and treats the resulting workspace as the artefact, which the
judge then uses as its overlay base. solve's own verdict is **artefact existence only** — real
grading belongs to the judge — and a session timeout is recorded as a note rather than a hard FAIL.
Note that an agent invoking a nested subagent inside the container is very slow and can hit the
container timeout (`solve.timeout.container`, 3600s by default).

### Isolation and egress
The solve container mounts only the writable game/ duplicate and the BRIEF; hidden seeds are injected
by the judge through argv at grading time. Model endpoint credentials arrive as `ANTHROPIC_*` env
vars, and the container sits on the `geb-egress` whitelist network, which admits only DNS and the
model endpoint IPs from the registry and rejects everything else (and injects no host proxy env in
this mode). judge and record are always `--network=none`. The mechanism is in `geb/infra/egress.py`;
the operational account and verification are in
[running.md, "The solve egress whitelist"](running.md#the-solve-egress-whitelist-geb-egress).

### Foundational design points (still in force)
- Grading is **fixed deterministic code**, not an LLM rubric.
- Task count and perturbations come from **our own parameterised generators**, whose interfaces we
  control, rather than reusing an external benchmark's heterogeneous projects.
- Generators and controllers are **loaded dynamically** from mounted absolute paths, so the harness
  project is never bound to a particular solution.
- The harness `cp`s to `/tmp/h` inside the container before running, which sidesteps Godot writing
  its `.godot` import cache under a read-only mount.
