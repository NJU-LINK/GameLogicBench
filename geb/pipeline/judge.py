from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from omegaconf import DictConfig

from geb.harness.godot import (
    GAME_BIND,
    HARNESS_BIND,
    OUT_BIND,
    SOLUTION_BIND,
    WORKSPACE_BIND,
    build_judge_command,
)
from geb.infra.scheduler import AsyncScheduler, is_seed_done
from geb.pipeline.common import (
    container_runner_from_cfg,
    layout_for,
    load_models,
    load_task,
    result_slug,
)
from geb.schema import ContainerRunResult, SeedResult, TaskSpec, read_seed_result

log = logging.getLogger(__name__)


async def run_judge(
    cfg: DictConfig,
    project_root: Path,
    tasks: tuple[str, ...],
    *,
    rerun: bool = False,
    samples: tuple[int, ...] = (0,),
) -> None:
    scheduler = AsyncScheduler(int(cfg.judge.concurrency))
    # Reference solutions (proper/naive) are deterministic — no sample dimension; a model
    # solution is judged once per sample subtree (each sample = an independent solve).
    is_model = str(cfg.solution) in load_models(project_root)
    for task_name in tasks:
        for sample in (samples if is_model else (None,)):
            await judge_task(cfg, project_root, task_name, scheduler=scheduler, rerun=rerun,
                             sample=sample)


async def judge_task(
    cfg: DictConfig,
    project_root: Path,
    task_name: str,
    *,
    scheduler: AsyncScheduler,
    rerun: bool = False,
    sample: int | None = None,
) -> None:
    """Judge every (scenario, seed) cell of ONE task×sample (the per-task unit run_all pipelines
    with solve; ``sample`` is None for reference solutions). Cells run concurrently through the
    shared scheduler's semaphore, so several tasks can be in their judge phase at once without
    exceeding cfg.judge.concurrency containers total."""
    solution = str(cfg.solution)
    models = load_models(project_root)
    # result-path + verdict-label segment: reference solutions keep their bare name (proper/naive),
    # a model solution is tagged <model>@<scaffold> — same slug the solve phase wrote its workspace
    # under, so the judge reads/writes the same subtree. The bare name still keys solutions/<name>/.
    slug = result_slug(cfg, models)

    task = load_task(project_root, task_name)
    layout = layout_for(cfg, project_root, task_name, slug, sample=sample)
    runner = container_runner_from_cfg(cfg, task)
    # Rendered judging (VIS tasks) runs windowed under Xvfb + llvmpipe inside the container;
    # pin the software-GL env exactly as the record pipeline does (P0 probe: these pins are
    # part of the bit-exact-pixels contract). Headless tasks get no extra env.
    render_env = None
    if task.judge_mode == "rendered":
        render_env = {
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "LP_NUM_THREADS": str(int(cfg.get("record", {}).get("lp_num_threads", 4))),
        }

    sol_dir = task.solution_dir(solution)
    # Workspace mode: the solution is an agent (solve output at .../sample_<i>/workspace/).
    # That edited project IS the judge baseline; there is no solutions/<sol>/ reference dir.
    # The deliverable path is task-declared (default res://logic/controller.gd; a repo producer
    # may own a game-tree path — see TaskSpec.deliverable_paths).
    deliverable_rel = task.deliverable_paths[0].removeprefix("res://").strip("/")
    workspace_dir = layout.workspace_dir
    use_workspace = task.has_game and (workspace_dir / "project.godot").exists()
    if use_workspace:
        if not (workspace_dir / deliverable_rel).exists():
            raise FileNotFoundError(
                f"agent workspace has no deliverable: {workspace_dir / deliverable_rel}")
    else:
        # Reference-solution overlay: the solution ships its deliverable at the same path (+ helpers).
        if not (sol_dir / deliverable_rel).exists():
            raise FileNotFoundError(
                f"solution deliverable not found: {sol_dir / deliverable_rel}")

    cells = list(task.cells)
    total = len(cells)
    sample_note = "" if sample is None else f" sample={sample}"
    log.info(f"[judge] task={task_name} solution={slug}{sample_note} cells={total} "
             f"(concurrency={int(cfg.judge.concurrency)})")

    counter = {"done": 0}

    def make_job(scenario: str, seed: int):
        async def job() -> None:
            res_json = layout.result_json(scenario, seed)
            if is_seed_done(res_json) and not rerun:
                _log(counter, total, "skip", task, slug, scenario, seed, 0.0, sample=sample)
                return
            cell_dir = layout.cell_dir(scenario, seed)
            cell_dir.mkdir(parents=True, exist_ok=True)
            # No stale reads: _build_seed_result trusts whatever result.json it finds, so the
            # container must be the only possible writer. Delete any prior round's file first —
            # a failed container (wall-kill/OOM/cp error) then reads as infra_error, never as
            # the previous round's verdict.
            res_json.unlink(missing_ok=True)
            mounts = {
                str(task.judge_dir): {"bind": HARNESS_BIND, "mode": "ro"},
                str(cell_dir): {"bind": OUT_BIND, "mode": "rw"},
            }
            if use_workspace:
                mounts[str(workspace_dir)] = {"bind": WORKSPACE_BIND, "mode": "ro"}
            else:
                mounts[str(sol_dir)] = {"bind": SOLUTION_BIND, "mode": "ro"}
                mounts[str(task.game_dir)] = {"bind": GAME_BIND, "mode": "ro"}
            start = time.monotonic()
            run = await runner.run(
                command=build_judge_command(task, scenario, seed, workspace=use_workspace),
                mounts=mounts,
                env=render_env,
                network="none",
            )
            dur = time.monotonic() - start
            # _build_seed_result reads the container's result.json (if any) and normalizes it:
            # task metrics fold into a `metrics` sub-object, judge.gd's raw `status` is dropped,
            # `outcome` is the single source. A container death with no result.json is attributed
            # by cause word.
            sr = _build_seed_result(task, slug, scenario, seed, run, res_json)
            res_json.write_text(_dump(sr))
            verdict = "PASS" if sr.passed else ("ERR" if not sr.usable else "FAIL")
            _log(counter, total, verdict, task, slug, scenario, seed, dur, sr, sample=sample)

        return job

    await scheduler.run(make_job(sc, s) for sc, s in cells)
    _write_judge_summary(task, layout, slug, sample)


def _write_judge_summary(task: TaskSpec, layout, slug: str, sample: int | None) -> None:
    """After judging a task's cells, drop a scenario-level pass summary at solve/judge.json for
    quick eyeballing (one line per scenario — pass / total, plus excluded when any cell is not
    usable). Reads back the just-written result.json cells; no per-cell detail (that lives in the
    cells themselves, and finer rollups come from `python scripts/score.py`)."""
    import json
    scenarios: dict = {}
    tot_pass = tot = tot_excl = 0
    for sc in task.scenarios:
        p = t = e = 0
        for seed in sc.seeds:
            data = read_seed_result(layout.result_json(sc.name, seed))
            if not data:
                continue
            if not data.get("usable", True):
                e += 1
                continue
            t += 1
            p += 1 if data.get("passed") else 0
        entry = {"pass": p, "total": t}
        if e:
            entry["excluded"] = e
        scenarios[sc.name] = entry
        tot_pass += p; tot += t; tot_excl += e
    summary = {
        "task": task.name,
        "solution": slug,
        "sample": sample,
        "scenarios": scenarios,
        "overall": {"pass": tot_pass, "total": tot, "excluded": tot_excl},
        "judged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    layout.solve_dir.mkdir(parents=True, exist_ok=True)
    (layout.solve_dir / "judge.json").write_text(json.dumps(summary, indent=2) + "\n")


# Container deaths that are orchestration/host-level, not caused by the controller running inside a
# healthy judge container (see _build_seed_result). exec error / SIGINT / SIGTERM — a segfault
# (rc=139) is NOT here: that is the controller crashing the engine, i.e. a solution failure.
_INFRA_EXIT_CODES = frozenset({126, 127, 130, 143})


def _is_infra_death(cause: str, rc: int | None) -> bool:
    """A container death with no result.json: infra (orchestration/host) vs solution (the
    controller overran a healthy container). The judge container is a fixed image + fixed
    resources + network=none + deterministic sim, and task authoring guarantees the reference
    solutions always reach a verdict in it — so oom / inner_timeout / segfault here are the
    controller's doing, while daemon/wall-clock/exec/external-kill failures are non-selective
    (they hit the reference solutions too)."""
    if cause in ("daemon_error", "wall_timeout"):
        return True
    if cause == "abnormal_exit" and rc in _INFRA_EXIT_CODES:
        return True
    return False


# Judge-side outcomes that are NOT usable data points: excluded from rates, re-judged next run.
# Config errors (unknown_scenario / no_press_axis / unknown_press_axis) are authoring/command
# mistakes that fail-fast in judge.gd; infra_error is a true orchestration-level death.
_JUDGE_UNUSABLE = frozenset({
    "infra_error", "unknown_scenario", "no_press_axis", "unknown_press_axis",
})

# Framework fields that live at the normalized top level; every OTHER key in a raw judge.gd /
# record.gd dict is a task-specific measure or run value and folds into `metrics`. The legacy
# words `status` (dropped) and `pass` (promoted to `passed`) are excluded so they never leak.
_FRAMEWORK_KEYS = frozenset({
    "solution", "task", "scenario", "seed", "seed_set",
    "usable", "passed", "outcome", "broken_link", "error", "status", "pass",
})


def collect_metrics(raw: dict) -> dict:
    """Fold a raw flat judge/record dict's task-specific measures + run values (controller,
    frames, switches, …) into a `metrics` sub-object; framework keys are excluded."""
    return {k: v for k, v in raw.items() if k not in _FRAMEWORK_KEYS}


def normalize_from_raw(raw: dict) -> dict:
    """Normalize a raw flat judge.gd / record.gd result dict to the canonical shape: framework
    fields at the top level + a `metrics` sub-object. Shared by the judge has-result path and
    record cross-check so both sides are directly comparable. judge.gd's own `status` is dropped
    (outcome is the single source), its `build_error` becomes the clearer `controller_invalid`,
    and `pass` becomes `passed` (None when not usable)."""
    outcome = str(raw.get("outcome", "pass"))
    if outcome == "build_error":
        outcome = "controller_invalid"   # submission's controller won't load/compile/expose on_tick
    usable = outcome not in _JUDGE_UNUSABLE
    passed = None if not usable else bool(raw.get("pass", False))
    norm = {
        "usable": usable,
        "passed": passed,
        "outcome": outcome,
        "broken_link": str(raw.get("broken_link", "")),
        "metrics": collect_metrics(raw),
        "error": str(raw.get("error", "")),
    }
    for coord in ("scenario", "seed"):   # carry coords a raw dict happens to have (record raw)
        if coord in raw:
            norm[coord] = raw[coord]
    return norm


def _build_seed_result(
    task: TaskSpec, solution: str, scenario: str, seed: int,
    run: ContainerRunResult, res_json: Path
) -> SeedResult:
    data = read_seed_result(res_json)
    seed_set = task.scenario(scenario).tier
    if data is None:
        # No result.json — the container died before the judge could rule. Attribute by cause word
        # (single source, see ContainerRunResult): oom / inner_timeout / segfault happened while
        # executing THIS controller in a container proven healthy by the reference solutions =>
        # usable solution failure (killed_* / crashed, counted as FAIL like controller_invalid);
        # daemon / wall-clock / exec error / external kill are orchestration-level => infra_error,
        # not usable (excluded and re-judged).
        detail = f"{run.cause} (rc={run.rc}, {run.elapsed_s:.1f}s)"
        if run.error:
            detail += f": {run.error}"
        if _is_infra_death(run.cause, run.rc):
            outcome, usable, passed = "infra_error", False, None
        else:
            outcome = {"oom": "killed_oom", "inner_timeout": "killed_timeout"}.get(run.cause, "crashed")
            usable, passed = True, False
        return SeedResult(
            solution=solution, task=task.name, scenario=scenario, seed=seed, seed_set=seed_set,
            usable=usable, passed=passed, outcome=outcome, error=detail,
        )
    norm = normalize_from_raw(data)
    return SeedResult(
        solution=solution, task=task.name, scenario=scenario, seed=seed, seed_set=seed_set,
        usable=norm["usable"], passed=norm["passed"], outcome=norm["outcome"],
        broken_link=norm["broken_link"], metrics=norm["metrics"], error=norm["error"],
    )


def _dump(sr: SeedResult) -> str:
    import json
    return json.dumps(sr.to_dict(), indent=2) + "\n"


def _log(counter, total, verdict, task, solution, scenario, seed, dur,
         sr: SeedResult | None = None, sample: int | None = None) -> None:
    counter["done"] += 1
    where = f"{task.name}/{solution}" if sample is None \
        else f"{task.name}/{solution}/sample_{sample}"
    log.info(f"[{counter['done']}/{total}] {verdict} {where} "
             f"cell={scenario}_{seed} ({dur:.1f}s)")
