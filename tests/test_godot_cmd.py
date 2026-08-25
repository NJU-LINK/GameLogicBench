from pathlib import Path

from geb.harness.godot import (
    DELIVER_SAVE,
    GAME_BIND,
    HARNESS_BIND,
    OUT_BIND,
    SOLUTION_BIND,
    VIZ_BIND,
    WORK_PROJ,
    WORKSPACE_BIND,
    build_judge_command,
    build_record_command,
)
from geb.schema import ScenarioSpec, TaskSpec


def _atom_task() -> TaskSpec:
    return TaskSpec(
        name="t", task_dir=Path("/tmp/tasks/t"),
        scenarios=(
            ScenarioSpec(name="baseline", seeds=(1,)),
            ScenarioSpec(name="pressure", seeds=(1,), press=(("pressure", "default"),)),
        ),
    )


def _combo_task(tmp_path: Path) -> TaskSpec:
    (tmp_path / "game").mkdir(parents=True)
    (tmp_path / "game" / "project.godot").write_text("")
    return TaskSpec(
        name="t", task_dir=tmp_path,
        kind="combo",
        scenarios=(
            ScenarioSpec(name="baseline", seeds=(1, 2)),
            ScenarioSpec(name="move_navigation", seeds=(1,),
                         press=(("move_navigation", "door_shut"),)),
            ScenarioSpec(name="attack_cooldown", seeds=(1,),
                         press=(("attack_cooldown", "long_cooldown"),)),
            ScenarioSpec(name="nav_x_cooldown", seeds=(1,),
                         press=(("move_navigation", "door_shut"),
                                ("attack_cooldown", "long_cooldown"))),
        ),
    )


def test_press_is_explicit_argv(tmp_path: Path):
    task = _combo_task(tmp_path)
    # hidden scenarios carry their press as explicit axis:tier pairs (2026-07-17 tier naming)
    assert "--press move_navigation:door_shut" in build_judge_command(task, "move_navigation", 1)
    assert "--press attack_cooldown:long_cooldown" in \
        build_judge_command(task, "attack_cooldown", 1)
    # coupled press: axis:tier pairs comma-joined in declaration order, one argv token
    assert "--press move_navigation:door_shut,attack_cooldown:long_cooldown" in \
        build_judge_command(task, "nav_x_cooldown", 1)
    # baseline never carries a press axis
    assert "--press" not in build_judge_command(task, "baseline", 1)
    # atoms (kind != combo) never carry one
    assert "--press" not in build_judge_command(_atom_task(), "pressure", 1)
    # the record pipeline renders the same armed world the judge scored
    assert "--press move_navigation:door_shut" in build_record_command(task, "move_navigation", 1)


def test_unknown_scenario_fails_fast(tmp_path: Path):
    task = _combo_task(tmp_path)
    try:
        build_judge_command(task, "door_shut", 1)
    except KeyError:
        pass
    else:
        raise AssertionError("unknown scenario must raise, never guess a tier")


def test_judge_command_overlay_shapes(tmp_path: Path):
    task = _combo_task(tmp_path)
    # reference-solution overlay: game baseline -> solution -> frozen judge LAST
    cmd = build_judge_command(task, "baseline", 1)
    assert cmd.index(f"cp -r {GAME_BIND} {WORK_PROJ}") \
        < cmd.index(f"cp -r {SOLUTION_BIND}/.") \
        < cmd.index(f"cp -r {HARNESS_BIND}/.")
    assert f"--path {WORK_PROJ} res://judge.tscn" in cmd
    assert f"--out {OUT_BIND}/result.json" in cmd
    # agent workspace IS the baseline; judge overlay still wins
    wcmd = build_judge_command(task, "baseline", 1, workspace=True)
    assert f"cp -r {WORKSPACE_BIND} {WORK_PROJ}" in wcmd
    assert wcmd.index(f"cp -r {WORKSPACE_BIND}") < wcmd.index(f"cp -r {HARNESS_BIND}/.")


def test_rendered_judge_mode_command(tmp_path: Path):
    # judge.mode=rendered (VIS): windowed under xvfb + software GL, with a pre-import pass;
    # crucially NO authoritative-visual overlay — the agent's rendering setup IS under test.
    (tmp_path / "game").mkdir(parents=True)
    (tmp_path / "game" / "project.godot").write_text("")
    task = TaskSpec(
        name="t", task_dir=tmp_path,
        judge={"mode": "rendered", "window": {"width": 320, "height": 240}},
        scenarios=(ScenarioSpec(name="baseline", seeds=(1,)),),
    )
    cmd = build_judge_command(task, "baseline", 1)
    assert f"--headless --path {WORK_PROJ} --import" in cmd
    assert 'xvfb-run -a -s "-screen 0 320x240x24"' in cmd
    assert "--rendering-driver opengl3" in cmd
    assert "--write-movie" not in cmd            # judging grabs a frame, not a movie
    assert f"--out {OUT_BIND}/result.json" in cmd
    # overlay order still holds: judge files last
    assert cmd.index(f"cp -r {GAME_BIND}") < cmd.index(f"cp -r {HARNESS_BIND}/.")
    # headless default unchanged
    plain = TaskSpec(
        name="t2", task_dir=tmp_path,
        scenarios=(ScenarioSpec(name="baseline", seeds=(1,)),),
    )
    assert "xvfb-run" not in build_judge_command(plain, "baseline", 1)


def test_formal_judge_command_scenario_argv(tmp_path: Path):
    task = _combo_task(tmp_path)
    cmd = build_judge_command(task, "move_navigation", 1)
    assert "--scenario move_navigation" in cmd
    assert "--seed 1" in cmd
    assert "--tier" not in cmd   # retired: tier is a property of the scenario, judge-side
    # PCG-era generator plumbing is gone (every task ships game/)
    assert "--generator" not in cmd
    assert "--eps_float" not in cmd


def test_build_record_command_reference_solution(tmp_path: Path):
    task = _combo_task(tmp_path)
    cmd = build_record_command(task, "move_navigation", 1, workspace=False)
    # overlay order: game baseline -> solution -> frozen judge -> viz record scene
    assert cmd.index(f"cp -r {GAME_BIND} {WORK_PROJ}") \
        < cmd.index(f"cp -r {SOLUTION_BIND}/.") \
        < cmd.index(f"cp -r {HARNESS_BIND}/.") \
        < cmd.index(f"cp -r {VIZ_BIND}/.")
    # unified pre-import before the windowed run (texture cache; no-op for vector tasks)
    assert f"godot --headless --path {WORK_PROJ} --import" in cmd
    assert cmd.index("--import") < cmd.index("--write-movie")
    assert "--scenario move_navigation" in cmd
    # godot's PASS/FAIL exit must not gate ffmpeg
    assert "; ffmpeg" in cmd


def test_build_record_command_workspace_forces_official_visuals(tmp_path: Path):
    task = _combo_task(tmp_path)
    cmd = build_record_command(task, "baseline", 1, workspace=True)
    # agent workspace is the baseline...
    assert f"cp -r {WORKSPACE_BIND} {WORK_PROJ}" in cmd
    # ...but the OFFICIAL visual layer is force-copied over it, after the judge overlay and
    # before the viz layer, so the video never renders agent-tampered art
    for p in ("view.gd", "visual", "assets"):
        assert f"cp -r {GAME_BIND}/{p} {WORK_PROJ}/" in cmd
    assert cmd.index(f"cp -r {HARNESS_BIND}/.") \
        < cmd.index(f"cp -r {GAME_BIND}/view.gd") \
        < cmd.index(f"cp -r {VIZ_BIND}/.")
    assert "--scenario baseline" in cmd


# --- boundary inversion -----------------------------------------------------------------------

def _producer_task(tmp_path: Path, deliverable: list[str]) -> TaskSpec:
    (tmp_path / "game").mkdir(parents=True, exist_ok=True)
    (tmp_path / "game" / "project.godot").write_text("")
    return TaskSpec(
        name="prod", task_dir=tmp_path, kind="repo",
        judge={"deliverable_paths": deliverable},
        scenarios=(ScenarioSpec(name="baseline", seeds=(1,)),),
    )


def test_default_task_does_not_invert_overlay(tmp_path: Path):
    # A task that declares no deliverable_paths keeps the exact pre-inversion overlay: judge files
    # overlay LAST and win everything, with no snapshot/restore fragments.
    task = _combo_task(tmp_path)
    assert task.inverts_overlay is False
    assert task.deliverable_paths == ("res://logic/controller.gd",)
    cmd = build_judge_command(task, "baseline", 1, workspace=True)
    assert DELIVER_SAVE not in cmd                      # no snapshot machinery
    assert "cp --parents" not in cmd
    assert "--controller res://logic/controller.gd" in cmd   # suite default unchanged
    # judge overlay is the final cp before the godot invocation
    assert cmd.index(f"cp -r {HARNESS_BIND}/.") < cmd.index("godot --headless")


def test_producer_task_inverts_overlay(tmp_path: Path):
    # A repo producer declares a game-tree deliverable; the judge overlays the whole authoritative
    # world, but the agent's module is snapshotted before and restored after so its version wins.
    engine = "res://src/combat/status/status_engine.gd"
    task = _producer_task(tmp_path, [engine])
    assert task.inverts_overlay is True
    assert task.deliverable_paths == (engine,)
    rel = "src/combat/status/status_engine.gd"

    for cmd in (build_judge_command(task, "baseline", 1, workspace=True),
                build_judge_command(task, "baseline", 1, workspace=False)):
        # deliverable travels on --controller so the judge loads the agent's module
        assert f"--controller {engine}" in cmd
        # snapshot the agent deliverable, THEN judge overlay (authoritative), THEN restore it on top
        snap = f"cp -r --parents {rel} {DELIVER_SAVE}/"
        assert snap in cmd
        assert cmd.index(snap) \
            < cmd.index(f"cp -r {HARNESS_BIND}/.") \
            < cmd.index(f"cp -r {DELIVER_SAVE}/. {WORK_PROJ}/")


def test_producer_multi_path_inversion(tmp_path: Path):
    paths = ["res://src/combat/status/status_engine.gd", "res://src/combat/status/effect.gd"]
    task = _producer_task(tmp_path, paths)
    cmd = build_judge_command(task, "baseline", 1, workspace=True)
    # both agent-owned paths are snapshotted in one cp --parents
    assert "cp -r --parents src/combat/status/status_engine.gd src/combat/status/effect.gd " \
        f"{DELIVER_SAVE}/" in cmd
    # the primary (first) path is what the judge is pointed at
    assert f"--controller {paths[0]}" in cmd


def test_producer_record_inverts_overlay(tmp_path: Path):
    engine = "res://src/combat/status/status_engine.gd"
    task = _producer_task(tmp_path, [engine])
    rel = "src/combat/status/status_engine.gd"
    cmd = build_record_command(task, "baseline", 1, workspace=True)
    # inversion applies to record too, so the rendered video runs the agent's engine
    assert f"cp -r --parents {rel} {DELIVER_SAVE}/" in cmd
    assert cmd.index(f"cp -r {HARNESS_BIND}/.") < cmd.index(f"cp -r {DELIVER_SAVE}/. {WORK_PROJ}/")
    assert f"--controller {engine}" in cmd
