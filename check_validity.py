"""Compare a solver's predictions to a ground-truth dataset.

The single public entry point is :func:`check_validity`. It takes two jsonl
files (predictions + ground truth) and returns a structured report describing
which integrals agree, which disagree, and which are missing — without any
metric arithmetic. ``score.py`` consumes that report to compute validity %
and step ratio.

A "record" is one line of a jsonl file in the format emitted by the FIRE
runner (see :mod:`solvers.fire.parse`):

    {"solver": "fire",
     "topology_path": "4D/box1lc",
     "params":  {"d": 1826, ...},
     "integrals": [[-1, 2, 1, 0]],
     "reductions": {"[-1, 2, 1, 0]": {"[0, 1, 0, 0]": 1826, ...}},
     "num_steps": 43,
     "finite_field": 2017}

Each record carries exactly one target integral in ``integrals[0]``. Both
sides of the comparison must be in the same master-integral basis (FIRE's
choice); see :mod:`solvers.fire.parse` for the schema.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from reductions import extract_reduction, reductions_agree


# ── Public API ──────────────────────────────────────────────────────────────

def check_validity(predictions, ground_truth, *,
                   solver=None, gt_solver="fire",
                   topology_filter=None):
    """Compare predictions to ground truth.

    Parameters
    ----------
    predictions : str | Path | Iterable[str|Path]
        One or more jsonl paths, directories, or globs containing solver
        results. Directories are walked recursively for ``results.jsonl``.
    ground_truth : str | Path
        Path to the ground-truth jsonl.
    solver : str, optional
        Filter prediction records to this solver name. ``None`` (default)
        keeps all records.
    gt_solver : str
        Solver name expected in ground-truth records (default: ``"fire"``).
    topology_filter : Iterable[str], optional
        If given, only score integrals whose ``topology_path`` is in this set.

    Returns
    -------
    dict with keys
        ``per_integral`` : ``{(topology, integral): {"agree": bool|None,
            "pred_red": dict|None, "gt_red": dict|None, "missing": bool,
            "steps_pred": int, "steps_gt": int}}``
        ``per_topology`` : ``{topology: {"n_gt", "n_covered", "n_valid",
            "n_missing", "steps_pred", "steps_gt"}}``
        ``totals`` : aggregate of the per_topology counts.
        ``gt_records``, ``pred_records`` : the indexed source records, keyed
            the same way as ``per_integral`` (useful for downstream
            diagnostics like ``score.py``'s comparison.md).
    """
    pred_paths = _expand_inputs(predictions)
    if not pred_paths:
        raise FileNotFoundError(
            f"check_validity: predictions matched no files (input: {predictions})"
        )

    topo_filter = set(topology_filter) if topology_filter is not None else None

    gt_records = _load_jsonl(ground_truth)
    gt_by_key = {}
    for rec in gt_records:
        if rec.get("solver") != gt_solver:
            continue
        k = _record_key(rec)
        if k is None:
            continue
        if topo_filter is not None and k[0] not in topo_filter:
            continue
        gt_by_key[k] = rec

    if not gt_by_key:
        raise ValueError(
            f"check_validity: ground truth has no records for solver={gt_solver!r}"
            + (f" in topologies {sorted(topo_filter)}" if topo_filter else "")
        )

    pred_by_key = {}
    for path in pred_paths:
        for rec in _load_jsonl(path):
            if solver is not None and rec.get("solver") != solver:
                continue
            k = _record_key(rec)
            if k is None:
                continue
            if topo_filter is not None and k[0] not in topo_filter:
                continue
            pred_by_key[k] = rec

    per_integral = {}
    per_topology = defaultdict(lambda: {
        "n_gt": 0, "n_covered": 0, "n_valid": 0, "n_missing": 0,
        "steps_pred": 0, "steps_gt": 0,
    })

    for key, gt_rec in gt_by_key.items():
        topo = key[0]
        bucket = per_topology[topo]
        bucket["n_gt"] += 1
        gt_red = extract_reduction(gt_rec)

        pred_rec = pred_by_key.get(key)
        if pred_rec is None:
            bucket["n_missing"] += 1
            per_integral[key] = {
                "agree": None, "pred_red": None, "gt_red": gt_red,
                "missing": True, "steps_pred": 0,
                "steps_gt": int(gt_rec.get("num_steps") or 0),
            }
            continue

        pred_red = extract_reduction(pred_rec)
        agree = reductions_agree(pred_red, gt_red)
        steps_pred = int(pred_rec.get("num_steps") or 0)
        steps_gt = int(gt_rec.get("num_steps") or 0)

        bucket["n_covered"] += 1
        bucket["steps_pred"] += steps_pred
        bucket["steps_gt"] += steps_gt
        if agree is True:
            bucket["n_valid"] += 1

        per_integral[key] = {
            "agree": agree, "pred_red": pred_red, "gt_red": gt_red,
            "missing": False, "steps_pred": steps_pred, "steps_gt": steps_gt,
        }

    totals = {"n_gt": 0, "n_covered": 0, "n_valid": 0, "n_missing": 0,
              "steps_pred": 0, "steps_gt": 0}
    for b in per_topology.values():
        for k in totals:
            totals[k] += b[k]

    return {
        "per_integral": per_integral,
        "per_topology": dict(per_topology),
        "totals": totals,
        "gt_records": gt_by_key,
        "pred_records": pred_by_key,
    }


# ── Helpers ─────────────────────────────────────────────────────────────────

def _expand_inputs(patterns):
    """Resolve jsonl paths, directories, or globs into a flat ordered list."""
    if isinstance(patterns, (str, Path)):
        patterns = [patterns]
    out = []
    for p in patterns:
        p = str(p)
        matches = sorted(glob.glob(p))
        if not matches and os.path.exists(p):
            matches = [p]
        for m in matches:
            if os.path.isdir(m):
                for jl in sorted(Path(m).rglob("results.jsonl")):
                    out.append(str(jl))
            else:
                out.append(m)
    seen, deduped = set(), []
    for p in out:
        ap = os.path.abspath(p)
        if ap not in seen:
            seen.add(ap)
            deduped.append(p)
    return deduped


def _load_jsonl(path):
    records = []
    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  [skip] {path}:{i}: {e}", file=sys.stderr)
    return records


def _record_key(rec):
    """Identity for matching: (topology_path, integral)."""
    integrals = rec.get("integrals") or []
    if not integrals:
        return None
    topo = rec.get("topology_path") or rec.get("topology")
    return (topo, tuple(integrals[0]))


