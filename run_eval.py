#!/usr/bin/env python3
"""Evaluate a solver against the ground-truth dataset.

For each target integral in ``ground_truth.jsonl`` (optionally filtered by
topology), run the requested solver, collect results into a jsonl, and score
against the ground-truth using :mod:`check_validity` + :mod:`score`.

Examples
--------
  # all topologies, fire solver, defaults (32 targets in parallel, 4 FIRE threads each)
  ./run_eval.py

  # single topology
  ./run_eval.py --topology 3D/bub2l

  # subset
  ./run_eval.py --topologies 3D/bub2l,4D/box1l,9D/p3lO4

  # tune parallelism
  ./run_eval.py --max-parallel 16 --threads 8

  # health check: verify FIRE is configured and runs end-to-end on the
  # smallest topology
  ./run_eval.py --check
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# Make the repo root importable when running via ``./run_eval.py`` or
# ``python3 run_eval.py``. (No-op when installed as a console script.)
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from score import score, write_per_topology_artifacts, _print_summary  # noqa: E402


# ── Ground-truth loading ───────────────────────────────────────────────────

def _load_ground_truth(path):
    """Return list[dict] of GT records."""
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def _resolve_topologies(repo_root, requested):
    """Return ordered list of topology paths (e.g. ['3D/bub2l', '4D/box1l']).

    If ``requested`` is empty, return every topology with a ``parameters.yaml``.
    Validates that each requested topology exists on disk.
    """
    topos_dir = repo_root / "topologies"
    available = sorted(
        str(p.parent.relative_to(topos_dir))
        for p in topos_dir.rglob("parameters.yaml")
    )
    if not requested:
        return available
    avail_set = set(available)
    missing = [t for t in requested if t not in avail_set]
    if missing:
        raise SystemExit(
            f"Unknown topology(ies): {', '.join(missing)}.\n"
            f"Available: {', '.join(available)}"
        )
    # Preserve input order, dedup
    seen, out = set(), []
    for t in requested:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ── Per-target work (must be picklable for ProcessPoolExecutor) ────────────

def _solver_task(args):
    """Run a single solver on a single (topology, integral, params) target.

    Returns (key, record_or_none, error_or_none).
    """
    solver_name, integral, params, topology, root_dir, threads, timeout = args
    key = (topology, tuple(integral))
    t0 = time.monotonic()
    try:
        record = _dispatch_solver(
            solver_name, integral=integral, params=params,
            topology=topology, root_dir=root_dir, threads=threads,
            timeout=timeout,
        )
        return key, record, None
    except Exception as e:  # noqa: BLE001
        return key, None, f"{type(e).__name__}: {e}  (elapsed {time.monotonic()-t0:.1f}s)"


def _dispatch_solver(name, **kwargs):
    if name == "fire":
        from solvers import fire
        # ``timeout`` is currently unused by the FIRE wrapper (FIRE doesn't
        # support timing out cleanly from Python); the outer ProcessPool can
        # be killed if needed. Drop the kwarg so run() doesn't get it.
        kwargs.pop("timeout", None)
        return fire.run(**kwargs)
    raise ValueError(f"Unknown solver: {name!r}")


# ── Eval orchestration ────────────────────────────────────────────────────

def run_eval(*, solver, topologies, repo_root, ground_truth_path,
             max_parallel, threads, timeout, out_subdir=None):
    repo_root = Path(repo_root).resolve()
    gt_path = Path(ground_truth_path).resolve()
    gt_records = _load_ground_truth(gt_path)

    # Filter targets by requested topologies.
    requested = set(topologies)
    targets = [r for r in gt_records if r.get("topology_path") in requested]
    if not targets:
        raise SystemExit(
            f"No targets in {gt_path} match topologies {sorted(requested)}"
        )

    # Output layout: results/<ts>_eval_<solver>/<topo_slug>/results.jsonl
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_subdir = out_subdir or f"{timestamp}_eval_{solver}"
    out_root = repo_root / "results" / out_subdir
    out_root.mkdir(parents=True, exist_ok=True)

    pred_dir_for_topology = {}
    topo_open_files = {}
    n_done = 0
    n_failed = 0
    t_start = time.monotonic()

    print(f"=" * 60)
    print(f"Eval: solver={solver}  topologies={len(requested)}  targets={len(targets)}")
    print(f"      max_parallel={max_parallel}  threads={threads}  timeout={timeout}s")
    print(f"      output: {out_root}")
    print(f"=" * 60)

    # Build the task list. Each task is fully self-contained.
    tasks = []
    for rec in targets:
        topo = rec["topology_path"]
        integral = rec["integrals"][0]
        params = rec["params"]
        tasks.append((solver, integral, params, topo, str(repo_root), threads, timeout))

    # Open the per-topology output files lazily.
    def _ensure_topo_file(topo):
        if topo in topo_open_files:
            return topo_open_files[topo]
        topo_slug = topo.replace("/", "_")
        topo_dir = out_root / topo_slug
        topo_dir.mkdir(parents=True, exist_ok=True)
        f = open(topo_dir / "results.jsonl", "w")
        topo_open_files[topo] = f
        pred_dir_for_topology[topo] = str(topo_dir)
        return f

    errors = []
    try:
        with ProcessPoolExecutor(max_workers=max_parallel) as pool:
            futures = {pool.submit(_solver_task, t): t for t in tasks}
            for fut in as_completed(futures):
                key, record, error = fut.result()
                topo, _ = key
                n_done += 1
                if error is not None:
                    n_failed += 1
                    errors.append((key, error))
                    print(f"  [{n_done}/{len(tasks)}] FAILED {topo} {key[1]}: {error}",
                          file=sys.stderr)
                    continue
                _ensure_topo_file(topo).write(
                    json.dumps(record, separators=(",", ":")) + "\n"
                )
                topo_open_files[topo].flush()
                if n_done % 25 == 0 or n_done == len(tasks):
                    elapsed = time.monotonic() - t_start
                    print(f"  [{n_done}/{len(tasks)}] {topo} done  "
                          f"(elapsed {elapsed:.0f}s, failed {n_failed})")
    finally:
        for f in topo_open_files.values():
            f.close()

    if errors:
        log = out_root / "errors.log"
        with open(log, "w") as f:
            for key, err in errors:
                f.write(f"{key[0]}  {list(key[1])}  {err}\n")
        print(f"\n  {len(errors)} target(s) failed; details in {log}")

    # Score
    summary = score(
        [str(out_root)], str(gt_path),
        solver=solver, gt_solver="fire",
        topology_filter=requested,
    )
    summary_for_disk = {k: v for k, v in summary.items() if not k.startswith("_")}
    score_path = out_root / "score.json"
    score_path.write_text(json.dumps(summary_for_disk, indent=2, default=float))
    n_topo_written = write_per_topology_artifacts(
        summary, pred_dir_for_topology=pred_dir_for_topology,
    )
    _print_summary(summary)
    print(f"Per-topology score.json + comparison.md written for {n_topo_written} topology dir(s).")
    print(f"Overall score:  {score_path}")
    return summary_for_disk


# ── --check mode ──────────────────────────────────────────────────────────

def _smoke_check(repo_root):
    """Run FIRE on the cheapest GT target to verify config + binary."""
    repo_root = Path(repo_root).resolve()
    gt_path = repo_root / "ground_truth_test.jsonl"
    if not gt_path.exists():
        raise SystemExit(f"Missing {gt_path}")
    gt = _load_ground_truth(gt_path)
    # Pick the smallest topology by propagator count, smallest num_steps.
    gt.sort(key=lambda r: (len(r["integrals"][0]), r.get("num_steps") or 0))
    target = gt[0]
    print(f"--check: running FIRE on {target['topology_path']} integral "
          f"{target['integrals'][0]}  (GT steps={target.get('num_steps')})")
    from solvers import fire
    t0 = time.monotonic()
    result = fire.run(
        integral=target["integrals"][0],
        params=target["params"],
        topology=target["topology_path"],
        root_dir=str(repo_root),
        threads=4,
    )
    elapsed = time.monotonic() - t0
    print(f"  FIRE returned in {elapsed:.1f}s with {len(result.get('reductions') or {})} reduction entries.")
    # Quick sanity: keys should match the target integral.
    key = str(list(target["integrals"][0]))
    if key not in (result.get("reductions") or {}):
        raise SystemExit(f"FIRE result missing expected key {key}; got "
                         f"{list((result.get('reductions') or {}).keys())}")
    print("--check: OK")


# ── CLI ────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--solver", default="fire",
                   help="Solver to evaluate (default: fire).")
    p.add_argument("--topology", default=None,
                   help="Single topology to evaluate, e.g. 4D/box1l.")
    p.add_argument("--topologies", default=None,
                   help="Comma-separated subset of topologies.")
    p.add_argument("--max-parallel", type=int, default=32,
                   help="Concurrent target integrals (default: 32).")
    p.add_argument("--threads", type=int, default=4,
                   help="FIRE-internal threads per target (default: 4).")
    p.add_argument("--solver-timeout", type=int, default=600,
                   help="Per-target soft timeout in seconds (default: 600).")
    p.add_argument("--ground-truth", default=None,
                   help="Override ground-truth path "
                        "(default: <repo>/ground_truth_test.jsonl).")
    p.add_argument("--check", action="store_true",
                   help="Health check: run FIRE on the cheapest target only.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    repo_root = _REPO_ROOT
    gt_path = args.ground_truth or (repo_root / "ground_truth_test.jsonl")

    if args.check:
        _smoke_check(repo_root)
        return 0

    requested = []
    if args.topology:
        requested.append(args.topology)
    if args.topologies:
        requested.extend(t.strip() for t in args.topologies.split(",") if t.strip())
    topologies = _resolve_topologies(repo_root, requested)

    run_eval(
        solver=args.solver, topologies=topologies, repo_root=repo_root,
        ground_truth_path=gt_path, max_parallel=args.max_parallel,
        threads=args.threads, timeout=args.solver_timeout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
