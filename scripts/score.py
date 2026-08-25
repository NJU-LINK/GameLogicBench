#!/usr/bin/env python
"""On-demand scoring: read the result/ tree live, print the PASS/FAIL matrix, write nothing.

    python scripts/score.py                         # whole library x every solution
    python scripts/score.py --task atom_move_navigation
    python scripts/score.py --task a,b --solution kimi-k3@cc@high

Stance: print only, never persist, never accumulate. A score is always computed from the result
tree as it stands right now, and no cross-run summary file is maintained -- a shared summary file
would, under several models running concurrently, accumulate other models' numbers, stale history
and half-written state, misleading whoever (or whatever) reads it. Run it whenever you want a
number; please do not add persistence or merging to this script.

Scoring conventions:
  - Reference solutions (proper/naive): result/<sol>/<task>/test/...          -> one entry, no
    sample dimension.
  - Model solutions: result/<sol>/<task>/sample_<i>/test/...  -> per-sample detail plus a
    task-level pass@k (a sample "solves" a task iff every (scenario, seed) cell passes;
    pass@k = any sample solved it).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Make `python scripts/score.py` able to import geb: only the script's own directory lands on
# sys.path, not the repo root. geb is not installed into site-packages -- normally it is found
# because cwd is on the path -- so prepend the repo root (this file's grandparent) and geb
# resolves from any cwd. tasks/ and result/ are still resolved relative to cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Run from the repo root: cwd provides ./tasks and ./result, same convention as the CLI.
from geb.pipeline.common import load_task, tasks_commit
from geb.schema import TaskSpec

_SAMPLE_DIR = re.compile(r"^sample_(\d+)$")
_MISSING = object()          # cell has no result.json (distinct from None = present but unreadable)
_CHUNK = 64                  # cells per thread task, see _read_all


def _discover_all_tasks(project_root: Path) -> tuple[str, ...]:
    """Every library task: tasks/<name>/task.yaml, excluding probe_* (sealed probes that are not
    part of the benchmark). Same rule as geb.cli._discover_all_tasks; sorted so the order is
    deterministic across runs."""
    names = sorted(
        p.parent.name for p in (project_root / "tasks").glob("*/task.yaml")
        if not p.parent.name.startswith("probe_"))
    if not names:
        raise SystemExit(f"--task all found no tasks/*/task.yaml under "
                         f"{(project_root / 'tasks').resolve()} (run from the repo root)")
    return tuple(names)


def _resolve_tasks(raw: str, project_root: Path) -> tuple[str, ...]:
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names:
        raise SystemExit("--task must name at least one task (or 'all')")
    if "all" in names:
        return _discover_all_tasks(project_root)
    return tuple(names)


def _scandir(path: Path | str) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:                                  # missing or unreadable: treat as empty
        return []


def _scan_result_tree(result_root: Path, tasks: tuple[str, ...],
                      solutions_filter: frozenset[str] | None,
                      ) -> tuple[dict[tuple[str, str], list[int]],
                                 dict[tuple[str, str], list[str]]]:
    """One recursive scandir to learn the shape of the result tree, returning (layout, cells):
         layout {(sol, task): [sample_id, ...]}    empty list = flat layout (reference solution)
         cells  {(sol, task): ["<cell dir>/result.json", ...]}   paths are str, not Path

    The result tree often sits on a network filesystem, where one readdir fetches a whole
    directory's entries in a single round trip while per-file exists() costs one round trip each.
    Do not switch back to a per-task glob: the last segment of
    `*/<task>/sample_*/test/*/result.json` is a literal name, so pathlib calls exists() on every
    candidate directory -- two probes per task over the library is tens of thousands of round
    trips, and the probed paths are then discarded while the very same files are read again right
    after. Paths stay str throughout: there are tens of thousands of them at this layer, and
    Path's construction plus hashing (twice per cell when used as a dict key) is real CPU cost at
    that volume.
    """
    task_set = set(tasks)
    layout: dict[tuple[str, str], list[int]] = {}
    cells: dict[tuple[str, str], list[str]] = {}
    for sol_e in _scandir(result_root):
        if not sol_e.is_dir():
            continue
        if solutions_filter is not None and sol_e.name not in solutions_filter:
            continue
        for task_e in _scandir(sol_e.path):
            if not task_e.is_dir() or task_e.name not in task_set:
                continue
            samples: list[int] = []
            test_dirs: list[str] = []
            for lvl_e in _scandir(task_e.path):      # sample_<i>/ or, for references, test/
                if not lvl_e.is_dir():
                    continue
                if m := _SAMPLE_DIR.match(lvl_e.name):
                    samples.append(int(m.group(1)))
                    test_dirs.append(f"{lvl_e.path}/test")
                elif lvl_e.name == "test":
                    test_dirs.append(lvl_e.path)
            key = (sol_e.name, task_e.name)
            layout[key] = sorted(samples)
            # Collect every cell in the directory, including stale ones left behind when a
            # task.yaml was renamed or lost seeds. This keeps solution discovery equivalent to
            # the original glob: any result.json under (sol, task) means it ran. The tally still
            # counts only cells in the spec, so a stale cell is read but never scored.
            cells[key] = [f"{c.path}/result.json"
                          for td in test_dirs for c in _scandir(td) if c.is_dir()]
    return layout, cells


def _read_cell(path: str) -> object:
    """Read one cell's result.json: missing file -> _MISSING; file present but unreadable -> None
    (the same None geb.schema.read_seed_result returns, counted downstream as FAIL). Three states
    exist so that solution discovery keys on file presence alone and is unaffected by corrupt
    content."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return _MISSING
    except Exception:
        return None


def _read_all(cells: dict[tuple[str, str], list[str]]) -> dict[str, object]:
    """Read every result.json into memory concurrently (living only in this process, never
    written back). On a network filesystem a single-file read costs roughly 1 ms of round trip,
    so reading tens of thousands of cells serially takes tens of seconds; the GIL is released
    during read, so a thread pool overlaps those waits instead of summing them. Work is submitted
    in _CHUNK blocks because ThreadPoolExecutor ignores map's chunksize -- with one future per
    cell, future-scheduling overhead exceeds the concurrency win when the cache is warm (reads
    nearly free) and ends up slower than serial."""
    paths = [p for group in cells.values() for p in group]
    if not paths:
        return {}
    chunks = [paths[i:i + _CHUNK] for i in range(0, len(paths), _CHUNK)]
    with ThreadPoolExecutor(max_workers=32) as pool:
        read = pool.map(lambda chunk: [_read_cell(p) for p in chunk], chunks)
        return dict(zip(paths, [v for chunk in read for v in chunk]))


def score(result_root: Path, project_root: Path, *, tasks: tuple[str, ...],
          solutions_filter: frozenset[str] | None = None) -> dict:
    """Read the result tree live and return {task: {sol: entry}}, writing nothing. When
    solutions_filter is None, every solution discovered in the tree is scored for each task."""
    summary: dict = {"_meta": {"tasks_commit": tasks_commit(project_root)}}
    layout, cells = _scan_result_tree(result_root, tasks, solutions_filter)
    results = _read_all(cells)
    for task_name in tasks:
        task = load_task(project_root, task_name)
        task_entry: dict = {}
        # A solution counts as having run this task once any result.json exists -- the same
        # rule for the flat and the sampled layout.
        solutions = sorted(
            sol for (sol, tn), paths in cells.items()
            if tn == task_name and any(results[p] is not _MISSING for p in paths))
        for sol in solutions:
            sol_task_dir = result_root / sol / task_name
            sample_ids = layout[(sol, task_name)]
            if not sample_ids:
                task_entry[sol] = _tally_test_dir(task, sol_task_dir / "test", results)
                continue
            per_sample = {
                f"sample_{i}": _tally_test_dir(
                    task, sol_task_dir / f"sample_{i}" / "test", results)
                for i in sample_ids
            }
            # solved iff every judged cell passed AND nothing is unresolved: total>0 guards the
            # 0/0 case, excluded==0 means no infra cell is still pending a re-judge (evidence
            # complete). An excluded cell leaves the task "not yet solved", not "failed".
            solved = [i for i in sample_ids
                      if (ov := per_sample[f"sample_{i}"]["overall"])["total"] > 0
                      and ov["excluded"] == 0
                      and ov["pass"] == ov["total"]]
            task_entry[sol] = {
                "pass_at_k": {
                    "k": len(sample_ids),
                    "solved": bool(solved),
                    "solved_samples": solved,
                },
                "samples": per_sample,
            }
        summary[task_name] = task_entry
    return summary


def _tally_test_dir(task: TaskSpec, test_dir: Path,
                    results: dict[str, object]) -> dict:
    """Tally one test/ tree (one reference solution, or one sample of a model solution).
    Cells come from the pre-read `results` map, not from disk — see _read_all."""
    td = str(test_dir)                                 # str keys: saves two Path builds per cell
    per_cell: dict[str, bool | None] = {}
    per_scenario: dict[str, dict] = {}
    tally = {"public": [0, 0, 0], "hidden": [0, 0, 0]}  # [pass, total, excluded]
    for sc in task.scenarios:
        sc_pass, sc_total, sc_excl = 0, 0, 0
        for seed in sc.seeds:
            cell = results.get(f"{td}/{sc.name}_{seed}/result.json", _MISSING)
            data = None if cell is _MISSING else cell
            if data and not data.get("usable", True):
                # Not a usable data point (infra_error / config error) — not a verdict. Exclude from
                # the denominator (neither pass nor fail) and leave it to be re-judged; never let it
                # read as FAIL. (A usable solution failure — killed_* / crashed / controller_invalid —
                # stays a real FAIL below: usable and not passed.)
                per_cell[f"{sc.name}_{seed}"] = None
                sc_excl += 1
                continue
            ok = bool(data and data.get("usable") and data.get("passed"))
            per_cell[f"{sc.name}_{seed}"] = ok
            sc_pass += ok
            sc_total += 1
        per_scenario[sc.name] = {"pass": sc_pass, "total": sc_total, "excluded": sc_excl,
                                 # canonical axis[:tier] serialisation — the atom↔combo join
                                 # key for absorbed-tier analysis; empty off combo (atoms'
                                 # press defaults aren't consumed by their judge)
                                 "press": sc.press_arg if task.press_enabled else ""}
        tally[sc.tier][0] += sc_pass
        tally[sc.tier][1] += sc_total
        tally[sc.tier][2] += sc_excl
    return {
        "public": {"pass": tally["public"][0], "total": tally["public"][1],
                   "excluded": tally["public"][2]},
        "hidden": {"pass": tally["hidden"][0], "total": tally["hidden"][1],
                   "excluded": tally["hidden"][2]},
        "overall": {"pass": tally["public"][0] + tally["hidden"][0],
                    "total": tally["public"][1] + tally["hidden"][1],
                    "excluded": tally["public"][2] + tally["hidden"][2]},
        "scenarios": per_scenario,
        "cells": per_cell,
    }


def _print_matrix(summary: dict) -> None:
    print(f"\n==== geb score (pass / total) | tasks_commit="
          f"{summary.get('_meta', {}).get('tasks_commit', '')} ====")
    for task_name, sols in summary.items():
        if task_name == "_meta":
            continue
        print(f"task: {task_name}")
        if not sols:
            print("  (no matching solutions in result tree)")
        for sol, e in sols.items():
            if "samples" in e:
                pk = e["pass_at_k"]
                mark = "PASS" if pk["solved"] else "fail"
                print(f"  {sol:<24} pass@{pk['k']} {mark} "
                      f"(solved samples: {pk['solved_samples'] or '-'})")
                for sample_name, se in e["samples"].items():
                    parts = " ".join(f"{name}:{sc['pass']}/{sc['total']}"
                                     for name, sc in se["scenarios"].items())
                    excl = se["overall"].get("excluded", 0)
                    excl_str = f" [{excl} infra excl]" if excl else ""
                    print(f"    {sample_name:<12} hidden {se['hidden']['pass']}"
                          f"/{se['hidden']['total']:<3}{excl_str} | {parts}")
            else:
                parts = " ".join(f"{name}:{sc['pass']}/{sc['total']}"
                                 for name, sc in e["scenarios"].items())
                excl = e["overall"].get("excluded", 0)
                excl_str = f" [{excl} infra excl]" if excl else ""
                print(f"  {sol:<24} hidden {e['hidden']['pass']}/{e['hidden']['total']:<3}{excl_str} "
                      f"| {parts}")
    print("====================================\n")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="On-demand scoring: read the result tree live, print the PASS/FAIL matrix, "
                    "write nothing.")
    ap.add_argument("--result-dir", default="./result",
                    help="result tree root (default ./result)")
    ap.add_argument("--task", default="all",
                    help="tasks: one name / comma-separated a,b / all "
                         "(default; tasks/*/task.yaml excluding probe_*)")
    ap.add_argument("--solution", default=None,
                    help="only score these solutions (comma-separated whitelist); "
                         "default = every solution found in the result tree")
    args = ap.parse_args(argv)

    project_root = Path.cwd()
    result_root = Path(args.result_dir)
    if not result_root.is_absolute():
        result_root = project_root / result_root
    if not result_root.exists():
        raise SystemExit(f"result dir not found: {result_root} (run from the repo root)")

    tasks = _resolve_tasks(args.task, project_root)
    solutions_filter = (
        frozenset(s.strip() for s in args.solution.split(",") if s.strip())
        if args.solution else None)

    summary = score(result_root, project_root, tasks=tasks,
                    solutions_filter=solutions_filter)
    _print_matrix(summary)


if __name__ == "__main__":
    main(sys.argv[1:])
