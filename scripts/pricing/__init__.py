"""Rate resolution: turn a model name into {input, cache_read, cache_write, output} in USD per 1M
tokens.

Three sources, MERGED PER MODEL, highest precedence first:

1. `rates.yaml` (next to this file) — hand-written. Fills in models the automatic sources do not
   know, or overrides a public list price with a real gateway settlement price.
2. The `models.dev` `api.json` snapshot (`cache/models_dev.json`) — THE PRIMARY SOURCE. Thousands
   of entries across ~180 providers, with first-party coverage for every model measured here.
   Re-fetch it with `--refresh-pricing`.
3. `litellm.model_cost` — last resort, used only when neither of the above has the model.

None of them has it -> return None and the caller prints cost as `n/a`. THERE IS NO DEFAULT RATE:
a fallback rate disguises "no price on file" as a plausible-looking number, which is more
dangerous than a blank. (A competitor implementation ships a single default entry in its pricing
map; that is deliberately not copied here.)

## Why models.dev is primary and litellm only a fallback

litellm's table lags on the models measured here and often carries a resale entry instead of the
vendor's own. Two concrete failures were observed:

- MISSING MODELS: three of the eleven models measured here are absent from litellm entirely.
  models.dev has a first-party entry for all three.
- STALE RESALE PRICING: litellm's only entry for one model was an `openrouter/...` route at
  1.000 / 0.200 / 3.000 while the vendor's own current price was 0.435 / 0.0036 / 0.87 — a 55x
  difference on cache_read. Against the real token ledger (fresh 3.7M / cache_read 99.3M /
  output 2.6M) that is $31.36 against $4.23, overstating the bill 7.4x. OpenRouter's own API now
  reports the vendor price too, so that litellm row was simply never updated.

Both sources index by short name and would collide, so the source-selection rule has to be
explicit. See below.

## Provider selection: first-party only, resale never

On models.dev one model commonly has 8-25 providers at completely different prices (one model had
20, ranging from 2/0.25/8 to 3.762/0.9405/18.81). The `_OFFICIAL` whitelist lists FIRST-PARTY
providers only; resellers and aggregators (azure / amazon-bedrock / vertex / openrouter /
fireworks-ai / llmgateway, ...) are not candidates at all — not merely ranked lower. The reason is
the case above: a resale price can be 55x off, and a "fallback" would silently turn a wrong price
into a plausible-looking number. No first-party entry -> n/a, and a human decides explicitly in
`rates.yaml`. The litellm fallback follows the same rule: BARE KEYS ONLY (no slash = the vendor's
own entry); every slashed resale route is discarded.

When one vendor has several first-party entrances (all official, different prices), whitelist
order decides: the international endpoint before the domestic one (2.0 against 1.777 for one
vendor, the domestic price being a local-currency conversion about 11% cheaper). SUBSCRIPTION
ENTRANCES ARE NEVER LISTED (`*-token-plan` / `*-coding-plan` carry 0 for all four rates — a
monthly plan is not a per-token price, and picking it up would make an entire configuration look
almost free).

Model-id casing follows each vendor's habit (one vendor writes `MiniMax-M3`), so the index is
lower-cased throughout. A local model name that differs from the upstream id goes through
`_ALIAS`.

Each resolved rate carries `_key` (which source and provider it actually came from) and `_updated`
(models.dev's last_updated) so the caller can print an audit table — which is mandatory whenever
the price was not hand-written.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import yaml

PER_M = 1_000_000
_HERE = Path(__file__).resolve().parent
_RATES = _HERE / "rates.yaml"
_CACHE = _HERE / "cache" / "models_dev.json"
MODELS_DEV_URL = "https://models.dev/api.json"

# First-party provider whitelist. Order IS precedence (when one vendor has several official
# entrances, the earlier one wins). Resellers and aggregators (azure, amazon-bedrock, vertex,
# openrouter, fireworks-ai, llmgateway, ...) are NEVER used — not ranked lower, not candidates at
# all. See the case in the module docstring: a resale price can be 55x the vendor's own, and a
# "fallback" would silently turn a wrong price into a plausible-looking number. A model with no
# first-party entry gets n/a, and a human decides explicitly in rates.yaml.
#
# Choosing between one vendor's several official entrances (all official, different prices):
#   international first, domestic second. A domestic price is a local-currency conversion (one
#   vendor's domestic rate is 1.777 against 2.0 international, about 11% cheaper); reporting USD
#   from the international list price is the more standard choice and the easier one for an
#   outside reader to check. Override in rates.yaml to use the domestic price instead.
#   SUBSCRIPTION ENTRANCES ARE NEVER LISTED (*-token-plan / *-coding-plan carry 0 for all four
#   rates) — a monthly plan is not a per-token price, and it would make a whole configuration
#   look almost free.
_OFFICIAL = (
    "anthropic", "openai",
    "moonshotai",                       # Kimi, api.moonshot.ai
    "deepseek",                         # DeepSeek, api.deepseek.com
    "zai", "zhipuai",                   # Z.AI (intl) -> Zhipu (domestic); GLM priced the same
    "xiaomi",                           # MiMo, api.xiaomimimo.com
    "alibaba", "alibaba-cn",            # Qwen international -> domestic
    "minimax", "minimax-cn",
    "google", "mistral", "xai", "meta", "cohere", "ai21",
)

# Local model name -> models.dev model id. Only needed when the two differ.
_ALIAS = {
    "qwen-3.8-max": "qwen3.8-max",
    # A self-hosted deployment's date suffix does not exist upstream; the unsuffixed official
    # entry is used as an equivalent conversion.
    "deepseek-v4-flash-0731": "deepseek-v4-flash",
}


def load() -> tuple[dict[str, dict], str]:
    """Return ({model: {input, cache_read, cache_write, output, _key, _updated}}, source note)."""
    manual = _load_manual()
    dev, dev_note = _load_models_dev()
    lite, lite_note = _load_litellm()
    table = {**lite, **dev, **manual}       # precedence: hand-written > models.dev > litellm
    notes = []
    if manual:
        notes.append(f"{_RATES.name}({len(manual)})")
    notes.append(dev_note)
    notes.append(lite_note)
    return table, " > ".join(notes)


def refresh(url: str = MODELS_DEV_URL, timeout: int = 120) -> str:
    """Fetch a fresh models.dev snapshot into cache/, keeping ONLY official providers' prices.
    Returns a human-readable result note.

    The snapshot is committed and nothing fetches implicitly: silently reaching the network while
    computing money would make the same result tree produce different totals at different times,
    and this tool writes nothing, so its readings have to be reproducible. Update prices with an
    explicit `--refresh-pricing`.

    Before writing, prune to `_OFFICIAL` and keep only cost + last_updated per model: the upstream
    api.json is about 3.6 MB (roughly 180 providers and 6000 models, the vast majority resellers
    and aggregators this module never uses), and about 39 KB after pruning. The file is a
    committed artefact, so every refresh produces a diff — unpruned, each refresh would add a few
    hundred KB of compressed delta to the repository. Keys are written in sorted order so the same
    upstream data always yields the same file (otherwise the diff is pure noise).
    """
    # Explicit User-Agent: models.dev answers 403 to urllib's default one.
    req = urllib.request.Request(url, headers={"User-Agent": "geb-cost/1.0"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310
        data = json.loads(resp.read())
    pruned = {
        pid: {
            "name": prov.get("name"),
            "api": prov.get("api"),
            "models": {mid: {"cost": e.get("cost"), "last_updated": e.get("last_updated")}
                       for mid, e in (prov.get("models") or {}).items()},
        }
        for pid in _OFFICIAL if (prov := data.get(pid))
    }
    if not pruned:
        raise RuntimeError(
            f"{url} contained no _OFFICIAL provider — the upstream structure may have changed; "
            f"the snapshot was NOT overwritten")
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(json.dumps(pruned, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                      encoding="utf-8")
    n = sum(len(p["models"]) for p in pruned.values())
    kb = _CACHE.stat().st_size / 1024
    return (f"wrote {_CACHE} — {len(pruned)} official providers / {n} models "
            f"({kb:.0f} KB; pruned resellers and aggregators out of {len(data)} upstream "
            f"providers)")


def _load_manual() -> dict[str, dict]:
    if not _RATES.exists():
        return {}
    raw = yaml.safe_load(_RATES.read_text(encoding="utf-8")) or {}
    table = raw.get("rates", raw)
    if not isinstance(table, dict):
        return {}
    out = {}
    for model, e in table.items():
        if not isinstance(e, dict) or e.get("input") is None:
            continue                        # a commented-out entry with no numbers counts as absent
        inp = float(e["input"])
        out[model] = {
            "input": inp,
            "cache_read": float(e.get("cache_read", inp)),
            "cache_write": float(e.get("cache_write", inp)),
            "output": float(e.get("output") or 0.0),
            "_key": _RATES.name, "_updated": str(e.get("source") or "hand-written"),
        }
    return out


def _load_models_dev() -> tuple[dict[str, dict], str]:
    if not _CACHE.exists():
        return {}, "models.dev(no snapshot; run --refresh-pricing)"
    try:
        data = json.loads(_CACHE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {}, f"models.dev(snapshot unreadable: {exc})"
    # (lower-cased model id) -> {provider: entry}
    idx: dict[str, dict[str, dict]] = {}
    for pid, prov in data.items():
        for mid, entry in (prov.get("models") or {}).items():
            idx.setdefault(mid.lower(), {})[pid] = entry
    out: dict[str, dict] = {}
    for local, mid in _model_names(idx):
        cands = idx[mid]
        pick = next((p for p in _OFFICIAL if p in cands), None)  # official only; order = precedence
        if pick is None:
            continue
        cost = (cands[pick].get("cost") or {})
        inp = cost.get("input")
        if inp is None:
            continue
        out[local] = {
            "input": float(inp),
            # A missing cache_read/write falls back to the input rate. That overstates cache
            # reads considerably, but overstating beats silently charging 0: at 0, a
            # configuration serving 96% of its input from cache would look almost free.
            "cache_read": float(cost["cache_read"]) if cost.get("cache_read") is not None
            else float(inp),
            "cache_write": float(cost["cache_write"]) if cost.get("cache_write") is not None
            else float(inp),
            "output": float(cost.get("output") or 0.0),
            "_key": f"models.dev:{pick}",
            "_updated": str(cands[pick].get("last_updated") or "?"),
        }
    n = sum(len(p.get("models", {})) for p in data.values())
    return out, f"models.dev({len(data)}p/{n}m)"


def _model_names(idx: dict[str, dict]) -> list[tuple[str, str]]:
    """(local name, lower-cased models.dev id). The alias table wins; everything else is looked
    up by identical name."""
    pairs = [(local, up.lower()) for local, up in _ALIAS.items() if up.lower() in idx]
    aliased = {local for local, _ in pairs}
    pairs += [(mid, mid) for mid in idx if mid not in aliased]
    return pairs


def _load_litellm() -> tuple[dict[str, dict], str]:
    """The litellm fallback, accepting BARE KEYS ONLY (no slash = the vendor's own entry).

    Every slashed key is a resale or cloud-marketplace route (`openrouter/...`,
    `fireworks_ai/...`, `cloudflare/...`) whose price is unrelated to the vendor's: litellm's only
    entry for one model was an openrouter route whose cache_read was 55x the vendor's own. So
    slashed keys are not ranked lower ("fewest slashes wins") but discarded outright — with no
    official price the model stays n/a and a human decides in rates.yaml, rather than a wrong
    price papering over the gap.
    """
    try:
        import litellm                                            # noqa: PLC0415
    except ImportError:
        return {}, "litellm(not installed)"
    out: dict[str, dict] = {}
    for key, e in getattr(litellm, "model_cost", {}).items():
        if "/" in key:                      # not a bare key = resale route, never used
            continue
        inp = e.get("input_cost_per_token")
        if inp is None:
            continue
        out[key] = {
            "input": inp * PER_M,
            "cache_read": (e.get("cache_read_input_token_cost") or inp) * PER_M,
            "cache_write": (e.get("cache_creation_input_token_cost") or inp) * PER_M,
            "output": (e.get("output_cost_per_token") or 0.0) * PER_M,
            "_key": f"litellm:{key}", "_updated": "-",
        }
    return out, "litellm(bare vendor keys only)"


def cost_of(fresh: int, cache_read: int, cache_write: int, output: int,
            rate: dict | None) -> float | None:
    """Four normalised token components x their rates -> USD. Returns None when rate is None
    (the model is not in any price table)."""
    if not rate:
        return None
    inp = rate.get("input", 0.0)
    return (fresh * inp
            + cache_read * rate.get("cache_read", inp)
            + cache_write * rate.get("cache_write", inp)
            + output * rate.get("output", 0.0)) / PER_M


def format_table(models_to_rates: dict[str, dict | None], src: str) -> list[str]:
    """The rate audit table. Mandatory whenever prices were not hand-written: one model routinely
    has a dozen-plus providers upstream at prices an order of magnitude apart, and without the
    provider name there is no way to tell whose pricing a number represents."""
    head = (f"{'model':<26} {'input':>8} {'cache_rd':>9} {'cache_wr':>9} {'output':>8}   "
            f"{'source':<26} upstream updated")
    lines = [f"\n==== rates actually used (USD per 1M tokens) | precedence: {src} ====", head,
             "-" * len(head)]
    for model, rate in sorted(models_to_rates.items()):
        if rate is None:
            lines.append(f"{model:<26} {'n/a':>8} {'n/a':>9} {'n/a':>9} {'n/a':>8}   "
                         f"{'-- (in none of the three sources)':<26}")
            continue
        g = rate.get
        lines.append(f"{model:<26} {g('input', 0):8.3f} {g('cache_read', g('input', 0)):9.4f} "
                     f"{g('cache_write', g('input', 0)):9.3f} {g('output', 0):8.3f}   "
                     f"{g('_key', '?'):<26} {g('_updated', '')}")
    lines += [
        "-" * len(head),
        "Selection rule: FIRST-PARTY PROVIDERS ONLY (models.dev anthropic/openai/moonshotai/",
        "  deepseek/zai/xiaomi/alibaba/minimax...; litellm bare vendor keys, no slash). Resellers",
        "  and aggregators (azure / bedrock / vertex / openrouter / fireworks...) are never used --",
        "  a resale price can be 55x the vendor's own (one openrouter entry had cache_read 0.2",
        "  against the vendor's 0.0036). International entrances outrank domestic ones; subscription",
        "  entrances (*-token-plan, *-coding-plan, all four rates 0) are not listed. No official",
        "  price -> n/a, decided explicitly in rates.yaml rather than papered over with a wrong one.",
        "Note: these are each vendor's PUBLIC list prices, not what a self-hosted gateway or proxy",
        f"  layer actually settled at. The amounts mean \"equivalent spend at public list price\" --",
        f"  comparable and checkable, but not a bill. Override in {_RATES.name} to report real spend.",
        "Note: a distant upstream-updated date may no longer match the current price -- run",
        "  --refresh-pricing to re-fetch the snapshot.",
        "=" * len(head), ""]
    return lines
