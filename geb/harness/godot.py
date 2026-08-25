from __future__ import annotations

from geb.schema import TaskSpec

# Container-internal paths (mounts are wired in pipeline/judge.py).
HARNESS_BIND = "/workspace/harness"      # judge/ (frozen, authoritative)
SOLUTION_BIND = "/workspace/solution"    # solutions/<sol>/ (reference-solution overlay)
GAME_BIND = "/workspace/game"            # game/ (playable project baseline)
WORKSPACE_BIND = "/workspace/agent"      # the agent's edited project (solve output; is the baseline)
OUT_BIND = "/out"
WORK_PROJ = "/tmp/proj"          # writable overlaid project root
DELIVER_SAVE = "/tmp/deliver"    # scratch snapshot of the agent's deliverable during overlay inversion
RESULT_NAME = "result.json"
CONTROLLER_RES = "res://logic/controller.gd"

# --- video recording (record pipeline; judge is unaffected) ---
VIZ_BIND = "/workspace/viz"      # tasks/<task>/viz/ (record scene + windowed project.godot)
MOVIE_TMP = "/tmp/run.avi"       # transient movie in-container; only the transcoded mp4 lands in /out
MOVIE_NAME = "run.mp4"
RECORD_RESULT_NAME = "record_result.json"  # record's own verdict — never clobbers judge's result.json
# The VISUAL layer of a game/ project (pure art; no sim, no tier knowledge). At record time these
# are force-copied from the OFFICIAL game/ so an agent-edited workspace cannot tamper with what
# the video shows — the rendered art is as authoritative as the judge files.
VISUAL_PATHS = ("view.gd", "visual", "assets")


def _press_arg(task: TaskSpec, scenario: str) -> str:
    """`--press axis[:tier][,axis[:tier]]` for hidden scenarios of combo tasks. Press is
    explicit experiment configuration living on the scenario (task.yaml `scenarios:` table;
    (axis, tier) pairs, see ScenarioSpec) — resolved here on the host and handed to the judge
    via argv. The "default" tier serialises as the bare axis, so pre-2026-07-16 cells emit a
    byte-identical argv (rng streams / archived results untouched). Empty for baseline and
    for atom tasks (whose press defaults simply aren't consumed by their judge)."""
    sc = task.scenario(scenario)
    if sc.tier == "public" or not task.press_enabled:
        return ""
    return f"--press {sc.press_arg} "


def _res_to_rel(res_path: str) -> str:
    """`res://src/combat/status/x.gd` -> `src/combat/status/x.gd` (path relative to the project
    root, i.e. WORK_PROJ). Trailing slashes are stripped so both files and directories work."""
    return res_path.removeprefix("res://").strip("/")


def _overlay_inversion(task: TaskSpec) -> tuple[str, str]:
    """Boundary inversion. Returns (save_cmd, restore_cmd) shell fragments
    that snapshot the agent-owned deliverable paths BEFORE the authoritative judge overlay and
    restore them AFTER, so the judge files win over the whole world EXCEPT the agent's module.

    Returns ("", "") when the task does not opt in (task.inverts_overlay False) — the overlay is
    then byte-identical to the pre-inversion behavior, so every existing task is untouched."""
    if not task.inverts_overlay:
        return "", ""
    rels = " ".join(_res_to_rel(p) for p in task.deliverable_paths)
    # cp --parents preserves each path's directory structure under DELIVER_SAVE; the subshell keeps
    # the cwd change local. Restore copies the snapshot back on top of the judge overlay.
    save = f"mkdir -p {DELIVER_SAVE} && ( cd {WORK_PROJ} && cp -r --parents {rels} {DELIVER_SAVE}/ )"
    restore = f"cp -r {DELIVER_SAVE}/. {WORK_PROJ}/"
    return save, restore


def build_judge_command(task: TaskSpec, scenario: str, seed: int, *, workspace: bool = False) -> str:
    """Shell command run inside the container for one (scenario, seed) cell.

    ``judge.mode`` (task.yaml, default "headless") picks the judging modality:

    * headless — behavioral assertions, zero rendering (`godot --headless`).
    * rendered — pixel-relation assertions (VIS-class tasks): same overlay, but the judge
      runs windowed under Xvfb with the software GL rasterizer (llvmpipe — the stack the
      record pipeline validated for bit-exact frames) plus one headless `--import` pass so
      the render cache exists. The judge scene itself grabs the settle frame via
      `get_viewport().get_texture().get_image()` and asserts pixel relations. Unlike record,
      there is NO authoritative-visual overlay: the agent's light/occluder setup IS the thing
      under test. Callers must pass the render env pins (see ``RENDER_ENV`` in pipeline code).

    Two overlay shapes (every task ships a game/ project):

    * Agent workspace (workspace=True): the agent's edited game project IS the baseline. Overlay
      that, then the FROZEN judge files LAST. No separate game/solution layers — the agent's
      controller + helpers already sit at their real res:// paths in the workspace.

    * Reference solution: overlay the game baseline, then the solution's brain + helpers, then
      the FROZEN judge files LAST so they win over any tampering.

    Godot exits 0 on PASS, 1 on FAIL; the result json is written to the rw-mounted /out.
    """
    args = (
        f"res://judge.tscn -- "
        f"--scenario {task.scenario(scenario).name} "
        f"--seed {int(seed)} "
        f"{_press_arg(task, scenario)}"
        f"--controller {task.deliverable_paths[0]} "
        f"--out {OUT_BIND}/{RESULT_NAME}"
    )
    if task.judge_mode == "rendered":
        rc = task.judge.get("window", {})
        width, height = int(rc.get("width", 640)), int(rc.get("height", 480))
        # Import pass is not gated (`;`): it can exit non-zero on cache-warming noise
        # while still producing the imports the windowed run needs (same as record).
        godot = (
            f"godot --headless --path {WORK_PROJ} --import ; "
            f'xvfb-run -a -s "-screen 0 {width}x{height}x24" '
            f"godot --path {WORK_PROJ} --rendering-driver opengl3 {args}"
        )
    else:
        godot = f"godot --headless --path {WORK_PROJ} {args}"
    save, restore = _overlay_inversion(task)
    if workspace:
        overlay = f"cp -r {WORKSPACE_BIND} {WORK_PROJ}"
    else:
        overlay = (
            f"cp -r {GAME_BIND} {WORK_PROJ} "
            f"&& cp -r {SOLUTION_BIND}/. {WORK_PROJ}/"
        )
    if save:
        # snapshot agent deliverable -> authoritative judge overlay -> restore deliverable on top
        overlay = f"{overlay} && {save} && cp -r {HARNESS_BIND}/. {WORK_PROJ}/ && {restore}"
    else:
        overlay = f"{overlay} && cp -r {HARNESS_BIND}/. {WORK_PROJ}/"
    return f"{overlay} && {godot}"


def build_record_command(
    task: TaskSpec, scenario: str, seed: int, *,
    workspace: bool = False, width: int = 640, height: int = 480
) -> str:
    """Shell command to RECORD one (scenario, seed) cell to an mp4 (formal tasks only).

    Same overlay as build_judge_command, plus:

    * the VISUAL layer (view.gd / visual/ / assets/) force-copied from the OFFICIAL game/ AFTER
      the workspace overlay — in workspace mode the agent's edited project is the baseline, and
      the human-verification video must not render agent-tampered art. GAME_BIND is therefore
      mounted in BOTH modes. (`cp` only copies paths that exist; vector-only tasks just carry
      view.gd.)
    * the viz layer (tasks/<task>/viz/: record scene + a windowed project.godot whose main_scene
      is res://record.tscn). record.gd extends the frozen judge driver, so the rendered run IS
      the judged simulation.
    * one headless `--import` pass before rendering, so texture-based tasks have their .godot
      import cache when the windowed run loads assets (a no-op for vector-only tasks). Import
      exit status is not gated (`;`): it can exit non-zero on cache-warming noise while still
      producing the imports we need.

    We run windowed under Xvfb with the software GL rasterizer (llvmpipe — no GPU) and Godot
    Movie Maker, then transcode the transient AVI to an mp4 in /out.

    The godot step is NOT `&&`-chained into ffmpeg: record.gd quit()s with code 1 on a FAILing run
    (exactly the runs we most want to see), and Movie Maker still flushes its frames on that exit —
    so ffmpeg must run regardless of the verdict.
    """
    visual_overlay = " ".join(
        # brace-grouped so each `|| true` only forgives ITS optional path being absent and can
        # never mask a failure earlier in the && chain
        f"&& {{ cp -r {GAME_BIND}/{p} {WORK_PROJ}/ 2>/dev/null || true; }}"
        for p in VISUAL_PATHS
    )
    save, restore = _overlay_inversion(task)
    inv = f" && {save} " if save else " "
    restore_after_judge = f" && {restore}" if restore else ""
    if workspace:
        overlay = (
            f"cp -r {WORKSPACE_BIND} {WORK_PROJ}"
            f"{inv}"
            f"&& cp -r {HARNESS_BIND}/. {WORK_PROJ}/{restore_after_judge} "
            f"{visual_overlay} "
            f"&& cp -r {VIZ_BIND}/. {WORK_PROJ}/"
        )
    else:
        overlay = (
            f"cp -r {GAME_BIND} {WORK_PROJ} "
            f"&& cp -r {SOLUTION_BIND}/. {WORK_PROJ}/"
            f"{inv}"
            f"&& cp -r {HARNESS_BIND}/. {WORK_PROJ}/{restore_after_judge} "
            f"&& cp -r {VIZ_BIND}/. {WORK_PROJ}/"
        )
    pre_import = f"godot --headless --path {WORK_PROJ} --import"
    godot = (
        f'xvfb-run -a -s "-screen 0 {int(width)}x{int(height)}x24" '
        f"godot --path {WORK_PROJ} --rendering-driver opengl3 "
        f"--write-movie {MOVIE_TMP} res://record.tscn -- "
        f"--scenario {task.scenario(scenario).name} "
        f"--seed {int(seed)} "
        f"{_press_arg(task, scenario)}"
        f"--controller {task.deliverable_paths[0]} "
        f"--out {OUT_BIND}/{RECORD_RESULT_NAME}"
    )
    ffmpeg = (
        f"ffmpeg -y -loglevel error -i {MOVIE_TMP} "
        f"-c:v libx264 -pix_fmt yuv420p -crf 20 {OUT_BIND}/{MOVIE_NAME}"
    )
    # overlay must succeed; godot's 0/1 verdict must NOT gate ffmpeg -> `;`.
    return f"{overlay} && {{ {pre_import} ; {godot} ; }} ; {ffmpeg}"
