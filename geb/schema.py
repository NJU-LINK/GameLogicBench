from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ScenarioSpec:
    """One hand-designed test scenario of a task (task.yaml `scenarios:` table, 2026-07-05).

    A scenario fixes the STRUCTURE (topology / traps / events — hand-designed experiment
    configuration, never drawn from an rng stream); its seeds only perturb values inside a
    safe numeric band. The reserved name "baseline" is the public tier — the twin of
    game/level.gd that the agent previews against. Every other scenario is hidden.

    ``press`` (combo only): the pressure axes this scenario arms, as (axis, tier) pairs
    (2026-07-16 mapping upgrade; 2026-07-17 default abolition). Axis words ≡ atom task names
    minus the ``atom_`` prefix ≡ broken_link — one vocabulary, zero mapping. Tier naming
    standard: on an atom axis the tier MUST equal a real hidden scenario of that atom (the
    trap's lineage — bands recalibrated per combo world are fine); combo-original axes name
    their tiers freely. Combo hidden scenarios must spell the mapping explicitly — the
    "default" tier and the legacy spellings are rejected for combos at load time.
    Scenario names stay pure display labels — machine identity lives here.

    Atom tasks keep the legacy spellings (their judges never consume press):
    absent → ``((<scenario name>, "default"),)``; list ``[axis, ...]`` → each at "default";
    mapping ``{axis: tier}`` → as written (tier may be a list for stacked same-axis tiers).
    """

    name: str
    seeds: tuple[int, ...]          # local numbering, 1..k per scenario
    press: tuple[tuple[str, str], ...] = ()   # ((axis, tier), ...); empty for baseline/atom

    @property
    def tier(self) -> str:
        return "public" if self.name == BASELINE_SCENARIO else "hidden"

    @property
    def press_axes(self) -> tuple[str, ...]:
        """Armed axes, deduplicated in declaration order (attribution set: broken_link ∈ axes)."""
        seen: list[str] = []
        for axis, _ in self.press:
            if axis not in seen:
                seen.append(axis)
        return tuple(seen)

    @property
    def press_arg(self) -> str:
        """Canonical CLI serialisation: ``axis[:tier][,axis[:tier]]`` — the "default" tier is
        omitted, so every pre-2026-07-16 cell serialises byte-identically to the old axis list
        (rng streams / archived results stay untouched)."""
        return ",".join(
            axis if tier == PRESS_DEFAULT_TIER else f"{axis}:{tier}"
            for axis, tier in self.press)


BASELINE_SCENARIO = "baseline"
PRESS_DEFAULT_TIER = "default"
# The agent's deliverable path when a task declares nothing (suite-wide convention: agent code
# lives at res://logic/controller.gd). Repo producer tasks override via judge.deliverable_paths.
DEFAULT_DELIVERABLE_PATHS = ("res://logic/controller.gd",)


@dataclass(frozen=True)
class TaskSpec:
    """A task loaded from tasks/<name>/task.yaml."""

    name: str
    task_dir: Path
    scenarios: tuple[ScenarioSpec, ...]                 # baseline first, then yaml order
    record: dict = field(default_factory=dict)          # record: {window} — set only when the world is not 640x480
    judge: dict = field(default_factory=dict)           # judge: {mode, resources} — resources are a frozen contract
    solve: dict = field(default_factory=dict)           # solve: {resources} — per-task override (repo tasks may raise)
    kind: str = "atom"                                  # atom | combo | repo | probe (sealed)
    press_semantics: bool | None = None                 # does the judge consume --press? (None = follow kind,
                                                        # i.e. true only for combo). A few repo-kind tasks do
                                                        # consume it and declare true explicitly.
    meta: dict = field(default_factory=dict)            # composes / intent / press_rule ...

    @property
    def press_enabled(self) -> bool:
        """Is the press/argv channel on? An explicit press_semantics wins; otherwise combo
        tasks default to on. harness and score decide "do we pass --press" from this property
        alone, never from kind directly."""
        return self.press_semantics if self.press_semantics is not None else self.kind == "combo"

    @property
    def judge_mode(self) -> str:
        """Judging modality for VIS-class tasks: "headless" (default — behavioral assertions,
        zero rendering) or "rendered" (pixel-relation assertions; declared here, command
        lands with the first VIS atom)."""
        return str(self.judge.get("mode", "headless"))

    @property
    def deliverable_paths(self) -> tuple[str, ...]:
        """res:// paths the AGENT owns — its deliverable module(s).

        Default: the suite-wide controller at res://logic/controller.gd (agent code lives under
        res://logic/; every existing task uses this and is unaffected). Repo producer tasks
        may declare game-tree paths (e.g. the status engine at
        res://src/combat/status/status_engine.gd) via ``judge.deliverable_paths`` — then the judge
        overlay is INVERTED for these paths: the authoritative judge files overlay everything
        EXCEPT them, whose agent version survives (boundary inversion — tamper-proofing covers
        the world,
        the whitelist carves out the deliverable). The FIRST path is the primary deliverable
        (existence-checked at judge time)."""
        raw = self.judge.get("deliverable_paths")
        if not raw:
            return DEFAULT_DELIVERABLE_PATHS
        return tuple(str(p) for p in raw)

    @property
    def inverts_overlay(self) -> bool:
        """True iff the task explicitly declares agent-owned paths (boundary-inversion opt-in).
        Absent => False => the overlay command is byte-identical to the pre-inversion behavior."""
        return bool(self.judge.get("deliverable_paths"))

    @property
    def baseline(self) -> ScenarioSpec:
        return self.scenarios[0]

    @property
    def hidden_scenarios(self) -> tuple[ScenarioSpec, ...]:
        return self.scenarios[1:]

    @property
    def cells(self) -> tuple[tuple[str, int], ...]:
        """Every (scenario, seed) judging cell, baseline first."""
        return tuple((sc.name, s) for sc in self.scenarios for s in sc.seeds)

    def scenario(self, name: str) -> ScenarioSpec:
        for sc in self.scenarios:
            if sc.name == name:
                return sc
        raise KeyError(f"task {self.name} has no scenario '{name}': "
                       f"{[s.name for s in self.scenarios]}")

    @property
    def judge_dir(self) -> Path:
        return self.task_dir / "judge"

    @property
    def game_dir(self) -> Path:
        return self.task_dir / "game"

    @property
    def viz_dir(self) -> Path:
        return self.task_dir / "viz"

    @property
    def has_viz(self) -> bool:
        """A task supports video recording if it ships a viz/ layer (record scene + project)."""
        return (self.viz_dir / "record.tscn").exists()

    @property
    def has_game(self) -> bool:
        """A formal task ships a playable game/ project (agent edits it; judge overlays on top)."""
        return (self.game_dir / "project.godot").exists()

    def solution_dir(self, solution: str) -> Path:
        return self.task_dir / "solutions" / solution


@dataclass(frozen=True)
class ContainerRunResult:
    """Outcome of one container run.

    ``cause`` is the single-source discriminated end signal (2026-07-05; consumers upstack reuse
    these words verbatim — never invent a parallel vocabulary):
      - "exit"           — process ran to completion; rc is its own answer (judge: 0=PASS, 1=FAIL)
      - "abnormal_exit"  — process never ran / died abnormally (rc 126/127 exec error, 139
                           segfault, ...); environment problem, NOT a verdict and NOT a timeout
      - "inner_timeout"  — the in-container `timeout` gate fired (rc 124 TERM; rc 137 non-OOM =
                           its SIGKILL escalation — same gate, one word, rc keeps the distinction)
      - "oom"            — docker killed the container at mem_limit (rc 137 + State.OOMKilled)
      - "wall_timeout"   — the outer wall-clock poll expired and we killed the container
      - "daemon_error"   — the docker API itself failed (not the workload)
    """

    rc: Optional[int]
    cause: str = "exit"          # THE single-source end signal; usability/outcome derive from it
    elapsed_s: float = 0.0
    stdout: str = ""
    error: str = ""


@dataclass(frozen=True)
class AgentResult:
    """Outcome of one agent invocation in the solve phase.

    ``outcome`` is THE single "what happened" word (code and JSON use the same term — there is no
    separate status). Each word has a globally fixed ``usable``; solve derives usability as
    ``outcome not in {"aborted", "daemon_error", "stream_broken"}``:
      - "completed"      — clean terminal event, agent finished on its own (usable)
      - "agent_error"    — cc reported ANY non-success subtype, the turn cap
                           (error_max_turns) included (usable)
      - "stream_broken"  — container exited normally but the trajectory has no terminal event
                           (gateway drop / 429 retry-exhaustion / CLI crash): the workspace never
                           reached turn.completed, so NOT usable — rerun (the break often precedes
                           any controller write; counting it usable left the cell silently missing)
      - "timeout"        — the in-container `timeout` gate or outer wall clock fired (usable —
                           slow session, judged anyway on whatever controller was produced)
      - "oom"            — docker killed the container at mem_limit (usable — model ran itself
                           out of memory; a real behavior signal)
      - "aborted"        — the agent CLI process died abnormally (segfault / exec error / external
                           kill): a tool/orchestration failure, NOT usable — rerun
      - "daemon_error"   — the docker API itself failed: NOT usable — rerun
    ``num_turns``/``duration_ms`` come from the terminal event when present (real turns, not
    stream chunks)."""

    rc: Optional[int]
    subtype: Optional[str]        # stream-json terminal-event subtype ("success" on clean finish)
    outcome: str                  # see class docstring (THE single-source word)
    num_turns: Optional[int] = None
    duration_ms: Optional[int] = None
    cost_usd: Optional[float] = None
    trajectory_path: str = ""
    usage: Optional[dict] = None
    error: str = ""


@dataclass
class SeedResult:
    """Judge verdict for one (solution, scenario, seed) cell.

    Three orthogonal axes, no status field (see judge._build_seed_result / normalize_result):
      - ``outcome``  — THE single "what happened" word (pass / a failure signature / build →
                       controller_invalid / killed_oom / killed_timeout / crashed / infra_error /
                       unknown_scenario ...). Each word has a globally fixed ``usable``.
      - ``usable``   — is this a usable data point? False => excluded from rates and re-judged
                       (infra_error / config errors); True => counts (including solution failures).
      - ``passed``   — the verdict: True/False when judged; None when not usable (N/A)."""

    solution: str
    task: str
    scenario: str
    seed: int
    seed_set: str                     # tier of the scenario: "public" (baseline) | "hidden"
    usable: bool                      # False => excluded from rates & re-judged; True => counts
    passed: Optional[bool]            # True/False = judged; None = not usable, N/A
    outcome: str                      # THE single-source "what happened" word
    broken_link: str = ""             # combo attribution: which armed axis broke (empty off combo)
    metrics: dict = field(default_factory=dict)   # task-specific measures + run values (controller/frames/…)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "solution": self.solution,
            "task": self.task,
            "scenario": self.scenario,
            "seed": self.seed,
            "seed_set": self.seed_set,
            "usable": self.usable,
            "passed": self.passed,
            "outcome": self.outcome,
            "broken_link": self.broken_link,
            "metrics": self.metrics,
            "error": self.error,
        }


@dataclass(frozen=True)
class ResultLayout:
    """Filesystem layout of one solution×task result tree (×sample for model solutions).

    Reference solutions (proper/naive — deterministic, no sampling variance):
        result/<solution>/<task>/test/<scenario>_<seed>/result.json
    Model solutions (pass@k, 2026-07-06): one subtree per sample —
        result/<model>@<scaffold>/<task>/sample_<i>/{workspace,solve,test}/...

    ``sample`` is None for reference solutions (flat layout) and the sample index for model
    solutions. The test/ wrapper separates judging artifacts from their siblings (workspace/,
    solve/) so "re-judge everything" is one directory delete and per-scenario aggregation is
    one glob. Cell paths are always CONSTRUCTED from task.yaml — never parsed back out of
    directory names (scenario names must not end in `_<digits>`, enforced at task load)."""

    result_root: Path
    solution: str
    task: str
    sample: Optional[int] = None

    @property
    def task_dir(self) -> Path:
        return self.result_root / self.solution / self.task

    @property
    def base_dir(self) -> Path:
        """Root of {workspace,solve,test}: task_dir itself (reference solutions) or
        task_dir/sample_<i> (model solutions)."""
        if self.sample is None:
            return self.task_dir
        return self.task_dir / f"sample_{int(self.sample)}"

    @property
    def workspace_dir(self) -> Path:
        """The agent's edited game/ project (solve phase output; judge uses it as baseline)."""
        return self.base_dir / "workspace"

    @property
    def solve_dir(self) -> Path:
        """Solve-session artifacts (trajectory.jsonl, status.json, prompt.txt, stderr.log)."""
        return self.base_dir / "solve"

    @property
    def test_dir(self) -> Path:
        return self.base_dir / "test"

    def cell_dir(self, scenario: str, seed: int) -> Path:
        return self.test_dir / f"{scenario}_{seed}"

    def result_json(self, scenario: str, seed: int) -> Path:
        return self.cell_dir(scenario, seed) / "result.json"


def read_seed_result(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None
