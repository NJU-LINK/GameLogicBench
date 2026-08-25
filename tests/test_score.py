import importlib.util
import json
from pathlib import Path

from geb.pipeline.common import load_task

ROOT = Path(__file__).resolve().parents[1]
TASK = "atom_move_navigation"

# scripts/ is not a package, so load the script under test straight from its file via importlib.
_spec = importlib.util.spec_from_file_location("geb_score", ROOT / "scripts" / "score.py")
score_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(score_mod)


def _write(result_root: Path, sol: str, scenario: str, seed: int, passed: bool,
           sample: int | None = None) -> None:
    base = result_root / sol / TASK
    if sample is not None:
        base = base / f"sample_{sample}"
    d = base / "test" / f"{scenario}_{seed}"
    d.mkdir(parents=True, exist_ok=True)
    # The verdict fields a real result.json carries: usable (is this a usable data point) + passed.
    (d / "result.json").write_text(json.dumps({"usable": True, "passed": passed}))


def test_score_matrix(tmp_path):
    # proper: all pass; naive: all fail — the calibration matrix (flat, no samples)
    task = load_task(ROOT, TASK)
    for sc, s in task.cells:
        _write(tmp_path, "proper", sc, s, True)
        _write(tmp_path, "naive", sc, s, False)

    before = set(tmp_path.rglob("*"))
    summary = score_mod.score(tmp_path, ROOT, tasks=(TASK,))

    e = summary[TASK]
    n_total = len(task.cells)
    n_public = len(task.baseline.seeds)
    assert e["proper"]["overall"]["pass"] == n_total
    assert e["proper"]["public"]["pass"] == n_public
    assert e["proper"]["hidden"]["pass"] == n_total - n_public
    assert e["naive"]["overall"]["pass"] == 0
    # per-scenario rollup covers every declared scenario
    assert set(e["proper"]["scenarios"]) == {sc.name for sc in task.scenarios}
    # reference solutions carry no sample layer
    assert "samples" not in e["proper"]
    # Core invariant: scoring only reads — the set of files in the result tree is identical
    # before and after the call.
    assert set(tmp_path.rglob("*")) == before


def test_score_pass_at_k(tmp_path):
    # model solution: sample_0 fails one hidden cell, sample_1 solves every cell ->
    # task-level pass@2 solved by sample 1 only ("solved" = ALL cells pass)
    task = load_task(ROOT, TASK)
    sol = "m@cc"
    failed_cell = task.cells[-1]
    for sc, s in task.cells:
        _write(tmp_path, sol, sc, s, (sc, s) != failed_cell, sample=0)
        _write(tmp_path, sol, sc, s, True, sample=1)

    summary = score_mod.score(tmp_path, ROOT, tasks=(TASK,))

    e = summary[TASK][sol]
    assert e["pass_at_k"] == {"k": 2, "solved": True, "solved_samples": [1]}
    n_total = len(task.cells)
    assert e["samples"]["sample_0"]["overall"]["pass"] == n_total - 1
    assert e["samples"]["sample_1"]["overall"]["pass"] == n_total
    sc_name, seed = failed_cell
    assert e["samples"]["sample_0"]["cells"][f"{sc_name}_{seed}"] is False


def test_score_solution_filter(tmp_path):
    # --solution is a whitelist: only matching solutions are scored, so no other model leaks in.
    task = load_task(ROOT, TASK)
    for sc, s in task.cells:
        _write(tmp_path, "proper", sc, s, True)
        _write(tmp_path, "naive", sc, s, False)

    summary = score_mod.score(tmp_path, ROOT, tasks=(TASK,),
                              solutions_filter=frozenset({"proper"}))
    assert set(summary[TASK]) == {"proper"}
