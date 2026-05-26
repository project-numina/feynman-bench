"""Shared helpers for working with solver-produced reduction dicts.

A "reduction" is the dict that solvers emit per integral, mapping a serialized
master-integral key (a JSON-encoded list, e.g. ``"[1,1,0,0,0,0,0]"``) to a
coefficient. Empty dict means "reduces to 0".

These helpers are used by both benchmark.py (multi-solver pairwise agreement)
and score.py (single-solver vs ground-truth comparison).
"""

import json


def reductions_agree(r1, r2):
    """True iff two reductions are identical (missing master keys treated as 0).

    Returns None when either side is None — the comparison is undefined, not
    "disagree". Callers can distinguish skipped/missing from a real
    disagreement.
    """
    if r1 is None or r2 is None:
        return None
    all_keys = set(r1.keys()) | set(r2.keys())
    return all(r1.get(k, 0) == r2.get(k, 0) for k in all_keys)


def extract_reduction(rec):
    """Pull the reduction dict for a single-integral result record.

    Each benchmark.py results.jsonl record carries one target integral in
    ``integrals[0]`` and the corresponding reduction under ``reductions[str(list(integral))]``.
    """
    if rec is None:
        return None
    reductions = rec.get("reductions") or {}
    integrals = rec.get("integrals") or []
    if not integrals:
        return None
    key = str(list(integrals[0]))
    return reductions.get(key)


def format_expr(r):
    """Render a reduction dict as a short markdown expression."""
    if r is None:
        return "*no result*"
    if len(r) == 0:
        return "`0`"
    terms = [f"{coeff}\\*G\\_{master}"
            for master, coeff in sorted(r.items(), key=lambda kv: json.loads(kv[0]))]
    return " + ".join(terms)
