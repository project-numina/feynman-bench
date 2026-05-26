#!/usr/bin/env python3
"""Render a banner of representative Feynman topologies for the README.

Each topology is hand-encoded — propagators are not generated from the
``generate_setup.m`` files but laid out by hand so the diagrams stay readable
at thumbnail size. One per loop dimension, picked to cover the range of
complexity in the dataset:

    2D/bub        — 1-loop massive bubble (2 props)
    4D/box1l      — 1-loop box (4 props, 4 ext)
    6D/vac3lBN    — 3-loop vacuum, basket (6 props, tetrahedron)
    7D/tri2l      — 2-loop triangle (7 props, 3 ext)
    9D/p3lBenz    — 3-loop planar benzene (9 props, 1 ext)
    15D/gravity3l — 3-loop gravity, double-box-style (15 props, 2 ext)

Outputs:
    docs/topologies.png   — composite banner (PNG, ~1500 px wide)
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "topologies.png"


# ── Topology encodings ─────────────────────────────────────────────────────
# Each topology dict has:
#   vertices  : {id: (x, y)} positions in a (-1, 1) x (-1, 1) box
#   internal  : list of (a, b, rad) tuples — edge from vertex a to b, with
#               connection rad (matplotlib FancyArrow connectionstyle arc3,rad=).
#               rad>0 curves left, rad<0 curves right (for parallel edges).
#   externals : list of (anchor_vertex, dx, dy, label) — external leg drawn from
#               vertex toward (vx+dx, vy+dy), arrow tail at the vertex.
#               label "" suppresses the momentum label.
#   loops, props, ext : str, displayed under the diagram for context.

TOPOLOGIES = {
    "2D/bub": {
        "title": "2D/bub",
        "subtitle": "1 loop · 2 props",
        "vertices": {0: (-0.5, 0), 1: (0.5, 0)},
        "internal": [(0, 1, 0.35), (0, 1, -0.35)],
        "externals": [(0, -0.4, 0, "q"), (1, 0.4, 0, "q")],
    },
    "4D/box1l": {
        "title": "4D/box1l",
        "subtitle": "1 loop · 4 props",
        "vertices": {
            0: (-0.4, 0.4),
            1: (0.4, 0.4),
            2: (0.4, -0.4),
            3: (-0.4, -0.4),
        },
        "internal": [(0, 1, 0), (1, 2, 0), (2, 3, 0), (3, 0, 0)],
        "externals": [
            (0, -0.3, 0.3, "p₁"),
            (1, 0.3, 0.3, "p₂"),
            (2, 0.3, -0.3, "p₃"),
            (3, -0.3, -0.3, "p₄"),
        ],
    },
    "6D/vac3lBN": {
        # tetrahedron projection: 4 vertices, 6 edges, no externals
        "title": "6D/vac3lBN",
        "subtitle": "3 loops · 6 props (vac)",
        "vertices": {
            0: (0.0, 0.6),
            1: (-0.6, -0.35),
            2: (0.6, -0.35),
            3: (0.0, 0.0),  # interior vertex
        },
        "internal": [
            (0, 1, 0),
            (1, 2, 0),
            (2, 0, 0),
            (0, 3, 0),
            (1, 3, 0),
            (2, 3, 0),
        ],
        "externals": [],
    },
    "7D/tri2l": {
        # 2-loop triangle: outer triangle + an internal edge spanning one side
        "title": "7D/tri2l",
        "subtitle": "2 loops · 7 props",
        "vertices": {
            0: (0.0, 0.55),
            1: (-0.55, -0.35),
            2: (0.55, -0.35),
            3: (-0.28, 0.08),
            4: (0.28, 0.08),
        },
        "internal": [
            (0, 3, 0),
            (0, 4, 0),
            (1, 3, 0),
            (2, 4, 0),
            (3, 4, 0),
            (1, 2, 0),
            # second edge between 3,4 to bring count to 7 (sub-bubble)
            (3, 4, 0.5),
        ],
        "externals": [
            (0, 0, 0.35, "p₁"),
            (1, -0.3, -0.2, "p₂"),
            (2, 0.3, -0.2, "p₃"),
        ],
    },
    "9D/p3lBenz": {
        # hexagon (6 outer edges) with 3 internal edges (3 "spokes" pattern)
        "title": "9D/p3lBenz",
        "subtitle": "3 loops · 9 props",
        "vertices": {
            0: (0.0, 0.55),    # top
            1: (0.48, 0.27),   # upper-right
            2: (0.48, -0.27),  # lower-right
            3: (0.0, -0.55),   # bottom
            4: (-0.48, -0.27), # lower-left
            5: (-0.48, 0.27),  # upper-left
        },
        "internal": [
            (0, 1, 0),
            (1, 2, 0),
            (2, 3, 0),
            (3, 4, 0),
            (4, 5, 0),
            (5, 0, 0),
            # three diameters
            (0, 3, 0),
            (1, 4, 0),
            (2, 5, 0),
        ],
        "externals": [
            (0, 0, 0.3, "Q"),
            (3, 0, -0.3, "Q"),
        ],
    },
    "15D/gravity3l": {
        # 3-loop double-box-style; many ISPs in the actual integral basis but
        # the underlying graph is a 3-loop double box
        "title": "15D/gravity3l",
        "subtitle": "3 loops · 15 props (w/ ISPs)",
        "vertices": {
            0: (-0.65, 0.35),
            1: (-0.22, 0.35),
            2: (0.22, 0.35),
            3: (0.65, 0.35),
            4: (0.65, -0.35),
            5: (0.22, -0.35),
            6: (-0.22, -0.35),
            7: (-0.65, -0.35),
        },
        "internal": [
            (0, 1, 0),
            (1, 2, 0),
            (2, 3, 0),
            (3, 4, 0),
            (4, 5, 0),
            (5, 6, 0),
            (6, 7, 0),
            (7, 0, 0),
            (1, 6, 0),
            (2, 5, 0),
        ],
        "externals": [
            (0, -0.3, 0.2, "p₁"),
            (3, 0.3, 0.2, "p₂"),
            (4, 0.3, -0.2, ""),
            (7, -0.3, -0.2, ""),
        ],
    },
}


# ── Drawing primitives ────────────────────────────────────────────────────

def _draw_edge(ax, p0, p1, rad=0.0):
    arrow = FancyArrowPatch(
        p0, p1, connectionstyle=f"arc3,rad={rad}",
        arrowstyle="-", color="black", linewidth=1.2,
    )
    ax.add_patch(arrow)


def _draw_external(ax, anchor, dx, dy, label):
    x0, y0 = anchor
    x1, y1 = x0 + dx, y0 + dy
    # External lines drawn from the vertex outward, with arrow tip on the
    # outer side and the label just past the tip.
    arrow = FancyArrowPatch(
        (x0, y0), (x1, y1),
        arrowstyle="->", mutation_scale=10,
        color="black", linewidth=1.0,
    )
    ax.add_patch(arrow)
    if label:
        # Place label a little beyond the arrow tip
        norm = max((dx ** 2 + dy ** 2) ** 0.5, 1e-9)
        lx = x1 + 0.10 * dx / norm
        ly = y1 + 0.10 * dy / norm
        ax.text(lx, ly, label, fontsize=10, ha="center", va="center", style="italic")


def _draw_vertex(ax, p):
    ax.scatter([p[0]], [p[1]], s=14, color="black", zorder=10)


def _draw_topology(ax, topo):
    vs = topo["vertices"]
    for a, b, rad in topo["internal"]:
        _draw_edge(ax, vs[a], vs[b], rad=rad)
    for v in vs.values():
        _draw_vertex(ax, v)
    for anchor_id, dx, dy, label in topo["externals"]:
        _draw_external(ax, vs[anchor_id], dx, dy, label)
    ax.set_xlim(-1.2, 1.2)
    ax.set_ylim(-1.2, 1.2)
    ax.set_aspect("equal")
    ax.axis("off")


# ── Composition ───────────────────────────────────────────────────────────

def render_banner():
    n = len(TOPOLOGIES)
    # Single row banner. Wider panels + a two-line title (name on top,
    # loops/props on a second smaller line) keep adjacent titles from
    # colliding.
    fig, axes = plt.subplots(
        1, n, figsize=(2.6 * n, 2.6), dpi=200,
        gridspec_kw={"wspace": 0.15},
    )
    if n == 1:
        axes = [axes]
    for ax, (name, topo) in zip(axes, TOPOLOGIES.items()):
        _draw_topology(ax, topo)
        # Stack the title in two lines so subtitle won't push into neighbour
        ax.set_title(f"{topo['title']}\n{topo['subtitle']}",
                     fontsize=10, pad=6, linespacing=1.3,
                     fontdict={"family": "monospace"})
    fig.tight_layout(pad=0.6)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=200, bbox_inches="tight", facecolor="white")
    print(f"wrote {OUT.relative_to(ROOT)}")
    plt.close(fig)


if __name__ == "__main__":
    render_banner()
