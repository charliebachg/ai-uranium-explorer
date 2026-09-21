"""The evaluation flow as a figure: what runs, and how it is judged. Icons and short labels only.

Writes docs/figures/evaluation-flow.svg. Run: python3 docs/figures/make_evaluation_flow.py
"""

from __future__ import annotations

from pathlib import Path

W, H = 1400, 560
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
INK, INK2, LINE, FILL, ACCENT, PEND = "#1c2430", "#5b6470", "#9aa4b2", "#f1f4f8", "#b7791f", "#8a93a0"
R = 36  # icon circle radius

parts: list[str] = []


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(x: float, y: float, s: str, size: float = 12, weight: str = "normal", fill: str = INK,
         anchor: str = "middle", spacing: str | None = None) -> None:
    style = f"font-family:{FONT};font-size:{size}px;font-weight:{weight};fill:{fill}"
    if spacing:
        style += f";letter-spacing:{spacing}"
    parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" style="{style}">{esc(s)}</text>')


# ---- icons: 24 x 24 stroke drawings, placed by their centre
ICONS = {
    "database": '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    "pack": '<path d="M6 2h9l5 5v15H6z"/><path d="M15 2v5h5"/><path d="M9 13h7M9 17h7"/>',
    "agent": '<circle cx="6" cy="6" r="2.6"/><circle cx="18" cy="6" r="2.6"/><circle cx="12" cy="18" r="2.6"/><path d="M8.3 7.3l2.6 8M15.7 7.3l-2.6 8M8.6 6h6.8"/>',
    "gate": '<path d="M12 2l8 3v6c0 5-3.4 8.6-8 11-4.6-2.4-8-6-8-11V5z"/><path d="M8.5 12.2l2.3 2.3 4.7-4.8"/>',
    "answer": '<path d="M4 4h16v11H9l-5 4z"/><path d="M8.5 9.6l2.2 2.2 4.8-4.8"/>',
    "benchmark": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18M15 3v18M3 9h18M3 15h18"/><rect x="9.6" y="9.6" width="4.8" height="4.8" fill="currentColor" stroke="none"/>',
    "baselines": '<path d="M12 3v18M4 21h16M12 6l-6 8h12z"/><path d="M6 14a3 3 0 0 0 6 0M12 14a3 3 0 0 0 6 0"/>',
    "scores": '<path d="M3 21h18"/><rect x="5" y="11" width="3.5" height="8"/><rect x="10.3" y="6" width="3.5" height="13"/><rect x="15.6" y="9" width="3.5" height="10"/>',
    "ablation": '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="2.6" fill="white"/><circle cx="15" cy="17" r="2.6" fill="white"/>',
    "eval": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M9 9v11"/><path d="M12 13h6M12 16.5h4"/>',
}


def node(cx: float, cy: float, icon: str, title: str, sub: str, *, accent: bool = False, pending: bool = False) -> None:
    stroke = ACCENT if accent else (PEND if pending else LINE)
    dash = ' stroke-dasharray="6 4"' if pending else ""
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="{R}" fill="{FILL}" stroke="{stroke}" stroke-width="{1.8 if accent else 1.3}"{dash}/>')
    colour = ACCENT if accent else INK
    parts.append(f'<g transform="translate({cx - 15},{cy - 15}) scale(1.25)" fill="none" stroke="{colour}" stroke-width="1.7" '
                 f'stroke-linecap="round" stroke-linejoin="round" style="color:{colour}">{ICONS[icon]}</g>')
    text(cx, cy + R + 24, title, size=13.5, weight="600", fill=colour)
    text(cx, cy + R + 42, sub, size=11.5, fill=INK2)


def arrow(x1: float, y: float, x2: float) -> None:
    parts.append(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{INK2}" stroke-width="1.5" marker-end="url(#head)"/>')


def row_label(y: float, s: str) -> None:
    text(40, y, s.upper(), size=10.5, weight="600", fill=INK2, anchor="start", spacing="0.1em")


X = [180, 440, 700, 960, 1220]
Y1, Y2 = 110, 360

row_label(48, "what runs")
node(X[0], Y1, "database", "Public data", "layers, files, imagery")
node(X[1], Y1, "pack", "Evidence pack", "one cell, labels hidden")
node(X[2], Y1, "agent", "Agent", "one call, or the staged loop")
node(X[3], Y1, "gate", "Fabrication gate", "no stored value, no number", accent=True)
node(X[4], Y1, "answer", "Answer", "verdict, cited values, or abstain")
for a, b in zip(X, X[1:]):
    arrow(a + R + 10, Y1, b - R - 12)

row_label(298, "how it is judged")
node(X[0], Y2, "benchmark", "Frozen benchmark", "stratified cells, one hashed manifest")
node(X[1], Y2, "baselines", "Baselines", "random, criteria, learned, effort null")
node(X[2], Y2, "scores", "Scores", "PR-AUC, F1, abstain rate, cost", pending=False)
node(X[3], Y2, "ablation", "Ablations", "one switch per arm", pending=True)
node(X[4], Y2, "eval", "Eval page", "every number with its run")
for a, b in zip(X, X[1:]):
    arrow(a + R + 10, Y2, b - R - 12)

# every answer is scored: from the answer down and across to the scores
parts.append(f'<path d="M {X[4]} {Y1 + R + 52} L {X[4]} 250 L {X[2]} 250 L {X[2]} {Y2 - R - 12}" fill="none" '
             f'stroke="{INK2}" stroke-width="1.5" stroke-dasharray="5 4" marker-end="url(#head)"/>')
text((X[2] + X[4]) / 2, 243, "every answer, scored against the same cells", size=11, fill=INK2)

# legend
parts.append(f'<circle cx="52" cy="512" r="7" fill="{FILL}" stroke="{PEND}" stroke-width="1.3" stroke-dasharray="4 3"/>')
text(66, 516, "dashed: scored for some arms; the ablation matrix is not complete", size=11, fill=INK2, anchor="start")
parts.append(f'<circle cx="262" cy="512" r="7" fill="{FILL}" stroke="{ACCENT}" stroke-width="1.8"/>')
text(276, 516, "amber: the line no number crosses without a stored value behind it", size=11, fill=INK2, anchor="start")

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
