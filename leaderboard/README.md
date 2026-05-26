# Leaderboard

The `leaderboard.md` table is regenerated from JSON submissions in `submissions/`.

## Submit your result

After running `./run_eval.py` (which writes a `score.json` under `results/<ts>_eval_<solver>/`),
add it to the leaderboard:

```bash
python3 leaderboard/submit.py \
    --score results/20260522_140000_eval_fire/score.json \
    --name "FIRE 7p baseline" \
    --solver fire \
    --notes "8 threads/target, 32 parallel, single-node"
```

This writes a new directory under `submissions/<solver>_<slug>_<utc-timestamp>/`
containing two files:

- `score.json` — score summary (validity %, step ratio, per-topology breakdown)
  plus provenance (`submitted_at`, `git_rev`, `hardware`, your `notes`).
- `predictions.jsonl` — the concatenated per-topology `results.jsonl` from your
  run, one record per target integral. Shipping the raw output makes the
  submission independently verifiable: anyone can re-score it with
  `python3 score.py --predictions <path> --ground-truth ground_truth.jsonl
  --solver <name>` (or call `check_validity` directly) without rerunning the
  solver.

To send your submission upstream, open a PR with the new directory under
`submissions/`.

## Re-render only

```bash
python3 leaderboard/submit.py --rerender
```

Useful after editing or removing submission directories manually. Updates
both `leaderboard.md` and the table embedded at the top of the project README.

## Submission `score.json` schema

```json
{
  "name": "FIRE 7p baseline",
  "solver": "fire",
  "submitted_at": "2026-05-22T14:00:00Z",
  "git_rev": "abc1234",
  "hardware": "x86_64 / Linux / 384 cores",
  "notes": "...",
  "n_predictions": 2454,
  "macro_validity": 1.0,
  "mean_step_ratio": 1.0,
  "n_topologies_covered": 19,
  "totals": {"n_gt": 2454, "n_covered": 2454, "n_valid": 2454, ...},
  "per_topology": {"3D/bub2l": {...}, ...}
}
```

## `predictions.jsonl` schema

One JSON record per line, matching the schema in `ground_truth.jsonl`:

```json
{"solver": "fire", "topology_path": "4D/box1lc", "params": {...},
 "integrals": [[-1, 2, 1, 0]],
 "reductions": {"[-1, 2, 1, 0]": {"[0, 1, 0, 0]": 1826, ...}},
 "num_steps": 43, "finite_field": 2017}
```
