# GameLogicBench — Environment and Running

> Answers "**how do I get it running**". Code organisation and mechanisms are in
> [`structure.md`](./structure.md), changing the code in [`maintenance.md`](./maintenance.md),
> result fields in [`status-fields.md`](./status-fields.md).

## Environment

### Python
Python 3.10+ with `hydra-core` / `omegaconf` / `docker` / `PyYAML` (see `pyproject.toml`). Use a
dedicated virtual environment so there is no ambiguity about which interpreter is in play. **The
CLI must run from the repository root** — cwd is what provides `import geb` and resolves `./tasks`
and `./result` (that is exactly why `conf/config.yaml` sets `hydra.job.chdir: false`).

### The task library, `tasks/`
The task library is **its own repository** and is gitignored here (task statements, frozen judges,
reference solutions and assets are content, not code). Place it at `tasks/` under the repository
root before running anything. Each result cell records a `tasks_commit` field, so any matrix can be
traced back to the library version it was judged against.

### Docker image `geb-all`
Judge and record containers use only their dedicated image `geb-all` (Debian-slim + Godot 4.4
headless + a vendored agent CLI + the rendering stack). Every container name is prefixed `geb-` and
runs with `--rm`; judge and record always run `--network=none`.

The build context needs two vendored dependencies (`.gitignore` excludes `third_party/`, so supply
them yourself):

```
third_party/vendor/godot        # Godot 4.4 headless binary
third_party/packages/*.tgz      # the agent CLI's npm tarball
```

```bash
docker build -t geb-all .
docker run --rm geb-all godot --headless --version   # self-check, should print 4.4.stable...
```

> The image carries the engine and the agent, **not task content** (judge/game/solutions are
> mounted in at runtime). Editing a task file needs **no rebuild**; only changing the Dockerfile,
> the Godot binary or the agent CLI version does.
> If your build network reaches the Debian mirrors slowly, pass a proxy through
> `--build-arg http_proxy=… --build-arg https_proxy=…`.

### record and judge share `geb-all`
Two independent paths, one image — it already ships the rendering stack (`xvfb`, the llvmpipe
software rasterizer, `ffmpeg`). judge runs `godot --headless`; record runs `xvfb-run godot`
(windowed) plus Movie Maker plus ffmpeg. There is no second image and no GPU dependency: software
rendering is both what makes it portable and part of the bit-exact determinism contract.

### Model registry
Model solutions read `conf/models/default.yaml` (**gitignored, holds a live api_key**; the template
is `conf/models/default.yaml.example`). Each entry declares `api_type` / `id` / `base_url` /
`api_key` / `defaults`, where `base_url` points at any Anthropic `/v1/messages`-compatible endpoint.
`solution=<entry name>` then selects it.

### The solve egress whitelist, `geb-egress`
solve containers do not get the default bridge's full egress. They use a custom docker bridge
network, **`geb-egress`** (`conf/config.yaml`, `solve.network`), plus a `DOCKER-USER` whitelist. The
reason: a solve agent can `git clone` or fetch a task's upstream repository and simply read the
answer, so full egress leaks the task. judge and record containers are already `network=none` and
are unaffected.

- **Implementation**: `ensure_egress()` in `geb/infra/egress.py`. Admitted: DNS (the nameservers in
  `/etc/resolv.conf`, udp/tcp 53), the IP:port of **every** model endpoint in the registry
  (resolved from `base_url` at call time, so multi-A round-robin hostnames work — the registry is
  the single truth), and return traffic (`-d <subnet> ctstate RELATED,ESTABLISHED`). Everything
  else leaving that subnet is `REJECT`ed (tcp-reset + icmp-admin-prohibited, deliberately **not**
  DROP: the agent gets an immediate failure instead of hanging until a tool timeout burns tokens).
- **No host proxy is injected**: in whitelist mode the runner is built with
  `ContainerRunner(inherit_host_proxy=False)` (`solve_runner_from_cfg` in
  `geb/pipeline/common.py` decides this from the network name). If the host's `HTTP_PROXY` /
  `HTTPS_PROXY` point at a proxy that can reach the public internet, injecting them would hand the
  agent a way around the whitelist. Belt and braces: the proxy's own IP is not whitelisted either,
  so setting a proxy by hand inside the container does not work.
- **Self-healing**: a host restart loses **both** the custom docker network and the iptables rules.
  `solve_task` calls `ensure_egress` once per process, before the first agent container starts, when
  `solve.network == geb-egress`. The rebuild is idempotent: every rule carrying the
  `-m comment --comment geb-egress` marker is deleted first, then rules are inserted in order, so
  repeated runs never stack duplicates. If the network already exists, **its existing subnet is
  adopted** so the rules can never drift from the real network. If the rules cannot be installed it
  raises and aborts — it will never degrade into "run the container unfiltered".
- **Verifying** (install/repair by hand and print the rules):
  ```bash
  python -m geb.infra.egress          # run from the repo root: prints the whitelist + iptables -S DOCKER-USER
  # anything off-whitelist fails immediately (milliseconds, not a hang)
  docker run --rm --network geb-egress geb-all bash -lc \
    'git clone --depth 1 https://github.com/godotengine/godot-demo-projects /tmp/c'
  ```
- **Temporarily back to bridge** (for a with/without-whitelist control run): override once with
  `python -m geb.cli … solve.network=bridge`, or edit `conf/config.yaml`. On bridge the host proxy
  env is injected as before.
- **Residual exposure**: `DOCKER-USER` hangs off the `FORWARD` chain, but packets a container sends
  to **the host's own IP** traverse `INPUT` and never `FORWARD`, so that path is outside the
  whitelist's reach — a container can talk to any port the host listens on. Narrowing it would mean
  touching `INPUT`, which affects other workloads on the host, so it is left alone; deploy on a
  host with no service usable as an egress stepping stone. Likewise DNS resolution goes through
  docker's embedded resolver (`127.0.0.11`), so **any public hostname still resolves** — but
  resolving is not connecting, and the TCP connection is rejected.

### A host Godot binary (for quick checks)
Keep a Godot 4.4 binary matching the one in the image and you can run the judge without a
container, with identical logic — faster than rebuilding a container while iterating on a judge.
The command is at the end of this document.

## Commands

Prerequisites: dependencies installed, the `geb-all` image present, `tasks/` in place. **Run from
the repository root.**

```bash
# Single-track config: command= / task= / rerun= and friends are all plain Hydra fields,
# overridden uniformly as key=value.

# Full solve->judge (command defaults to run; scores are not persisted, use scripts/score.py)
python -m geb.cli task=atom_move_navigation solution=proper   # should be all PASS
python -m geb.cli task=atom_move_navigation solution=naive    # hidden should FAIL (clipping)

# One phase only (explicit command=)
python -m geb.cli command=judge task=atom_move_navigation solution=naive

# Several tasks at once: task= takes three shapes (one / list / all)
python -m geb.cli command=solve 'task=[combo_reverse_dock,atom_keeper_arc]' solution=<model> scaffold=cc
# or a quoted comma string (Hydra rejects a bare task=a,b as a sweep, but says so helpfully)
python -m geb.cli command=solve "task='combo_reverse_dock,atom_keeper_arc'" solution=<model> scaffold=cc

# The whole library: task=all expands to every tasks/*/task.yaml (excluding probe_* sealed probes)
python -m geb.cli command=judge task=all solution=proper       # re-judge every reference solution
python -m geb.cli command=judge task=all solution=naive

# Model solve: scaffold in {cc, codex, opencode}
python -m geb.cli command=solve 'task=[…]' solution=<model> scaffold=codex

# pass@k (model solutions only): sample_num=k runs samples 0..k-1 (each an independent
# solve->judge, idempotently skipped per sample); sample_idx=i runs or re-runs just one sample
# (targeted top-up, or picking a sample to record)
python -m geb.cli 'task=[…]' solution=<model> sample_num=3
python -m geb.cli task=combo_boss solution=<model> sample_idx=4 rerun=true

# Filling in several solutions concurrently: just run each command=run separately. Each process
# writes only its own result subtree, so there is no write contention by construction.
python -m geb.cli command=run 'task=[…]' solution=<model-a> scaffold=cc     effort=high
python -m geb.cli command=run 'task=[…]' solution=<model-b> scaffold=codex  effort=high

# Current scores: a standalone script that reads the result tree live and prints the PASS/FAIL
# matrix. Nothing is persisted or accumulated.
python scripts/score.py                                    # whole library x every solution present
python scripts/score.py --task atom_move_navigation        # one task
python scripts/score.py --solution '<model>@cc@high@egress' # one solution (comma-separated for more)

# Video recording (optional, not part of grading): renders the judge's deterministic simulation
# to mp4, sharing the geb-all image with judge.
python -m geb.cli command=record task=combo_boss solution=<sol>
```

- `solution` names a registry entry -> solve first (the agent writes the controller in-container),
  then judge. `proper` / `naive` -> judge the calibration reference directly (references are
  deterministic and have no sample dimension, so `sample_num=` does nothing for them).
- **A model solution's result slug has four segments**, `<model>@<scaffold>@<effort>@<net>`
  (`result_slug` in `geb/pipeline/common.py`): scaffold = agent backend, effort = reasoning tier,
  net = solve network regime (`geb-egress` shortens to `egress`; `bridge` = full egress). Any of the
  four differing lands in its own subtree, so runs never overwrite each other — this is what makes
  "same model, different backend / tier / network" comparable. Re-judging or recording an existing
  `@bridge` subtree therefore needs an explicit `solve.network=bridge`, or the slug will not resolve
  to the old directory.
- In the judge phase a model solution is still passed as two arguments,
  `solution=<model> scaffold=<s>`; the harness assembles the slug. **Do not** pass
  `solution=<model>@<s>` directly — it would look for `solutions/<model>@<s>/` and fail.
- **Single-track config**: `command` / `task` / `solution` / `scaffold` / `effort` / `sample_num` /
  `sample_idx` / `scenario` / `seed` / `rerun` are all ordinary top-level fields in
  `conf/config.yaml`, overridden uniformly as Hydra `key=value`. There is no such thing as a
  CLI-only key.
  - `task=` covers four shapes in one key: `task=a` / `task=[a,b]` / `task='a,b'` / `task=all`.
  - `sample_num=k` runs samples 0..k-1; `sample_idx=i` pins a single sample.
  - Scoring is a separate on-demand step, not part of the pipeline: `command=run` does solve+judge
    and nothing else.
- Recordings land at `…/test/<scenario>_<n>/record/run.mp4` (`scenario=<name> seed=<n>` picks the
  cell, defaulting to baseline's first seed; for model solutions inside the relevant `sample_<i>/`
  subtree, with `sample_idx=<i>` picking the sample and defaulting to sample_0). They sit beside
  `result.json` and never overwrite it.

### Quick host check (no container)
When the image is missing, or you only want to verify a task quickly, run the judge with a host
Godot binary — the logic is identical to the in-container judge (overlay assembly order is in
[`structure.md`](./structure.md), "Overlay assembly"):

```bash
rm -rf /tmp/probe && mkdir -p /tmp/probe/proj
cp -r tasks/<task>/game/. /tmp/probe/proj/     # base (add solutions/<sol>/ to check a reference)
cp tasks/<task>/judge/*   /tmp/probe/proj/     # judge overlays last
<godot> --headless --path /tmp/probe/proj res://judge.tscn -- \
  --scenario <name> --seed <N> --controller res://logic/controller.gd --out /tmp/probe/r<N>.json
```
