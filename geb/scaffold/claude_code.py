from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Mapping

from geb.infra.container import ContainerRunner
from geb.scaffold.common import cause_to_outcome
from geb.schema import AgentResult

SUCCESS_SUBTYPE = "success"
CONTAINER_LOG_DIR = "/geb/run"
# Built-in tools disabled by default under headless -p (incident notes in the
# build_claude_command docstring). ExitPlanMode has to go too: with only EnterPlanMode blocked,
# a model was observed calling ExitPlanMode directly (its init tool list had Exit but no Enter),
# which likewise waits for an approval nobody answers and ends the session with zero output.
DEFAULT_DISALLOWED_TOOLS = ["EnterPlanMode", "ExitPlanMode"]


class ClaudeCodeRunner:
    """claude-code backend for the solve phase.

    Runs ``claude -p --output-format stream-json`` inside a container, then reads the trajectory's
    final-event ``subtype`` to decide completion (exit code is NOT trusted). Adapted from C2T's
    ClaudeCodeRunner. The agent edits a workspace mounted rw; artefacts land on the host directly.
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
        effort: str = "high",
        env: Mapping[str, str] | None = None,
        mounts: Mapping[str, dict[str, str]] | None = None,
        network: str | None = None,
        disallowed_tools: list[str] | None = None,
    ) -> AgentResult:
        """Run one claude-code session and read back its result.

        prompt.txt is written on the host at ``output_path.parent``; the command/env use the
        container-side log dir (``CONTAINER_LOG_DIR``), and that host dir is auto-mounted there rw.
        After the run, the stream-json trajectory's last event gives subtype/usage; that decides
        the session ``outcome`` (see AgentResult).
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path = output_path.parent / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        command = build_claude_command(
            prompt_path=f"{CONTAINER_LOG_DIR}/prompt.txt",
            output_path=f"{CONTAINER_LOG_DIR}/{output_path.name}",
            stderr_path=f"{CONTAINER_LOG_DIR}/stderr.log",
            model=model,
            effort=effort,
            disallowed_tools=disallowed_tools,
        )
        merged_env = default_claude_env(CONTAINER_LOG_DIR)
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

        final = parse_final_event(output_path)
        subtype = final.get("subtype") if final else None
        usage = final.get("usage") if final else None
        # With a terminal event the CLI's own num_turns is authoritative (completed sessions
        # are unchanged). Without one (timeout / oom / stream_broken kill the run before it
        # emits its result), estimate from the trajectory so the session isn't left unturned.
        num_turns = final.get("num_turns") if final else (count_turns(output_path) or None)
        duration_ms = final.get("duration_ms") if final else None
        cost_usd = final.get("total_cost_usd") if final else None
        # THE single "what happened" word (see AgentResult). With a terminal event the CLI's own
        # subtype decides; without one, the container cause word maps in (single vocabulary, see
        # cause_to_outcome) — except cause=="exit", where the container finished but the stream
        # never got its result event: a broken stream (gateway drop / CLI crash).
        if final is not None:
            if subtype == SUCCESS_SUBTYPE:
                outcome = "completed"
            else:
                outcome = "agent_error"
        elif result.cause == "exit":
            outcome = "stream_broken"
        else:
            outcome = cause_to_outcome(result.cause)
        if outcome == "agent_error":
            error = subtype or "unknown_agent_error"
        elif outcome == "stream_broken":
            error = error or "container exited on its own but the trajectory has no result event"
        elif outcome != "completed":
            error = error or f"container {result.cause} (rc={rc}, {result.elapsed_s:.1f}s)"
        return AgentResult(
            rc=rc,
            subtype=subtype,
            outcome=outcome,
            num_turns=num_turns,
            duration_ms=duration_ms,
            cost_usd=cost_usd,
            trajectory_path=str(output_path),
            usage=usage,
            error=error or "",
        )


def default_claude_env(log_dir: str) -> dict[str, str]:
    """claude-code default env: per-op timeouts, disable nonessential traffic + autoupdate,
    log dir, sandbox marker (pairs with bypassPermissions)."""
    return {
        "API_TIMEOUT_MS": "600000",
        "BASH_DEFAULT_TIMEOUT_MS": "600000",
        "BASH_MAX_TIMEOUT_MS": "1200000",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_LOG_DIR": str(log_dir),
        "DISABLE_AUTOUPDATER": "1",
        "IS_SANDBOX": "1",
    }


def build_claude_command(
    *,
    prompt_path: str,
    output_path: str,
    stderr_path: str,
    model: str,
    effort: str = "high",
    disallowed_tools: list[str] | None = None,
) -> str:
    """Build the ``claude -p --output-format stream-json ...`` command string (container-side).

    stdout -> output_path, stderr -> stderr_path; the prompt is fed via stdin (``< prompt.txt``),
    NOT inlined into the argv: the prompt names ``godot``, so an agent cleaning up a stuck preview
    with ``pkill -f godot`` would match its own session command line and SIGTERM itself
    (kimi@euwar hit this twice, 2026-07-27; opencode.py still carries the inline pattern).

    disallowed_tools (EnterPlanMode + ExitPlanMode by default; the registry key
    scaffolds.cc.disallowed_tools appends more per model): built-in tools that must be blocked
    under headless -p. One model entered plan mode in 41 of 72 sessions; the ExitPlanMode
    approval has nobody to answer it, so the session ended on "Plan ready for approval" with
    zero output. Claude models never call it, but any model can, so it is a scaffold-wide
    default. Blocking Enter alone is not enough — another model called ExitPlanMode without
    ever entering plan mode, for the same zero-output ending (see the note beside
    DEFAULT_DISALLOWED_TOOLS).
    """
    merged = list(DEFAULT_DISALLOWED_TOOLS)
    for tool in disallowed_tools or []:
        if tool not in merged:
            merged.append(tool)
    parts = [
        "claude",
        "--verbose",
        "-p",
        "--output-format",
        "stream-json",
        "--effort",
        effort,
        "--model",
        model,
        "--permission-mode",
        "bypassPermissions",
    ]
    if merged:
        parts += ["--disallowed-tools", ",".join(merged)]
    quoted = " ".join(shlex.quote(part) for part in parts)
    return (f"{quoted} < {shlex.quote(str(prompt_path))} "
            f"> {shlex.quote(str(output_path))} 2> {shlex.quote(str(stderr_path))}")


def parse_final_event(path: Path) -> dict[str, Any] | None:
    """Read the stream-json trajectory; return the final ``type=="result"`` event.

    A clean session ends with exactly one result event. A killed session's trajectory just stops
    (often mid-stream, trailing system events) — taking "the last valid JSON object" would then
    misreport a stray system subtype (e.g. ``thinking_tokens``) as the session outcome, so we
    scan for the result event specifically and return None when the session never got to emit one.
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
        if isinstance(candidate, dict) and candidate.get("type") == "result":
            final = candidate
    return final


def count_turns(path: Path) -> int:
    """Estimate the agent's num_turns from the stream-json trajectory.

    Fallback for a session whose terminal ``result`` event never arrived (timeout / oom /
    stream_broken kill the CLI mid-run). claude-code's own num_turns == ``user events in the
    stream`` + 1: the ``-p`` prompt is turn 1 but is not streamed as a user event, and every
    later turn lands one user(tool_result) event — sub-agent (Task) turns are flattened into the
    top-level stream too, so this count covers them. Verified to match the CLI's reported
    num_turns on 129/129 completed cc sessions (opus / kimi / glm), MAE 0. On a truncated
    (timed-out) trajectory it counts the turns completed before the kill. Returns 0 (not 1) when
    the trajectory is absent, so the caller can map that to None."""
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
        if isinstance(candidate, dict) and candidate.get("type") == "user":
            n += 1
    return n + 1
