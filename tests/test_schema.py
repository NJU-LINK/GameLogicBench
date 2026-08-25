from pathlib import Path

from geb.schema import ResultLayout, ScenarioSpec, SeedResult, TaskSpec


def _task() -> TaskSpec:
    return TaskSpec(
        name="t", task_dir=Path("/tmp/tasks/t"),
        scenarios=(
            ScenarioSpec(name="baseline", seeds=(1, 2)),
            ScenarioSpec(name="door_shut", seeds=(1, 2, 3), press=("door_shut",)),
        ),
    )


def test_task_scenarios():
    t = _task()
    assert t.baseline.name == "baseline"
    assert t.baseline.tier == "public"
    assert t.scenario("door_shut").tier == "hidden"
    assert t.cells == (
        ("baseline", 1), ("baseline", 2),
        ("door_shut", 1), ("door_shut", 2), ("door_shut", 3),
    )
    assert t.judge_dir == Path("/tmp/tasks/t/judge")
    assert t.game_dir == Path("/tmp/tasks/t/game")
    assert t.solution_dir("proper") == Path("/tmp/tasks/t/solutions/proper")


def test_judge_mode_defaults_headless():
    assert _task().judge_mode == "headless"
    rendered = TaskSpec(
        name="t", task_dir=Path("/tmp/tasks/t"), judge={"mode": "rendered"},
        scenarios=(ScenarioSpec(name="baseline", seeds=(1,)),),
    )
    assert rendered.judge_mode == "rendered"


def test_unknown_scenario_raises():
    t = _task()
    try:
        t.scenario("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("unknown scenario must raise, never guess")


def test_result_layout_paths():
    lay = ResultLayout(result_root=Path("/r"), solution="naive", task="t")
    assert lay.cell_dir("door_shut", 2) == Path("/r/naive/t/test/door_shut_2")
    assert lay.result_json("door_shut", 2) == Path("/r/naive/t/test/door_shut_2/result.json")
    assert lay.workspace_dir == Path("/r/naive/t/workspace")
    assert lay.solve_dir == Path("/r/naive/t/solve")


def test_result_layout_sample_paths():
    # model solutions get one subtree per pass@k sample; reference layout above stays flat
    lay = ResultLayout(result_root=Path("/r"), solution="m@cc", task="t", sample=2)
    assert lay.workspace_dir == Path("/r/m@cc/t/sample_2/workspace")
    assert lay.solve_dir == Path("/r/m@cc/t/sample_2/solve")
    assert lay.cell_dir("door_shut", 1) == Path("/r/m@cc/t/sample_2/test/door_shut_1")
    assert lay.result_json("baseline", 1) == Path("/r/m@cc/t/sample_2/test/baseline_1/result.json")


def test_seed_result_to_dict():
    # Three orthogonal axes: usable / passed / outcome. There is no status field, by design.
    sr = SeedResult(
        solution="naive", task="t", scenario="baseline", seed=2, seed_set="public",
        usable=True, passed=False, outcome="crashed",
    )
    d = sr.to_dict()
    assert d["passed"] is False
    assert d["usable"] is True
    assert d["outcome"] == "crashed"
    assert d["scenario"] == "baseline"
    assert d["seed_set"] == "public"
    assert "status" not in d
    # PCG-era fields are gone from the normalized verdict (2026-07-06 cleanup)
    for legacy in ("n_items", "n_requested", "floating_fails", "clipping_fails"):
        assert legacy not in d
