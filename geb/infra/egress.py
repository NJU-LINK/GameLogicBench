from __future__ import annotations

import fcntl
import ipaddress
import logging
import os
import shlex
import socket
import subprocess
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# Docker network name. conf/config.yaml `solve.network` selects it; set it back to `bridge` for a
# no-whitelist control run (all historical readings are the bridge reading).
NETWORK = "geb-egress"
# Preferred subnet for a fresh network. Only used at creation time — if the network already exists
# we adopt whatever subnet it has, so the rules can never drift from the real network.
PREFERRED_SUBNET = "172.31.250.0/24"
# Every iptables rule we own carries this comment, so purge/rebuild touches only our rules and
# never anyone else's (the host may be shared with other workloads).
MARK = "geb-egress"
CHAIN = "DOCKER-USER"
# Registry keys that hold an endpoint URL (mirrors geb.pipeline.common._first).
_URL_KEYS = ("base_url", "anthropic_base_url", "api_base", "endpoint")

_lock = threading.Lock()
_ensured = False
# Cross-process mutex for the purge+install sequence. threading.Lock only covers this process's
# threads, but DOCKER-USER is host-global shared state: two geb processes started at once (the
# documented "one command=run per solution" parallel pattern) both read the rule list, both delete
# it, and the loser's `iptables -D` hits an already-deleted rule -> RuntimeError kills that unit
# before it ever reaches solve (2026-08-04: r23 lost atom_active_frames + atom_attack_cooldown on
# both kimi and glm this way). The window also briefly leaves the chain without our REJECT rules,
# so a concurrent process's already-running containers lose their egress filter for that instant.
LOCK_PATH = "/tmp/geb-egress.lock"


@contextmanager
def _host_lock() -> Iterator[None]:
    """Serialize the rule rebuild across processes (flock on a well-known path)."""
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def ensure_egress(models: dict[str, Any]) -> None:
    """Make the geb-egress docker network + its DOCKER-USER whitelist exist and be correct.

    Idempotent and self-healing: the host loses both the custom docker network and all iptables
    rules on restart, so solve calls this once per process before starting any agent container.
    The whitelist is re-resolved from the model registry every time (the registry is the truth,
    and a gateway hostname may be a multi-A round-robin name whose IPs can change).

    Fails loudly: if we cannot install the rules we must NOT fall through to an unfiltered
    container — that is exactly the backdoor this closes.
    """
    global _ensured
    with _lock:
        if _ensured:
            return
        with _host_lock():
            subnet = _ensure_network()
            allowed = _whitelist(models)
            _purge_rules()
            _install_rules(subnet, allowed)
        _ensured = True
        log.info(f"[egress] network={NETWORK} subnet={subnet} whitelist="
                 + ", ".join(f"{ip}:{port}/{proto}" for ip, port, proto in allowed))


# ---------------------------------------------------------------- docker network

def _ensure_network() -> str:
    """Return the geb-egress subnet, creating the network on first use."""
    import docker
    from docker.errors import APIError, NotFound

    client = docker.from_env()
    try:
        return _subnet_of(client.networks.get(NETWORK))
    except NotFound:
        pass
    ipam = docker.types.IPAMConfig(
        pool_configs=[docker.types.IPAMPool(subnet=PREFERRED_SUBNET)])
    try:
        net = client.networks.create(
            NETWORK, driver="bridge", ipam=ipam, labels={"geb.egress": "true"})
    except APIError:
        # Another geb process won the race — adopt its network.
        net = client.networks.get(NETWORK)
    log.info(f"[egress] created docker network {NETWORK}")
    return _subnet_of(net)


def _subnet_of(net) -> str:
    for cfg in net.attrs.get("IPAM", {}).get("Config") or []:
        subnet = cfg.get("Subnet")
        if subnet and ipaddress.ip_network(subnet).version == 4:
            return str(subnet)
    raise RuntimeError(f"docker network {NETWORK} has no IPv4 subnet: {net.attrs.get('IPAM')}")


# ---------------------------------------------------------------- whitelist

def _whitelist(models: dict[str, Any]) -> list[tuple[str, int, str]]:
    """(ip, port, proto) triples the solve containers may reach: cluster DNS + every model
    endpoint in the registry (all models, not just the one being run — the rules are per-network
    and a second geb process may be driving another model)."""
    allowed: list[tuple[str, int, str]] = []
    for ip in _cluster_dns():
        allowed.append((ip, 53, "udp"))
        allowed.append((ip, 53, "tcp"))
    for host, port in sorted(_endpoints(models)):
        for ip in _resolve(host):
            allowed.append((ip, port, "tcp"))
    # dedupe, keep order
    return list(dict.fromkeys(allowed))


def _cluster_dns() -> list[str]:
    ips: list[str] = []
    with open("/etc/resolv.conf", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver" and _is_ipv4(parts[1]):
                ips.append(parts[1])
    if not ips:
        raise RuntimeError("no IPv4 nameserver in /etc/resolv.conf — cannot whitelist DNS")
    return ips


def _endpoints(models: dict[str, Any]) -> set[tuple[str, int]]:
    """Collect (host, port) from every endpoint URL in the registry, at any nesting depth
    (models.<name>.scaffolds.<scaffold>.base_url today)."""
    found: set[tuple[str, int]] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _URL_KEYS and isinstance(value, str):
                    parsed = urlsplit(value)
                    if parsed.hostname:
                        found.add((parsed.hostname,
                                   parsed.port or (443 if parsed.scheme == "https" else 80)))
                else:
                    walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(models)
    if not found:
        raise RuntimeError("no model endpoint URL found in the registry — refusing to install an "
                           "egress whitelist that would cut solve off from its gateway")
    return found


def _resolve(host: str) -> list[str]:
    """All IPv4 addresses of host (a gateway hostname may resolve to several A records)."""
    if _is_ipv4(host):
        return [host]
    infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(info[4][0] for info in infos))
    if not ips:
        raise RuntimeError(f"cannot resolve model endpoint host {host}")
    return ips


def _is_ipv4(text: str) -> bool:
    try:
        return ipaddress.ip_address(text).version == 4
    except ValueError:
        return False


# ---------------------------------------------------------------- iptables

def _rules(subnet: str, allowed: Iterable[tuple[str, int, str]]) -> list[list[str]]:
    """The ordered DOCKER-USER rule list. Every rule is scoped to our subnet, so nothing else on
    this shared box (default bridge, other users' containers) can be affected.

    REJECT rather than DROP: the agent gets an immediate connection failure instead of hanging
    until its tool timeout, which would burn tokens and muddy the trajectory.
    """
    marked = ["-m", "comment", "--comment", MARK]
    rules = [
        # Return traffic for whatever we allowed out (destination-scoped: cannot open a new path).
        ["-d", subnet, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
    ]
    for ip, port, proto in allowed:
        rules.append(["-s", subnet, "-d", ip, "-p", proto, "--dport", str(port), "-j", "ACCEPT"])
    rules.append(["-s", subnet, "-p", "tcp", "-j", "REJECT", "--reject-with", "tcp-reset"])
    rules.append(["-s", subnet, "-j", "REJECT", "--reject-with", "icmp-admin-prohibited"])
    return [rule + marked for rule in rules]


def _install_rules(subnet: str, allowed: Iterable[tuple[str, int, str]]) -> None:
    for pos, rule in enumerate(_rules(subnet, allowed), start=1):
        _iptables(["-I", CHAIN, str(pos)] + rule)


def _purge_rules() -> None:
    """Delete every rule in DOCKER-USER carrying our comment marker (idempotent rebuild)."""
    listing = _iptables(["-S", CHAIN]).splitlines()
    for line in listing:
        if MARK not in line:
            continue
        args = shlex.split(line)
        if not args or args[0] != "-A":
            continue
        _iptables(["-D"] + args[1:])


def _iptables(args: list[str]) -> str:
    proc = subprocess.run(["iptables", "-w", "5"] + args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"iptables {' '.join(args)} failed (rc={proc.returncode}): "
                           f"{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


if __name__ == "__main__":  # manual ops: install/repair the whitelist and print it
    import logging as _logging
    from pathlib import Path

    from geb.pipeline.common import load_models

    _logging.basicConfig(level=_logging.INFO, format="%(message)s")
    ensure_egress(load_models(Path.cwd()))
    print(_iptables(["-S", CHAIN]), end="")
