from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Mapping

from geb.infra.container import ContainerRunner
from geb.scaffold.common import cause_to_outcome
from geb.schema import AgentResult

CONTAINER_LOG_DIR = "/geb/run"
# codex dumps its whole home (sqlite state dbs, sessions/, skills/, shell_snapshots/, tmp/) into
# CODEX_HOME. Point it at a container-local path (NOT the mounted CONTAINER_LOG_DIR, which may sit
# on a network filesystem) so clutter lands on the ephemeral container overlay and is discarded on
# --rm — only
# the real artefacts (trajectory / prompt / config / last_message / status) stay in the log dir.
CODEX_HOME_DIR = "/geb/codex_home"
# ...except the session ROLLOUT log, which is the one piece of CODEX_HOME worth keeping: it is the
# only place codex records PER-REQUEST token usage (event_msg/token_count, carrying both
# last_token_usage and total_token_usage). The stdout event stream reports usage exactly once, in
# turn.completed at the very end — so a session killed by the container timeout leaves NO usage at
# all (2026-08-10: 27/360 codex cells, all timeouts, had to fall back to a cost estimate).
# The rollout is appended as the session runs, so it survives SIGKILL. We therefore symlink
# $CODEX_HOME/sessions onto the mounted log dir: the rollout streams straight onto the host while
# the sqlite/skills/shell_snapshots clutter stays on the ephemeral overlay (verified 2026-08-10).
# Post-hoc copying was rejected: `timeout` signals the inner bash, so a trailing `cp` never runs in
# exactly the timeout case this exists to fix.
CODEX_SESSIONS_SUBDIR = "codex_sessions"


class CodexRunner:
    """codex-CLI backend for the solve phase (second scaffold, ``scaffold=codex``).

    Runs ``codex exec --json`` inside a container, then reads the JSONL event stream's
    ``turn.completed`` event to decide completion (exit code is NOT trusted, same discipline as
    ClaudeCodeRunner). The agent edits a workspace mounted rw; artefacts land on the host directly.

    Unlike claude-code (pure env config), codex needs a config.toml declaring the model provider
    (base_url / wire_api / env_key). We write it into the mounted log dir and point CODEX_HOME
    there; the api_key is passed via the GATEWAY_KEY env the config's env_key references.
    """

    def __init__(self, container: ContainerRunner) -> None:
        self.container = container

    async def invoke(
        self,
        *,
        prompt: str,
        workdir: str,
        output_path: Path,
        model: str,
        provider: Mapping[str, Any],
        effort: str = "high",
        max_turns: int | None = None,   # accepted for signature parity; codex exec has no turn cap
        env: Mapping[str, str] | None = None,
        mounts: Mapping[str, dict[str, str]] | None = None,
        network: str | None = None,
    ) -> AgentResult:
        """Run one codex-exec session and read back its result.

        prompt.txt + config.toml are written on the host at ``output_path.parent`` (mounted at
        CONTAINER_LOG_DIR rw, which is also CODEX_HOME). The JSONL trajectory's ``turn.completed``
        event gives usage and marks a clean finish; its absence (with a normal container exit) is a
        broken stream (gateway drop / CLI crash), mirroring ClaudeCodeRunner's cause vocabulary.

        A killed session has no turn.completed and therefore no usage on the stdout stream, so usage
        (and turns, if the stream yielded none) falls back to the session rollout log, which is
        appended live and survives SIGKILL — see CODEX_SESSIONS_SUBDIR. The outcome verdict is
        unaffected by the rollout.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path = output_path.parent / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        (output_path.parent / "config.toml").write_text(
            build_codex_config(model=model, provider=provider), encoding="utf-8")

        command = build_codex_command(
            config_path=f"{CONTAINER_LOG_DIR}/config.toml",
            prompt_path=f"{CONTAINER_LOG_DIR}/prompt.txt",
            output_path=f"{CONTAINER_LOG_DIR}/{output_path.name}",
            last_message_path=f"{CONTAINER_LOG_DIR}/last_message.txt",
            stderr_path=f"{CONTAINER_LOG_DIR}/stderr.log",
            workdir=workdir,
        )
        merged_env = {"CODEX_HOME": CODEX_HOME_DIR}
        api_key = str(provider.get("api_key", ""))
        if api_key:
            merged_env["GATEWAY_KEY"] = api_key
        if env:
            merged_env.update(dict(env))

        merged_mounts = dict(mounts or {})
        merged_mounts[str(output_path.parent)] = {"bind": CONTAINER_LOG_DIR, "mode": "rw"}
        result = await self.container.run(
            command=command,
            mounts=merged_mounts,
            env=merged_env,
            workdir=workdir,
            network=network,
        )
        rc = result.rc
        error = result.error
        rollout_path = find_rollout(output_path.parent)

        final = parse_turn_completed(output_path)
        usage = final.get("usage") if final else None
        # The rollout log (symlinked onto the mount, see CODEX_SESSIONS_SUBDIR) is the ONLY record of
        # usage when the session is killed before turn.completed. It never overrides a clean
        # turn.completed — that stays the official number — it only fills the hole.
        rollout = parse_rollout(rollout_path) if rollout_path is not None else None
        if usage is None and rollout is not None and rollout.get("usage"):
            usage = rollout["usage"]
        # THE single "what happened" word (mirror ClaudeCodeRunner): a turn.completed event is
        # codex's own "I finished cleanly" word; without one, the container cause word maps in —
        # except cause=="exit", where the container finished but the stream never got its
        # turn.completed: a broken stream (gateway drop / CLI crash). codex exec has no turn cap /
        # subtype, so there is no max_turns / agent_error branch.
        # The outcome deliberately keys on the stdout stream alone: the rollout supplies accounting
        # (usage / turns), never the verdict, so adding it cannot reclassify a historical cell.
        if final is not None:
            outcome = "completed"
        elif result.cause == "exit":
            outcome = "stream_broken"
        else:
            outcome = cause_to_outcome(result.cause)
        if outcome == "stream_broken":
            error = error or "container exited on its own but the trajectory has no turn.completed"
        elif outcome != "completed":
            error = error or f"container {result.cause} (rc={rc}, {result.elapsed_s:.1f}s)"

        num_turns = count_turns(output_path) or None
        if num_turns is None and rollout is not None:
            num_turns = rollout.get("turns") or None
        return AgentResult(
            rc=rc,
            subtype="turn.completed" if final is not None else None,
            outcome=outcome,
            num_turns=num_turns,
            duration_ms=None,          # codex JSONL carries no wall-clock; leave unset
            cost_usd=None,             # codex trajectory carries no cost — compute offline from usage
            trajectory_path=str(output_path),
            usage=usage,
            error=error or "",
        )


def build_codex_config(*, model: str, provider: Mapping[str, Any]) -> str:
    """Render the codex config.toml declaring one custom model provider.

    provider keys: base_url (to /v1 — codex appends /responses), wire_api ("responses"|"chat"),
    reasoning_effort (optional), http_headers (optional dict of static request headers — e.g.
    ``{"anthropic-beta": "oauth-2025-04-20"}`` when driving a Claude model through a cross-protocol
    proxy that requires it on the wire). The api_key is NOT written here; it is referenced via
    env_key and passed as the GATEWAY_KEY env var (keeps the key out of the on-disk config artefact).
    """
    base_url = str(provider.get("base_url", ""))
    wire_api = str(provider.get("wire_api", "responses"))
    effort = provider.get("reasoning_effort")
    context_window = provider.get("context_window")
    lines = [
        f"model = {_toml_str(model)}",
        'model_provider = "gateway"',
    ]
    if effort:
        lines.append(f"model_reasoning_effort = {_toml_str(str(effort))}")
    if context_window:
        # Gateway model names are unknown to codex's metadata table ("Model metadata not found,
        # defaulting to fallback"), so it never learns the real window and auto-compact never
        # fires — a 1M-token repo task then rides the context straight into the wall (qwen r38,
        # 2026-08-09). Declaring the window explicitly re-arms auto-compact.
        lines.append(f"model_context_window = {int(context_window)}")
    lines += [
        "",
        "[model_providers.gateway]",
        'name = "gateway"',
        f"base_url = {_toml_str(base_url)}",
        'env_key = "GATEWAY_KEY"',
        f"wire_api = {_toml_str(wire_api)}",
        # Stream-death hardening: a gateway connection died silently
        # mid-turn and codex 0.144.1 froze forever (zero TCP sockets, futex_wait, no retry) —
        # the session burned its whole 3600s budget doing nothing. Pin explicit retry/idle
        # knobs so a dead stream is detected and re-tried instead of awaited indefinitely.
        # (Key names verified present in the vendored binary: request_max_retries,
        # stream_max_retries, stream_idle_timeout_ms.)
        "request_max_retries = 4",
        "stream_max_retries = 8",
        "stream_idle_timeout_ms = 120000",
    ]
    headers = provider.get("http_headers")
    if isinstance(headers, dict) and headers:
        pairs = ", ".join(f"{_toml_str(str(k))} = {_toml_str(str(v))}" for k, v in headers.items())
        lines.append(f"http_headers = {{ {pairs} }}")
    return "\n".join(lines) + "\n"


def build_codex_command(
    *,
    config_path: str,
    prompt_path: str,
    output_path: str,
    last_message_path: str,
    stderr_path: str,
    workdir: str,
) -> str:
    """Build the ``codex exec --json ...`` command string (container-side).

    Key flags: --dangerously-bypass-approvals-and-sandbox (the container IS the sandbox, same role
    as claude's bypassPermissions), --skip-git-repo-check (workspace is not a git repo),
    --disable plugins (kill the startup marketplace sync that hits chatgpt.com / github.com and
    hangs when they are unreachable). The prompt is fed via stdin (``-`` + ``< prompt.txt``), NOT
    inlined into the argv: the prompt names ``godot``, so an agent cleaning up a stuck preview with
    ``pkill -f godot`` would match its own session command line and SIGTERM itself (kimi@euwar hit
    this twice, 2026-07-27). The stdin redirect also keeps codex exec from blocking on a non-TTY
    stdin. stdout -> trajectory JSONL, stderr -> stderr.log.

    Prologue seeds a container-local CODEX_HOME (CODEX_HOME_DIR) with the config.toml written to
    the mounted log dir, so codex reads its provider config from a local path while dumping its
    home internals (sqlite/sessions) onto the ephemeral overlay — not the mounted log dir.
    """
    parts = [
        "codex", "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--disable", "plugins",
        "-C", workdir,
        "-o", last_message_path,
        "-",
    ]
    quoted = " ".join(shlex.quote(part) for part in parts)
    sessions_host = f"{CONTAINER_LOG_DIR}/{CODEX_SESSIONS_SUBDIR}"
    prologue = (f"mkdir -p {shlex.quote(CODEX_HOME_DIR)} && "
                f"cp {shlex.quote(config_path)} {shlex.quote(CODEX_HOME_DIR + '/config.toml')} && "
                # sessions/ -> mounted dir so the rollout (the only per-request usage record)
                # streams onto the host and survives the timeout SIGKILL; see CODEX_SESSIONS_SUBDIR.
                f"mkdir -p {shlex.quote(sessions_host)} && "
                f"ln -sfn {shlex.quote(sessions_host)} "
                f"{shlex.quote(CODEX_HOME_DIR + '/sessions')} && ")
    return (f"{prologue}{quoted} < {shlex.quote(prompt_path)} > {shlex.quote(output_path)} "
            f"2> {shlex.quote(stderr_path)}")


def find_rollout(log_dir: Path) -> Path | None:
    """Newest ``rollout-*.jsonl`` under the log dir's codex_sessions/ tree, or None.

    codex nests its rollouts as sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl; the symlink in
    the prologue puts that tree under the mounted log dir. Absent for every cell recorded before
    2026-08-10 (their rollout died with the container) — callers must fall back to the stdout stream.
    """
    root = log_dir / CODEX_SESSIONS_SUBDIR
    if not root.is_dir():
        return None
    found = sorted(root.glob("**/rollout-*.jsonl"))
    return found[-1] if found else None


def _iter_events(path: Path):
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def parse_rollout(path: Path) -> dict[str, Any] | None:
    """Session summary from a rollout log: ``{"usage": ..., "turns": n, "completed": bool}``.

    Usage comes from the LAST ``event_msg/token_count``'s ``total_token_usage`` (cumulative, same
    four keys as turn.completed's usage, so downstream normalization is unchanged). On a killed
    session this is the total up to the last completed request — real data, missing only the
    in-flight request. ``completed`` keys on ``task_complete``, codex's own "I finished" word in the
    rollout vocabulary (the equivalent of turn.completed on the stdout stream); turns count
    ``agent_message`` events, matching count_turns' definition (verified equal on both a clean and a
    SIGKILLed session, 2026-08-10).
    """
    if not path.exists():
        return None
    usage: dict[str, Any] | None = None
    turns = 0
    completed = False
    for event in _iter_events(path):
        payload = event.get("payload") or {}
        kind = payload.get("type")
        if kind == "token_count":
            total = (payload.get("info") or {}).get("total_token_usage")
            if isinstance(total, dict):
                usage = total
        elif kind == "agent_message":
            turns += 1
        elif kind == "task_complete":
            completed = True
    if usage is None and not turns and not completed:
        return None
    return {"usage": usage, "turns": turns, "completed": completed}


def parse_turn_completed(path: Path) -> dict[str, Any] | None:
    """Return the final ``type=="turn.completed"`` event from the JSONL trajectory.

    A clean codex-exec session ends with a turn.completed event carrying usage. A killed session's
    trajectory just stops (no such event). Note: an ``item.completed`` with ``type=="error"`` can
    appear even on success (e.g. the harmless "Model metadata not found, using fallback" notice),
    so success keys on turn.completed's presence — never on the absence of error items.
    """
    if not path.exists():
        return None
    final: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("type") == "turn.completed":
            final = candidate
    return final


def count_turns(path: Path) -> int:
    """Count agent turns = the model's own speaking turns.

    codex-exec emits exactly ONE ``turn.completed`` for the whole session (codex's "turn" == the
    entire exec run), so counting that is always 1 — useless. The real per-iteration signal is an
    ``item.completed`` whose item ``type == "agent_message"`` (one model message). This mirrors
    harbor's ``count_agent_turns`` (steps with source == 'agent') and lines up with claude-code's
    self-reported ``num_turns`` (assistant turns), so all three scaffolds count the same thing:
    how many turns the model spoke."""
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(candidate, dict) and candidate.get("type") == "item.completed"
                and (candidate.get("item") or {}).get("type") == "agent_message"):
            n += 1
    return n


def _toml_str(value: str) -> str:
    """Encode a string as a TOML basic string (JSON string form is valid TOML basic string)."""
    return json.dumps(value)
