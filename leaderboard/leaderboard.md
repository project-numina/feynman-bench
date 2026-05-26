# Leaderboard

Regenerated from `leaderboard/submissions/`.
Submit your run with `python3 leaderboard/submit.py --score <path-to-score.json> --name <name> --solver <solver>`.

| # | Name | Solver | Validity | Mean step-ratio | Topologies | Submitted | Files | Notes |
|---|---|---|---|---|---|---|---|---|
| 1 | FIRE 7p baseline | fire | 100.00% | 1.000 | 9/9 | 2026-05-22 | [score](submissions/fire_fire-7p-baseline_20260522_164409/score.json) · [raw](submissions/fire_fire-7p-baseline_20260522_164409/predictions.jsonl) | Reference baseline: FIRE scored on its own GT (test split).  |
| 2 | FIRE 6p | fire | 100.00% | 1.068 | 9/9 | 2026-05-25 | [score](submissions/fire_fire-6p_20260525_164523/score.json) · [raw](submissions/fire_fire-6p_20260525_164523/predictions.jsonl) | FIRE 6p baseline |

Lower step-ratio is better; validity is fraction of covered integrals matching ground truth.
Each submission ships both its score summary and the raw per-target predictions, so anyone can re-score it independently with `check_validity`.
