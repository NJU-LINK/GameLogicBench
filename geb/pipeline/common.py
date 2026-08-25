from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from omegaconf import DictConfig, OmegaConf

from geb.infra.container import ContainerRunner, ResourceConfig, TimeoutConfig
from geb.infra.egress import NETWORK as EGRESS_NETWORK
from geb.schema import (BASELINE_SCENARIO, PRESS_DEFAULT_TIER, ResultLayout, ScenarioSpec,
                        TaskSpec)

_SCENARIO_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
# press axis/tier tokens ride the `--press axis[:tier][,...]` argv — ':' and ',' are separators.
_PRESS_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")


def _parse_press(name: str, sc_name: str, raw_press: Any,
                 press_enabled: bool = False) -> tuple[tuple[str, str], ...]:
    """Normalise the scenario `press` field to ((axis, tier), ...).

    Combo tasks (2026-07-17, default-tier abolition): every hidden scenario MUST spell
    press as an explicit {axis: tier} mapping and every tier must be a real name — either
    the atom hidden scenario the trap lineage comes from, or a combo-original tier name.
    "default" is reserved and rejected. Tier may be a list to stack several tiers of the
    SAME axis in one world (use sparingly — per-tier rows read cleaner).

    Atom tasks keep the historical spellings (their judges never consume press):
      - absent/empty  -> ((<scenario name>, "default"),);
      - list [axis..] -> each axis at the "default" tier.
    """
    if press_enabled:
        if not isinstance(raw_press, dict) or not raw_press:
            raise ValueError(
                f"task {name}: scenario '{sc_name}' needs an explicit "
                f"{{axis: tier}} press mapping (combo tasks name every tier "
                f"explicitly; 'default' is reserved), got {raw_press!r}")
    if not raw_press:
        return ((str(sc_name), PRESS_DEFAULT_TIER),)
    pairs: list[tuple[str, str]] = []
    if isinstance(raw_press, dict):
        for axis, tier in raw_press.items():
            tiers = tier if isinstance(tier, (list, tuple)) else [tier]
            for t in tiers:
                pairs.append((str(axis), str(t) if t is not None else PRESS_DEFAULT_TIER))
    elif isinstance(raw_press, (list, tuple)):
        pairs = [(str(axis), PRESS_DEFAULT_TIER) for axis in raw_press]
    else:
        raise ValueError(
            f"task {name}: scenario '{sc_name}' press must be a list or an "
            f"{{axis: tier}} mapping, got {type(raw_press).__name__}")
    if press_enabled and any(t == PRESS_DEFAULT_TIER for _, t in pairs):
        raise ValueError(
            f"task {name}: scenario '{sc_name}' uses the reserved 'default' tier — "
            f"name the tier (atom lineage scenario name, or a combo-original name)")
    for axis, tier in pairs:
        if not _PRESS_TOKEN.match(axis) or not _PRESS_TOKEN.match(tier):
            raise ValueError(
                f"task {name}: scenario '{sc_name}' bad press token '{axis}:{tier}' "
                f"(lowercase [a-z0-9_] only — ':' and ',' are argv separators)")
    if len({a for a, _ in pairs}) < len(pairs) and any(
            t == PRESS_DEFAULT_TIER for _, t in pairs):
        raise ValueError(
            f"task {name}: scenario '{sc_name}' stacks a duplicate axis with the "
            f"'default' tier — name every tier when stacking one axis")
    return tuple(pairs)


def _parse_scenarios(name: str, raw: dict) -> tuple[ScenarioSpec, ...]:
    """Parse the task.yaml `scenarios:` table.

    Structural invariants enforced here (fail-fast, not conventions):
      - exactly one reserved `baseline` scenario (≡ public tier, the game/level.gd twin);
      - scenario names never end in `_<digits>` (cell dirs are `<scenario>_<seed>`);
      - `press` normalises to (axis, tier) pairs; combo hidden scenarios must name every
        tier explicitly ('default' abolished 2026-07-17); baseline carries no press.
    """
    kind = str(raw.get("kind", "atom"))
    raw_ps = raw.get("press_semantics", None)
    press_enabled = bool(raw_ps) if raw_ps is not None else (kind == "combo")
    table = raw.get("scenarios")
    if not isinstance(table, dict) or BASELINE_SCENARIO not in table:
        raise ValueError(
            f"task {name}: task.yaml needs a `scenarios:` table with exactly one "
            f"'{BASELINE_SCENARIO}' entry; got: "
            f"{sorted(table) if isinstance(table, dict) else type(table).__name__}")
    scenarios: list[ScenarioSpec] = []
    for sc_name in [BASELINE_SCENARIO] + [k for k in table if k != BASELINE_SCENARIO]:
        entry = table[sc_name] or {}
        if not _SCENARIO_NAME.match(str(sc_name)) or re.search(r"_\d+$", str(sc_name)):
            raise ValueError(
                f"task {name}: bad scenario name '{sc_name}' (lowercase [a-z0-9_], "
                f"must not end in _<digits> — cell dirs are <scenario>_<seed>)")
        seeds = tuple(int(s) for s in (entry.get("seeds") or []))
        if not seeds:
            raise ValueError(f"task {name}: scenario '{sc_name}' has no seeds")
        if sc_name == BASELINE_SCENARIO:
            press: tuple[tuple[str, str], ...] = ()
        else:
            press = _parse_press(name, str(sc_name), entry.get("press"), press_enabled)
        scenarios.append(ScenarioSpec(name=str(sc_name), seeds=seeds, press=press))
    return tuple(scenarios)


def load_task(project_root: Path, name: str) -> TaskSpec:
    # Tasks (each a playable game/ project + judge/ + solutions/) live under tasks/.
    task_dir = project_root / "tasks" / name
    cfg_path = task_dir / "task.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"task.yaml not found: {cfg_path}")
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    judge = dict(raw.get("judge", {}) or {})
    mode = str(judge.get("mode", "headless"))
    if mode not in ("headless", "rendered"):
        raise ValueError(f"task {name}: judge.mode must be 'headless' or 'rendered', got '{mode}'")
    return TaskSpec(
        name=raw.get("name", name),
        task_dir=task_dir,
        scenarios=_parse_scenarios(name, raw),
        record=dict(raw.get("record", {}) or {}),
        judge=judge,
        solve=dict(raw.get("solve", {}) or {}),
        kind=str(raw.get("kind", "atom")),
        press_semantics=(None if raw.get("press_semantics", None) is None
                         else bool(raw.get("press_semantics"))),
        meta=dict(raw.get("meta", {}) or {}),
    )


def resolve_result_root(cfg: DictConfig, project_root: Path) -> Path:
    p = Path(str(cfg.result_dir))
    return p if p.is_absolute() else (project_root / p)


def tasks_commit(project_root: Path) -> str:
    """HEAD of the tasks/ nested git repo (the task-library version results were produced on).

    tasks/ is its own repository (main repo gitignores it); every result artifact records this
    hash so a matrix cell is always traceable to the exact task-library state. Returns "" when
    tasks/ is not a git repo or git is unavailable — recorded as-is, never a failure.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(project_root / "tasks"), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        commit = out.stdout.strip() if out.returncode == 0 else ""
        if commit:
            dirty = subprocess.run(
                ["git", "-C", str(project_root / "tasks"), "status", "--porcelain"],
                capture_output=True, text=True, timeout=10,
            )
            if dirty.returncode == 0 and dirty.stdout.strip():
                commit += "-dirty"
        return commit
    except Exception:
        return ""


def layout_for(cfg: DictConfig, project_root: Path, task: str, solution: str,
               sample: int | None = None) -> ResultLayout:
    """``sample`` is the pass@k sample index — pass it for model solutions only; reference
    solutions (proper/naive) are deterministic and keep the flat no-sample layout."""
    return ResultLayout(
        result_root=resolve_result_root(cfg, project_root),
        solution=solution,
        task=task,
        sample=sample,
    )


def result_slug(cfg: DictConfig, models: dict[str, Any]) -> str:
    """The result-path segment for cfg.solution.

    Reference solutions (proper/naive — anything not in the model registry) keep their bare name.
    A model solution is tagged with its agent backend, reasoning effort AND solve network as
    ``<model>@<scaffold>@<effort>@<net>`` so the same model run through different scaffolds (cc = vendored
    claude-code, later codex / gui), different effort levels, or different network regimes lands in a
    distinct subtree and never overwrites another run's results. This is what makes a same-model ×
    different-scaffold comparison (e.g. F5 CLI-vs-GUI), same-model × different-effort comparison, AND
    same-model × different-network comparison (bridge historical vs geb-egress gated) representable
    in result/.
    (The effort dimension exists to stop medium/high runs from overwriting each other.)
    (2026-07-31: network dimension added with the egress gateway — ``geb-egress`` shortens to
    ``egress``; historical bridge-era directories were renamed in place to ``@bridge``. Re-judging a
    historical subtree therefore needs ``solve.network=bridge`` on the command line so the slug
    resolves to the old directory.)
    """
    sol = str(cfg.solution)
    if sol in models:
        scaffold = cfg.get('scaffold', 'cc')
        effort = cfg.get('effort', 'medium')
        net = str(cfg.solve.network).removeprefix("geb-")
        return f"{sol}@{scaffold}@{effort}@{net}"
    return sol


def container_runner_from_cfg(cfg: DictConfig, task: TaskSpec) -> ContainerRunner:
    # task.judge.resources / task.judge.timeout override conf/config.yaml judge.*
    # (the timeout override exists for repo tasks whose simulation is legitimately slower —
    # e.g. repo_tanks_warbrain's AI-vs-AI animation waits; criteria stay logical-time,
    # the wall clock is only the container safety net)
    base = OmegaConf.to_container(cfg.judge.resources, resolve=True) or {}
    task_judge = task.judge or {}
    merged = {**base, **(task_judge.get("resources") or {})}
    base_to = OmegaConf.to_container(cfg.judge.timeout, resolve=True) or {}
    merged_to = {**base_to, **(task_judge.get("timeout") or {})}
    return ContainerRunner(
        image=str(cfg.image),
        timeout=TimeoutConfig(
            container=int(merged_to["container"]),
            kill_after=int(merged_to["kill_after"]),
            wall=int(merged_to["wall"]),
        ),
        resources=ResourceConfig(
            cpus=merged.get("cpus"),
            memory=merged.get("memory"),
            pids_limit=merged.get("pids_limit"),
        ),
        name_prefix=str(cfg.get("container_name_prefix", "geb")),
    )


def solve_runner_from_cfg(cfg: DictConfig, task: TaskSpec) -> ContainerRunner:
    """A ContainerRunner for the solve phase: same image, but resources/timeouts come from
    cfg.solve.{resources,timeout}; task.solve.{resources,timeout} can override per-task
    (repo tasks need both: bigger self-test compute AND a longer session — one youtd2
    preview match runs 300s+, so 3600s only buys ~6 test iterations)."""
    base = OmegaConf.to_container(cfg.solve.resources, resolve=True) or {}
    task_solve = task.solve or {}
    merged = {**base, **(task_solve.get("resources") or {})}
    base_to = OmegaConf.to_container(cfg.solve.timeout, resolve=True) or {}
    merged_to = {**base_to, **(task_solve.get("timeout") or {})}
    return ContainerRunner(
        image=str(cfg.image),
        timeout=TimeoutConfig(
            container=int(merged_to["container"]),
            kill_after=int(merged_to["kill_after"]),
            wall=int(merged_to["wall"]),
        ),
        resources=ResourceConfig(
            cpus=merged.get("cpus"),
            memory=merged.get("memory"),
            pids_limit=merged.get("pids_limit"),
        ),
        name_prefix=str(cfg.get("container_name_prefix", "geb")),
        # On the egress-whitelist network the host proxy env must NOT be injected: it reaches the
        # public internet and would be a route around the whitelist (see geb/infra/egress.py).
        # The model endpoints are all internal direct connections and need no proxy.
        inherit_host_proxy=str(cfg.solve.network) != EGRESS_NETWORK,
    )


def load_models(project_root: Path) -> dict[str, Any]:
    """Load the model registry from conf/models/default.yaml (holds live api keys, gitignored).

    Read directly from disk (not via Hydra config groups) because default.yaml wraps entries under
    a top-level ``models:`` key; loading it as a Hydra group would double-nest to cfg.models.models.
    """
    path = project_root / "conf" / "models" / "default.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"model registry not found: {path} (copy conf/models/default.yaml.example)")
    raw = yaml.safe_load(path.read_text()) or {}
    models = raw.get("models", raw)
    return models if isinstance(models, dict) else {}


def resolve_provider(models: dict[str, Any], model_name: str, scaffold: str) -> dict[str, Any]:
    """Resolve one (model, scaffold) pair to its endpoint config.

    Every model declares each scaffold it supports under a mandatory ``scaffolds: {<name>: {...}}``
    block, and each block is SELF-CONTAINED — api_type / id / base_url / api_key, plus wire_api /
    defaults where that scaffold needs them. There are NO top-level endpoint fields: endpoint URL,
    wire protocol, AND the model id sent on the wire are all (model × scaffold) properties (e.g. cc
    drives gpt-5.6-sol through a cross-protocol proxy under the alias ``gpt-5.6-sol``, while codex
    hits the raw gateway endpoint id ``ep-...``). A top-level default would silently leak the wrong
    value into a scaffold — exactly the ep-id-as-``--model`` bug (2026-07-21). So we read only
    ``scaffolds.<scaffold>`` and fail-fast when the pair is not declared — never guess.
    """
    raw = models.get(model_name)
    if not isinstance(raw, dict):
        raise KeyError(f"unknown model '{model_name}' in registry (see conf/models/)")
    scaffolds = raw.get("scaffolds")
    per = scaffolds.get(scaffold) if isinstance(scaffolds, dict) else None
    if not isinstance(per, dict):
        declared = sorted(scaffolds) if isinstance(scaffolds, dict) else []
        raise ValueError(
            f"model '{model_name}' has no config for scaffold '{scaffold}' — declare it under "
            f"scaffolds.{scaffold} (self-contained endpoint params; top-level endpoint fields are "
            f"abolished, 2026-07-21). Declared scaffolds: {declared or '(none)'}")
    return {k: v for k, v in per.items() if v is not None}


def model_env(provider: dict[str, Any]) -> dict[str, str]:
    """Build the claude-code env from a resolved provider dict: base_url -> ANTHROPIC_BASE_URL,
    token -> ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY, merge any nested ``env`` block, with
    host-env fallback for the three keys. Pass the output of resolve_provider(..., scaffold='cc')."""
    env: dict[str, str] = {}
    if not isinstance(provider, dict):
        return env

    nested_env = provider.get("env")
    if isinstance(nested_env, dict):
        env.update({str(k): str(v) for k, v in nested_env.items() if v is not None})

    base_url = _first(provider, "anthropic_base_url", "base_url", "api_base", "endpoint")
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url

    token = _first(provider, "anthropic_auth_token", "auth_token", "api_key", "apiKey", "token")
    if token:
        env.setdefault("ANTHROPIC_AUTH_TOKEN", token)
        env.setdefault("ANTHROPIC_API_KEY", token)

    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        if key in os.environ and key not in env:
            env[key] = os.environ[key]
    return env


def _first(raw: dict[str, Any], *keys: str) -> str | None:
    """Return the first non-empty value among the given keys (as str); None if none."""
    for key in keys:
        value = raw.get(key)
        if value is not None and str(value):
            return str(value)
    return None

