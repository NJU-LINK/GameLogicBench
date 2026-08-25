from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Mapping

from geb.infra.container import ContainerRunner
from geb.scaffold.common import cause_to_outcome
from geb.schema import AgentResult

CONTAINER_LOG_DIR = "/geb/run"
# opencode scatters its state/cache/config under XDG dirs. Point them at container-local paths (NOT
# the mounted CONTAINER_LOG_DIR, which may sit on a network filesystem) so clutter lands on the
# ephemeral overlay and is
# discarded on --rm — only the real artefacts (trajectory / prompt / config) stay in the log dir.
OC_XDG_DATA = "/geb/oc_data"
OC_XDG_CONFIG = "/geb/oc_config"
OC_XDG_CACHE = "/geb/oc_cache"


class OpencodeRunner:
    """opencode-CLI backend for the solve phase (third scaffold, ``scaffold=opencode``).

    Runs ``opencode run --pure --auto --format json`` inside a container, then reads the JSONL
    event stream's final ``step_finish`` with ``reason=="stop"`` to decide completion (exit code is
    NOT trusted, same discipline as ClaudeCodeRunner / CodexRunner). The agent edits a workspace
    mounted rw; artefacts land on the host directly.

    Like codex (and unlike claude-code's pure env config), opencode needs a JSON config declaring
    the model provider (an openai-compatible provider pointing at the vLLM endpoint). We write it
    into the mounted log dir and point OPENCODE_CONFIG at it; XDG_* env keeps opencode's own
    state/cache off the mounted log dir.
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
        max_turns: int | None = None,   # accepted for signature parity; opencode run has no turn cap
        env: Mapping[str, str] | None = None,
        mounts: Mapping[str, dict[str, str]] | None = None,
        network: str | None = None,
    ) -> AgentResult:
        """Run one opencode-run session and read back its result.

        prompt.txt + opencode.json are written on the host at ``output_path.parent`` (mounted at
        CONTAINER_LOG_DIR rw). The JSONL trajectory's final ``step_finish`` with ``reason=="stop"``
        marks a clean finish; its absence (with a normal container exit) is a broken stream
        (gateway drop / CLI crash), mirroring the other runners' cause vocabulary.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path = output_path.parent / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        (output_path.parent / "opencode.json").write_text(
            build_opencode_config(model=model, provider=provider), encoding="utf-8")

        command = build_opencode_command(
            config_path=f"{CONTAINER_LOG_DIR}/opencode.json",
            prompt_path=f"{CONTAINER_LOG_DIR}/prompt.txt",
            output_path=f"{CONTAINER_LOG_DIR}/{output_path.name}",
            stderr_path=f"{CONTAINER_LOG_DIR}/stderr.log",
            model_ref=f"vllm/{model}",
            workdir=workdir,
            effort=effort,
        )
        merged_env = {
            "OPENCODE_CONFIG": f"{CONTAINER_LOG_DIR}/opencode.json",
            "XDG_DATA_HOME": OC_XDG_DATA,
            "XDG_CONFIG_HOME": OC_XDG_CONFIG,
            "XDG_CACHE_HOME": OC_XDG_CACHE,
            # Break opencode's hard 32000 output-token clamp (transform.ts OUTPUT_TOKEN_MAX:
            # the requested maxOutputTokens is Math.min(limit.output, 32000), applied to every
            # model and not reachable from config — upstream issues #29363 / #20078). This
            # undocumented env var is the only escape hatch on v1 (PR #5679, present in the
            # vendored 1.17.18). Without it a model hit exactly 32000 on a long code-writing
            # step and returned reason=length, breaking the stream with zero output (reproduced
            # four times). Must be a positive integer; the effective request value is still
            # min(limit.output, this), so build_opencode_config declares limit.output as well.
            "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX": str(
                int(provider.get("max_output_tokens") or 131072)),
        }
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

        final = parse_final_stop(output_path)
        usage, self_cost = accumulate_usage(output_path)
        # Discriminate how the session ended (mirror the other runners): a step_finish with
        # THE single "what happened" word (mirror ClaudeCodeRunner): reason=="stop" is opencode's
        # "I finished cleanly" word; without one, the container cause word maps in — except
        # cause=="exit", where the container finished but the stream never got its terminal stop:
        # a broken stream (gateway drop / CLI crash).
        if final is not None:
            outcome = "completed"
        elif result.cause == "exit":
            outcome = "stream_broken"
        else:
            outcome = cause_to_outcome(result.cause)
        if outcome == "stream_broken":
            error = error or "container exited on its own but the trajectory has no terminal stop"
        elif outcome != "completed":
            error = error or f"container {result.cause} (rc={rc}, {result.elapsed_s:.1f}s)"

        num_turns = count_steps(output_path) or None
        # cost_usd only from what the trajectory itself reports: opencode's summed per-step cost
        # (0 for custom vllm/gateway providers whose price it doesn't know -> left None). No
        # token×rate computation here — that's done offline from the recorded usage tokens.
        cost_usd = self_cost or None
        return AgentResult(
            rc=rc,
            subtype="stop" if final is not None else None,
            outcome=outcome,
            num_turns=num_turns,
            duration_ms=None,          # opencode JSONL carries no wall-clock; leave unset
            cost_usd=cost_usd,
            trajectory_path=str(output_path),
            usage=usage,
            error=error or "",
        )


def build_opencode_config(*, model: str, provider: Mapping[str, Any]) -> str:
    """Render the opencode.json declaring one model provider (named ``vllm`` for compatibility).

    provider keys: base_url, api_key, api_type. api_type selects the AI SDK package
    (both are bundled in the vendored opencode binary — no network fetch):
      - "anthropic"    -> @ai-sdk/anthropic (/v1/messages; thinking signatures round-trip, and
                          the baseURL must include /v1)
      - anything else  -> @ai-sdk/openai-compatible (Chat Completions; stateless, so reasoning
                          signatures cannot be sent back — use only for non-reasoning models or
                          a comparison that knowingly accepts the downgrade)
    Optional context_window / max_output_tokens become the model entry's limit:{context,output}.
    Note that limit.output alone does NOT fix the 32000 truncation: opencode clamps the requested
    maxOutputTokens to Math.min(limit.output, 32000) (transform.ts OUTPUT_TOKEN_MAX, upstream
    issue #29363), so OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX (see invoke) is required to get past
    it. limit still has to be written here, because compaction derives its usable budget from it.
    The model id (which may contain slashes, e.g. an absolute served-model path) is used verbatim
    both as the models-map key and as its name — opencode sends it as the request's model field.
    """
    base_url = str(provider.get("base_url", ""))
    api_key = str(provider.get("api_key", "") or "EMPTY")
    api_type = str(provider.get("api_type", "") or "openai-compatible")
    npm_pkg = "@ai-sdk/anthropic" if api_type == "anthropic" else "@ai-sdk/openai-compatible"
    model_entry: dict[str, Any] = {"name": model}
    # opencode's config schema requires context and output to appear together in the limit block
    # (a missing context is rejected outright with "Configuration is invalid"). context must be
    # clearly larger than output: compaction's usable input budget is roughly
    # context - maxOutputTokens, so equal values collapse it to 0 (upstream #38835). The registry
    # should declare context_window explicitly; the 400000 default is only a fallback.
    if provider.get("max_output_tokens") or provider.get("context_window"):
        limit = {
            "context": int(provider.get("context_window") or 400000),
            "output": int(provider.get("max_output_tokens") or 32000),
        }
        model_entry["limit"] = limit
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "vllm": {
                "npm": npm_pkg,
                "name": "geb provider",
                "options": {"baseURL": base_url, "apiKey": api_key},
                "models": {model: model_entry},
            }
        },
    }
    return json.dumps(cfg, indent=2) + "\n"


def build_opencode_command(
    *,
    config_path: str,
    prompt_path: str,
    output_path: str,
    stderr_path: str,
    model_ref: str,
    workdir: str,
    effort: str = "high",
) -> str:
    """Build the ``opencode run ...`` command string (container-side).

    Key flags: --pure (skip external plugins — kills the startup plugin sync that would hit the
    network, same role as codex's --disable plugins), --auto (auto-approve permissions, the
    container IS the sandbox), --format json (JSONL event stream), --variant <effort> (provider-specific
    reasoning effort: low/medium/high/xhigh/max). The prompt is injected via ``$(cat <prompt>)``; stdout
    -> trajectory JSONL, stderr -> stderr.log. OPENCODE_CONFIG + XDG_* (set by the caller as env) point
    opencode at the provider config and keep its state local.
    """
    # model_ref (vllm/<model>) and workdir are trusted internal values; the prompt is the only
    # untrusted content and it is injected via $(cat ...) rather than inlined.
    parts = [
        "opencode", "run",
        "--pure",
        "--auto",
        "--format", "json",
        "--variant", effort,
        "-m", model_ref,
        '"$(cat ' + shlex.quote(prompt_path) + ')"',
    ]
    quoted = " ".join(
        shlex.quote(part) if not part.startswith('"$(cat ') else part for part in parts
    )
    return (f"{quoted} < /dev/null > {shlex.quote(output_path)} "
            f"2> {shlex.quote(stderr_path)}")


def parse_final_stop(path: Path) -> dict[str, Any] | None:
    """Return the final ``step_finish`` event whose ``part.reason == "stop"``.

    A clean opencode-run session ends with a step_finish carrying reason "stop" (intermediate
    steps carry "tool-calls"). A killed session's trajectory just stops (no terminal "stop"). The
    event's ``part.tokens`` gives usage.
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
        if (isinstance(candidate, dict) and candidate.get("type") == "step_finish"
                and candidate.get("part", {}).get("reason") == "stop"):
            final = candidate
    return final


def count_steps(path: Path) -> int:
    """Count step_finish events (real agent steps, not stream chunks)."""
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
        if isinstance(candidate, dict) and candidate.get("type") == "step_finish":
            n += 1
    return n


def accumulate_usage(path: Path) -> tuple[dict[str, int] | None, float]:
    """Sum token counts + opencode's self-reported cost across ALL step_finish events.

    opencode reports per-step usage under ``part.tokens`` = {input, output, reasoning,
    cache:{read,write}} and a per-step ``part.cost``. A naive read of only the FINAL step
    undercounts output/cost (each step bills its own input+output), so we accumulate — mirrors
    harbor's opencode adapter. Returns (normalized usage dict, summed self-reported cost), or
    (None, 0.0) when the trajectory has no step_finish."""
    if not path.exists():
        return None, 0.0
    inp = out = rea = cr = cw = 0
    self_cost = 0.0
    seen = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not (isinstance(e, dict) and e.get("type") == "step_finish"):
            continue
        seen = True
        part = e.get("part", {}) or {}
        t = part.get("tokens", {}) or {}
        c = t.get("cache", {}) or {}
        inp += int(t.get("input", 0) or 0)
        out += int(t.get("output", 0) or 0)
        rea += int(t.get("reasoning", 0) or 0)
        cr += int(c.get("read", 0) or 0)
        cw += int(c.get("write", 0) or 0)
        self_cost += float(part.get("cost", 0) or 0)
    if not seen:
        return None, 0.0
    usage = {"input_tokens": inp, "output_tokens": out, "reasoning_tokens": rea,
             "cache_read_tokens": cr, "cache_write_tokens": cw,
             "self_reported_cost_usd": self_cost}
    return usage, self_cost
