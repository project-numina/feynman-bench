#!/usr/bin/env python3
"""Score solver predictions against the ground-truth dataset.

Two metrics, both grouped by topology and computed over *covered* integrals
(those for which the solver produced a result). Missing predictions are
tracked separately via ``n_missing`` and don't drag validity or step_ratio
down — that lets a slow solver report partial coverage honestly.

  validity    = n_valid / n_covered        (per topology, macro-averaged)
  step_ratio  = sum(steps_pred) / sum(steps_gt) on covered integrals
                (per topology, macro-averaged; lower is better)

This script is a thin layer on top of :func:`check_validity.check_validity`,
which owns the indexing and agreement logic.

Usage:
  python3 score.py \\
      --predictions results/2026*_eval_fire/results.jsonl \\
      --ground-truth ground_truth.jsonl \\
      --solver fire \\
      --output results/2026*_eval_fire/score.json
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from check_validity import check_validity
from reductions import format_expr


# ── Scoring ────────────────────────────────────────────────────────────────

def score(predictions, ground_truth, *, solver, gt_solver="fire",
          topology_filter=None):
    """Run check_validity, fold per-topology counts into the two metrics.

    Returns a JSON-serializable summary dict (the same shape that
    score.py's --output writes).
    """
    report = check_validity(
        predictions, ground_truth,
        solver=solver, gt_solver=gt_solver,
        topology_filter=topology_filter,
    )

    per_topology = {}
    topo_validities = []
    topo_step_ratios = []
    for topo, b in sorted(report["per_topology"].items()):
        validity = (b["n_valid"] / b["n_covered"]) if b["n_covered"] else None
        step_ratio = (b["steps_pred"] / b["steps_gt"]) if b["steps_gt"] else None
        per_topology[topo] = {
            "n_gt": b["n_gt"],
            "n_covered": b["n_covered"],
            "n_valid": b["n_valid"],
            "n_missing": b["n_missing"],
            "steps_pred": b["steps_pred"],
            "steps_gt": b["steps_gt"],
            "validity": validity,
            "step_ratio": step_ratio,
        }
        if b["n_covered"]:
            topo_validities.append(validity)
            topo_step_ratios.append(step_ratio)

    macro_validity = (sum(topo_validities) / len(topo_validities)) if topo_validities else None
    mean_step_ratio = (sum(topo_step_ratios) / len(topo_step_ratios)) if topo_step_ratios else None

    return {
        "solver": solver,
        "gt_solver": gt_solver,
        "macro_validity": macro_validity,
        "mean_step_ratio": mean_step_ratio,
        "n_topologies_covered": len(topo_validities),
        "per_topology": per_topology,
        "totals": report["totals"],
        # Carry through for callers that want to render comparison.md.
        "_report": report,
    }


# ── Per-topology comparison.md ─────────────────────────────────────────────

def _format_params(p):
    if not p:
        return ""
    if isinstance(p, dict):
        return ",".join(f"{k}->{v}" for k, v in p.items())
    return str(p)


def write_comparison_md(path, *, topology, solver, gt_solver, stats,
                        report):
    """Per-topology comparison: agree / disagree / missing summary +
    a discrepancy table.

    ``stats`` is the per-topology dict produced by :func:`score` for one
    topology. ``report`` is the full ``check_validity`` report (used to
    enumerate per-integral expressions).
    """
    gt_records = report["gt_records"]
    per_integral = report["per_integral"]

    keys = sorted((k for k in gt_records if k[0] == topology), key=lambda k: k[1])
    n_props = len(keys[0][1]) if keys else 0

    level_counts = {}
    for _, integral in keys:
        lvl = sum(1 for v in integral if v > 0)
        level_counts[lvl] = level_counts.get(lvl, 0) + 1
    targets_summary = ", ".join(f"{c} at level {l}"
                                for l, c in sorted(level_counts.items()))

    params_str = ""
    for k in keys:
        params_str = _format_params(gt_records[k].get("params"))
        if params_str:
            break

    n_agree = stats["n_valid"]
    n_disagree = stats["n_covered"] - stats["n_valid"]
    n_skipped = stats["n_missing"]
    pct = (f"{100*stats['validity']:.1f}%"
           if stats["validity"] is not None else "N/A")

    md = []
    md.append(f"# Benchmark Comparison: {solver} vs {gt_solver}")
    md.append("")
    md.append(f"**Topology:** `{topology}` ({n_props} propagators)  ")
    if params_str:
        md.append(f"**Parameters:** `{params_str}`  ")
    md.append(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  ")
    md.append(f"**Targets:** {stats['n_gt']} integrals ({targets_summary})")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## Pairwise Agreement")
    md.append("")
    md.append("| Pair | Agree | Disagree | Missing | Agree % |")
    md.append("|---|---|---|---|---|")
    md.append(f"| {solver} vs {gt_solver} | {n_agree} | {n_disagree} | {n_skipped} | {pct} |")
    md.append("")

    discrepancies = [
        (k[1], per_integral[k]) for k in keys
        if per_integral[k]["agree"] is not True
    ]
    if discrepancies:
        md.append(f"## Discrepancies ({len(discrepancies)} integrals)")
        md.append("")
        for integral, entry in discrepancies:
            integral_str = ",".join(str(x) for x in integral)
            md.append(f"### `[{integral_str}]`")
            md.append("")
            flag = (f"{solver} missing" if entry["missing"]
                    else f"{solver} ≠ {gt_solver}")
            md.append(f"_{flag}_")
            md.append("")
            md.append("| Solver | Expression |")
            md.append("|---|---|")
            md.append(f"| {solver} | {format_expr(entry['pred_red'])} |")
            md.append(f"| {gt_solver} | {format_expr(entry['gt_red'])} |")
            md.append("")

    Path(path).write_text("\n".join(md), encoding="utf-8")


def write_per_topology_artifacts(summary, *, pred_dir_for_topology):
    """For each scored topology, write score.json + comparison.md next to
    the originating results.jsonl (when we can resolve the directory).

    ``pred_dir_for_topology``: {topology: results_dir_path} mapping built by
    the caller from how it laid out per-topology results.jsonl files.
    """
    report = summary["_report"]
    written = 0
    for topo, stats in summary["per_topology"].items():
        d = pred_dir_for_topology.get(topo)
        if not d:
            continue
        topo_summary = {
            "solver": summary["solver"],
            "gt_solver": summary["gt_solver"],
            "topology": topo,
            **stats,
        }
        (Path(d) / "score.json").write_text(
            json.dumps(topo_summary, indent=2, default=float)
        )
        write_comparison_md(
            Path(d) / "comparison.md",
            topology=topo, solver=summary["solver"],
            gt_solver=summary["gt_solver"], stats=stats, report=report,
        )
        written += 1
    return written


# ── CLI ────────────────────────────────────────────────────────────────────

def _print_summary(summary):
    print()
    print("=" * 60)
    print(f"Score: {summary['solver']} vs {summary['gt_solver']}")
    print("=" * 60)
    mv = summary["macro_validity"]
    sr = summary["mean_step_ratio"]
    print(f"  macro validity:   {'N/A' if mv is None else f'{100*mv:.2f}%'}")
    print(f"  mean step_ratio:  {'N/A' if sr is None else f'{sr:.3f}'}")
    print(f"  topologies:       {summary['n_topologies_covered']}/{len(summary['per_topology'])}")
    print(f"  total integrals:  covered {summary['totals']['n_covered']}"
          f" / valid {summary['totals']['n_valid']}"
          f" / missing {summary['totals']['n_missing']}"
          f" / GT {summary['totals']['n_gt']}")
    print()
    print(f"{'topology':<24} {'validity':>10} {'step_ratio':>12} {'covered':>10}")
    print("-" * 60)
    for topo, b in summary["per_topology"].items():
        v = "N/A" if b["validity"] is None else f"{100*b['validity']:.1f}%"
        sr = "N/A" if b["step_ratio"] is None else f"{b['step_ratio']:.3f}"
        cov = f"{b['n_covered']}/{b['n_gt']}"
        print(f"{topo:<24} {v:>10} {sr:>12} {cov:>10}")
    print()


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--predictions", nargs="+", required=True,
                   help="One or more jsonl files, directories, or globs with the solver's results.")
    p.add_argument("--ground-truth", required=True,
                   help="Path to ground_truth.jsonl.")
    p.add_argument("--solver", required=True,
                   help="Solver name to filter prediction records on.")
    p.add_argument("--gt-solver", default="fire",
                   help="Solver name in ground truth (default: fire).")
    p.add_argument("--topologies", default=None,
                   help="Comma-separated list of topology paths to score (default: all in GT).")
    p.add_argument("--output", default=None,
                   help="Write the score summary as JSON to this path.")
    p.add_argument("--json", action="store_true",
                   help="Emit the score summary as JSON to stdout.")
    return p.parse_args()


def main():
    args = parse_args()
    topo_filter = (set(t.strip() for t in args.topologies.split(",") if t.strip())
                   if args.topologies else None)

    summary = score(
        args.predictions, args.ground_truth,
        solver=args.solver, gt_solver=args.gt_solver,
        topology_filter=topo_filter,
    )
    summary_for_disk = {k: v for k, v in summary.items() if not k.startswith("_")}

    if args.json:
        json.dump(summary_for_disk, sys.stdout, indent=2, default=float)
        print()

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        Path(args.output).write_text(json.dumps(summary_for_disk, indent=2, default=float))
        print(f"Score summary written to: {args.output}")

    _print_summary(summary)


if __name__ == "__main__":
    main()
