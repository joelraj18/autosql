"""The business brief: a one-page infographic of the analysis, as a self-contained HTML file.

Every number in it comes from ``metrics`` (what the generated SQL returned) through ``business.executive_summary``.
Charts are inline SVG drawn to one scale each, coloured through CSS tokens so that light and dark themes both work,
with a hover / focus tooltip on every mark and a table view under every chart. Nothing is loaded from the network,
so the file works offline and can be attached or published as it is.

Chart rules (data-viz reference): one y-axis per chart (the suspected driver gets its own small chart under the
symptom, never a second scale), thin marks with 4 px rounded data-ends, hairline solid gridlines, selective direct
labels, a single series in slot-1 blue, increases / decreases in a blue / red diverging pair, totals in neutral ink,
partial periods de-emphasised in grey.
"""
from __future__ import annotations

import html
import math

from .business import KPI_BY_KEY, TRUST_WEIGHTS, Summary, effect_shares, executive_summary
from .codegen import Builder, assumptions
from .decision import Decision

W = 400          # SVG user units: charts sit in half-width cards (desktop) or full width (phone), so text scales ~0.85-1.15x


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def compact(v: float, money: bool = False) -> str:
    a = abs(v)
    if a >= 1e9:
        s = f"{v / 1e9:.1f}B"
    elif a >= 1e6:
        s = f"{v / 1e6:.1f}M"
    elif a >= 1e3:
        s = f"{v / 1e3:.0f}K" if a >= 1e4 else f"{v / 1e3:.1f}K"
    else:
        s = f"{v:,.0f}" if money or a >= 100 else f"{v:,.1f}"
    return s.replace(".0K", "K").replace(".0M", "M").replace(".0B", "B")


def full(v: float, unit: str = "money") -> str:
    if unit == "money":
        return f"{v:,.2f}"
    if unit == "pct":
        return f"{v:.1f}%"
    if unit == "days":
        return f"{v:.1f} days"
    if unit == "score":
        return f"{v:.2f}"
    return f"{v:,.0f}"


def nice_ticks(top: float, bottom: float = 0.0, n: int = 4) -> list[float]:
    span = top - bottom
    if span <= 0:
        return [bottom, bottom + 1]
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(bottom / step) * step
    ticks, t = [], start
    while t < top + step * 0.999:
        ticks.append(round(t, 10))
        t += step
    return ticks


def column(x: float, y_base: float, y_top: float, w: float, r: float = 4) -> str:
    """A column with a rounded data-end and a square baseline (grows up or down from the baseline)."""
    h = abs(y_base - y_top)
    r = min(r, w / 2, h)
    if y_top <= y_base:   # positive: rounded at the top
        return (f"M{x:.1f},{y_base:.1f}V{y_top + r:.1f}Q{x:.1f},{y_top:.1f} {x + r:.1f},{y_top:.1f}"
                f"H{x + w - r:.1f}Q{x + w:.1f},{y_top:.1f} {x + w:.1f},{y_top + r:.1f}V{y_base:.1f}Z")
    return (f"M{x:.1f},{y_base:.1f}V{y_top - r:.1f}Q{x:.1f},{y_top:.1f} {x + r:.1f},{y_top:.1f}"
            f"H{x + w - r:.1f}Q{x + w:.1f},{y_top:.1f} {x + w:.1f},{y_top - r:.1f}V{y_base:.1f}Z")


def hbar(x_base: float, x_end: float, y: float, h: float, r: float = 4) -> str:
    """A horizontal bar with a rounded data-end (right for positive, left for negative)."""
    w = abs(x_end - x_base)
    r = min(r, h / 2, w)
    if x_end >= x_base:
        return (f"M{x_base:.1f},{y:.1f}H{x_end - r:.1f}Q{x_end:.1f},{y:.1f} {x_end:.1f},{y + r:.1f}"
                f"V{y + h - r:.1f}Q{x_end:.1f},{y + h:.1f} {x_end - r:.1f},{y + h:.1f}H{x_base:.1f}Z")
    return (f"M{x_base:.1f},{y:.1f}H{x_end + r:.1f}Q{x_end:.1f},{y:.1f} {x_end:.1f},{y + r:.1f}"
            f"V{y + h - r:.1f}Q{x_end:.1f},{y + h:.1f} {x_end + r:.1f},{y + h:.1f}H{x_base:.1f}Z")


def svg(height: float, label: str, body: str) -> str:
    return (f'<svg viewBox="0 0 {W} {height:.0f}" role="img" aria-label="{esc(label)}" preserveAspectRatio="xMinYMin meet">'
            f"{body}</svg>")


def table(headers: list[str], rows: list[list[str]], caption: str = "Table view") -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return (f'<details class="tv"><summary>{esc(caption)}</summary><div class="scroll"><table><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div></details>")


# ── Charts ───────────────────────────────────────────────────────────────
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def period_parts(label: str) -> tuple[str, str]:
    """'1996-Q3' -> ('Q3', '1996'), '1996-07' -> ('Jul', '1996'), '2022-W07' -> ('W07', '2022'), '1996' -> ('1996', '')."""
    if "-Q" in label:
        year, q = label.split("-Q")
        return f"Q{q}", year
    if "-W" in label:
        year, w = label.split("-W")
        return f"W{w}", year
    if len(label) == 7 and label[4] == "-":
        return MONTHS[int(label[5:]) - 1], label[:4]
    return label, ""


def _ygrid(ticks: list[float], y, left: float, money: bool = False, fmt=None) -> str:
    fmt = fmt or (lambda t: compact(t, money))
    return "".join(f'<line class="grid" x1="{left}" x2="{W - 8}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                   f'<text class="tick" x="{left - 6}" y="{y(t) + 4:.1f}" text-anchor="end">{esc(fmt(t))}</text>' for t in ticks)


def _xlabel(x: float, y: float, text: str, cls: str = "tick") -> str:
    lines = text.split("\n")
    spans = "".join(f'<tspan x="{x:.1f}" dy="{0 if i == 0 else 13}">{esc(line)}</tspan>' for i, line in enumerate(lines))
    return f'<text class="{cls}" x="{x:.1f}" y="{y:.1f}" text-anchor="middle">{spans}</text>'


def _period_axis(rows: list[dict], xs: list[float], band: float, y: float) -> str:
    """Two-line period labels: the short label on every k-th period, the year under the first period of each year."""
    every = max(1, math.ceil(30 / band))
    out, last_year = [], None
    for i, (r, x) in enumerate(zip(rows, xs)):
        short, year = period_parts(r["period"])
        show_year = year and year != last_year
        if i % every == 0 or show_year:
            out.append(_xlabel(x, y, short + (f"\n{year}" if show_year else "")))
        last_year = year
    return "".join(out)


def _partial(r: dict) -> bool:
    return r.get("period_status", "complete") != "complete"


def period_columns(rows: list[dict], key: str, unit: str, title: str) -> str:
    """Columns per period; partial periods de-emphasised; the last complete period labelled."""
    n = len(rows)
    left, top, bottom = 44, 18, 36
    h = 220
    plot_w, plot_h = W - left - 8, h - top - bottom
    values = [float(r.get(key) or 0) for r in rows]
    ticks = nice_ticks(max(values + [0]) * 1.08)
    ymax = ticks[-1] or 1
    band = plot_w / max(n, 1)
    bw = min(24, band * 0.62)
    y = lambda v: top + plot_h * (1 - v / ymax)  # noqa: E731
    xs = [left + band * i + band / 2 for i in range(n)]
    parts = [_ygrid(ticks, y, left, unit == "money")]
    complete = [i for i, r in enumerate(rows) if not _partial(r)]
    label_at = complete[-1] if complete else n - 1
    for i, (r, v, cx) in enumerate(zip(rows, values, xs)):
        partial = _partial(r)
        tip = f"{r['period']}: {full(v, unit)}" + (" (partial period: excluded from comparisons)" if partial else "")
        parts.append(f'<path class="{"m-muted" if partial else "m-series"}" d="{column(cx - bw / 2, y(0), y(v), bw)}" tabindex="0" '
                     f'data-tip="{esc(tip)}"/>')
        if i == label_at:
            parts.append(f'<text class="val" x="{cx:.1f}" y="{y(v) - 6:.1f}" text-anchor="middle">{esc(compact(v, unit == "money"))}</text>')
        elif partial:
            parts.append(f'<text class="note" x="{cx:.1f}" y="{y(v) - 6:.1f}" text-anchor="middle">partial</text>')
    parts.append(f'<line class="axis" x1="{left}" x2="{W - 8}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>')
    parts.append(_period_axis(rows, xs, band, h - bottom + 15))
    return svg(h, title, "".join(parts))


def period_line(rows: list[dict], key: str, unit: str, title: str) -> str:
    """The suspected driver on the same periods as period_columns, on its own axis (never a second scale on one plot)."""
    pts = [(i, float(r[key])) for i, r in enumerate(rows) if r.get(key) is not None]
    if len(pts) < 2:
        return ""
    n = len(rows)
    left, top, bottom = 44, 18, 36
    h = 220
    plot_w, plot_h = W - left - 8, h - top - bottom
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    pad = (hi - lo) * 0.25 or max(abs(hi) * 0.1, 1)
    ticks = nice_ticks(hi + pad, max(0.0, lo - pad), 4)
    ymin, ymax = ticks[0], ticks[-1]
    band = plot_w / max(n, 1)
    xs = [left + band * i + band / 2 for i in range(n)]
    y = lambda v: top + plot_h * (1 - (v - ymin) / ((ymax - ymin) or 1))  # noqa: E731
    fmt = (lambda t: f"{t:g}%") if unit == "pct" else (lambda t: f"{t:g}")
    parts = [_ygrid(ticks, y, left, fmt=fmt)]
    solid = [(i, v) for i, v in pts if not _partial(rows[i])]
    if len(solid) >= 2:
        parts.append('<path class="line-driver" d="' + " ".join(f"{'M' if j == 0 else 'L'}{xs[i]:.1f},{y(v):.1f}" for j, (i, v) in enumerate(solid)) + '"/>')
    for i, v in pts:
        partial = _partial(rows[i])
        tip = f"{rows[i]['period']}: {full(v, unit)}" + (" (partial period)" if partial else "")
        parts.append(f'<circle class="{"dot-muted" if partial else "dot-driver"}" cx="{xs[i]:.1f}" cy="{y(v):.1f}" r="4" tabindex="0" '
                     f'data-tip="{esc(tip)}"/>')
    if solid:
        i, v = solid[-1]
        parts.append(f'<text class="val" x="{xs[i]:.1f}" y="{y(v) - 10:.1f}" text-anchor="middle">{esc(full(v, unit).replace(" days", "d"))}</text>')
    parts.append(_period_axis(rows, xs, band, h - bottom + 15))
    return svg(h, title, "".join(parts))


def waterfall(split: dict, before: str, after: str) -> str:
    """Before → orders effect → order-value effect → after (an exact two-effect split). Totals neutral, increases blue, decreases red."""
    start = split["before_value"]
    steps = [(before, start, "total"), ("Orders", split["volume"], "step"), ("Order\nvalue", split["order_value"], "step"),
             (after, start + split["delta"], "total")]
    spans, level = [], 0.0
    for name, amount, kind in steps:
        a, b = (0.0, amount) if kind == "total" else (level, level + amount)
        spans.append((name, a, b, kind, amount))
        level = b
    left, right, top, bottom = 44, 8, 18, 40
    h = 230
    plot_w, plot_h = W - left - right, h - top - bottom
    ticks = nice_ticks(max(max(a, b) for _, a, b, _, _ in spans) * 1.08, min(0.0, min(min(a, b) for _, a, b, _, _ in spans)))
    ymin, ymax = ticks[0], ticks[-1]
    y = lambda v: top + plot_h * (1 - (v - ymin) / ((ymax - ymin) or 1))  # noqa: E731
    band = plot_w / len(spans)
    bw = min(24, band * 0.5)
    parts = [_ygrid(ticks, y, left, money=True)]
    for i, (name, a, b, kind, amount) in enumerate(spans):
        x = left + band * i + (band - bw) / 2
        lo_v, hi_v = min(a, b), max(a, b)
        if kind == "total":
            cls, d = "m-total", column(x, y(0), y(amount), bw)
            tip, text = f"{name}: {full(amount)}", compact(amount, True)
        else:   # a floating step: the rounded end is the end the step moves towards
            cls = "m-up" if amount >= 0 else "m-down"
            d = column(x, y(lo_v), y(hi_v), bw) if amount >= 0 else column(x, y(hi_v), y(lo_v), bw)
            tip, text = f"{name.replace(chr(10), ' ')}: {amount:+,.2f}", ("+" if amount >= 0 else "−") + compact(abs(amount), True)
        parts.append(f'<path class="{cls}" d="{d}" tabindex="0" data-tip="{esc(tip)}"/>')
        parts.append(f'<text class="val" x="{x + bw / 2:.1f}" y="{y(hi_v) - 6:.1f}" text-anchor="middle">{esc(text)}</text>')
        parts.append(_xlabel(x + bw / 2, h - bottom + 15, name))
        if i < len(spans) - 1:
            parts.append(f'<line class="connector" x1="{x + bw:.1f}" x2="{x + band:.1f}" y1="{y(b):.1f}" y2="{y(b):.1f}"/>')
    parts.append(f'<line class="axis" x1="{left}" x2="{W - right}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>')
    return svg(h, f"Revenue change from {before} to {after}, split into its drivers", "".join(parts))


def diverging_bars(rows: list[dict], title: str, limit: int = 8) -> str:
    """Change per segment: blue to the right added revenue, red to the left removed it; the biggest movers first."""
    rows = sorted(rows, key=lambda r: -abs(r["change"]))
    if len(rows) > limit:
        rest = rows[limit - 1:]
        rows = rows[: limit - 1] + [{"segment": f"Other ({len(rest)})", "change": sum(r["change"] for r in rest)}]
    left, right, top = 112, 50, 6
    rh, bh = 24, 14
    h = top + rh * len(rows) + 6
    lo, hi = min(0.0, min(r["change"] for r in rows)), max(0.0, max(r["change"] for r in rows))
    span = (hi - lo) or 1
    pad = 44 if lo < 0 else 0                      # room for the value label left of the most negative bar
    plot_w = W - left - right - pad
    x = lambda v: left + pad + plot_w * (v - lo) / span  # noqa: E731
    parts = []
    for i, r in enumerate(rows):
        yy = top + rh * i + (rh - bh) / 2
        v = r["change"]
        parts.append(f'<text class="cat" x="{left - 8}" y="{yy + bh - 3:.1f}" text-anchor="end">{esc(_clip(r["segment"], 17))}</text>')
        parts.append(f'<path class="{"m-up" if v >= 0 else "m-down"}" d="{hbar(x(0), x(v), yy, bh)}" tabindex="0" '
                     f'data-tip="{esc(r["segment"])}: {v:+,.2f}"/>')
        tx, anchor = (x(v) + 5, "start") if v >= 0 else (x(v) - 5, "end")
        parts.append(f'<text class="val" x="{tx:.1f}" y="{yy + bh - 3:.1f}" text-anchor="{anchor}">'
                     f'{esc(("+" if v >= 0 else "−") + compact(abs(v), True))}</text>')
    parts.append(f'<line class="axis" x1="{x(0):.1f}" x2="{x(0):.1f}" y1="{top - 2}" y2="{h - 2}"/>')
    return svg(h, title, "".join(parts))


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def single_hbars(rows: list[tuple[str, float, str]], title: str, unit: str = "pct") -> str:
    """One measure per category as horizontal bars, labelled at the end."""
    left, right, top = 112, 64, 6
    rh, bh = 24, 14
    h = top + rh * len(rows) + 6
    hi = max([v for _, v, _ in rows] + [1e-9])
    plot_w = W - left - right
    parts = []
    for i, (name, v, tip) in enumerate(rows):
        yy = top + rh * i + (rh - bh) / 2
        parts.append(f'<text class="cat" x="{left - 8}" y="{yy + bh - 3:.1f}" text-anchor="end">{esc(_clip(name, 17))}</text>')
        if v > 0:
            parts.append(f'<path class="m-series" d="{hbar(left, left + plot_w * v / hi, yy, bh)}" tabindex="0" data-tip="{esc(tip)}"/>')
        label = full(v, unit) if unit != "money" else compact(v, True)
        parts.append(f'<text class="val" x="{left + plot_w * max(v, 0) / hi + 5:.1f}" y="{yy + bh - 3:.1f}">{esc(label)}</text>')
    parts.append(f'<line class="axis" x1="{left}" x2="{left}" y1="{top - 2}" y2="{h - 2}"/>')
    return svg(h, title, "".join(parts))


def concentration_curve(points: list[tuple[float, float]], top20: float, entity: str) -> str:
    """Cumulative share of revenue against share of entities, largest first (a Lorenz-style curve) with the even line."""
    left, top, bottom = 40, 12, 40
    h = 250
    plot_w, plot_h = W - left - 12, h - top - bottom
    x = lambda v: left + plot_w * v / 100  # noqa: E731
    y = lambda v: top + plot_h * (1 - v / 100)  # noqa: E731
    parts = [_ygrid([0, 25, 50, 75, 100], y, left, fmt=lambda t: f"{t:g}%")]
    parts += [f'<text class="tick" x="{x(t):.1f}" y="{h - bottom + 16:.1f}" text-anchor="middle">{t}%</text>' for t in (25, 50, 75, 100)]
    parts.append(f'<line class="ref" x1="{x(0):.1f}" y1="{y(0):.1f}" x2="{x(100):.1f}" y2="{y(100):.1f}"/>')
    angle, nx, ny = -math.degrees(math.atan2(plot_h, plot_w)), x(86), y(86) + 22      # just under the diagonal, along it
    parts.append(f'<text class="note" x="{nx:.1f}" y="{ny:.1f}" text-anchor="middle" transform="rotate({angle:.1f} {nx:.1f} {ny:.1f})">'
                 "if revenue were even</text>")
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(a):.1f},{y(b):.1f}" for i, (a, b) in enumerate(points))
    parts.append(f'<path class="area-series" d="{path} L{x(points[-1][0]):.1f},{y(0):.1f} L{x(0):.1f},{y(0):.1f} Z"/>')
    parts.append(f'<path class="line-series" d="{path}"/>')
    parts.append(f'<line class="ref" x1="{x(20):.1f}" x2="{x(20):.1f}" y1="{y(0):.1f}" y2="{y(top20):.1f}"/>')
    parts.append(f'<circle class="dot-series" cx="{x(20):.1f}" cy="{y(top20):.1f}" r="5" tabindex="0" '
                 f'data-tip="Top 20% of {esc(entity)}: {top20:.0f}% of revenue"/>')
    parts.append(f'<text class="val" x="{x(20) + 9:.1f}" y="{y(top20) + 16:.1f}">Top 20% → {top20:.0f}%</text>')
    parts.append(f'<text class="tick" x="{x(50):.1f}" y="{h - 6:.1f}" text-anchor="middle">% of {esc(entity)}, largest first</text>')
    return svg(h, f"Cumulative share of revenue across {entity}", "".join(parts))


# ── Page ─────────────────────────────────────────────────────────────────
CSS = """
/* Layout: a single reading column for a board brief. Summary first (headline, hero, tiles, actions), then the evidence
   (what happened, why, who), then trust and method. Colours are tokens; charts read them through classes. */
:root {
  --bg: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781; --rule: #e1e0d9; --axis: #c3c2b7;
  --series: #2a78d6; --series-wash: rgba(42, 120, 214, 0.10); --driver: #eb6834; --up: #2a78d6; --down: #e34948; --total: #52514e;
  --partial: #d4d2ca; --good: #006300; --bad: #b42a2a; --st-good: #0ca30c; --st-warn: #fab219; --st-crit: #d03b3b;
  --chip: #f0efec; --tip-bg: #0b0b0b; --tip-ink: #f0efec;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781; --rule: #2c2c2a; --axis: #383835;
    --series: #3987e5; --series-wash: rgba(57, 135, 229, 0.14); --driver: #d95926; --up: #3987e5; --down: #e66767; --total: #c3c2b7;
    --partial: #4a4a46; --good: #0ca30c; --bad: #e66767; --chip: #262624; --tip-bg: #f0efec; --tip-ink: #0b0b0b; color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781; --rule: #2c2c2a; --axis: #383835;
  --series: #3987e5; --series-wash: rgba(57, 135, 229, 0.14); --driver: #d95926; --up: #3987e5; --down: #e66767; --total: #c3c2b7;
  --partial: #4a4a46; --good: #0ca30c; --bad: #e66767; --chip: #262624; --tip-bg: #f0efec; --tip-ink: #0b0b0b; color-scheme: dark;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.55 var(--sans); -webkit-text-size-adjust: 100%; }
html { scroll-behavior: smooth; }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
.section { scroll-margin-top: 16px; }
.page { max-width: 980px; margin: 0 auto; padding-block: 32px 56px;
        padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
        display: grid; gap: 40px; }
h1, h2, h3 { margin: 0; text-wrap: balance; letter-spacing: -0.01em; }
h1 { font-size: clamp(1.7rem, 4vw, 2.4rem); font-weight: 750; letter-spacing: -0.025em; line-height: 1.1; }
h2 { font-size: 1.25rem; font-weight: 700; }
h3 { font-size: 0.98rem; font-weight: 650; }
p { margin: 0; }
.eyebrow { font: 600 0.72rem/1.2 var(--sans); letter-spacing: 0.09em; text-transform: uppercase; color: var(--muted); }
.meta { color: var(--ink-2); font-size: 0.92rem; }
.lede { font-size: 1.12rem; max-width: 68ch; color: var(--ink); }
header.top { display: grid; gap: 10px; }
.section { display: grid; gap: 16px; }
.section > header { display: grid; gap: 4px; }
.section > header p { color: var(--ink-2); max-width: 70ch; }
.hero { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr); gap: 16px; align-items: stretch; }
.hero-figure { background: var(--surface); border: 1px solid var(--rule); border-radius: 12px; padding: 18px 20px; display: grid; gap: 6px; align-content: start; }
.hero-figure .value { font-size: clamp(3rem, 7vw, 3.75rem); font-weight: 700; letter-spacing: -0.03em; line-height: 1; font-variant-numeric: tabular-nums; }
.tiles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.tile { background: var(--surface); border: 1px solid var(--rule); border-radius: 10px; padding: 12px 14px; display: grid; gap: 4px; align-content: start; }
.tile .label { font-size: 0.82rem; color: var(--ink-2); }
.tile .value { font-size: 1.5rem; font-weight: 650; letter-spacing: -0.01em; font-variant-numeric: tabular-nums; }
.tile .value small { font-size: 0.8rem; font-weight: 500; color: var(--ink-2); }
.delta { font-size: 0.82rem; font-weight: 600; display: flex; flex-wrap: wrap; gap: 3px 6px; align-items: center; }
.delta .num, .delta .vs, .delta .noise { white-space: nowrap; }
.delta.good { color: var(--good); } .delta.bad { color: var(--bad); } .delta.flat { color: var(--muted); }
.delta .vs { color: var(--muted); font-weight: 400; }
.delta .noise { font: 600 0.68rem/1 var(--sans); padding: 2px 6px; border-radius: 999px; background: var(--chip); color: var(--ink-2);
                text-transform: uppercase; letter-spacing: 0.04em; }
.grid2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.card { background: var(--surface); border: 1px solid var(--rule); border-radius: 12px; padding: 16px 18px; display: grid; gap: 10px; min-width: 0; align-content: start; }
.card .sub { color: var(--ink-2); font-size: 0.9rem; }
.chart { min-width: 0; }
.foot { color: var(--muted); font-size: 0.85rem; }
svg { width: 100%; height: auto; display: block; overflow: visible; font-family: var(--sans); }
svg .grid { stroke: var(--rule); stroke-width: 1; }
svg .axis { stroke: var(--axis); stroke-width: 1; }
svg .connector { stroke: var(--axis); stroke-width: 1; }
svg .ref { stroke: var(--axis); stroke-width: 1; }
svg .tick, svg .cat { fill: var(--ink-2); font-size: 11.5px; }
svg .cat { fill: var(--ink); }
svg .val { fill: var(--ink); font-size: 12px; font-weight: 600; }
svg .note { fill: var(--muted); font-size: 11px; }
svg .m-series { fill: var(--series); } svg .m-muted { fill: var(--partial); }
svg .m-up { fill: var(--up); } svg .m-down { fill: var(--down); } svg .m-total { fill: var(--total); }
svg .line-series { fill: none; stroke: var(--series); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
svg .area-series { fill: var(--series-wash); stroke: none; }
svg .line-driver { fill: none; stroke: var(--driver); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
svg .dot-driver { fill: var(--driver); stroke: var(--surface); stroke-width: 2; }
svg .dot-series { fill: var(--series); stroke: var(--surface); stroke-width: 2; }
svg .dot-muted { fill: var(--partial); stroke: var(--surface); stroke-width: 2; }
svg [data-tip] { cursor: default; outline: none; }
svg [data-tip]:hover, svg [data-tip]:focus-visible { filter: brightness(1.12); }
svg path[data-tip]:focus-visible, svg circle[data-tip]:focus-visible { stroke: var(--ink); stroke-width: 2; }
#tip { position: fixed; z-index: 20; pointer-events: none; background: var(--tip-bg); color: var(--tip-ink); font: 600 12.5px/1.3 var(--sans);
       padding: 6px 9px; border-radius: 6px; max-width: 260px; }
.actions { display: grid; gap: 12px; }
.action { background: var(--surface); border: 1px solid var(--rule); border-radius: 12px; padding: 14px 16px; display: grid;
          grid-template-columns: 6.6rem minmax(0, 1fr); gap: 6px 14px; }
.action .body { display: grid; gap: 6px; min-width: 0; }
.action .pri { align-self: start; justify-self: start; font: 700 0.7rem/1 var(--sans); letter-spacing: 0.06em; text-transform: uppercase;
               padding: 6px 8px; border-radius: 6px; background: var(--chip); color: var(--ink); white-space: nowrap; }
.pri::before { content: ""; display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: 0; }
.pri.p1::before { background: var(--st-crit); } .pri.p2::before { background: var(--st-warn); } .pri.p3::before { background: var(--st-good); }
.action .ev { color: var(--ink-2); font-size: 0.92rem; }
.action .todo { font-size: 0.95rem; }
.action .todo b { font-weight: 650; }
.tags { display: flex; flex-wrap: wrap; gap: 6px; }
.tag { font: 500 0.72rem/1 var(--sans); padding: 4px 7px; border-radius: 999px; background: var(--chip); color: var(--ink-2); }
.tag.mono { font-family: var(--mono); }
.meters { display: grid; gap: 12px; }
.meter { display: grid; grid-template-columns: 11rem minmax(0, 1fr) 2.6rem; gap: 4px 12px; align-items: center; }
.meter .name { display: flex; gap: 8px; align-items: baseline; font-weight: 600; }
.meter .w { color: var(--muted); font-weight: 400; font-size: 0.78rem; }
.meter .track { height: 8px; border-radius: 4px; background: var(--chip); overflow: hidden; }
.meter .fill { height: 100%; border-radius: 4px; }
.meter .fill.good { background: var(--st-good); } .meter .fill.warn { background: var(--st-warn); } .meter .fill.crit { background: var(--st-crit); }
.meter .detail { grid-column: 2 / 4; color: var(--ink-2); font-size: 0.86rem; }
.meter .num { text-align: right; font-variant-numeric: tabular-nums; font-weight: 650; }
.stamp { display: inline-flex; gap: 8px; align-items: center; font-weight: 600; font-size: 0.92rem; }
.st { display: inline-grid; place-items: center; width: 1.15rem; height: 1.15rem; border-radius: 50%; font: 800 0.7rem/1 var(--sans); color: #fff; flex: none; }
.st.good { background: var(--st-good); } .st.warn { background: var(--st-warn); color: #0b0b0b; } .st.crit { background: var(--st-crit); }
.toc { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.toc a { font-size: 0.84rem; color: var(--ink-2); text-decoration: none; padding: 5px 10px; border: 1px solid var(--rule); border-radius: 999px; }
.toc a:hover, .toc a:focus-visible { color: var(--ink); border-color: var(--axis); outline: none; }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr)); gap: 10px; margin: 0; }
.facts > div { background: var(--surface); border: 1px solid var(--rule); border-radius: 10px; padding: 10px 14px; min-width: 0; }
.facts dt { font-size: 0.78rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
.facts dd { margin: 2px 0 0; font-weight: 600; overflow-wrap: anywhere; }
details.tv summary { cursor: pointer; color: var(--ink-2); font-size: 0.85rem; }
details.tv summary:focus-visible { outline: 2px solid var(--series); outline-offset: 2px; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 0.86rem; margin-top: 8px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--rule); vertical-align: top; }
th { color: var(--ink-2); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; }
td { font-variant-numeric: tabular-nums; }
ul.plain { margin: 0; padding-left: 1.1em; display: grid; gap: 6px; color: var(--ink-2); }
code { font-family: var(--mono); font-size: 0.86em; }
footer.end { color: var(--muted); font-size: 0.85rem; border-top: 1px solid var(--rule); padding-top: 14px; }
@media (max-width: 760px) {
  .hero, .grid2 { grid-template-columns: minmax(0, 1fr); }
  .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .meter { grid-template-columns: minmax(0, 1fr) 2.6rem; } .meter .track { grid-column: 1 / 3; } .meter .detail { grid-column: 1 / 3; }
  .action { grid-template-columns: minmax(0, 1fr); }
  .card { padding: 14px 12px; }
}
@media (max-width: 520px) {   /* the charts scale down to the phone width: enlarge their text to keep it legible */
  svg .tick, svg .cat, svg .note { font-size: 13px; } svg .val { font-size: 13.5px; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""

SCRIPT = """
(function () {
  var tip = document.getElementById("tip");
  function show(el, x, y) {
    tip.textContent = el.getAttribute("data-tip");
    tip.hidden = false;
    var r = tip.getBoundingClientRect();
    var left = Math.min(Math.max(8, x + 12), window.innerWidth - r.width - 8);
    var top = y - r.height - 12 < 8 ? y + 16 : y - r.height - 12;
    tip.style.left = left + "px"; tip.style.top = top + "px";
  }
  document.querySelectorAll("[data-tip]").forEach(function (el) {
    el.addEventListener("pointermove", function (e) { show(el, e.clientX, e.clientY); });
    el.addEventListener("pointerleave", function () { tip.hidden = true; });
    el.addEventListener("focus", function () { var b = el.getBoundingClientRect(); show(el, b.left + b.width / 2, b.top); });
    el.addEventListener("blur", function () { tip.hidden = true; });
  });
})();
"""


def _delta_html(t, compared: str) -> str:
    if t.delta_pct is None:
        return ""
    if abs(t.delta_pct) < 0.05:
        return f'<span class="delta flat"><span class="num">● no change</span><span class="vs">vs {esc(compared)}</span></span>'
    arrow = "▲" if t.delta_pct > 0 else "▼"
    suffix = " pts" if t.unit == "pct" else "%"
    text = f"{t.delta_pct:+.1f}{suffix}" + (f" {t.note}" if t.note else "")
    ci = f"95% confidence interval {t.ci[0]:+.1f} to {t.ci[1]:+.1f}{suffix}" if t.ci else ""
    if t.significant is False:   # could be noise: neither green nor red
        return (f'<span class="delta flat" title="{esc(ci)}: the change could be noise"><span class="num">{arrow} {esc(text)}</span>'
                f'<span class="noise">within noise</span><span class="vs">vs {esc(compared)}</span></span>')
    meaning = "an improvement" if t.good else "a deterioration"
    return (f'<span class="delta {"good" if t.good else "bad"}" title="{esc(meaning + (" · " + ci if ci else ""))}">'
            f'<span class="num">{arrow} {esc(text)}</span><span class="vs">vs {esc(compared)}</span></span>')


def _status(score: float) -> tuple[str, str, str]:
    """(css class, icon, word): status colours always travel with an icon and a word."""
    return ("good", "✓", "Good") if score >= 90 else ("warn", "!", "Check") if score >= 75 else ("crit", "✕", "Fix")


def _score(v: float) -> str:
    """Scores are floored, so 99.9 never shows as a perfect 100."""
    return str(math.floor(v + 1e-9))


def tile_value(t) -> str:
    return compact(t.value, True) if t.unit == "money" else full(t.value, t.unit).replace(" days", "")


def _card(title: str, sub: str, chart: str, tv: str, wide: bool = False) -> str:
    return (f'<div class="card{" wide" if wide else ""}"><h3>{esc(title)}</h3>' + (f'<p class="sub">{sub}</p>' if sub else "")
            + f'<div class="chart">{chart}</div>{tv}</div>')


def _section(anchor: str, eyebrow: str, heading: str, intro: str, body: str) -> str:
    return (f'<section class="section" id="{anchor}" aria-labelledby="{anchor}-h"><header><p class="eyebrow">{esc(eyebrow)}</p>'
            f'<h2 id="{anchor}-h">{esc(heading)}</h2>' + (f"<p>{intro}</p>" if intro else "") + f"</header>{body}</section>")


def _context(d: Decision, m: dict) -> list[tuple[str, str]]:
    """What the data is, in business terms: the facts a reader needs before any number."""
    facts = [("Tables", f"{len(d.profile.tables)} tables, {len(d.schema.accepted)} links")]
    if d.fact:
        facts.append(("One row is", d.profile[d.fact].grain.replace("one row per ", "").replace("`", "")))
    k = m.get("kpis") or {}
    if k.get("orders") is not None:
        facts.append(("Orders", f"{k['orders']:,.0f}" + (f" from {k['customers']:,.0f} customers" if k.get("customers") else "")))
    if k.get("revenue") is not None:
        facts.append(("Revenue, all periods", full(k["revenue"])))
    if d.kpi and d.revenue:
        facts.append(("Revenue is", Builder(d).revenue_formula()))
    if d.period:
        n, partial = len(d.complete_periods), [d.label(p) for p in d.partial_periods]
        facts.append(("Periods", f"{n} complete {d.period}s" + (f"; {', '.join(partial)} partial" if partial else "")))
    return facts


def to_business_brief(d: Decision, metrics: dict, *, title: str = "Business brief", source: str = "uploaded files",
                      standalone: bool = True) -> str:
    """The brief as HTML: a full document, or (standalone=False) a fragment for a host that adds the document skeleton."""
    s: Summary = executive_summary(d, metrics, title)
    m = metrics
    rows = m.get("period_table") or []
    main = m.get("main_measure", "revenue")
    money = main == "revenue"
    sections: list[str] = []
    nav: list[tuple[str, str]] = []

    # ── Header: what, when, the one-sentence answer ──────────────────────
    meta = " · ".join(x for x in (f"Data {s.coverage}" if s.coverage else "", f"latest complete {d.period} {s.period}" if rows else "",
                                  f"source: {source}") if x)
    stamp_cls, stamp_icon, _ = _status(s.trust.score)
    sections.append(f'<header class="top"><p class="eyebrow">Business brief · AutoSQL Explorer</p><h1>{esc(title)}</h1>'
                    f'<p class="meta">{esc(meta)}</p><p class="lede">{esc(s.headline)}</p>'
                    f'<p class="stamp"><span class="st {stamp_cls}" aria-hidden="true">{stamp_icon}</span>Data trust {_score(s.trust.score)}/100: '
                    f'{esc(s.trust.grade)}</p>NAV</header>')

    # ── Key figures: the hero number and the tiles ───────────────────────
    if s.tiles:
        hero, rest = s.tiles[0], s.tiles[1:7]
        hero_html = (f'<div class="hero-figure"><span class="eyebrow">{esc(hero.label)} · {esc(s.period)}</span>'
                     f'<span class="value">{esc(compact(hero.value, hero.unit == "money"))}</span>'
                     f'<span class="meta">{esc(full(hero.value, hero.unit))}</span>{_delta_html(hero, s.compared_with)}'
                     + (f'<span class="meta">{m["yoy"]["pct"]:+.1f}% on the same {esc(d.period)} a year earlier</span>' if m.get("yoy") else "")
                     + "</div>")
        tiles = "".join(f'<div class="tile"><span class="label">{esc(t.label)}</span><span class="value">{esc(tile_value(t))}'
                        + (' <small>days</small>' if t.unit == "days" else "") + f"</span>{_delta_html(t, s.compared_with)}</div>"
                        for t in rest)
        sections.append(f'<section class="section" aria-label="Key figures"><div class="hero">{hero_html}<div class="tiles">{tiles}</div></div>'
                        + ('<p class="foot">Figures are for the latest complete period; arrows compare it with the one before. '
                           "Green means better for the business (fewer days to ship is better), red means worse; <b>within noise</b> means the "
                           "change's 95% confidence interval includes zero, so it could be normal variation. Hover a change for its interval.</p>"
                           if s.compared_with else "")
                        + "</section>")

    # ── Context: what the data is ────────────────────────────────────────
    facts = "".join(f"<div><dt>{esc(a)}</dt><dd>{esc(b)}</dd></div>" for a, b in _context(d, m))
    sections.append(_section("data", "Context", "What this data is", "", f'<dl class="facts">{facts}</dl>'))
    nav.append(("data", "Data"))

    # ── What to do ───────────────────────────────────────────────────────
    if s.insights:
        names = {1: "Act now", 2: "Plan", 3: "Monitor"}
        cards = "".join(
            f'<article class="action"><span class="pri p{i.priority}">{names[i.priority]}</span>'
            f'<div class="body"><h3>{esc(i.headline)}</h3><p class="ev">{esc(i.evidence)}. {esc(i.implication)}</p>'
            f'<p class="todo"><b>Next step:</b> {esc(i.action)}</p>'
            f'<div class="tags"><span class="tag">{esc(i.area)}</span><span class="tag">{esc(i.confidence)}</span>'
            f'<span class="tag mono">{esc(i.source)}</span></div></div></article>' for i in s.insights[:8])
        sections.append(_section("actions", "Recommendations", "What to do",
                                 "Ordered by urgency. Each one cites its numbers and the SQL step that produced them. “Needs a test” marks a "
                                 "pattern in the data, not a proven cause.", f'<div class="actions">{cards}</div>'))
        nav.append(("actions", "What to do"))

    # ── What happened ────────────────────────────────────────────────────
    if rows:
        unit = "money" if money else "count"
        partial = m.get("partial_periods") or []
        cards = [_card(f"{main.capitalize()} per {d.period}",
                       (f"Grey: {esc(', '.join(partial))} is partial, shown but never compared." if partial else ""),
                       period_columns(rows, main, unit, f"{main} per {d.period}"),
                       table(["Period", main.capitalize(), "Status"],
                             [[r["period"], full(float(r.get(main) or 0), unit), r.get("period_status", "complete")] for r in rows]))]
        driver_key = next((k for k in (f"avg_{d.lead_name}", "late_pct", "avg_rating") if any(r.get(k) is not None for r in rows)), None)
        if driver_key:
            label = {"late_pct": "Late shipments", "avg_rating": "Average rating"}.get(driver_key, f"Average {d.lead_name.replace('_', ' ')}")
            dunit = "pct" if driver_key == "late_pct" else "score" if driver_key == "avg_rating" else "days"
            chart = period_line(rows, driver_key, dunit, f"{label} per {d.period}")
            if chart:
                cards.append(_card(f"{label}: the suspected driver", "The same periods on its own scale; read it against the revenue chart.",
                                   chart, table(["Period", label], [[r["period"], full(float(r[driver_key]), dunit)]
                                                                   for r in rows if r.get(driver_key) is not None])))
        sections.append(_section("trend", "Trend", "What happened", "", f'<div class="grid2">{"".join(cards)}</div>'))
        nav.append(("trend", "Trend"))

    # ── Why it moved ─────────────────────────────────────────────────────
    split = m.get("driver_split")
    before_row = next((r for r in rows if r["period"] == (split or {}).get("from")), None)
    if money and split and split.get("delta") and before_row:
        before_value = float(before_row.get("revenue") or 0)
        shares = effect_shares(split)
        cards = [_card("Revenue = orders × average order value",
                       f"{esc(split['from'])} → {esc(split['to'])}: the number of orders explains {shares['volume']:.0%} of the movement, "
                       f"order value {shares['order_value']:.0%}. Each effect is measured at the average of the other, so the two add up exactly.",
                       waterfall(dict(split, before_value=before_value), split["from"], split["to"]),
                       table(["Step", "Amount"], [[split["from"], full(before_value)], ["Number of orders", f"{split['volume']:+,.2f}"],
                                                  ["Average order value", f"{split['order_value']:+,.2f}"],
                                                  [split["to"], full(before_value + split["delta"])]]))]
        for col, seg in list((m.get("segment_changes") or {}).items())[:3]:
            cards.append(_card(f"Change by {col.replace('_', ' ')}", "Blue bars added revenue, red bars removed it; biggest movers first.",
                               diverging_bars(seg, f"Revenue change by {col}"),
                               table([col, split["from"], split["to"], "Change"],
                                     [[r["segment"], full(r["before"]), full(r["after"]), f"{r['change']:+,.2f}"] for r in seg])))
        sections.append(_section("drivers", "Drivers", "Why it moved",
                                 f"The change from {esc(split['from'])} to {esc(split['to'])}, split exactly into its two drivers "
                                 "(the steps add up to the total) and by segment.", f'<div class="grid2">{"".join(cards)}</div>'))
        nav.append(("drivers", "Why"))

    # ── Customers ────────────────────────────────────────────────────────
    cust = []
    pareto = (m.get("pareto") or {}).get("customers")
    if pareto and pareto.get("curve"):
        cust.append(_card("How concentrated is revenue?",
                          f"Class A, the customers making up the first 80% of revenue: {pareto['a_entities']} customers "
                          f"({pareto['a_entities_pct']:.0f}% of them).",
                          concentration_curve(pareto["curve"], pareto["top20_revenue_pct"], "customers"),
                          table(["% of customers", "% of revenue"], [[f"{a:.1f}%", f"{b:.1f}%"] for a, b in pareto["curve"]])))
    rfm = m.get("rfm") or {}
    if rfm:
        order = sorted(rfm.items(), key=lambda kv: -kv[1]["revenue_pct"])
        cust.append(_card("Revenue by customer segment", "RFM: recency, frequency and money, each scored 1–5 with NTILE(5).",
                          single_hbars([(k, v["revenue_pct"], f"{k}: {v['revenue_pct']:.1f}% of revenue, {v['customers']} customers")
                                        for k, v in order], "Revenue share by RFM segment"),
                          table(["Segment", "Customers", "% of revenue"], [[k, str(v["customers"]), f"{v['revenue_pct']:.1f}%"] for k, v in order])))
    if cust:
        gaps = m.get("purchase_gaps") or {}
        intro = ""
        if gaps.get("avg_days") is not None:
            intro = (f"Repeat customers reorder every {gaps['avg_days']:.0f} days on average. {gaps['overdue']} of {gaps['judged']} customers with "
                     "3+ orders are overdue: silent for more than twice their own usual gap"
                     + (f", and they brought {gaps['overdue_revenue_pct']:.0f}% of those customers' revenue." if gaps.get("overdue_revenue_pct") else "."))
        sections.append(_section("customers", "Customers", "Who brings the revenue", intro, f'<div class="grid2">{"".join(cust)}</div>'))
        nav.append(("customers", "Customers"))

    # ── Pricing ──────────────────────────────────────────────────────────
    disc = m.get("discounts")
    if disc and disc.get("bands"):
        bands = disc["bands"]
        lift = ""
        if disc.get("qty_discounted") and disc.get("qty_full_price"):
            lift = (f" Discounted lines average {disc['qty_discounted']:.1f} units against {disc['qty_full_price']:.1f} at full price: "
                    "check whether the discount buys that volume.")
        sections.append(_section("pricing", "Pricing", "What discounts cost",
                                 f"{disc['pct_of_gross']:.1f}% of gross revenue ({full(disc['given'])}) was given away on "
                                 f"{disc['discounted_lines_pct']:.0f}% of order lines.{esc(lift)}",
                                 '<div class="grid2">' + _card("Revenue given away, by discount band", "",
                                       single_hbars([(b["band"], b["given"], f"{b['band']}: {b['given']:,.2f} given away on {b['lines_pct']:.1f}% "
                                                      f"of lines, {b['qty']:.1f} units per line") for b in bands], "Discount given by band", "money"),
                                       table(["Band", "% of lines", "Given away", "Units per line"],
                                             [[b["band"], f"{b['lines_pct']:.1f}%", full(b["given"]), f"{b['qty']:.1f}"] for b in bands]))
                                 + _card("Units per line, by discount band", "Does a bigger discount come with bigger orders?",
                                         single_hbars([(b["band"], b["qty"], f"{b['band']}: {b['qty']:.1f} units per line") for b in bands],
                                                      "Units per line by discount band", "score"), "") + "</div>"))
        nav.append(("pricing", "Pricing"))

    # ── Trust ────────────────────────────────────────────────────────────
    t = s.trust
    meters = []
    for x in t.dimensions:
        cls, icon, word = _status(x.score)
        meters.append(f'<div class="meter"><span class="name"><span class="st {cls}" aria-hidden="true">{icon}</span>{esc(x.name)}'
                      f'<span class="w">weight {TRUST_WEIGHTS[x.name]:.0%}</span></span>'
                      f'<div class="track" role="img" aria-label="{esc(x.name)}: {_score(x.score)} of 100, {word}">'
                      f'<div class="fill {cls}" style="width:{max(2.0, x.score):.1f}%"></div></div>'
                      f'<span class="num">{_score(x.score)}</span><span class="detail">{esc(x.detail)}</span></div>')
    sections.append(_section("trust", "Data quality", "Can we trust these numbers?",
                             "Five checks run in SQL before any KPI is computed. The score is their weighted average: 90+ is high, 75–90 usable "
                             "with the caveats listed, below 75 use with care.", f'<div class="card meters">{"".join(meters)}</div>'))
    nav.append(("trust", "Trust"))

    # ── Method ───────────────────────────────────────────────────────────
    k = m.get("kpis") or {}
    have = {key for key, v in k.items() if v is not None} | ({"aov"} if k.get("avg_order_value") is not None else set())
    have |= ({"qoq", "yoy"} if rows else set()) | ({"top20_share"} if pareto else set()) | ({"discount_pct"} if disc else set())
    have |= {"days_between_orders", "overdue_customers"} if m.get("purchase_gaps") else set()
    used = [x for x in KPI_BY_KEY.values() if x.key in have]
    sections.append(_section("method", "Method", "How the numbers were made", "",
                             '<div class="grid2"><div class="card"><h3>Assumptions</h3><ul class="plain">'
                             + "".join(f"<li>{esc(a.replace('`', ''))}</li>" for a in assumptions(d))
                             + '</ul></div><div class="card"><h3>KPI definitions</h3><p class="sub">What each figure means and the SQL behind it.</p>'
                             + table(["KPI", "Definition", "SQL"], [[x.name, x.definition, x.formula] for x in used], "Show the definitions")
                             + "</div></div>"))
    nav.append(("method", "Method"))
    sections.append(f'<footer class="end">Generated by AutoSQL Explorer from {esc(source)}. Every figure is printed by a SQL step that reruns on '
                    "MySQL 8 or PostgreSQL (download the project ZIP from the app). Hover or focus a mark to read its value; every chart has a "
                    "table view.</footer>")

    links = "".join(f'<a href="#{a}">{esc(b)}</a>' for a, b in nav)
    sections[0] = sections[0].replace("NAV", f'<nav class="toc" aria-label="Sections">{links}</nav>')
    body = f'<main class="page">{"".join(sections)}</main><div id="tip" role="tooltip" hidden></div><script>{SCRIPT}</script>'
    head = (f"<title>{esc(title)}</title>\n" + ('<meta name="claude:viewport-fit-sides" content="cover">\n' if not standalone else "")
            + f"<style>{CSS}</style>\n")
    if not standalone:
        return head + body + "\n"
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            f"{head}</head>\n<body>\n{body}\n</body>\n</html>\n")
