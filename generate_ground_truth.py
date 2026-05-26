#!/usr/bin/env python3
"""Build the ground-truth dataset (train + test) by sampling target integrals
per topology and reducing each with FIRE.

The split is defined in ``dataset_split.yaml``. For each entry, this script:

  1. Samples a fixed (per-topology) parameter assignment from ``--seed``.
     Train and test for the same topology share the same parameter dict so the
     two splits live in the same kinematic point.
  2. Samples ``targets_per_level`` distinct integrals at each requested sector
     level, with non-zero indices drawn uniformly from ``index_range``.
     Zero sectors (read from the FIRE ``.start`` file) are rejected.
  3. Runs FIRE on each target via a ``ProcessPoolExecutor`` and appends one
     record per successful reduction to ``ground_truth_<split>.jsonl``.

Examples
--------
  # Both splits, default config, 32 parallel FIRE processes.
  ./generate_ground_truth.py

  # Only the test split.
  ./generate_ground_truth.py --split test

  # One topology, useful for spot-checks.
  ./generate_ground_truth.py --split train --topology 5D/bl2em

  # Dry run: show what would be sampled without running FIRE.
  ./generate_ground_truth.py --dry-run

Output records match the schema of the existing ``ground_truth.jsonl`` (one
JSON object per line, with ``solver``, ``topology``, ``finite_field``,
``params``, ``integrals``, ``reductions``, ``num_steps``, ``topology_path``).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from math import comb
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


DEFAULT_SPLIT_CONFIG = "dataset_split.yaml"
DEFAULT_PARAM_LO = 2
DEFAULT_PARAM_HI = 9999


# ── Topology discovery ─────────────────────────────────────────────────────

def discover_num_propagators(topo_dir: Path) -> int:
    """Count propagator indices from the FIRE ``.start`` file's ExampleDimension."""
    fire_dir = topo_dir / "fire"
    starts = list(fire_dir.glob("*.start"))
    if not starts:
        raise FileNotFoundError(f"No FIRE .start file in {fire_dir}")
    text = starts[0].read_text()
    m = re.search(r"ExampleDimension\[0\]\s*=\s*(\d+)", text)
    if m:
        return int(m.group(1))
    # Fallback: count entries inside the first SBasisL sign pattern.
    m = re.search(r"SBasisL\[0,\s*\{([^\}]+)\}\]", text)
    if not m:
        raise ValueError(f"Cannot determine propagator count from {starts[0]}")
    return len([x for x in m.group(1).split(",") if x.strip()])


def load_zero_sectors(topo_dir: Path) -> set:
    """Read ``zero_sectors.txt`` (one 0/1 mask per line) if it exists.

    A mask ``(b_1, ..., b_n)`` with ``b_i ∈ {0, 1}`` flags the sector where
    propagator i has index 0 iff ``b_i == 0`` as identically zero. Sampled
    targets whose support mask is in this set are skipped at sampling time
    (they would just produce a zero FIRE reduction).
    """
    f = topo_dir / "zero_sectors.txt"
    if not f.exists():
        return set()
    masks = set()
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        masks.add(tuple(int(x) for x in line.split(",")))
    return masks


def discover_param_keys(topo_dir: Path) -> list:
    params_file = topo_dir / "parameters.yaml"
    with open(params_file) as f:
        data = yaml.safe_load(f) or {}
    keys = list((data.get("parameters") or {}).keys())
    if not keys:
        raise ValueError(f"No parameters block in {params_file}")
    return keys


# ── Sampling ───────────────────────────────────────────────────────────────

def sample_params(keys: list, rng: random.Random,
                  lo: int = DEFAULT_PARAM_LO, hi: int = DEFAULT_PARAM_HI) -> dict:
    return {k: rng.randint(lo, hi) for k in keys}


def sample_targets(num_propagators: int, sector_levels: list,
                   targets_per_level: int, index_range: tuple,
                   rng: random.Random, zero_sectors: set | None = None,
                   exclude: set | None = None) -> list:
    """Sample distinct integrals across requested sector levels.

    Each level N produces up to ``targets_per_level`` integrals with exactly
    N non-zero indices, drawn uniformly from ``index_range`` excluding 0.

    ``zero_sectors``: support masks (0/1 tuples) for sectors known to be
    trivially zero. Sampled candidates whose mask falls here are skipped.

    ``exclude``: integrals (as tuples) already chosen — used by the
    oversample-and-top-up path so resamples produce fresh candidates.
    """
    nz = [v for v in range(index_range[0], index_range[1] + 1) if v != 0]
    if not nz:
        raise ValueError(f"index_range {index_range} contains no non-zero values")
    zero_sectors = zero_sectors or set()
    exclude = exclude or set()

    targets = []
    for level in sector_levels:
        if not (1 <= level <= num_propagators):
            print(f"  [skip] sector level {level} outside [1, {num_propagators}]",
                  file=sys.stderr)
            continue
        max_distinct = (len(nz) ** level) * comb(num_propagators, level)
        want = min(targets_per_level, max_distinct)
        seen = set()
        attempts = 0
        cap = max(want * 200, 2000)
        while len(seen) < want and attempts < cap:
            attempts += 1
            positions = rng.sample(range(num_propagators), level)
            indices = [0] * num_propagators
            for pos in positions:
                indices[pos] = rng.choice(nz)
            t = tuple(indices)
            if t in exclude:
                continue
            mask = tuple(1 if v != 0 else 0 for v in indices)
            if mask in zero_sectors:
                continue
            seen.add(t)
        targets.extend(sorted(seen))
    return targets


def _topo_seed(seed: int, *parts) -> int:
    """Stable per-(topology, purpose) sub-seed derived from the master seed."""
    return abs(hash((seed, *parts))) % (2**31 - 1)


# ── Worker (FIRE per target) ───────────────────────────────────────────────

def _fire_task(args):
    integral, params, topology, root_dir, threads = args
    try:
        from solvers import fire
        t0 = time.monotonic()
        record = fire.run(
            integral=integral, params=params, topology=topology,
            root_dir=root_dir, threads=threads,
        )
        return (topology, integral, record, None, time.monotonic() - t0)
    except Exception as e:  # noqa: BLE001
        return (topology, integral, None, f"{type(e).__name__}: {e}", 0.0)


# ── Per-split orchestration ────────────────────────────────────────────────

def _resolve_split_entries(cfg: dict, split: str, topology_filter: set | None):
    entries = list(cfg.get(split) or [])
    if topology_filter:
        entries = [e for e in entries if e["topology"] in topology_filter]
    return entries


def _plan_targets(entries: list, seed: int, split: str, repo_root: Path,
                  topo_params: dict, oversample: float = 1.0) -> list:
    """Returns flat list of (integral_tuple, params, topology) for this split.

    ``oversample``: multiplier on ``targets_per_level``. Used by the
    nonzero-filter path to draw extra candidates so that after dropping
    FIRE-zero reductions we still have enough.
    """
    plan = []
    for entry in entries:
        topo = entry["topology"]
        topo_dir = repo_root / "topologies" / topo
        n_props = discover_num_propagators(topo_dir)
        zero_secs = load_zero_sectors(topo_dir)
        sl = list(entry["sector_levels"])
        lo, hi = entry["index_range"]
        tpl = max(1, int(round(entry["targets_per_level"] * oversample)))
        rng = random.Random(_topo_seed(seed, split, topo))
        targets = sample_targets(n_props, sl, tpl, (lo, hi), rng,
                                 zero_sectors=zero_secs)
        for t in targets:
            plan.append((list(t), topo_params[topo], topo))
        extra = f"  oversample×{oversample:g}" if oversample != 1.0 else ""
        zs_msg = f"  zero_sectors={len(zero_secs)}" if zero_secs else ""
        print(f"  {topo:<25s} levels={sl}  range=[{lo},{hi}]  "
              f"requested={tpl}/level{extra}{zs_msg} → {len(targets)} targets")
    return plan


def _is_nonzero_record(record: dict) -> bool:
    """A FIRE record counts as 'nonzero' if its reduction dict is non-empty.

    FIRE represents an integral that reduces to zero as ``reductions[key] = {}``.
    """
    reds = record.get("reductions") or {}
    return any(bool(v) for v in reds.values())


def _run_fire_batch(tasks, max_parallel, label, n_total_label):
    """Run ``tasks`` through the FIRE process pool, yielding (topo, integral,
    record, err, elapsed) tuples as they complete. ``n_total_label`` is used
    only in progress prints."""
    done = 0
    n_ok = n_fail = 0
    with ProcessPoolExecutor(max_workers=max_parallel) as pool:
        futures = {pool.submit(_fire_task, t): t for t in tasks}
        for fut in as_completed(futures):
            topo, integral, record, err, elapsed = fut.result()
            done += 1
            if err is None:
                n_ok += 1
            else:
                n_fail += 1
                print(f"  [{label}: {done}/{n_total_label}] FAIL {topo} "
                      f"{integral}: {err}", file=sys.stderr)
            yield topo, integral, record, err, elapsed, done, n_ok, n_fail


def run_split(*, split, cfg, repo_root, out_path, max_parallel, threads,
              topology_filter, seed, dry_run, require_nonzero,
              nonzero_oversample, nonzero_max_rounds):
    entries = _resolve_split_entries(cfg, split, topology_filter)
    if not entries:
        print(f"[{split}] no entries match filter — skipping")
        return

    # One params dict per topology, shared across splits (deterministic from seed).
    all_topos = sorted({e["topology"] for s in cfg.values() for e in s})
    topo_params = {}
    for topo in all_topos:
        rng = random.Random(_topo_seed(seed, "params", topo))
        topo_params[topo] = sample_params(
            discover_param_keys(repo_root / "topologies" / topo), rng,
        )

    print()
    print("=" * 70)
    print(f"Split: {split}   ({len(entries)} topologies)")
    if require_nonzero:
        print(f"  require_nonzero=True  oversample={nonzero_oversample}  "
              f"max_rounds={nonzero_max_rounds}")
    print("=" * 70)
    print("Sampling targets:")
    plan = _plan_targets(entries, seed, split, repo_root, topo_params,
                         oversample=nonzero_oversample if require_nonzero else 1.0)
    n_total = len(plan)
    print(f"  → {n_total} (target, topology) tasks total")

    if dry_run:
        print(f"[dry-run] would run FIRE {n_total} times → {out_path}")
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    t_start = time.monotonic()

    if not require_nonzero:
        tasks = [(integral, params, topo, str(repo_root), threads)
                 for integral, params, topo in plan]
        print(f"\nReducing with FIRE  (max_parallel={max_parallel}, threads={threads})")
        print("-" * 70)
        n_ok = n_fail = 0
        with open(out_tmp, "w") as out:
            for topo, integral, rec, err, _el, done, n_ok, n_fail in \
                    _run_fire_batch(tasks, max_parallel, "all", n_total):
                if rec is not None:
                    out.write(json.dumps(rec, separators=(",", ":")) + "\n")
                    out.flush()
                if done % 25 == 0 or done == n_total:
                    print(f"  [{done}/{n_total}] elapsed "
                          f"{time.monotonic() - t_start:.0f}s  "
                          f"(ok={n_ok}, failed={n_fail})")
        os.replace(out_tmp, out_path)
        print()
        print(f"[{split}] wrote {n_ok} records → {out_path}  ({n_fail} failed)")
        return

    # ── Nonzero-filtered path ────────────────────────────────────────────
    # Each topology has a target count = entry.targets_per_level × n_levels.
    # We run FIRE on the oversampled plan, keep only nonzero reductions, and
    # top up with additional rounds (each at the requested oversample) until
    # every topology reaches its target — or we hit max_rounds.

    targets_needed = {}
    used_integrals = {}  # topo -> set of integral tuples already sampled
    for entry in entries:
        topo = entry["topology"]
        targets_needed[topo] = entry["targets_per_level"] * len(entry["sector_levels"])
        used_integrals[topo] = set()
    for integral, _params, topo in plan:
        used_integrals[topo].add(tuple(integral))

    nonzero_kept = {t: [] for t in targets_needed}
    print(f"\nReducing with FIRE  (max_parallel={max_parallel}, threads={threads})")
    print("-" * 70)
    rnd_label = "round 1"
    tasks = [(integral, params, topo, str(repo_root), threads)
             for integral, params, topo in plan]
    for round_idx in range(1, nonzero_max_rounds + 1):
        if round_idx > 1:
            rnd_label = f"round {round_idx}"
        n_total_label = len(tasks)
        n_zero_dropped = 0
        for topo, integral, rec, err, _el, done, n_ok, n_fail in \
                _run_fire_batch(tasks, max_parallel, rnd_label, n_total_label):
            if rec is None:
                continue
            if len(nonzero_kept[topo]) >= targets_needed[topo]:
                # Already full for this topology — discard.
                continue
            if _is_nonzero_record(rec):
                nonzero_kept[topo].append(rec)
            else:
                n_zero_dropped += 1
            if done % 25 == 0 or done == n_total_label:
                missing = sum(max(0, targets_needed[t] - len(nonzero_kept[t]))
                              for t in targets_needed)
                print(f"  [{rnd_label} {done}/{n_total_label}] elapsed "
                      f"{time.monotonic() - t_start:.0f}s  "
                      f"zero_drop={n_zero_dropped}  still_needed={missing}")

        # Plan a top-up round for topologies still short of target.
        short = {t: targets_needed[t] - len(nonzero_kept[t])
                 for t in targets_needed
                 if len(nonzero_kept[t]) < targets_needed[t]}
        if not short:
            break
        print(f"\n  After {rnd_label}: short on "
              f"{', '.join(f'{t}={n}' for t, n in sorted(short.items()))}")
        if round_idx == nonzero_max_rounds:
            print("  (max rounds reached — stopping)")
            break
        # Build a top-up task list: per-topology, sample fresh candidates
        # (rejecting integrals already used) and queue them up.
        topup_plan = []
        for entry in entries:
            topo = entry["topology"]
            if topo not in short:
                continue
            topo_dir = repo_root / "topologies" / topo
            n_props = discover_num_propagators(topo_dir)
            zero_secs = load_zero_sectors(topo_dir)
            sl = list(entry["sector_levels"])
            lo, hi = entry["index_range"]
            need = short[topo]
            # Oversample heavily for difficult topologies; estimate from this
            # round's observed nonzero rate per topo.
            seen_this_topo = sum(1 for _, _, t in plan if t == topo)
            kept_this_topo = len(nonzero_kept[topo])
            rate = max(0.05, kept_this_topo / seen_this_topo) if seen_this_topo else 0.1
            per_level = max(1, int((need / len(sl)) / rate * 1.5) + 4)
            rng = random.Random(_topo_seed(seed, split, topo, "topup", round_idx))
            extra = sample_targets(n_props, sl, per_level, (lo, hi), rng,
                                   zero_sectors=zero_secs,
                                   exclude=used_integrals[topo])
            for t in extra:
                used_integrals[topo].add(t)
                topup_plan.append((list(t), topo_params[topo], topo))
            print(f"    top-up {topo}: nonzero-rate≈{rate:.2f} → "
                  f"queuing {len(extra)} more")
        plan = topup_plan
        tasks = [(integral, params, topo, str(repo_root), threads)
                 for integral, params, topo in plan]
        if not tasks:
            break

    # Trim to target and write.
    n_written = 0
    with open(out_tmp, "w") as out:
        for topo in sorted(nonzero_kept):
            recs = nonzero_kept[topo][:targets_needed[topo]]
            for rec in recs:
                out.write(json.dumps(rec, separators=(",", ":")) + "\n")
                n_written += 1
    os.replace(out_tmp, out_path)

    print()
    print(f"[{split}] wrote {n_written} nonzero records → {out_path}")
    print("  per-topology:")
    for topo in sorted(nonzero_kept):
        kept = min(len(nonzero_kept[topo]), targets_needed[topo])
        target = targets_needed[topo]
        marker = "" if kept == target else f"  (SHORT by {target - kept})"
        print(f"    {topo:<25s} {kept}/{target}{marker}")


# ── CLI ────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--split", choices=["train", "test", "all"], default="all",
                   help="Which split(s) to (re)generate (default: all).")
    p.add_argument("--split-config", default=DEFAULT_SPLIT_CONFIG,
                   help=f"Split YAML (default: {DEFAULT_SPLIT_CONFIG}).")
    p.add_argument("--out-dir", default=None,
                   help="Directory for ground_truth_<split>.jsonl outputs "
                        "(default: repo root).")
    p.add_argument("--topology", default=None,
                   help="Restrict to a single topology, e.g. 5D/bl2em.")
    p.add_argument("--topologies", default=None,
                   help="Comma-separated topology subset.")
    p.add_argument("--max-parallel", type=int, default=32,
                   help="Concurrent FIRE processes (default: 32).")
    p.add_argument("--threads", type=int, default=4,
                   help="FIRE-internal threads per target (default: 4).")
    p.add_argument("--seed", type=int, default=42,
                   help="Master RNG seed (default: 42).")
    p.add_argument("--dry-run", action="store_true",
                   help="Sample targets and print the plan; do not run FIRE.")
    p.add_argument("--require-nonzero", action="store_true",
                   help="Keep only targets whose FIRE reduction is non-empty. "
                        "Oversamples (see --nonzero-oversample) and tops up in "
                        "additional rounds (--nonzero-max-rounds) until each "
                        "topology has targets_per_level × n_levels nonzero "
                        "records. Recommended for the test split.")
    p.add_argument("--nonzero-oversample", type=float, default=3.0,
                   help="Initial oversample multiplier when --require-nonzero is "
                        "set (default: 3.0). Per-topology rounds use observed "
                        "nonzero rates to size top-ups, not this knob.")
    p.add_argument("--nonzero-max-rounds", type=int, default=4,
                   help="Maximum sampling+FIRE rounds when --require-nonzero "
                        "is set (default: 4).")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    repo_root = _REPO_ROOT
    cfg_path = (repo_root / args.split_config) if not os.path.isabs(args.split_config) \
        else Path(args.split_config)
    if not cfg_path.exists():
        raise SystemExit(f"Split config not found: {cfg_path}")
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f) or {}

    out_dir = Path(args.out_dir).resolve() if args.out_dir else repo_root

    topology_filter = set()
    if args.topology:
        topology_filter.add(args.topology)
    if args.topologies:
        topology_filter.update(t.strip() for t in args.topologies.split(",") if t.strip())
    topology_filter = topology_filter or None

    splits = ["train", "test"] if args.split == "all" else [args.split]
    started = datetime.now()
    print(f"generate_ground_truth.py  started {started.isoformat()}")
    print(f"  split={args.split}  config={cfg_path}  out_dir={out_dir}  seed={args.seed}")
    if topology_filter:
        print(f"  topology_filter={sorted(topology_filter)}")

    for split in splits:
        out_path = out_dir / f"ground_truth_{split}.jsonl"
        run_split(
            split=split, cfg=cfg, repo_root=repo_root, out_path=out_path,
            max_parallel=args.max_parallel, threads=args.threads,
            topology_filter=topology_filter, seed=args.seed,
            dry_run=args.dry_run,
            require_nonzero=args.require_nonzero,
            nonzero_oversample=args.nonzero_oversample,
            nonzero_max_rounds=args.nonzero_max_rounds,
        )

    print(f"\nDone. Elapsed: {(datetime.now() - started)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
