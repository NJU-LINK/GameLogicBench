from __future__ import annotations

import asyncio
import os
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from geb.schema import ContainerRunResult


@dataclass(frozen=True)
class TimeoutConfig:
    container: int      # inner `timeout` seconds
    kill_after: int     # SIGTERM grace before SIGKILL
    wall: int           # outer wall-clock poll deadline


@dataclass(frozen=True)
class ResourceConfig:
    cpus: float | int | None = None
    memory: str | None = None
    pids_limit: int | None = None


def _nano_cpus(cpus: float | int | None) -> Optional[int]:
    return int(float(cpus) * 1_000_000_000) if cpus else None


class ContainerRunner:
    """docker-py execution core for geb judge containers.

    Adapted from C2T's ContainerRunner. Hard rules for this shared box: a dedicated `image`
    (`geb-all`), a `geb-` name prefix, and `remove(force=True)` in finally — we never reuse other
    images or touch containers we did not start. Judge containers run with `network="none"`,
    `--user uid:gid`, and cpu/mem/pids caps. Dual-layer timeout: an inner `timeout` wrap plus an
    outer wall-clock poll that kills a hung container.
    """

    def __init__(
        self,
        *,
        image: str,
        timeout: TimeoutConfig,
        resources: ResourceConfig | None = None,
        name_prefix: str = "geb",
        inherit_host_proxy: bool = True,
    ) -> None:
        self.image = image
        self.timeout = timeout
        self.resources = resources or ResourceConfig()
        self.name_prefix = name_prefix
        self.inherit_host_proxy = inherit_host_proxy

    async def run(
        self,
        *,
        command: str,
        mounts: Mapping[str, dict[str, str]],
        env: Mapping[str, str] | None = None,
        workdir: str | None = None,
        network: str | None = "none",
        name: str | None = None,
    ) -> ContainerRunResult:
        return await asyncio.to_thread(
            self._run_sync,
            command=command,
            mounts=mounts,
            env=dict(env or {}),
            workdir=workdir,
            network=network,
            name=name,
        )

    def _wrap_timeout(self, command: str) -> str:
        return (
            f"timeout --signal=TERM --kill-after={self.timeout.kill_after}s "
            f"{self.timeout.container}s bash -lc {shlex.quote(command)}"
        )

    def _run_sync(
        self,
        *,
        command: str,
        mounts: Mapping[str, dict[str, str]],
        env: dict[str, str],
        workdir: str | None,
        network: str | None,
        name: str | None,
    ) -> ContainerRunResult:
        import docker  # lazy import so non-docker code paths (tests) don't require the daemon

        client = docker.from_env()
        container_name = name or f"{self.name_prefix}-{uuid.uuid4().hex[:12]}"
        run_env = {"HOME": "/tmp"}
        run_env.update(env)
        # Solve containers run claude-code against an internal Anthropic gateway: pass host proxy
        # settings through so in-container traffic uses the same egress (and NO_PROXY keeps the
        # internal gateway on a direct route). Judge containers run network="none" and are
        # unaffected. Values are never hardcoded — absent on host => absent in container.
        # inherit_host_proxy=False for egress-whitelist runs: the host proxy reaches the public
        # internet, so injecting it would hand the agent a route around the whitelist (the
        # whitelist rejects the proxy IP too, but not injecting it is the primary defense).
        if self.inherit_host_proxy:
            for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                        "http_proxy", "https_proxy", "no_proxy"):
                value = os.environ.get(var)
                if value:
                    run_env.setdefault(var, value)
        volumes = {
            str(host): {"bind": spec["bind"], "mode": spec.get("mode", "ro")}
            for host, spec in mounts.items()
        }
        wrapped = self._wrap_timeout(command)

        container = None
        try:
            container = client.containers.run(
                self.image,
                command=["bash", "-lc", wrapped],
                detach=True,
                environment=run_env,
                name=container_name,
                network_mode=network,
                user=f"{os.getuid()}:{os.getgid()}",
                working_dir=workdir or "/tmp",
                volumes=volumes,
                pids_limit=self.resources.pids_limit,
                mem_limit=self.resources.memory,
                nano_cpus=_nano_cpus(self.resources.cpus),
                remove=False,
            )
            started = time.monotonic()
            daemon_failures = 0
            while True:
                try:
                    res = container.wait(timeout=1)
                    elapsed = time.monotonic() - started
                    rc = int(res.get("StatusCode", -1))
                    logs = _safe_logs(container)
                    cause = _discriminate_exit(container, rc)
                    # rc 0 = PASS, rc 1 = judge FAIL; both are valid judge outcomes (cause=="exit").
                    return ContainerRunResult(
                        rc=rc, cause=cause, elapsed_s=elapsed, stdout=logs,
                    )
                except Exception as exc:
                    # container.wait(timeout=1) raises on every 1s poll while the workload runs —
                    # that is the expected idle path, not an error. A real daemon fault raises
                    # repeatedly with the container in a non-running state, or on the API itself;
                    # we detect it by asking the daemon for container state and counting failures.
                    elapsed = time.monotonic() - started
                    if _is_poll_timeout(exc):
                        daemon_failures = 0
                    else:
                        daemon_failures += 1
                        if daemon_failures >= 5:
                            return ContainerRunResult(
                                rc=None, cause="daemon_error", elapsed_s=elapsed,
                                stdout=_safe_logs(container), error=f"docker API: {exc}",
                            )
                    if elapsed >= self.timeout.wall:
                        try:
                            container.kill()
                        except Exception:
                            pass
                        return ContainerRunResult(
                            rc=None, cause="wall_timeout", elapsed_s=elapsed,
                            stdout=_safe_logs(container),
                        )
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass


def _discriminate_exit(container, rc: int) -> str:
    """Map a completed wait() to its cause word (see ContainerRunResult docstring)."""
    if rc in (0, 1):
        return "exit"
    if rc == 124:
        return "inner_timeout"
    if rc == 137:
        return "oom" if _was_oom_killed(container) else "inner_timeout"
    return "abnormal_exit"


def _was_oom_killed(container) -> bool:
    try:
        container.reload()
        return bool(container.attrs.get("State", {}).get("OOMKilled", False))
    except Exception:
        return False


def _is_poll_timeout(exc: Exception) -> bool:
    """True for the expected 1s-poll timeout raised while the container is still running."""
    import requests

    if isinstance(exc, requests.exceptions.Timeout):
        return True
    # docker-py wraps read timeouts differently across versions; match on the message as fallback.
    return "read timed out" in str(exc).lower() or "timed out" in str(exc).lower()


def _safe_logs(container) -> str:
    try:
        return container.logs(stdout=True, stderr=True).decode("utf-8", "replace")
    except Exception:
        return ""
