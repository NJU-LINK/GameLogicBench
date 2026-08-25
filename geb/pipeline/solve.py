from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from omegaconf import DictConfig

from geb.infra.egress import NETWORK as EGRESS_NETWORK
from geb.infra.egress import ensure_egress
from geb.pipeline.common import (
    layout_for,
    load_models,
    load_task,
    model_env,
    resolve_provider,
    result_slug,
    solve_runner_from_cfg,
    tasks_commit,
)
from geb.scaffold.claude_code import ClaudeCodeRunner
from geb.scaffold.codex import CodexRunner
from geb.scaffold.common import solve_usable
from geb.scaffold.opencode import OpencodeRunner
from geb.schema import TaskSpec

log = logging.getLogger(__name__)

# Container-internal path where the agent edits the game project.
WORKSPACE_BIND = "/workspace/game"

BRIEF = """\
You are developing in the Godot 4.4 game project mounted at {ws}. Your working directory is that
project root ({ws}).

Read {ws}/README.md first — it states what the game needs, exactly which file you implement
({deliverable}), the information your code receives each frame, and where your
deliverable ends.

Then implement the behavior the README asks for. You may split your logic across helper scripts
under {helper_dir} and preload them from {deliverable_base}.

You can run the project headless to try your work, e.g.:
    godot --headless --path {ws} {entry_scene}
Its [preview] lines report what happened, including any rule violations. Use them to debug.

When done, make sure your final code is saved in {ws}/{deliverable_rel} (plus any helpers it uses)
and that it delivers everything the README requires.
"""

# Appended to BRIEF when the solve container runs on the egress-whitelisted network
# (solve.network == geb-egress): outbound connections other than the model API are rejected at the
# firewall, so tell the agent up front instead of letting it burn turns on doomed fetches.
# Wording deliberately avoids naming what exists online (no "upstream repo" hints).
BRIEF_NO_NET = """
Note: this environment has no internet access. Outbound connections (git clone, curl, web fetch,
web search) fail immediately. Work only from the files in {ws}.
"""


def _res_rel(res_path: str) -> str:
    """res://logic/controller.gd -> logic/controller.gd"""
    return res_path.removeprefix("res://")


def _brief_for(task: TaskSpec, *, network: str = "") -> str:
    """Format BRIEF from the task's deliverables (schema.deliverable_paths).

    Default tasks (no judge.deliverable_paths) produce the historical wording byte for byte;
    repo producer tasks get their real module path(s) and whichever entry scene the game ships
    (main.tscn, else preview.tscn). On the egress-whitelisted network a no-internet note is
    appended (BRIEF_NO_NET) so the agent doesn't waste turns on firewalled fetches; any other
    network value keeps the historical prompt byte for byte."""
    paths = task.deliverable_paths
    primary = paths[0]
    deliverable = " and ".join(paths)
    rel = " and ".join(
        [_res_rel(primary)] + [f"{WORKSPACE_BIND}/{_res_rel(p)}" for p in paths[1:]])
    helper_dir = primary.rsplit("/", 1)[0] + "/"
    entry = "res://main.tscn"
    if not (task.game_dir / "main.tscn").exists() and (task.game_dir / "preview.tscn").exists():
        entry = "res://preview.tscn"
    return BRIEF.format(
        ws=WORKSPACE_BIND,
        deliverable=deliverable,
        helper_dir=helper_dir,
        deliverable_base=primary.rsplit("/", 1)[1],
        entry_scene=entry,
        deliverable_rel=rel,
    ) + (BRIEF_NO_NET.format(ws=WORKSPACE_BIND) if network == EGRESS_NETWORK else "")


async def run_solve(
    cfg: DictConfig,
    project_root: Path,
    tasks: tuple[str, ...],
    *,
    rerun: bool = False,
    samples: tuple[int, ...] = (0,),
) -> None:
    """Solve phase: for each (task, sample), run the vendored claude-code agent in a container to
    edit the game/ project and produce a controller. Output = result/<model>@<scaffold>/<task>/
    sample_<i>/workspace/ (the agent's edited project), which the judge then uses as its overlay
    baseline. Units run concurrently up to solve.concurrency (each = one agent container; they
    touch disjoint workspaces)."""
    sem = asyncio.Semaphore(max(1, int(cfg.solve.get("concurrency", 1))))
    await asyncio.gather(
        *(solve_task(cfg, project_root, t, sem=sem, rerun=rerun, sample=i)
          for t in tasks for i in samples))


async def solve_task(
    cfg: DictConfig,
    project_root: Path,
    task_name: str,
    *,
    sem: asyncio.Semaphore,
    rerun: bool = False,
    sample: int = 0,
) -> bool:
    """Solve ONE (task, sample) in its own agent container (the per-task unit run_all pipelines
    with the judge). Each sample is an independent solve session — the pass@k unit. Returns True
    when it ends with a workspace worth judging — a produced controller, or an existing workspace
    kept by the idempotent skip."""
    model_name = str(cfg.solution)
    models = load_models(project_root)
    if model_name not in models:
        raise KeyError(f"solve model '{model_name}' not in conf/models/default.yaml: {list(models)}")

    scaffold = str(cfg.scaffold)
    slug = result_slug(cfg, models)   # <model>@<scaffold>@<effort> — the result-path segment
    # Single source of truth for this (model, scaffold)'s endpoint: top-level fields + optional
    # scaffolds.<scaffold> override (config is the truth; no implicit URL rewriting here).
    provider = resolve_provider(models, model_name, scaffold)
    # The --model id is a (model × scaffold) property too (e.g. a cross-protocol proxy alias for cc
    # vs the raw endpoint id for codex), so take it from the resolved provider — honoring the
    # scaffolds.<scaffold>.id override — not a top-level-only lookup.
    cli_id = provider.get("id") or model_name
    env = model_env(provider)
    effort = str(cfg.get("effort", "high"))
    network = str(cfg.solve.network)

    task = load_task(project_root, task_name)
    if not task.has_game:
        log.info(f"[solve] task={task_name} has no game/ project — skip (nothing to solve)")
        return False
    layout = layout_for(cfg, project_root, task_name, slug, sample=sample)
    ws = layout.workspace_dir

    if _solve_done(ws, task, layout.solve_dir,
                   int(cfg.solve.get("judge_unproduced_after_turns", 0))) and not rerun:
        log.info(f"[solve] task={task_name} model={model_name} sample={sample} "
                 f"skip (workspace exists)")
        return True

    if network == EGRESS_NETWORK:
        # Self-heal the egress whitelist before any agent container starts (the host loses the
        # custom docker network AND its iptables rules on restart). Once per process; fails loud
        # rather than letting an unfiltered container run.
        await asyncio.to_thread(ensure_egress, models)

    async with sem:
        # Fresh copy of the game/ baseline for the agent to edit.
        if ws.exists():
            shutil.rmtree(ws)
        ws.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(task.game_dir, ws)
        # The old workspace (and its controller) is gone, so any cell verdicts judged against
        # it are void. Drop the whole test/ tree now, or a later plain `judge` (no --rerun)
        # skips every cell via is_seed_done and the new controller never gets judged.
        test_dir = layout.test_dir
        if test_dir.exists():
            shutil.rmtree(test_dir)

        runner = solve_runner_from_cfg(cfg, task)
        prompt = _brief_for(task, network=network)
        traj = layout.solve_dir / "trajectory.jsonl"
        ws_mount = {str(ws): {"bind": WORKSPACE_BIND, "mode": "rw"}}
        log.info(f"[solve] task={task_name} model={model_name} sample={sample} (id={cli_id}) "
                 f"scaffold={scaffold} network={network} ...")
        if scaffold == "codex":
            # codex builds its own env (CODEX_HOME + GATEWAY_KEY) from the provider dict; the
            # claude ANTHROPIC_* env does not apply. base_url / wire_api come straight from the
            # resolved provider (scaffolds.codex in the registry declares the /v1 base_url and
            # wire_api explicitly — config is the truth). reasoning_effort lives under `defaults`.
            codex_provider = {
                "base_url": provider.get("base_url"),
                "api_key": provider.get("api_key"),
                "wire_api": provider.get("wire_api", "responses"),
                # reasoning_effort: cfg.effort overrides the registry defaults, so the CLI can
                # override a per-model default.
                "reasoning_effort": effort,
                # static request headers (e.g. anthropic-beta for a Claude model reached through a
                # cross-protocol proxy that speaks Responses on the wire — see build_codex_config).
                "http_headers": provider.get("http_headers"),
                # real context window of the gateway model — arms codex auto-compact (see
                # build_codex_config); omit and codex falls back to unknown-model metadata.
                "context_window": provider.get("context_window"),
            }
            res = await CodexRunner(runner).invoke(
                prompt=prompt, workdir=WORKSPACE_BIND, output_path=traj, model=cli_id,
                provider=codex_provider, effort=effort, mounts=ws_mount, network=network)
        elif scaffold == "opencode":
            # opencode builds its own env (OPENCODE_CONFIG + XDG_*) from the provider dict; the
            # claude ANTHROPIC_* env does not apply. base_url / api_key come from the resolved
            # provider (scaffolds.opencode declares the /v1 base_url — config is the truth).
            opencode_provider = {
                "base_url": provider.get("base_url"),
                "api_key": provider.get("api_key"),
                # api_type picks the AI SDK package in opencode.json: "anthropic" -> @ai-sdk/anthropic
                # (thinking signatures round-trip), anything else -> @ai-sdk/openai-compatible
                # (see build_opencode_config).
                "api_type": provider.get("api_type"),
                # Output cap: fed to both OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX and limit.output
                # to get past opencode's hard 32000 clamp (see the incident note in
                # OpencodeRunner.invoke). The runner falls back to 131072 when the registry is
                # silent. context_window goes into limit.context (the compaction budget).
                "max_output_tokens": provider.get("max_output_tokens"),
                "context_window": provider.get("context_window"),
            }
            res = await OpencodeRunner(runner).invoke(
                prompt=prompt, workdir=WORKSPACE_BIND, output_path=traj, model=cli_id,
                provider=opencode_provider, effort=effort, mounts=ws_mount, network=network)
        else:
            res = await ClaudeCodeRunner(runner).invoke(
                prompt=prompt, workdir=WORKSPACE_BIND, output_path=traj, model=cli_id,
                effort=effort, env=env, mounts=ws_mount, network=network,
                disallowed_tools=provider.get("disallowed_tools"))

    produced = _controller_written(ws, task)

    # Gate: NOT-usable outcomes skip judge and are flagged for rerun — those sessions never
    # produced a trustworthy workspace: aborted (CLI tool failure / external kill), daemon_error
    # (docker API), and stream_broken (container exited with no terminal event — gateway drop /
    # 429 retry-exhaustion / CLI crash; the break often precedes any controller write). Everything
    # else (completed / timeout / oom / agent_error) is usable: even a session that hit the time
    # budget or ran itself out of memory still yields whatever controller it wrote, and the real
    # verdict is the judge's on that workspace.
    usable = solve_usable(res.outcome)
    skip_judge = not usable

    _write_status(layout.solve_dir / "status.json", {
        "task": task_name,
        "model": model_name,
        "scaffold": str(cfg.scaffold),
        "effort": effort,
        "sample": sample,
        "usable": usable,
        "produced": produced,
        "outcome": res.outcome,
        "session": {
            "rc": res.rc,
            "subtype": res.subtype,
            "detail": res.error,
            "num_turns": res.num_turns,
            "duration_ms": res.duration_ms,
            "cost_usd": res.cost_usd,
            "usage": res.usage,
        },
        "tasks_commit": tasks_commit(project_root),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })

    if skip_judge:
        log.info(f"[solve] SKIP_JUDGE task={task_name} model={model_name} sample={sample} "
                 f"outcome={res.outcome} — not usable, rerun needed")
        return False

    if produced:
        note = "" if res.outcome == "completed" else f"(session {res.outcome}: {res.error}; judged anyway)"
        log.info(f"[solve] OK task={task_name} model={model_name} sample={sample} "
                 f"controller produced {note}".rstrip())
        return True

    # Unproduced but the session credibly worked (usable outcome, >= threshold turns — the
    # num_turns here is the trajectory-recounted value for killed sessions): judge the bare
    # stub anyway, so "spent the budget, never wrote" lands as a real all-fail reading instead
    # of a hole in the matrix. Below the threshold (refusals / instant bail-outs) the old
    # behavior stands: not judged, eligible for rerun. 0 disables.
    threshold = int(cfg.solve.get("judge_unproduced_after_turns", 0))
    if threshold > 0 and (res.num_turns or 0) >= threshold:
        log.info(f"[solve] FAIL task={task_name} model={model_name} sample={sample} "
                 f"no controller produced after {res.num_turns} turns (session {res.outcome}: "
                 f"{res.error}) — judging the unmodified stub as a fail reading")
        return True
    log.info(f"[solve] FAIL task={task_name} model={model_name} sample={sample} "
             f"no controller produced (session {res.outcome}: {res.error})")
    return False


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _solve_done(ws: Path, task: TaskSpec, solve_dir: Path, threshold: int) -> bool:
    """Idempotent skip: a workspace is done only when its own status.json records a usable
    session (see solve_usable) AND the workspace matches what that session left behind — an
    agent-WRITTEN primary deliverable (differs from the shipped stub), or a bare stub from a
    credible >=threshold-turn session that earned a judged-fail reading (see
    judge_unproduced_after_turns). A modified deliverable alone proves nothing about how the
    session ended: a mid-session kill leaves a half-edit and no status record, and a
    not-usable outcome (aborted / stream_broken) is flagged for re-solve by the post-session
    gate — neither may silently turn into a skip+judge."""
    if not (ws / "project.godot").exists():
        return False
    rel = _res_rel(task.deliverable_paths[0])
    ctrl = ws / rel
    if not ctrl.exists():
        return False
    status_path = solve_dir / "status.json"
    if not status_path.exists():
        return False
    try:
        st = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not st.get("usable"):
        return False
    try:
        if ctrl.read_text() != (task.game_dir / rel).read_text():
            return True
    except OSError:
        return True
    if threshold <= 0:
        return False
    return (not st.get("produced")
            and int(st.get("session", {}).get("num_turns") or 0) >= threshold)


def _controller_written(ws: Path, task: TaskSpec) -> bool:
    """The agent produced something only if the primary deliverable exists and differs from the
    shipped baseline (default tasks: the controller stub; producer tasks: the hollow module)."""
    rel = _res_rel(task.deliverable_paths[0])
    ctrl = ws / rel
    if not ctrl.exists():
        return False
    stub = task.game_dir / rel
    try:
        return ctrl.read_text() != stub.read_text()
    except OSError:
        return True
