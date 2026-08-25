from __future__ import annotations

import asyncio
from pathlib import Path

import hydra
from omegaconf import DictConfig, ListConfig

COMMANDS = {"solve", "judge", "run", "record"}


def _discover_all_tasks() -> tuple[str, ...]:
    """Every judgeable task: a tasks/<name>/task.yaml, excluding probe_* (sealed probes that
    validate the pipeline but are not part of the benchmark — see geb-task-author.md). cwd is the
    repo root (the CLI must run there), so tasks/ resolves relatively. Sorted for deterministic
    order across runs."""
    tasks_dir = Path("tasks")
    names = sorted(
        p.parent.name for p in tasks_dir.glob("*/task.yaml")
        if not p.parent.name.startswith("probe_")
    )
    if not names:
        raise SystemExit(f"task=all found no tasks/*/task.yaml under {tasks_dir.resolve()} "
                         f"(run the CLI from the repo root)")
    return tuple(names)


def _resolve_tasks(raw: object) -> tuple[str, ...]:
    """Resolve the single ``task=`` field to a task tuple — one key covers all three shapes:
      - a single name        task=atom_move_navigation
      - several names         task=[a,b]  or the quoted  task='a,b'  (Hydra rejects bare a,b)
      - the whole library     task=all    -> tasks/*/task.yaml, probe_* excluded
    (Hydra parses [a,b] into a ListConfig and 'a,b' into a string we split here — the old
    task=/tasks= split is gone; this single key subsumes both.)"""
    if isinstance(raw, (list, ListConfig)):
        names = [str(x).strip() for x in raw if str(x).strip()]
    else:
        names = [part.strip() for part in str(raw).split(",") if part.strip()]
    if not names:
        raise SystemExit("task= must name at least one task (or 'all')")
    if "all" in names:
        return _discover_all_tasks()
    return tuple(names)


@hydra.main(version_base=None, config_path="../conf", config_name="config")
def _hydra_main(cfg: DictConfig) -> None:
    """Single entry: every run selector (command / task / sample_num / sample_idx / scenario /
    seed / rerun) is an ordinary Hydra config field overridden the standard key=value way. The
    only things done here that Hydra can't express declaratively are: dispatch on ``command``,
    expand ``task`` (comma/list/all), and derive the pass@k sample indices."""
    project_root = Path.cwd()

    command = str(cfg.command)
    if command not in COMMANDS:
        raise SystemExit(f"unknown command '{command}' — pass command=<{'|'.join(sorted(COMMANDS))}>")

    tasks = _resolve_tasks(cfg.task)
    rerun = bool(cfg.rerun)

    # pass@k sample selection: sample_idx=<i> pins ONE sample (targeted re-run / top-up / record
    # pick); otherwise sample_num=<k> covers 0..k-1. Only consumed for model solutions — reference
    # solutions (proper/naive) are deterministic and have no sample dimension.
    sample_idx = cfg.sample_idx
    if sample_idx is not None:
        samples: tuple[int, ...] = (int(sample_idx),)
    else:
        samples = tuple(range(max(1, int(cfg.sample_num))))

    # scenario=/seed= locate ONE cell (scenario × seed) within ONE task; they are meaningless
    # across a task set, so fail-fast when several tasks are in play (record uses them; a targeted
    # judge may later). This makes the single-task requirement explicit instead of letting a lower
    # layer trip over a scenario name that only some tasks define.
    scenario = cfg.scenario
    seed = cfg.seed
    if (scenario is not None or seed is not None) and len(tasks) != 1:
        raise SystemExit(
            f"scenario=/seed= select a cell within ONE task, but task= resolved to "
            f"{len(tasks)} tasks: {list(tasks)}")

    if command == "solve":
        from geb.pipeline.solve import run_solve
        asyncio.run(run_solve(cfg, project_root, tasks, rerun=rerun, samples=samples))
    elif command == "judge":
        from geb.pipeline.judge import run_judge
        asyncio.run(run_judge(cfg, project_root, tasks, rerun=rerun, samples=samples))
    elif command == "record":
        from geb.pipeline.record import run_record
        asyncio.run(run_record(cfg, project_root, tasks,
                               scenario=scenario, seed=seed,
                               sample=(int(sample_idx) if sample_idx is not None else None)))
    else:  # run
        from geb.pipeline.run import run_all
        asyncio.run(run_all(cfg, project_root, tasks, rerun=rerun, samples=samples))


def main() -> None:
    """Console entry point (pyproject [project.scripts] geb = geb.cli:main). No bespoke argv
    pre-parsing anymore — Hydra owns the whole command line."""
    _hydra_main()


if __name__ == "__main__":
    main()
