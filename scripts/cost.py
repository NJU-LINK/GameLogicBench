#!/usr/bin/env python
"""Solve spend per model, aggregated by level: cost per task and total cost, computed live,
written nowhere.

    python scripts/cost.py                          # whole library x every model
    python scripts/cost.py --solution glm-5.2@cc@high@egress
    python scripts/cost.py --pricing                # append the rate detail (vendor, upstream date)
    python scripts/cost.py --refresh-pricing        # re-fetch the models.dev price snapshot

Run from the repository root: python scripts/cost.py

Same stance as score.py / leaderboard.py: read the result tree live, print, persist nothing (do
not add persistence or merging). A row is one sample, as in leaderboard's per-sample table: ONE
SAMPLE IS AN INDEPENDENT RUN AND ITS MONEY IS COUNTED SEPARATELY. Merging a multi-sample
configuration into one row would make it incomparable with single-sample configurations and would
hide the spread between samples. Multi-sample configurations get an extra total row.

## Why the cost_usd in status.json is not used

IT IS WRONG. The claude-code CLI prices every model at Anthropic's own $5/$25 per M, no matter
which endpoint was actually called. Solving for the implied unit price across 1128 completed cells
confirms it: four different non-Anthropic backends fit that rate with 0.00-0.10% residual, i.e.
exactly. codex reports no cost at all, and opencode always reports 0.0. So all three scaffolds are
recomputed from tokens.

## The three scaffolds' usage conventions are mutually incompatible (the correctness core here)

| scaffold | raw fields | how they add up |
|---|---|---|
| cc | input / cache_creation_input / cache_read_input / output | FOUR DISJOINT fields, add directly |
| codex | input / cached_input / output / reasoning_output | cached is a SUBSET of input, reasoning of output -- must subtract |
| opencode | input / cache_read / cache_write / output / reasoning | disjoint (cache_write and reasoning are always 0 here) |

codex's subset relation was verified on all 266 cells that carry usage; skipping the subtraction
double-bills the cached portion. Cache reads are 65-96% of all input at roughly 0.1x the unit
price, so GETTING THE CONVENTION WRONG ONCE MOVES THE TOTAL BY 5-8x.

## Reporting volume and reporting money are two different conventions -- do not mix them

| purpose | use | why |
|---|---|---|
| token volume | `Ledger.new_in` (= fresh + cache writes) AND `cr`, two columns | newly appended content lands in cache-write on rows talking straight to Anthropic but in fresh on rows going through a gateway; reporting fresh alone compares two different things |
| reuse rate | `cr / total_in`, denominator including cache writes | a cache write is part of the prompt, so fresh+cr alone is the wrong denominator |
| money | four components at four rates, `Ledger.cost()` | a cache write costs 1.25x fresh, so collapsing them into one bucket undercounts |

See `Ledger.new_in`'s docstring for the detail.

## Cells with no usage

The rule is the same for all three scaffolds: IF THE TERMINAL EVENT REPORTED OFFICIAL USAGE, USE
IT; only when it did not (the session was killed by a timeout or a broken stream, so no terminal
event landed) is usage reconstructed. About 311 of 1847 cells are in the latter group.

| tier | source | accuracy | applies to |
|---|---|---|---|
| 1 | official usage (status.json terminal event) | exact | all three scaffolds |
| 2 | opencode trajectory, summing `step_finish.part.tokens` | exact: all four components match official to the digit (verified on 216 of 216) | opencode |
| 2 | codex rollout, last `token_count.total_token_usage` | exact: equal to turn.completed (zero difference measured), missing only requests in flight at the kill | codex, for runs after the rollout log began persisting |
| 2 | cc trajectory, summing per-message usage | INPUT SIDE ONLY, and only where the endpoint backfills per-message usage | cc, see below |
| 3 | a per-solution "length -> token" calibration | estimate | everything else |

Whether cc's per-message summing works depends on WHETHER THE ENDPOINT BACKFILLS per-message
usage. It has nothing to do with the protocol (cc always speaks anthropic) or with whether the
model is a Claude: measured across endpoints, one non-Claude backend matched on the input side in
70 of 70 cells (the most accurate of all), two Claude models managed 42/46 and 41/48, three other
gateway models reported a constant 0 per message, and one model reached through a cross-protocol
proxy reported non-zero numbers that disagreed with official in all 72 cells (per-message and the
final summary are not the same ledger there). The code filters the always-zero group with
`recon.total_in > 0`. THE OUTPUT SIDE NEVER MATCHES (0 of 236: per-message output_tokens is a
message_start snapshot -- one cell summed to 367 against an official 73430), so a reconstructed cc
cell is always a mix of tier-2 input and tier-3 output and still counts as an estimated cell.

codex's rollout only persists for runs after it was symlinked onto the mounted volume; earlier
codex timeout cells have to fall back to tier 3, where all 27 of them share a single arithmetic
mean while the official group's real spread reaches 95x -- and since timeout cells are precisely
the longest-running ones, that is a DIRECTIONAL UNDERESTIMATE.

Rates come from `scripts/pricing/`: first-party vendor pricing only, n/a when not found.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))         # scripts/ → import leaderboard
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -> import geb

import pricing
from leaderboard import LEVELS, _sort_key
from score import _resolve_tasks

_SAMPLE_DIR = re.compile(r"^sample_(\d+)$")
_MIN_FIT = 8            # do not fit below this many cells with official values (least squares on
                        # too few samples is not trustworthy)
_W = 44                 # row-label column width: longest slug + " #s0" (the longest is 41 chars)
                        # Labels must stay ASCII -- a wide character occupies two terminal columns
                        # but counts as one in len(), which skews the whole row.


# ---------------------------------------------------------------- token ledger

class Ledger:
    """Normalised billing components: all three scaffolds converted to one convention.

    fresh = new input that missed cache / cr = cache reads / cw = cache writes / out = output
    (reasoning included, never billed twice). `estimated` marks a cell as an estimate rather than
    official usage.
    """

    __slots__ = ("fresh", "cr", "cw", "out", "estimated")

    def __init__(self, fresh=0, cr=0, cw=0, out=0, estimated=False):
        self.fresh, self.cr, self.cw, self.out = fresh, cr, cw, out
        self.estimated = estimated

    def __iadd__(self, o: "Ledger") -> "Ledger":
        self.fresh += o.fresh; self.cr += o.cr; self.cw += o.cw; self.out += o.out
        return self

    @property
    def total_in(self) -> int:
        return self.fresh + self.cr + self.cw

    @property
    def new_in(self) -> int:
        """Prompt tokens that could not come from cache and had to be processed = fresh + writes.

        ALWAYS REPORT VOLUME WITH THIS, NEVER WITH fresh ALONE. Anthropic's three input fields are
        disjoint, and newly appended content (tool results plus the model's own previous turn)
        takes the path "recorded as a cache write, read back from cache next turn". So on rows
        talking straight to Anthropic (the only rows with non-zero cw) the new content sits in cw
        and fresh is near zero, while rows through a gateway report no cw at all and the new
        content lands in fresh. Reporting fresh alone puts those two side by side and produces an
        absurd reading: one Anthropic model showed just 110 fresh tokens per turn, which would mean
        it read almost nothing all session. Combined, that model is 3,217 per turn, back inside the
        1,268-6,412 range of every other configuration.

        Do NOT price with it: a cache write costs 1.25x fresh (6.25 against 5.0 for one model), so
        collapsing them into one bucket at the input rate systematically undercounts. cost() keeps
        four components at four rates.
        """
        return self.fresh + self.cw

    def cost(self, rate: dict | None) -> float | None:
        return pricing.cost_of(self.fresh, self.cr, self.cw, self.out, rate)


def normalize(scaffold: str, usage: dict | None) -> Ledger | None:
    """Raw usage -> normalised components. The convention differences are tabulated in the
    module docstring."""
    if not usage:
        return None
    if scaffold == "cc":
        return Ledger(fresh=usage.get("input_tokens") or 0,
                      cw=usage.get("cache_creation_input_tokens") or 0,
                      cr=usage.get("cache_read_input_tokens") or 0,
                      out=usage.get("output_tokens") or 0)
    if scaffold == "codex":
        # cached is a subset of input: subtract to get the miss. The clamp guards against an
        # occasional upstream cached > input.
        # reasoning is a subset of output, already inside out, so it is not added again.
        inp = usage.get("input_tokens") or 0
        cached = min(usage.get("cached_input_tokens") or 0, inp)
        return Ledger(fresh=inp - cached, cr=cached, out=usage.get("output_tokens") or 0)
    if scaffold == "opencode":
        return Ledger(fresh=usage.get("input_tokens") or 0,
                      cr=usage.get("cache_read_tokens") or 0,
                      cw=usage.get("cache_write_tokens") or 0,
                      out=usage.get("output_tokens") or 0)
    raise ValueError(f"unknown scaffold '{scaffold}' (expected cc/codex/opencode)")


# ------------------------------------------- cc trajectory replay (for cells missing usage)

class Feats:
    """Features replayed from one cc trajectory, plus the input side rebuilt from per-message usage.

    sum_ctx = context length integrated over calls: every API call's prompt is the whole transcript
    up to that moment, so total input is approximately the sum of the transcript length at each
    call. `calls` is carried as its own term to absorb the fixed per-call overhead (system prompt
    plus tool definitions, multiplied by the call count through cache reads).
    sig = the length of a thinking block's encrypted signature. Claude blanks the thinking text, so
    visible character count badly underestimates output and the signature length is the only usable
    proxy.
    """

    __slots__ = ("calls", "sum_ctx", "vis", "sig", "recon")

    def __init__(self):
        self.calls = 0
        self.sum_ctx = 0.0
        self.vis = 0.0          # visible output chars (text + tool_use args + plain thinking)
        self.sig = 0.0
        self.recon: Ledger | None = None


def replay_cc(path: Path) -> Feats:
    f = Feats()
    ctx = 0.0
    seen: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = e.get("type")
        if kind == "system":
            ctx += len(json.dumps(e.get("tools", []), ensure_ascii=False))
        elif kind == "user":
            ctx += len(json.dumps(e.get("message", {}).get("content", ""), ensure_ascii=False))
        elif kind == "assistant":
            msg = e.get("message") or {}
            mid = msg.get("id")
            # One message is streamed across several lines sharing an id; dedupe by id so one
            # id counts as one API call.
            if mid not in seen:
                f.calls += 1
                f.sum_ctx += ctx
            seen[mid] = msg.get("usage") or {}
            for b in msg.get("content", []) or []:
                bt = b.get("type")
                if bt == "text":
                    s = len(b.get("text", "")); f.vis += s; ctx += s
                elif bt == "tool_use":
                    s = len(json.dumps(b.get("input", {}), ensure_ascii=False))
                    f.vis += s; ctx += s
                elif bt == "thinking":
                    f.sig += len(b.get("signature", ""))
                    s = len(b.get("thinking", "")); f.vis += s; ctx += s
    recon = Ledger(fresh=sum(u.get("input_tokens") or 0 for u in seen.values()),
                   cw=sum(u.get("cache_creation_input_tokens") or 0 for u in seen.values()),
                   cr=sum(u.get("cache_read_input_tokens") or 0 for u in seen.values()),
                   estimated=True)
    # The output side is not rebuilt: per-message output_tokens is a message_start snapshot
    # (one cell summed to 367 against an official 73430).
    f.recon = recon if recon.total_in > 0 else None
    return f


def replay_opencode(path: Path) -> Ledger | None:
    """opencode trajectory -> an exact ledger, not an estimate.

    Every ``step_finish`` event's ``part.tokens`` carries all four components (input / output /
    cache.read / cache.write), and summing them per turn matches official usage TO THE DIGIT
    (verified on 216 of 216 cells), output side included -- which is stronger than the cc rebuild,
    where per-message output_tokens is an unusable message_start snapshot. So when official usage
    is missing this is ground truth and does not count as an estimated cell.
    """
    led = Ledger()
    hit = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "step_finish":
            continue
        tok = (e.get("part") or {}).get("tokens") or {}
        cache = tok.get("cache") or {}
        led.fresh += tok.get("input") or 0
        led.out += tok.get("output") or 0          # reasoning is inside output, not added again
        led.cr += cache.get("read") or 0
        led.cw += cache.get("write") or 0
        hit = True
    return led if hit else None


def replay_codex(solve_dir: Path) -> Ledger | None:
    """codex session rollout -> an exact ledger, not an estimate.

    codex's stdout event stream reports usage exactly once per session (the final turn.completed),
    so a session killed by the container timeout yields nothing at all. Per-request usage exists
    only in the rollout log's ``event_msg/token_count``; that log is symlinked onto the mounted
    volume, written as the session runs, and survives SIGKILL (see geb/scaffold/codex.py). We take
    the last entry's cumulative ``total_token_usage``, which has the same shape as
    turn.completed's usage and therefore goes through the same normalize.

    This is ground truth, not an estimate, missing only the one request in flight at the kill.
    It only works for runs made after the rollout log started persisting -- for earlier cells the
    rollout died with the container, this returns None, and the calibrated estimate is used.
    """
    from geb.scaffold.codex import find_rollout, parse_rollout

    rollout = find_rollout(solve_dir)
    if rollout is None:
        return None
    parsed = parse_rollout(rollout)
    if not parsed or not parsed.get("usage"):
        return None
    return normalize("codex", parsed["usage"])


def replay_codex_feats(path: Path) -> Feats:
    """codex trajectory -> calibration features (the last resort when neither official usage nor
    the rollout is available).

    Structurally the same as replay_cc: codex also sends the whole transcript up to that moment as
    each request's prompt, so total input is approximately the sum of transcript length per request
    = sum_ctx, with `calls` carried separately to absorb the fixed per-request overhead (system
    prompt plus tool definitions, multiplied through cache reads). One request = one agent_message,
    the same convention as count_turns (see geb/scaffold/codex.py).

    `vis` accumulates the characters the model produced (agent_message text + the commands in
    command_execution + reasoning text); `sig` is always 0, because codex's reasoning is plain text
    and never blanked down to a signature the way Claude's is, so Estimator degenerates to a
    one-variable fit on its own.

    Why this is needed: without features, every codex cell missing usage falls into
    Estimator.mean, where 27 cells would share a single constant -- while the 45 cells of the same
    configuration that DO have official usage span 21x (127K to 2.68M total input). A constant
    would systematically underestimate repo-scale tasks and overestimate atom-scale ones.
    """
    f = Feats()
    ctx = 0.0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "item.completed":
            continue
        item = e.get("item") or {}
        kind = item.get("type")
        if kind == "agent_message":
            # One agent_message = one request: bill the ctx as it stood when the request went
            # out, then fold the output into ctx.
            f.calls += 1
            f.sum_ctx += ctx
            s = len(item.get("text") or "")
            f.vis += s
            ctx += s
        elif kind == "reasoning":
            s = len(json.dumps(item.get("summary") or item.get("text") or "", ensure_ascii=False))
            f.vis += s
            ctx += s
        elif kind == "command_execution":
            # The command is the model's output; its stdout feeds back into the next prompt, so
            # it enters ctx but does not count as produced output.
            s = len(item.get("command") or "")
            f.vis += s
            ctx += s + len(item.get("aggregated_output") or "")
        else:
            ctx += len(json.dumps(item, ensure_ascii=False))
    return f


def _fit(xs: list[tuple[float, ...]], ys: list[float]) -> tuple[float, ...] | None:
    """Least squares through the origin, in one or two variables. Returns None when the design
    matrix is degenerate rather than forcing a solution."""
    if not xs:
        return None
    if len(xs[0]) == 1:
        den = sum(x[0] * x[0] for x in xs)
        return (sum(x[0] * y for x, y in zip(xs, ys)) / den,) if den > 0 else None
    s11 = sum(x[0] * x[0] for x in xs); s12 = sum(x[0] * x[1] for x in xs)
    s22 = sum(x[1] * x[1] for x in xs)
    b1 = sum(x[0] * y for x, y in zip(xs, ys)); b2 = sum(x[1] * y for x, y in zip(xs, ys))
    det = s11 * s22 - s12 * s12
    if det <= 0:
        return None
    return ((b1 * s22 - b2 * s12) / det, (s11 * b2 - s12 * b1) / det)


class Estimator:
    """Calibrate on the cells of this solution that have official usage, then apply to the ones
    that do not.

    Only TOTAL INPUT and OUTPUT are predicted, then split back into three components using the
    measured fresh/cr/cw proportions -- those proportions are set by caching behaviour and are far
    more stable within a single run than the absolute volumes. CALIBRATION HAPPENS WITHIN ONE
    SAMPLE (see fill(): pooling across samples imports another run's caching regime, which once
    inflated a configuration by 17%).
    """

    def __init__(self) -> None:
        self.cin: tuple[float, ...] | None = None
        self.cout: tuple[float, ...] | None = None
        self.mean: Ledger | None = None                            # fallback when no features
        self.share: tuple[float, float, float] = (1.0, 0.0, 0.0)   # fresh/cr/cw share of input

    def train(self, official: list[Ledger], with_feats: list[tuple[Feats, Ledger]]) -> None:
        if official:
            tot = Ledger()
            for led in official:
                tot += led
            n = len(official)
            self.mean = Ledger(tot.fresh // n, tot.cr // n, tot.cw // n, tot.out // n,
                               estimated=True)
            if tot.total_in:
                self.share = (tot.fresh / tot.total_in, tot.cr / tot.total_in,
                              tot.cw / tot.total_in)
        if len(with_feats) < _MIN_FIT:
            return
        xin = [(f.sum_ctx, float(f.calls)) for f, _ in with_feats]
        xout: list[tuple[float, ...]] = [(f.vis, f.sig) for f, _ in with_feats]
        if not any(x[1] for x in xout):      # sig all 0 (plain-text thinking) -> one variable
            xout = [(x[0],) for x in xout]
        self.cin = _fit(xin, [float(l.total_in) for _, l in with_feats])
        self.cout = _fit(xout, [float(l.out) for _, l in with_feats])

    def estimate(self, f: Feats | None) -> Ledger | None:
        if f is None or f.calls == 0 or self.cin is None or self.cout is None:
            if self.mean is None:
                return None
            m = self.mean
            return Ledger(m.fresh, m.cr, m.cw, m.out, estimated=True)
        ti = max(0.0, sum(c * v for c, v in zip(self.cin, (f.sum_ctx, float(f.calls)))))
        out = max(0.0, sum(c * v for c, v in zip(self.cout, (f.vis, f.sig))))
        sf, sr, sw = self.share
        return Ledger(int(ti * sf), int(ti * sr), int(ti * sw), int(out), estimated=True)


# ------------------------------------------------------------------------ scanning

class Cell:
    __slots__ = ("level", "sample", "model", "led", "feats")

    def __init__(self, task, sample, model, led, feats):
        self.level = task.split("_", 1)[0]
        self.sample, self.model = sample, model
        self.led, self.feats = led, feats


def collect(result_root: Path, tasks: frozenset[str],
            solutions_filter: frozenset[str] | None) -> dict[str, list[Cell]]:
    """One Cell per (solution, task, sample). Reference solutions have no solve session and no
    sample_* layout, so they drop out naturally."""
    out: dict[str, list[Cell]] = {}
    for sol_dir in sorted(p for p in result_root.iterdir() if p.is_dir()):
        if solutions_filter and sol_dir.name not in solutions_filter:
            continue
        cells = [c for task_dir in sorted(p for p in sol_dir.iterdir() if p.is_dir())
                 if task_dir.name in tasks
                 for sample_dir in sorted(p for p in task_dir.iterdir() if p.is_dir())
                 if (m := _SAMPLE_DIR.match(sample_dir.name))
                 and (c := _read_cell(sample_dir / "solve", int(m.group(1)))) is not None]
        if cells:
            out[sol_dir.name] = cells
    return out


def _read_cell(solve_dir: Path, sample: int) -> Cell | None:
    status_path = solve_dir / "status.json"
    if not status_path.exists():
        return None
    try:
        st = json.loads(status_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    scaffold = st.get("scaffold")
    if scaffold not in ("cc", "codex", "opencode"):
        return None
    led = normalize(scaffold, (st.get("session") or {}).get("usage"))
    traj = solve_dir / "trajectory.jsonl"
    feats = None
    if traj.exists():
        if scaffold == "cc":
            feats = replay_cc(traj)
        elif scaffold == "codex":
            feats = replay_codex_feats(traj)
    # Second-tier fallback when official usage is missing. cc can only rebuild the input side (the
    # output side still has to be estimated, hence the estimated flag); opencode and codex
    # trajectories yield EXACT four components, so they are not flagged as estimates.
    if led is None:
        if scaffold == "opencode" and traj.exists():
            led = replay_opencode(traj)
        elif scaffold == "codex":
            led = replay_codex(solve_dir)
    if led is None and feats is not None:
        led = feats.recon
    return Cell(st.get("task", solve_dir.parent.parent.name), sample, st.get("model"),
                led, feats)


def fill(cells: list[Cell]) -> None:
    """Calibrate and fill in missing or half-missing cells in place. GROUPED BY SAMPLE, EACH
    CALIBRATED ON ITS OWN.

    A sample is an independent run and its caching behaviour can differ wholesale: one
    configuration's three runs measured 64.0% / 6.8% / 47.3% cache hit rate (one of them ran in a
    window where the gateway had caching switched off). Pooling calibration across samples imports
    another run's caching regime into this one's missing cells: pooling all three gave sample 0's
    missing cells a 42.1% hit rate while sample 0's own official cells were at 64.0%, inflating
    that row's total by 17% ($373 against $310). Grouping by sample removes the bias and avoids
    having to maintain a list of "which run was invalid". Single-sample configurations behave
    exactly as before.
    """
    for sample in sorted({c.sample for c in cells}):
        _fill_one([c for c in cells if c.sample == sample])


def _fill_one(cells: list[Cell]) -> None:
    """Calibrate and fill the cells within one run."""
    est = Estimator()
    official = [c.led for c in cells if c.led is not None and not c.led.estimated]
    est.train(official, [(c.feats, c.led) for c in cells
                         if c.led is not None and not c.led.estimated and c.feats is not None])
    for c in cells:
        if c.led is not None and not c.led.estimated:
            continue
        guess = est.estimate(c.feats)
        if c.led is None:
            c.led = guess
        elif guess is not None:
            c.led.out = guess.out           # the rebuild covers input only; output is estimated
        if c.led is not None:
            c.led.estimated = True


# ------------------------------------------------------------------------ printing

def _usd(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"${v:,.2f}" if v >= 0.005 else f"${v:.4f}"


def _print_table(title: str, note: str, rows: list[tuple[str, dict, int]],
                 cell: str) -> None:
    """rows = [(label, {level: (total, task count)}, overall count)]; cell in {"per_task",
    "total"}."""
    header = f"{'model':<{_W}}" + "".join(f"{lv:>14}" for lv in LEVELS) + f"{'total':>16}"
    print(f"\n---- {title} ----")
    print(f"     {note}")
    print(header)
    print("-" * len(header))
    for label, per_lv, n_tasks in rows:
        if not label:
            print()
            continue
        out = [f"{label:<{_W}}"]
        tot_cost, tot_n = 0.0, 0
        any_none = False
        for lv in LEVELS:
            c, n = per_lv.get(lv, (None, 0))
            if not n:                       # no session cells at this level
                out.append(f"{'-':>14}")
                continue
            if c is None:                   # cells exist but no rate is known for the model
                any_none = True
                out.append(f"{'n/a':>14}")
                continue
            tot_cost += c; tot_n += n
            out.append(f"{_usd(c / n) if cell == 'per_task' else _usd(c):>14}")
        if any_none:
            out.append(f"{'n/a':>16}")
        elif cell == "per_task":
            out.append(f"{_usd(tot_cost / tot_n if tot_n else None):>16}")
        else:
            out.append(f"{_usd(tot_cost):>16}")
        print("".join(out))
    print("-" * len(header))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Model solve spend by level: cost per task and total cost "
                    "(computed live, nothing written).")
    ap.add_argument("--result-dir", default="./result", help="result tree root (default ./result)")
    ap.add_argument("--task", default="all", help="tasks: one name / comma-separated / all (default)")
    ap.add_argument("--solution", default=None,
                    help="only these solutions (comma-separated whitelist)")
    ap.add_argument("--pricing", action="store_true", help="append the rate detail table")
    ap.add_argument("--refresh-pricing", action="store_true",
                    help="re-fetch the models.dev snapshot into scripts/pricing/cache/, then exit")
    args = ap.parse_args(argv)

    if args.refresh_pricing:
        print(pricing.refresh())
        return

    project_root = Path.cwd()
    result_root = Path(args.result_dir)
    if not result_root.is_absolute():
        result_root = project_root / result_root
    if not result_root.exists():
        raise SystemExit(f"result dir not found: {result_root} (run from the repo root)")

    tasks = _resolve_tasks(args.task, project_root)
    solutions_filter = (frozenset(s.strip() for s in args.solution.split(",") if s.strip())
                        if args.solution else None)

    by_sol = collect(result_root, frozenset(tasks), solutions_filter)
    if not by_sol:
        raise SystemExit("no solve sessions found (reference solutions have none, which is normal)")

    table, pricing_src = pricing.load()
    rows: list[tuple[str, dict, int]] = []
    used_rates: dict[str, dict | None] = {}
    n_est = n_all = 0

    for sol in sorted(by_sol, key=_sort_key):
        cells = by_sol[sol]
        fill(cells)
        model = next((c.model for c in cells if c.model), sol.split("@")[0])
        rate = table.get(model)
        used_rates[model] = rate
        n_all += len(cells)
        n_est += sum(1 for c in cells if c.led is not None and c.led.estimated)

        def agg(subset: list[Cell]) -> tuple[dict, int]:
            per_lv, n_tasks = {}, 0
            for lv in LEVELS:
                sub = [c for c in subset if c.level == lv and c.led is not None]
                led = Ledger()
                for c in sub:
                    led += c.led
                per_lv[lv] = (led.cost(rate), len(sub))
                n_tasks += len(sub)
            return per_lv, n_tasks

        sample_ids = sorted({c.sample for c in cells})
        for i in sample_ids:
            per_lv, n = agg([c for c in cells if c.sample == i])
            rows.append((f"{sol} #s{i}", per_lv, n))
        if len(sample_ids) > 1:
            per_lv, n = agg(cells)
            rows.append((f"{sol} ALL(x{len(sample_ids)})", per_lv, n))
        rows.append(("", {}, 0))
    if rows and not rows[-1][0]:
        rows.pop()

    print(f"\n==== geb cost | rates: {pricing_src} ====")
    print("One row = one sample's independent run; a multi-sample configuration gets an extra "
          "ALL(xN) row\nsummarising every sample.")
    _print_table("cost per task", "level total / session count at that level", rows, "per_task")
    _print_table("total cost", "sum of every session's spend at that level", rows, "total")

    missing = sorted(m for m, r in used_rates.items() if r is None)
    if missing:
        print(f"\nNote: no rate on file for these models, so their rows read n/a: "
              f"{', '.join(missing)}"
              f"\n  Add them to scripts/pricing/rates.yaml and re-run.")
    if n_est:
        print(f"\nNote: {n_est}/{n_all} session cells landed no terminal event (timeout or broken "
              f"stream), so their tokens\n  come from a trajectory rebuild or a length calibration. "
              f"They are included in the totals above.")
    if args.pricing:
        print("\n".join(pricing.format_table(used_rates, pricing_src)))
    print()


if __name__ == "__main__":
    main(sys.argv[1:])
