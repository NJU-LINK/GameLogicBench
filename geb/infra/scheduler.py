from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from geb.schema import read_seed_result


class AsyncScheduler:
    """Run async jobs with bounded concurrency (mirrors C2T's AsyncScheduler)."""

    def __init__(self, concurrency: int) -> None:
        self.semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(self, jobs: Iterable[Callable[[], Awaitable[None]]]) -> None:
        async def guarded(job: Callable[[], Awaitable[None]]) -> None:
            async with self.semaphore:
                await job()

        await asyncio.gather(*(guarded(job) for job in jobs))


def is_seed_done(result_json: Path) -> bool:
    """Idempotent skip: a seed is done once it holds a usable verdict (usable==true — the judge
    ruled, or a deterministic solution failure whose re-run just re-fails). A not-usable cell
    (usable==false: infra_error / config error) is transient/excluded, so it stays not-done and
    the next run re-judges it."""
    if not result_json.exists():
        return False
    data = read_seed_result(result_json)
    return bool(data) and bool(data.get("usable"))
