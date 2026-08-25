#!/usr/bin/env python
"""Live leaderboard: per-model pass rates aggregated by level (atom/combo/repo), three tables.

    python scripts/leaderboard.py                       # whole library x every model column
    python scripts/leaderboard.py --solution claude-opus-5@cc@high
    python scripts/leaderboard.py --task atom_hitbox,combo_boss

Same stance as score.py: read the result tree live, print, write nothing (do not add persistence
or merging). The three tables are three aggregations of ONE read, not three scans:
  - pass@3      a task counts as solved if ANY of its 3 samples solved it. Optimistic upper
                bound, and the one most inflated by luck.
  - worst@3     a task counts as solved only if ALL 3 samples solved it. Stability lower bound.
  - per-sample  each sample is treated as an independent run scored pass@1. A model with a
                single sample has only its #s0 row.

Conventions:
  - Model columns only; reference solutions (proper/naive) do not enter the board.
  - Solved = every judged cell of the task passed (score.score decides this).
  - Half-finished work is excluded: a (task, sample) enters the denominator only once
    solve/judge.json exists, i.e. the judge phase closed it out. A task still mid-run is
    therefore never misread as a fail, and the denominator you see is "tasks closed out".
  - pass@3 / worst@3 only count tasks whose 3 samples ALL closed out (the metric is undefined
    otherwise); each per-sample row is denominated by that sample's own closed-out count. So
    denominators can differ between tables AND between rows of one table -- read the denominator
    before comparing scores, and never put 35/63 next to 36/72.
  - level comes from the task-name prefix (atom_/combo_/repo_, the library naming convention
    that matches task.kind).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))         # scripts/ -> import score
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -> import geb

from score import _resolve_tasks, score

LEVELS = ("atom", "combo", "repo")
K = 3                              # the k in pass@k / worst@k (= samples per task by convention)
FULL = frozenset(range(K))
_W = 40                            # model column width: longest slug + " #s0"


def _fmt(p: int, t: int) -> str:
    return f"{p}/{t} ({p / t * 100:.0f}%)" if t else "-"


def _sort_key(s: str) -> tuple:
    # Fixed order: scaffold (second slug segment, e.g. cc/codex) then model name. Never by
    # score, so rows do not jump around while a run is in progress.
    parts = s.split("@")
    return (parts[1] if len(parts) > 1 else "", s)


def _collect(summary: dict, result_root: Path,
             ) -> dict[str, list[tuple[str, frozenset[int], frozenset[int]]]]:
    """Flatten summary into {sol: [(level, closed-out samples, solved samples), ...]}, shared by
    all three tables. The half-finished gate lives here: a sample without judge.json never enters
    `done`, and a (sol, task) with no closed-out sample at all is dropped entirely."""
    recs: dict[str, list[tuple[str, frozenset[int], frozenset[int]]]] = {}
    for task_name, sols in summary.items():
        if task_name == "_meta":
            continue
        level = task_name.split("_", 1)[0]
        for sol, e in sols.items():
            if "samples" not in e:                       # reference solution (flat layout)
                continue
            done = frozenset(
                int(s.split("_")[1]) for s in e["samples"]
                if (result_root / sol / task_name / s / "solve" / "judge.json").exists())
            if not done:
                continue
            recs.setdefault(sol, []).append(
                (level, done, frozenset(e["pass_at_k"]["solved_samples"])))
    return recs


def _rows_at_k(recs: dict, agg) -> list[tuple[str, dict]]:
    """Rows for pass@k / worst@k: only tasks whose K samples all closed out; `agg` decides
    whether a task counts as solved. A solution with fewer than K samples gets no row at all --
    pass@K is undefined for it, and a row with a small denominator would only mislead."""
    rows = []
    for sol in sorted(recs, key=_sort_key):
        st = {lv: [0, 0] for lv in LEVELS}
        for level, done, solved in recs[sol]:
            if not FULL <= done:
                continue
            st.setdefault(level, [0, 0])
            st[level][1] += 1
            st[level][0] += agg(solved)
        if any(v[1] for v in st.values()):
            rows.append((sol, st))
    return rows


def _rows_per_sample(recs: dict) -> list[tuple[str, dict]]:
    """Rows for per-sample: one row per (solution, sample), denominated by that sample's own
    closed-out task count."""
    rows = []
    for sol in sorted(recs, key=_sort_key):
        for i in sorted({i for _, done, _ in recs[sol] for i in done}):
            st = {lv: [0, 0] for lv in LEVELS}
            for level, done, solved in recs[sol]:
                if i not in done:
                    continue
                st.setdefault(level, [0, 0])
                st[level][1] += 1
                st[level][0] += i in solved
            rows.append((f"{sol} #s{i}", st))
    return rows


def _print_table(title: str, note: str, rows: list[tuple[str, dict]]) -> None:
    header = f"{'model':<{_W}}" + "".join(f"{lv:>16}" for lv in LEVELS) + f"{'total':>18}"
    print(f"\n---- {title} ----")
    print(f"     {note}")
    print(header)
    print("-" * len(header))
    for label, st in rows:
        tp = sum(v[0] for v in st.values())
        tt = sum(v[1] for v in st.values())
        print(f"{label:<{_W}}" + "".join(f"{_fmt(*st.get(lv, [0, 0])):>16}" for lv in LEVELS)
              + f"{_fmt(tp, tt):>18}")
    if not rows:
        print("(no model cells satisfy this table's criteria)")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=f"Model leaderboard by level: pass@{K} / worst@{K} / per-sample "
                    f"(computed live from the result tree, nothing written).")
    ap.add_argument("--result-dir", default="./result", help="result tree root (default ./result)")
    ap.add_argument("--task", default="all", help="tasks: one name / comma-separated / all (default)")
    ap.add_argument("--solution", default=None,
                    help="only score these solutions (comma-separated whitelist)")
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
    summary = score(result_root, project_root, tasks=tasks, solutions_filter=solutions_filter)

    lib = {lv: 0 for lv in LEVELS}                       # library size per level, for context
    for task_name in summary:
        if task_name == "_meta":
            continue
        level = task_name.split("_", 1)[0]
        if level in lib:
            lib[level] += 1
    recs = _collect(summary, result_root)

    commit = summary.get("_meta", {}).get("tasks_commit", "")
    print(f"\n==== geb leaderboard | tasks_commit={commit} ====")
    print(f"library: atom {lib['atom']} / combo {lib['combo']} / repo {lib['repo']}")
    print("Denominator = tasks closed out (a task whose judge has not landed is excluded). "
          "Denominators\ndiffer between tables and between rows -- read them before comparing scores.")

    _print_table(f"pass@{K} (solved if ANY of the {K} samples solved it - optimistic bound)",
                 f"only tasks whose {K} samples all closed out",
                 _rows_at_k(recs, lambda solved: bool(solved & FULL)))
    _print_table(f"worst@{K} (solved only if ALL {K} samples solved it - stability bound)",
                 f"only tasks whose {K} samples all closed out",
                 _rows_at_k(recs, lambda solved: FULL <= solved))
    _print_table("per-sample pass@1 (each sample as an independent run)",
                 "denominated by that sample's own closed-out count",
                 _rows_per_sample(recs))
    print()


if __name__ == "__main__":
    main(sys.argv[1:])
