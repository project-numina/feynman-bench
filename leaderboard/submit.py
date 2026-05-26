#!/usr/bin/env python3
"""Add a run to the leaderboard.

Each submission becomes its own directory under ``leaderboard/submissions/``:

    submissions/<solver>_<slug>_<utc-timestamp>/
        score.json          score summary + provenance (name, git rev, hardware)
        predictions.jsonl   raw solver output for every target

The score.json is read straight from your eval's output dir, and
predictions.jsonl is the concatenation of every per-topology ``results.jsonl``
under that same run dir. Together they let anyone re-score the submission
independently with :mod:`check_validity` without having to rerun the solver.

Examples
--------
  # Standard: point at the score.json from a run
  python3 leaderboard/submit.py \\
      --score results/20260522_140000_eval_fire/score.json \\
      --name "FIRE 7p baseline" \\
      --solver fire \\
      --notes "8 threads/target, 32 parallel"

  # Point at a run directory directly (score.json + */results.jsonl are
  # auto-discovered)
  python3 leaderboard/submit.py \\
      --run results/20260522_140000_eval_fire \\
      --name "FIRE 7p baseline" --solver fire

  # Just regenerate leaderboard.md and the README table from submissions/
  python3 leaderboard/submit.py --rerender
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LEADERBOARD_DIR = Path(__file__).resolve().parent
SUBMISSIONS_DIR = LEADERBOARD_DIR / "submissions"
LEADERBOARD_MD = LEADERBOARD_DIR / "leaderboard.md"
REPO_README = LEADERBOARD_DIR.parent / "README.md"

# Markers that delimit the leaderboard table inside README.md so submit.py can
# splice in updated content without disturbing the rest of the file.
README_START = "<!-- LEADERBOARD-START -->"
README_END = "<!-- LEADERBOARD-END -->"


# ── Helpers ────────────────────────────────────────────────────────────────

def _slugify(s):
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "unnamed"


def _git_rev():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=LEADERBOARD_DIR.parent,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _hardware_str():
    bits = [platform.machine(), platform.system()]
    n_cpu = os.cpu_count() or 0
    if n_cpu:
        bits.append(f"{n_cpu} cores")
    return " / ".join(b for b in bits if b)


def _resolve_run_dir(score_path, run_dir):
    """Return the eval run directory (containing score.json + per-topology
    subdirs with results.jsonl). One of the two args must be given.
    """
    if run_dir:
        d = Path(run_dir).resolve()
        if not d.is_dir():
            raise SystemExit(f"--run dir not found: {d}")
        return d
    if score_path:
        p = Path(score_path).resolve()
        if p.is_file() and p.name == "score.json":
            return p.parent
        if p.is_dir() and (p / "score.json").exists():
            return p
        raise SystemExit(f"--score path doesn't point at a score.json or run dir: {p}")
    raise SystemExit("Pass either --score or --run.")


def _gather_predictions(run_dir):
    """Concatenate every per-topology results.jsonl under ``run_dir``.

    Returns ``(raw_text, n_records)``. Empty files are skipped silently.
    """
    chunks = []
    n = 0
    for jl in sorted(run_dir.rglob("results.jsonl")):
        text = jl.read_text()
        if not text.endswith("\n"):
            text += "\n"
        chunks.append(text)
        n += sum(1 for line in text.splitlines() if line.strip())
    if not chunks:
        raise SystemExit(
            f"No results.jsonl files found under {run_dir}. Did the run "
            "complete?"
        )
    return "".join(chunks), n


# ── Submit ─────────────────────────────────────────────────────────────────

def submit(*, score_path=None, run_dir=None, name, solver,
           notes=None, hardware=None):
    run_dir = _resolve_run_dir(score_path, run_dir)
    score_file = run_dir / "score.json"
    if not score_file.exists():
        raise SystemExit(f"No score.json in run dir: {run_dir}")

    score_data = json.loads(score_file.read_text())
    if score_data.get("solver") != solver:
        print(f"  WARN: score.json solver={score_data.get('solver')!r} "
              f"but --solver={solver!r}; using --solver value.",
              file=sys.stderr)

    raw_text, n_predictions = _gather_predictions(run_dir)

    submission_score = {
        "name": name,
        "solver": solver,
        "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_rev": _git_rev(),
        "hardware": hardware or _hardware_str(),
        "notes": notes or "",
        "n_predictions": n_predictions,
        "macro_validity": score_data.get("macro_validity"),
        "mean_step_ratio": score_data.get("mean_step_ratio"),
        "n_topologies_covered": score_data.get("n_topologies_covered"),
        "totals": score_data.get("totals"),
        "per_topology": score_data.get("per_topology"),
    }

    SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slugify(name)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    target_dir = SUBMISSIONS_DIR / f"{solver}_{slug}_{ts}"
    target_dir.mkdir(parents=True, exist_ok=False)
    (target_dir / "score.json").write_text(
        json.dumps(submission_score, indent=2, default=float)
    )
    (target_dir / "predictions.jsonl").write_text(raw_text)
    rel = target_dir.relative_to(LEADERBOARD_DIR.parent)
    print(f"Submission written: {rel}/")
    print(f"  score.json:        {len(json.dumps(submission_score))} bytes")
    print(f"  predictions.jsonl: {n_predictions} records, {len(raw_text)} bytes")
    return target_dir


# ── Render ─────────────────────────────────────────────────────────────────

def _load_submissions():
    """Return a list of (dir, score_dict) for every well-formed submission."""
    SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)
    subs = []
    for d in sorted(SUBMISSIONS_DIR.iterdir()):
        if not d.is_dir():
            continue
        score_file = d / "score.json"
        if not score_file.exists():
            print(f"  WARN: skipping submission without score.json: {d}",
                  file=sys.stderr)
            continue
        try:
            subs.append((d, json.loads(score_file.read_text())))
        except json.JSONDecodeError as e:
            print(f"  WARN: skipping malformed score.json in {d}: {e}",
                  file=sys.stderr)
    return subs


def _render_table_rows(*, path_prefix):
    """Markdown table rows. ``path_prefix`` is the relative path from the
    document being written to ``leaderboard/submissions/``."""
    subs = _load_submissions()

    def _sort_key(item):
        _, s = item
        v = s.get("macro_validity")
        r = s.get("mean_step_ratio")
        return (
            -v if v is not None else float("inf"),
            r if r is not None else float("inf"),
        )
    subs.sort(key=_sort_key)

    if not subs:
        return ["_No submissions yet._"]

    lines = []
    lines.append("| # | Name | Solver | Validity | Mean step-ratio | Topologies | Submitted | Files | Notes |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for i, (d, s) in enumerate(subs, 1):
        v = s.get("macro_validity")
        r = s.get("mean_step_ratio")
        n_topo = s.get("n_topologies_covered")
        total_topo = len(s.get("per_topology") or {})
        v_str = "N/A" if v is None else f"{100*v:.2f}%"
        r_str = "N/A" if r is None else f"{r:.3f}"
        topo_str = f"{n_topo}/{total_topo}" if n_topo is not None else "?"
        submitted = (s.get("submitted_at") or "").split("T")[0]
        notes = (s.get("notes") or "").replace("|", "\\|")[:60]
        files = (f"[score]({path_prefix}{d.name}/score.json) · "
                 f"[raw]({path_prefix}{d.name}/predictions.jsonl)")
        lines.append(
            f"| {i} | {s.get('name','?')} | {s.get('solver','?')} "
            f"| {v_str} | {r_str} | {topo_str} | {submitted} | {files} | {notes} |"
        )
    return lines


def _splice_readme(table_lines):
    if not REPO_README.exists():
        return
    text = REPO_README.read_text()
    if README_START not in text or README_END not in text:
        return
    before, _, rest = text.partition(README_START)
    _, _, after = rest.partition(README_END)
    new = (
        before + README_START + "\n"
        + "\n".join(table_lines) + "\n"
        + README_END + after
    )
    if new != text:
        REPO_README.write_text(new)
        print(f"Updated: {REPO_README.relative_to(LEADERBOARD_DIR.parent)}")


def render():
    rows = _render_table_rows(path_prefix="submissions/")
    md = [
        "# Leaderboard",
        "",
        "Regenerated from `leaderboard/submissions/`.",
        "Submit your run with `python3 leaderboard/submit.py --score <path-to-score.json> --name <name> --solver <solver>`.",
        "",
        *rows,
        "",
        "Lower step-ratio is better; validity is fraction of covered integrals matching ground truth.",
        "Each submission ships both its score summary and the raw per-target predictions, so anyone can re-score it independently with `check_validity`.",
    ]
    LEADERBOARD_MD.write_text("\n".join(md) + "\n")
    print(f"Rendered: {LEADERBOARD_MD.relative_to(LEADERBOARD_DIR.parent)}")

    readme_rows = _render_table_rows(path_prefix="leaderboard/submissions/")
    _splice_readme(readme_rows)


# ── CLI ────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--score", help="Path to a score.json or to a run directory.")
    p.add_argument("--run", help="Path to a run directory (alternative to --score).")
    p.add_argument("--name", help='Human-readable submission name.')
    p.add_argument("--solver", help='Solver identifier, e.g. "fire" or "my_solver".')
    p.add_argument("--notes", default=None, help="Optional one-line notes.")
    p.add_argument("--hardware", default=None,
                   help="Override the auto-detected hardware string.")
    p.add_argument("--rerender", action="store_true",
                   help="Regenerate leaderboard.md + README table from submissions/, without adding a new one.")
    return p.parse_args()


def main():
    args = parse_args()
    if not args.rerender:
        if not (args.score or args.run):
            sys.exit("Pass --score or --run (or use --rerender).")
        if not (args.name and args.solver):
            sys.exit("--name and --solver are required when adding a submission.")
        submit(score_path=args.score, run_dir=args.run, name=args.name,
               solver=args.solver, notes=args.notes, hardware=args.hardware)
    render()


if __name__ == "__main__":
    main()
