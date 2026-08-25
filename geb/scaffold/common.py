from __future__ import annotations

# Shared solve-side outcome vocabulary (see AgentResult docstring in geb/schema.py).
# ``outcome`` is THE single "what happened" word; each word has a globally fixed ``usable``.

# Container cause word -> solve session outcome, for the no-terminal-event path. cause=="exit" is
# the caller's business (stream_broken: container finished but the trajectory has no terminal
# event). Every other cause maps here.
_CAUSE_OUTCOME = {
    "inner_timeout": "timeout",   # in-container timeout gate fired — slow session, judged anyway
    "wall_timeout": "timeout",    # outer wall clock fired — same, still usable
    "oom": "oom",                 # model ran the container out of memory — a real behavior signal
    "abnormal_exit": "aborted",   # CLI segfault / exec error / external kill — tool failure, rerun
    "daemon_error": "daemon_error",  # docker API failed — orchestration failure, rerun
}

# Outcomes that are NOT usable data points: excluded from rates, flagged for rerun.
# stream_broken (container exited with no terminal event: gateway drop / 429 retry-exhaustion /
# CLI crash) is here because its workspace is untrustworthy — the session never reached
# turn.completed, and in practice the break often lands before any controller is written, which
# used to leave the cell SILENTLY MISSING (usable=true skips the rerun gate, produced=false skips
# judge → neither judged nor re-queued). Treating it as unusable routes it to rerun instead.
_SOLVE_UNUSABLE = frozenset({"aborted", "daemon_error", "stream_broken"})


def cause_to_outcome(cause: str) -> str:
    """Map a container cause word to a solve outcome (no-terminal-event path). Unknown causes
    fall back to ``aborted`` (treated as a tool failure to rerun, never silently usable)."""
    return _CAUSE_OUTCOME.get(cause, "aborted")


def solve_usable(outcome: str) -> bool:
    """Is this solve outcome a usable data point? False => skip judge + rerun."""
    return outcome not in _SOLVE_UNUSABLE
