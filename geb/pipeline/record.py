from __future__ import annotations

import logging
from pathlib import Path

from omegaconf import DictConfig

from geb.harness.godot import (
    GAME_BIND,
    HARNESS_BIND,
    MOVIE_NAME,
    OUT_BIND,
    RECORD_RESULT_NAME,
    SOLUTION_BIND,
    VIZ_BIND,
    WORKSPACE_BIND,
    build_record_command,
)
from geb.infra.container import ContainerRunner, ResourceConfig, TimeoutConfig
from geb.pipeline.common import layout_for, load_models, load_task, result_slug
from geb.pipeline.judge import normalize_from_raw
from geb.schema import read_seed_result

log = logging.getLogger(__name__)


async def run_record(
    cfg: DictConfig,
    project_root: Path,
    tasks: tuple[str, ...],
    *,
    scenario: str | None = None,
    seed: int | None = None,
    sample: int | None = None,
) -> None:
    """Record ONE (scenario, seed) cell of a solution to an mp4, in the geb-all container run
    windowed under Xvfb + software GL + Godot Movie Maker. This renders the AUTHORITATIVE judge
    simulation via each task's viz/ layer, so the video reproduces the judged trajectory. Output:
    .../test/<scenario>_<n>/record/run.mp4 (inside the sample_<i> subtree for model solutions;
    ``sample`` picks which one via select_sample=<i>, default 0). Judging is untouched — this is
    a separate CLI path that only shares the image, and reads the same workspace / reference
    solutions the judge does."""
    solution = str(cfg.solution)
    models = load_models(project_root)
    slug = result_slug(cfg, models)
    is_model = solution in models
    # Reference solutions have no sample dimension; model solutions default to sample_0.
    sample_idx: int | None = (0 if sample is None else int(sample)) if is_model else None

    rc = cfg.record
    image = str(cfg.image)   # Single image source: judge/solve/record all use the global image

    for task_name in tasks:
        task = load_task(project_root, task_name)
        # task.record.{resources,timeout,lp_num_threads} override conf record.* per task (same
        # merge pattern as judge/solve — repo-tier recordings are legitimately heavier: 3D voxel
        # scenes under llvmpipe want more raster threads and minutes of wall clock for the same
        # judged simulation; render speed never enters the sim — Movie Maker pins the frame clock).
        res_base = dict(rc.resources)
        merged_res = {**res_base, **(task.record.get("resources") or {})}
        to_base = dict(rc.timeout)
        merged_to = {**to_base, **(task.record.get("timeout") or {})}
        # Bound llvmpipe's rasterizer threads (one-per-core by default blows the pids cap on a
        # many-core host and adds noise); the physics sim is single-threaded so this does not
        # affect the verdict.
        lp_threads = int(task.record.get("lp_num_threads", rc.get("lp_num_threads", 4)))
        render_env = {
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "LP_NUM_THREADS": str(lp_threads),
        }
        runner = ContainerRunner(
            image=image,
            timeout=TimeoutConfig(
                container=int(merged_to["container"]),
                kill_after=int(merged_to["kill_after"]),
                wall=int(merged_to["wall"]),
            ),
            resources=ResourceConfig(
                cpus=merged_res.get("cpus"),
                memory=merged_res.get("memory"),
                pids_limit=merged_res.get("pids_limit"),
            ),
            name_prefix=str(cfg.get("container_name_prefix", "geb")),
        )
        if not task.has_game:
            log.info(f"[record] task={task_name} has no game/ — nothing to render, skip")
            continue
        if not task.has_viz:
            log.info(f"[record] task={task_name} has no viz/ layer (record.tscn) — skip. Add "
                     f"tasks/{task_name}/viz/ to enable recording.")
            continue

        # Default cell: the baseline's first seed; scenario/seed selectable via CLI
        # (scenario=<name> seed=<n>). A bare seed=<n> stays on baseline.
        sc_name = scenario if scenario is not None else task.baseline.name
        sc = task.scenario(sc_name)   # raises on unknown name — fail-fast, never guess
        seed_val = int(seed) if seed is not None else sc.seeds[0]
        if seed_val not in sc.seeds:
            raise ValueError(
                f"task {task_name}: seed {seed_val} not in scenario '{sc_name}' "
                f"(seeds: {list(sc.seeds)})")
        # window size = world size; per-task override in task.yaml's record.window, falling back
        # to conf record.window (matches the viz/project.godot viewport).
        win = task.record.get("window", {}) or {}
        width = int(win.get("width", rc.window.width))
        height = int(win.get("height", rc.window.height))
        layout = layout_for(cfg, project_root, task_name, slug, sample=sample_idx)
        # Recording artifacts live in their OWN subfolder, separate from the judge's result.json.
        rec_dir = layout.cell_dir(sc_name, seed_val) / "record"
        rec_dir.mkdir(parents=True, exist_ok=True)

        mounts = {
            str(task.judge_dir): {"bind": HARNESS_BIND, "mode": "ro"},
            str(task.viz_dir): {"bind": VIZ_BIND, "mode": "ro"},
            # game/ is mounted in BOTH modes: in workspace mode the official visual layer
            # (view.gd / visual/ / assets/) is force-copied over the agent's edits, so the video
            # renders authoritative art no matter what the agent touched.
            str(task.game_dir): {"bind": GAME_BIND, "mode": "ro"},
            str(rec_dir): {"bind": OUT_BIND, "mode": "rw"},
        }
        if is_model:
            ws = layout.workspace_dir
            deliverable_rel = task.deliverable_paths[0].removeprefix("res://").strip("/")
            if not (ws / deliverable_rel).exists():
                raise FileNotFoundError(
                    f"agent workspace has no deliverable (run solve first): "
                    f"{ws / deliverable_rel}")
            mounts[str(ws)] = {"bind": WORKSPACE_BIND, "mode": "ro"}
            use_workspace = True
        else:
            sol_dir = task.solution_dir(solution)
            deliverable_rel = task.deliverable_paths[0].removeprefix("res://").strip("/")
            if not (sol_dir / deliverable_rel).exists():
                raise FileNotFoundError(
                    f"solution deliverable not found: {sol_dir / deliverable_rel}")
            mounts[str(sol_dir)] = {"bind": SOLUTION_BIND, "mode": "ro"}
            use_workspace = False

        command = build_record_command(
            task, sc_name, seed_val, workspace=use_workspace, width=width, height=height)
        log.info(f"[record] task={task_name} solution={slug} cell={sc_name}_{seed_val} "
                 f"image={image} ...")
        run = await runner.run(
            command=command,
            mounts=mounts,
            env=render_env,
            network="none",
        )

        mp4 = rec_dir / MOVIE_NAME
        if mp4.exists():
            raw = read_seed_result(rec_dir / RECORD_RESULT_NAME) or {}
            rec = normalize_from_raw(raw) if raw else {}
            verdict = ("PASS" if rec.get("passed") else "FAIL") if rec.get("usable") else "?"
            note = f"{verdict} {rec.get('outcome', '')}".strip()
            if not rec.get("passed") and rec.get("broken_link"):
                note += f" (broken_link={rec['broken_link']})"
            note += f"  [{_crosscheck(rec, layout.result_json(sc_name, seed_val))}]"
            log.info(f"[record] OK task={task_name} cell={sc_name}_{seed_val} -> {mp4}  [{note}]")
        else:
            tail = "\n".join((run.stdout or "").splitlines()[-15:])
            log.info(f"[record] FAIL task={task_name} cell={sc_name}_{seed_val}: no mp4 produced "
                     f"(cause={run.cause}, rc={run.rc})\n{tail}")


def _crosscheck(record: dict, judge_result_path: Path) -> str:
    """MACHINE check that the video is faithful to the official verdict. Both sides are the
    normalize_from_raw shape (verdict fields at top level + a `metrics` sub-object), so verdict
    fields compare directly and task metrics compare inside `metrics`. One documented exception:
    runs whose world re-bakes the nav map mid-run (door-close scenarios) carry the
    NavigationServer's known ±1-frame async-bake jitter PER REBAKE (see CHANGELOG 2026-07-04;
    multi_door closes twice → ±2), so `frames` may differ by up to 2 and `time` by two DT —
    verdict fields stay bit-exact. Returns a status string for the log line.

    The result is informational only — record is a SHOWCASE channel and never gates anything.
    Known benign case: full-game repo tasks whose upstream render-path code shares the global
    RNG stream with gameplay (repo_tanks_warbrain: camera-shake randf, building spawn shuffle,
    collateral rolls) diverge trajectory-wise between the headless-judged and rendered runs
    while each modality is internally deterministic (3 record runs across 4/16 llvmpipe
    threads: state_fingerprint bit-identical, 2026-07-21) — their metrics MISMATCH here is
    expected and carries no requirement."""
    judge = read_seed_result(judge_result_path) or {}
    if not judge:
        return "cross-check SKIPPED (no judge result.json — run judge first)"
    diffs = []
    for k in ("usable", "passed", "outcome", "broken_link"):
        if k in record and record[k] != judge.get(k):
            diffs.append(f"{k}: record={record[k]!r} judge={judge.get(k)!r}")
    rec_m, judge_m = record.get("metrics", {}), judge.get("metrics", {})
    for k, v in rec_m.items():
        jv = judge_m.get(k)
        if v == jv:
            continue
        if k == "frames" and jv is not None and abs(int(v) - int(jv)) <= 2:
            continue
        if k == "time" and jv is not None and abs(float(v) - float(jv)) <= 2.0 / 60.0 + 0.01 + 1e-9:
            # two DT of rebake jitter plus the 0.01 snapping quantum both sides round to
            continue
        diffs.append(f"metrics.{k}: record={v!r} judge={jv!r}")
    if diffs:
        return f"cross-check MISMATCH ({', '.join(diffs)})"
    return "cross-check OK"
