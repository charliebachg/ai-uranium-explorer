"""The evaluation flow as a figure: the system under test on one band, what it is scored against on the other.

Writes docs/figures/evaluation-flow.svg. High level on purpose: the benchmark is built but its runs are
pending, so the figure shows the shape of the judgement, not results. Run: python3 docs/figures/make_evaluation_flow.py
"""

from __future__ import annotations

from pathlib import Path

W, H = 1400, 930
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
INK, INK2, LINE, PANEL, ACCENT, PEND = "#1c2430", "#4b5563", "#9aa4b2", "#f5f7fa", "#b7791f", "#6b7280"

parts: list[str] = []


def text(x: float, y: float, s: str, size: float = 11, weight: str = "normal", fill: str = INK, anchor: str = "start",
         italic: bool = False, spacing: str | None = None) -> None:
    style = f"font-family:{FONT};font-size:{size}px;font-weight:{weight};fill:{fill}"
    if italic:
        style += ";font-style:italic"
    if spacing:
        style += f";letter-spacing:{spacing}"
    parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" style="{style}">{esc(s)}</text>')


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def box(x: float, y: float, w: float, h: float, title: str, lines: list[str], *, accent: bool = False,
        pending: bool = False) -> None:
    stroke = ACCENT if accent else (PEND if pending else LINE)
    dash = ' stroke-dasharray="6 4"' if pending else ""
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" ry="8" fill="{PANEL}" stroke="{stroke}" '
                 f'stroke-width="{1.6 if accent else 1.2}"{dash}/>')
    text(x + 14, y + 22, title, size=12.5, weight="600", fill=ACCENT if accent else INK)
    for i, line in enumerate(lines):
        text(x + 14, y + 42 + i * 15, line, size=11, fill=INK2)


def arrow(x1: float, y1: float, x2: float, y2: float, *, dashed: bool = False, label: str | None = None) -> None:
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{INK2}" stroke-width="1.4" '
                 f'marker-end="url(#head)"{dash}/>')
    if label:
        text((x1 + x2) / 2, (y1 + y2) / 2 - 6, label, size=10, fill=INK2, anchor="middle", italic=True)


def elbow(points: list[tuple[float, float]], *, dashed: bool = False, label: str | None = None,
          label_at: tuple[float, float] | None = None) -> None:
    d = "M " + " L ".join(f"{x} {y}" for x, y in points)
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    parts.append(f'<path d="{d}" fill="none" stroke="{INK2}" stroke-width="1.4" marker-end="url(#head)"{dash}/>')
    if label and label_at:
        text(label_at[0], label_at[1], label, size=10, fill=INK2, anchor="middle", italic=True)


def band(y: float, h: float, label: str) -> None:
    parts.append(f'<rect x="28" y="{y}" width="{W - 56}" height="{h}" rx="12" ry="12" fill="none" stroke="{LINE}" '
                 f'stroke-width="0.8" stroke-dasharray="2 4"/>')
    text(44, y - 8, label.upper(), size=10.5, weight="600", fill=INK2, spacing="0.08em")


# ------------------------------------------------------------------ header
text(40, 40, "How the reasoning model's performance will be understood", size=18, weight="700")
text(40, 62, "The system under test on the upper band; what its answers are scored against on the lower one. "
     "Built; the benchmark runs come after the acceptance story.", size=11.5, fill=INK2)

# ------------------------------------------------------------------ band A: the system under test
band(96, 210, "the system under test")
box(44, 122, 210, 150, "Public data", [
    "provincial GIS layers, scanned",
    "assessment files, imagery",
    "",
    "a tiered store: native, read,",
    "derived, agent, expert; every",
    "value with its provenance",
])
box(290, 122, 250, 150, "Evidence pack, one cell", [
    "features with observation counts",
    "criteria memberships and coverage",
    "out-of-fold scores for its fold",
    "passages, own files blind-listed",
    "unknown kept distinct from absent",
])
box(576, 122, 300, 150, "The agent", [
    "v0: one closed-book call",
    "v1: plan, execute per criterion,",
    "node gate, verify with repair rounds,",
    "two deciders, publish",
    "",
    "every switch is an arm file",
])
box(912, 122, 210, 150, "Fabrication gate", [
    "every number must resolve to",
    "a stored value id returned",
    "in this session",
    "",
    "else the answer is withheld,",
    "objection in its place",
], accent=True)
box(1158, 122, 198, 150, "Chain or answer", [
    "verdict and probability",
    "cited value ids",
    "unknown versus absent",
    "or an abstention with",
    "its reason",
])
arrow(254, 197, 288, 197)
arrow(540, 197, 574, 197)
arrow(876, 197, 910, 197)
arrow(1122, 197, 1156, 197)

# ------------------------------------------------------------------ band B: what it is scored against
band(352, 500, "what it is scored against")
box(44, 378, 250, 150, "Frozen benchmark", [
    "stratified cells: deposits thinned",
    "to one per block, drilled",
    "occurrences, drilled negatives,",
    "never-drilled probes",
    "labels masked, held-out split,",
    "one hashed manifest",
])
box(330, 378, 250, 150, "Baselines, the same cells", [
    "random",
    "criteria score",
    "learned model, spatial folds",
    "effort null: drilling history alone",
    "",
    "match the null: read history,",
    "not geology",
])
box(616, 378, 300, 150, "Scoring, with denominators", [
    "PR-AUC, F1, calibration with",
    "bootstrap intervals",
    "abstention rate, gate-rejection rate",
    "cost per chain on the same row",
    "an abstention is never a positive;",
    "withheld counts against recall",
], pending=True)
box(952, 378, 404, 150, "Per-stage metrics from the traces", [
    "node-gate refusals per attempt",
    "verifier catch rate and rounds to valid",
    "agreement between the two deciders,",
    "and with the verifier's own label",
    "",
    "where a stack spends its capacity, not only whether it won",
], pending=True)

box(44, 574, 300, 150, "Ablation arms, one file each", [
    "single call against the staged loop",
    "template against model planner",
    "verifier off; rounds K",
    "cheap executor under a strong verifier",
    "segment-scoped views, batch executor",
    "same frozen cells, matched budgets",
], pending=True)
box(380, 574, 270, 150, "Gate suite, no model", [
    "honest and deliberately corrupted",
    "claims over real evidence packs",
    "",
    "a measured refusal rate",
    "and its known holes",
])
box(686, 574, 270, 150, "Interface tiers", [
    "deterministic: exact gold computed",
    "by code, refusals included",
    "adversarial: grown only from",
    "failures actually observed",
    "",
    "built, not yet run",
], pending=True)
box(44, 770, 1312, 60, "Eval page", [
    "every table with its run id and the store snapshot it read; no human rater in the prototype, so the mechanical "
    "tiers, the gate suite and second-reader agreement stand in, each with its denominator",
])

# ------------------------------------------------------------------ the flows between the bands
elbow([(260, 378), (260, 300), (415, 300), (415, 274)])
text(272, 316, "packs are built from the frozen cells", size=10, fill=INK2, italic=True)
elbow([(1257, 274), (1257, 310), (766, 310), (766, 376)], label="chains and answers, scored", label_at=(1100, 304))
elbow([(860, 274), (860, 336), (1154, 336), (1154, 376)], dashed=True, label="spans of every call", label_at=(1060, 330))
arrow(582, 453, 614, 453)
elbow([(194, 574), (194, 546), (726, 546), (726, 530)], dashed=True, label="each arm runs the loop with one switch changed",
      label_at=(460, 540))
elbow([(515, 574), (515, 556), (934, 556), (934, 274)], dashed=True)
text(944, 560, "the gate's own claim check", size=10, fill=INK2, italic=True)
elbow([(766, 528), (766, 554), (668, 554), (668, 768)])
arrow(1154, 530, 1154, 768)
arrow(515, 724, 515, 768)
arrow(821, 724, 821, 768)

# ------------------------------------------------------------------ legend
parts.append(f'<rect x="44" y="868" width="26" height="14" rx="4" fill="{PANEL}" stroke="{PEND}" stroke-width="1.2" stroke-dasharray="6 4"/>')
text(78, 879, "dashed box: built, runs pending until the benchmark is finished", size=10.5, fill=INK2)
parts.append(f'<rect x="470" y="868" width="26" height="14" rx="4" fill="{PANEL}" stroke="{ACCENT}" stroke-width="1.6"/>')
text(504, 879, "the boundary a number cannot cross without a stored value behind it", size=10.5, fill=INK2)
parts.append(f'<line x1="900" y1="875" x2="940" y2="875" stroke="{INK2}" stroke-width="1.4" stroke-dasharray="5 4" marker-end="url(#head)"/>')
text(950, 879, "dashed arrow: traces and switches, not data", size=10.5, fill=INK2)

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<defs>
  <marker id="head" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth">
    <path d="M0,0 L8,4 L0,8 z" fill="{INK2}"/>
  </marker>
</defs>
<rect width="{W}" height="{H}" fill="#ffffff"/>
{chr(10).join(parts)}
</svg>
'''
out = Path(__file__).with_name("evaluation-flow.svg")
out.write_text(svg)
print(f"wrote {out} ({len(svg):,} bytes)")
