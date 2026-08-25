from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from omegaconf import DictConfig

from geb.infra.scheduler import AsyncScheduler
from geb.pipeline.common import load_models
from geb.pipeline.judge import judge_task
from geb.pipeline.solve import solve_task

log = logging.getLogger(__name__)


async def run_all(
    cfg: DictConfig,
    project_root: Path,
    tasks: tuple[str, ...],
    *,
    rerun: bool = False,
    samples: tuple[int, ...] = (0,),
) -> None:
    """Full pipeline: each (task, sample) unit runs its own solve→judge lifecycle independently
    (a unit enters judging the moment ITS solve finishes — no cross-unit barrier, so the slowest
    agent session never holds up units that are already done). For reference solutions the unit
    is just the task (no sample dimension).

    Scoring is NOT part of the pipeline: `run` only writes each unit's own result subtree and
    never touches a shared file, so any number of `command=run` processes on different solutions
    run without racing. Read the current score on demand with `python scripts/score.py` (reads the
    result trees fresh, prints a matrix, writes nothing) — there is no persistent summary to go stale.

    Concurrency is two independent pools: solve.concurrency agent containers (solve_sem) and
    cfg.judge.concurrency judge containers (the shared scheduler) — both can be busy at the same time.

    A unit that fails its phase (no controller produced, judge precondition missing) is isolated:
    its error is reported and the other units keep going (its cells just stay missing in the score).
    """
    # If the solution names a model in the registry (not a reference solution like proper/naive),
    # run the solve phase first so the agent produces its workspace, then judge that workspace.
    is_model = str(cfg.solution) in load_models(project_root)
    solve_sem = asyncio.Semaphore(max(1, int(cfg.solve.get("concurrency", 1))))
    scheduler = AsyncScheduler(int(cfg.judge.concurrency))

    async def _run_one(task_name: str, sample: int | None) -> None:
        if is_model:
            judgeable = await solve_task(
                cfg, project_root, task_name, sem=solve_sem, rerun=rerun, sample=sample or 0)
            if not judgeable:
                log.info(f"[run] task={task_name} sample={sample} solve yielded no controller "
                         f"— skip judge")
                return
        await judge_task(cfg, project_root, task_name, scheduler=scheduler, rerun=rerun,
                         sample=sample)

    units = [(t, i) for t in tasks for i in (samples if is_model else (None,))]
    results = await asyncio.gather(
        *(_run_one(t, i) for t, i in units), return_exceptions=True)
    for (task_name, sample), res in zip(units, results):
        if isinstance(res, BaseException):
            log.info(f"[run] task={task_name} sample={sample} pipeline error: "
                     f"{type(res).__name__}: {res}")
