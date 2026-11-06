"""The LinkedIn speed page (/linkedin-speed): HTML for lispeed.compute(). Read-only.

The numbers come from lispeed (its docstring says what is measured and what is estimated); the page frame, theme and live refresh are
quality._frame's. Every time on the page is IST. Charts are inline SVG and plain HTML bars, coloured only with the board's own tokens
(ink, graphite, grid, rested, and the yellow sign for what needs a look), so dark mode works without extra code.
"""
from __future__ import annotations

import html
import math
import statistics
from datetime import datetime, timedelta

import lispeed
import quality

e = html.escape
RESET = timedelta(hours=5, minutes=30)

_CSS = """
.q, .q #body { grid-template-columns: minmax(0, 1fr); }
.q .lede { max-width: 96ch; }
.q nav.jump { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 14px; }
.q nav.jump a { color: var(--ink); }
.q ol.path { margin: 0; padding-left: 22px; display: grid; gap: 6px; max-width: 100ch; }
.q .cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px 16px; }
@media (max-width: 900px) { .q .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .q .cards { grid-template-columns: minmax(0, 1fr); } .q .hb { grid-template-columns: 1fr; } .q .hb .val { text-align: left; } }
.q .card { border-top: 2px solid var(--ink); padding-top: 8px; }
.q .card .k { font-size: 12px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); font-weight: 600; }
.q .card .v { font-size: 34px; line-height: 40px; font-weight: 600; }
.q .card .s { color: var(--muted); font-size: 13px; max-width: 34ch; }
.q .card.sign .k, .q .card.sign .s { color: var(--sign-ink); }
.q ul.short { margin: 0; padding-left: 20px; display: grid; gap: 8px; max-width: 100ch; }
.q .tg { font-size: 11px; letter-spacing: .06em; border: 1px solid var(--ink); padding: 0 4px; margin-left: 6px; white-space: nowrap; font-weight: 400; color: var(--ink); }
.q .tg.est { border-style: dashed; border-color: var(--muted); color: var(--muted); }
.q .tg.code { border-style: dotted; border-color: var(--muted); color: var(--muted); }
.q table.t td, .q table.t th { white-space: nowrap; }
.q table.t td.w, .q table.t th.w { white-space: normal; min-width: 260px; }
.q table.t tr.sum th, .q table.t tr.sum td { border-top: 2px solid var(--ink); font-weight: 600; }
.q table.t .dim { color: var(--muted); }
.q .muted { color: var(--muted); }
.q h3 { margin: 4px 0 0; font-size: 16px; font-weight: 600; }
.q svg.chart { width: 100%; height: auto; display: block; }
.q svg text { fill: var(--muted); font: 11px Bahnschrift, 'Segoe UI', sans-serif; }
.q svg text.ink { fill: var(--ink); }
.q svg text.b { font-weight: 700; fill: var(--ink); font-size: 12px; }
.q .legend { display: flex; flex-wrap: wrap; gap: 6px 18px; color: var(--muted); font-size: 13px; margin-top: 12px; }
.q .legend span { display: inline-flex; align-items: center; gap: 6px; }
.q .sw { display: inline-block; width: 18px; height: 12px; border: 1px solid var(--ink); }
.q .stack { display: flex; height: 30px; border: 1px solid var(--ink); background: var(--paper); }
.q .stack > i { display: block; height: 100%; min-width: 1px; border-right: 1px solid var(--paper); }
.q .stackrow { display: grid; gap: 6px; margin-top: 14px; }
.q .stackrow .cap { display: flex; flex-wrap: wrap; gap: 4px 14px; align-items: baseline; }
.q .stackrow .cap b { font-size: 15px; }
.q .p-unr { background: var(--sign); background-image: repeating-linear-gradient(135deg, transparent 0 4px, rgba(27,34,48,.40) 4px 6px); }
.q .p-opn { background: var(--muted); background-image: repeating-linear-gradient(135deg, transparent 0 4px, var(--paper) 4px 6px); }
.q .p-kwn { background: color-mix(in srgb, var(--muted) 55%, var(--sign)); background-image: repeating-linear-gradient(45deg, transparent 0 4px, var(--paper) 4px 6px); }
.q .p-mov { background: var(--grid); background-image: repeating-linear-gradient(135deg, transparent 0 4px, var(--paper) 4px 6px); }
.q .p-pag { background: var(--rested); background-image: repeating-linear-gradient(135deg, transparent 0 4px, var(--paper) 4px 6px); }
.q .p-frm { background: var(--ink); }
.q .p-rej { background: color-mix(in srgb, var(--ink) 60%, var(--paper)); }
.q .p-uae { background: color-mix(in srgb, var(--ink) 35%, var(--paper)); }
.q .p-rst { background: color-mix(in srgb, var(--ink) 80%, var(--paper)); }
.q .p-stp { background: var(--paper); background-image: repeating-linear-gradient(90deg, transparent 0 5px, var(--grid) 5px 6px); }
.q .p-nex { background: var(--paper); }
.q .hb { display: grid; grid-template-columns: minmax(150px, 220px) 1fr 170px; gap: 4px 12px; align-items: center; }
.q .hb .bar { height: 16px; background: var(--ink); min-width: 1px; }
.q .hb .bar.sign { background: var(--sign); box-shadow: inset 0 0 0 1px var(--ink); }
.q .hb .bar.dim { background: var(--muted); }
.q .hb .lab { font-size: 14px; }
.q .hb .val { color: var(--muted); font-size: 13px; text-align: right; }
.q table.heat td { text-align: center; padding: 4px 2px; min-width: 26px; font-size: 12px; border-bottom: 1px solid var(--grid); }
.q table.heat th { font-size: 11px; padding: 4px 2px; text-align: center; }
.q table.heat th.l, .q table.heat td.l { text-align: left; white-space: nowrap; padding-left: 0; }
.q details.fold { border-top: 1px solid var(--grid); }
.q details.fold > summary { cursor: pointer; padding: 10px 0; font-size: 16px; font-weight: 600; }
.q .note { color: var(--muted); font-size: 13px; max-width: 100ch; }
"""


# ------------------------------------------------------------------ small formatters
def hm(m) -> str:
    if m is None:
        return "n/a"
    m = round(float(m))
    return f"{m} min" if m < 90 else f"{m // 60} h {m % 60:02d} min"


def sec(s) -> str:
    return "n/a" if s is None else (f"{s:.1f} s" if s < 10 else f"{s:.0f} s")


def each(s) -> str:
    return "under 1 s, not separable" if s is not None and s < 0.5 else f"{sec(s)} each"


def pc(a, b) -> str:
    return f"{100 * a / b:.0f}%" if b else "n/a"


def num(n) -> str:
    return f"{n:,.0f}"


def tag(kind: str) -> str:
    return {"measured": '<span class="tg">measured</span>', "estimated": '<span class="tg est">estimated</span>',
            "code": '<span class="tg code">from the code</span>'}.get(kind, "")


def w0_of(day: dict) -> datetime:
    return datetime.strptime(day["key"], "%Y-%m-%d") + RESET


def part(day: dict, prefix: str) -> float:
    b = day.get("budget")
    return next((p["min"] for p in b["parts"] if p["name"].startswith(prefix)), 0.0) if b else 0.0


def setting(r: dict, prefix: str, default: str = "") -> str:
    return next((s["value"] for s in r["settings"] if s["name"].startswith(prefix)), default).replace(", random", "")


# ------------------------------------------------------------------ top of the page
def short_section(r: dict) -> str:
    days, ty, fl, fu, cc = r["days"], r["typical"], r["flow"]["saved"], r["funnel"], r["card_costs"]
    full = next((d for d in reversed(days) if 35 in d["reach"]), None)
    items = []
    if full and ty:
        items.append(f"Reaching 35 takes <b>{hm(ty['to35'])}</b> at the median over the {ty['n']} counts since {ty['since']:%d %b}. The last full count "
                     f"({e(full['label'])}) took <b>{hm(full['reach'][35])}</b>, {full['reach'][35] / ty['to35']:.1f} times that.")
    if fl.get("n") and fu["saved"]:
        items.append(f"The form is not the slow part. A saved application takes <b>{sec(fl['median'])}</b> from passing the filters to saved (nine in ten under "
                     f"{sec(fl['p90'])}). The {fu['saved']} saves of the last {fu['days']} counts took {hm(fl['total_min'])} of the {hm(fu['minutes'])} the bot ran.")
    cards = fu["title"] + fu["badge"] + fu["pass"]
    if fu["saved"]:
        items.append(f"The slow part is looking at cards. For every saved application the bot decides on <b>{cards / fu['saved']:.0f} cards</b>: {fu['title'] / fu['saved']:.0f} fail the title "
                     f"check (about {sec(cc['title'])} each), {fu['badge'] / fu['saved']:.1f} are opened and fail the activity-badge check "
                     f"({sec(cc['badge'])} each, mostly the built-in {setting(r, 'Pacing', '8 to 20 s')} pacing wait) and {fu['pass'] / fu['saved']:.1f} pass the filters and are tried.")
        if fu["unrendered"]:
            share = f", {pc(part(full, 'Clicking'), full['wall'])} of the last full count" if full and full.get("budget") else ""
            items.append(f"The largest single cost is clicking job cards LinkedIn had not drawn yet: <b>{fu['unrendered'] / fu['saved']:.0f} clicks per saved application</b>, "
                         f"about {sec(r['fit'].get('cost', {}).get('click_fail'))} each{share}.")
    cov_days = [d for d in days if d["key"] >= r["coverage_from"]]
    known = sum(d["prev_rejected"] + d["already_applied"] for d in cov_days)
    if known and fu["saved"]:
        share = f", {pc(part(full, 'Cards seen'), full['wall'])} of the last full count" if full and full.get("budget") else ""
        items.append(f"The second sink: cards the bot has already applied to or rejected still wait out the pacing wait before they are skipped, <b>{known / fu['saved']:.0f} per saved "
                     f"application</b>, about {sec(r['fit'].get('cost', {}).get('prev_rejected'))} each{share}.")
    ev = {x["code"]: x for x in r["eras"] if x["unrendered_per_saved"] is not None}
    if len(ev) == 3:
        items.append(f"Clicks on undrawn cards per saved application were <b>{ev['A']['unrendered_per_saved']:.0f}</b> ({e(ev['A']['rule'])}), <b>{ev['B']['unrendered_per_saved']:.0f}</b> "
                     f"({e(ev['B']['rule'])}, badge rule on) and <b>{ev['C']['unrendered_per_saved']:.0f}</b> ({e(ev['C']['rule'])}, the day the title check began rejecting cards in half a second).")
    return "<ul class=\"short\">" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"


def cards_section(r: dict) -> str:
    days, ty, fl, gb = r["days"], r["typical"], r["flow"]["saved"], r["gap_by"]
    full = next((d for d in reversed(days) if 35 in d["reach"]), None)
    today = days[-1] if days and days[-1]["running"] else None
    out = []

    def card(k, v, s, cls=""):
        out.append(f'<div class="card {cls}"><div class="k">{e(k)}</div><div class="v">{v}</div><div class="s">{s}</div></div>')

    if full and ty:
        slow = full["reach"][35] > 2 * ty["to35"]
        card("Time to 35, last full count", hm(full["reach"][35]), f"{e(full['label'])}. Median of {ty['n']} counts: {hm(ty['to35'])}; best {hm(ty['best'])}, worst {hm(ty['worst'])}.",
             "sign" if slow else "")
        card("Saves per running hour", f"{full['per_hour']:.1f}", f"{e(full['label'])}. Median of {ty['n']} counts: {ty['per_hour']:.1f}.")
    if fl.get("n"):
        card("One saved form", sec(fl["median"]), f"From passing the filters to saved, {fl['n']} saves. Nine in ten under {sec(fl['p90'])}, slowest {sec(fl['max'])}. Measured.")
    if gb.get("badge", {}).get("n"):
        card("One opened card", sec(gb["badge"]["median"]), f"Median gap before a card is rejected on its badge, {gb['badge']['n']} cards. Includes the pacing wait. Measured.")
    if full and full.get("budget"):
        card("Clicks on cards not drawn yet", num(full["unrendered"]), f"{e(full['label'])}: about {hm(part(full, 'Clicking'))}, {pc(part(full, 'Clicking'), full['wall'])} of that count. Estimated.", "sign")
    if today:
        card("This count so far", f"{today['saved']} saved", f"In {hm(today['wall'])}, {today['per_hour']:.1f} per running hour. Started {today['start']:%H:%M}.")
    return '<div class="cards">' + "".join(out) + "</div>"


def path_section(r: dict) -> str:
    cc, fl, fu = r["card_costs"], r["flow"], r["funnel"]
    cards = fu["title"] + fu["badge"] + fu["pass"]
    if not cards:
        return ""
    uae = [u["minutes"] for u in r["uae"][-6:] if u["minutes"] > 1]       # the last six passes: earlier ones in October were longer
    ea = fl["fail"].get("Easy Apply problem", {})
    other = [v["median"] for k, v in fl["fail"].items() if k != "Easy Apply problem" and v.get("n")]
    items = [
        f"<b>A start.</b> The supervisor launches the bot, which checks the sign-in, runs the UAE pass (about {statistics.median(uae):.0f} min lately) and then the India search: "
        f"{e(setting(r, 'Search terms', 'its search terms'))}, up to {e(setting(r, 'Jobs read', '30 jobs'))} each, results page by results page." if uae else
        f"<b>A start.</b> The supervisor launches the bot, which checks the sign-in and then runs the India search.",
        f"<b>Title check</b>, about {sec(cc['title'])}. A title that fails the fit rules is skipped without opening the card; {pc(fu['title'], cards)} of all cards end here.",
        f"<b>Pacing wait</b>, {e(setting(r, 'Pacing', '8 to 20 s'))}, before every card that passes the title check. It is built in on purpose.",
        f"<b>Open the card and read it.</b> The activity-badge check rejects {pc(fu['badge'], cards)} of all cards; with the pacing wait, a rejected card costs about {sec(cc['badge'])}. "
        "A card the bot already applied to or rejected earlier is only recognised here, after the wait, and skipped.",
        f"<b>The other filters</b> (experience, bad words, clearance, blacklisted words) reject another {fu['pass'] and pc(sum(v for k, v in fu['failed'].items() if k != 'Easy Apply problem'), fu['pass'])} of the cards "
        f"that got here, in about {sec(statistics.median(other)) if other else 'a second or two'} each." if fu["pass"] else "",
        f"<b>The Easy Apply form.</b> Median {sec(fl['saved'].get('median'))} from the pass line to the saved row; a form that goes wrong costs about {sec(ea.get('median'))} and is recorded as rejected.",
        "<b>The end.</b> After the 35th save LinkedIn shows its banner on the next try; the bot exits with code 44 and the chain rests until 05:35, when the count resets. If all search terms are done first the bot exits with code 0 and "
        f"restarts after 60 s (UAE pass first), up to {e(setting(r, 'Restarts', '6 per session'))}. A run that saves nothing for 3 hours is stopped by the yield watchdog and waits for a human.",
    ]
    return f"""<section id="path"><h2>The path of one card</h2>
<p>What the bot does, in order, with what each step costs. The detailed numbers are further down.</p>
<ol class="path">{''.join(f'<li>{i}</li>' for i in items if i)}</ol></section>"""


# ------------------------------------------------------------------ time to 35 per count
def trend_svg(r: dict) -> str:
    trend, ty = r["trend"], r["typical"]
    if not trend:
        return ""
    W, H, L, R, T, B = 1100, 300, 46, 10, 26, 44
    vals = [t["to35"] or t["to_last"] or 0 for t in trend]
    ymax = max(240, 120 * math.ceil(min(max(vals), 720) / 120))
    n, bw = len(trend), (W - L - R) / len(trend)

    def y(v):
        return H - B - (min(v, ymax) / ymax) * (H - T - B)

    s = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Minutes to reach 35 saved applications, one bar per count">']
    for code, name, fill in (("A", "before the badge rule", "none"), ("B", "badge rule on", "var(--rested)"), ("C", "title check on as well", "var(--grid)")):
        idx = [i for i, t in enumerate(trend) if t["era"] == code]
        if idx:
            x0, x1 = L + idx[0] * bw, L + (idx[-1] + 1) * bw
            s.append(f'<rect x="{x0:.1f}" y="{T}" width="{x1 - x0:.1f}" height="{H - T - B}" fill="{fill}" opacity=".7"/>')
            if x1 - x0 < 160:      # a short period: its label ends at the period's right edge instead of running off the chart
                s.append(f'<text x="{x1 - 2:.1f}" y="{T - 9}" text-anchor="end">{name}</text>')
            else:
                s.append(f'<text x="{x0 + 4:.1f}" y="{T - 9}">{name}</text>')
    for v in range(0, ymax + 1, 120):
        s.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--grid)" stroke-width="1"/>')
        s.append(f'<text x="{L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v}</text>')
    s.append(f'<text x="4" y="{T - 9}">minutes</text>')
    for i, t in enumerate(trend):
        x, w = L + i * bw + 2, bw - 4
        reached = t["to35"] is not None
        v = t["to35"] if reached else (t["to_last"] or 0)
        h = H - B - y(v)
        slow = reached and ty and v > 2 * ty["to35"]
        fill = "var(--sign)" if slow else "var(--ink)" if reached else "none"
        extra = "" if reached else ' stroke-dasharray="3 2"'
        tip = (f"{t['label']}: 35 saved in {hm(v)}, {t['per_hour']:.1f} per running hour" if reached
               else f"{t['label']}: {t['saved']} saved, last save after {hm(v)}; the count did not reach 35")
        s.append(f'<g><title>{e(tip)}</title><rect x="{x:.1f}" y="{y(v):.1f}" width="{w:.1f}" height="{h:.1f}" fill="{fill}" stroke="var(--ink)" stroke-width="1"{extra}/></g>')
        if v > ymax:
            s.append(f'<text x="{x + w / 2:.1f}" y="{T + 10}" text-anchor="middle" class="ink">{v:.0f}</text>')
        if n <= 26 or i % 2 == 0:
            s.append(f'<text x="{x + w / 2:.1f}" y="{H - B + 12}" text-anchor="end" transform="rotate(-50 {x + w / 2:.1f} {H - B + 12})">{e(t["label"])}</text>')
    if ty:
        yy = y(ty["to35"])
        s.append(f'<line x1="{L}" x2="{W - R}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="var(--ink)" stroke-width="1.5" stroke-dasharray="6 4"/>')
        s.append(f'<text x="{L + 6}" y="{yy - 5:.1f}" class="ink">median {hm(ty["to35"])}</text>')
    s.append("</svg>")
    return "".join(s)


def trend_section(r: dict) -> str:
    rows = []
    for x in r["eras"]:
        rows.append(f"<tr><th scope=\"row\" class=\"w\">{e(x['name'])}<span class=\"id\">{e(x['rule'])}</span></th><td class=\"n\">{x['n']}</td>"
                    f"<td class=\"n\">{hm(x['median_to35']) if x['median_to35'] is not None else 'n/a'}</td>"
                    f"<td class=\"n\">{x['median_per_hour'] if x['median_per_hour'] is not None else 'n/a'}</td>"
                    f"<td class=\"n\">{x['unrendered_per_saved'] if x['unrendered_per_saved'] is not None else 'n/a'}</td></tr>")
    return f"""<section id="trend"><h2>How long a count takes to reach 35</h2>
<p>One bar per count (05:30 to 05:30 IST) that saved 20 or more; the bar is the time from the chain's start to the 35th save. A dashed outline is a count that
stopped short of 35. Yellow is more than twice the median (the dashed line). The shading marks what changed in the bot's rules: the "actively reviewing" badge
became mandatory on 13 Oct, and the title check that rejects off-role cards at once began on the first day it logged skips.</p>
<div class="box">{trend_svg(r)}</div>
<div class="box"><table class="t"><thead><tr><th>Period</th><th class="n">Counts at 35</th><th class="n">Median time to 35</th><th class="n">Median saves per hour</th>
<th class="n">Unrendered clicks per save</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="note">"Unrendered clicks" are clicks on a job card LinkedIn had not drawn yet (explained below). The text log that counts them starts on 1 Oct. A period with two or three counts says little on its own.</p></section>"""


# ------------------------------------------------------------------ one row per count
def days_section(r: dict) -> str:
    ty = r["typical"]
    rows = []
    for x in r["days"][-14:][::-1]:
        slow = 35 in x["reach"] and ty and x["reach"][35] > 2 * ty["to35"]
        reach = "".join(f'<td class="n{" sign" if k == 35 and slow else ""}">{x["reach"].get(k, "")}</td>' for k in (10, 20, 30, 35))
        state = " (running)" if x["running"] else ""
        rows.append(f"<tr><th scope=\"row\">{e(x['label'])}{state}</th><td>{x['start']:%H:%M}&ndash;{x['end']:%H:%M}</td><td class=\"n\">{x['saved']}</td>{reach}"
                    f"<td class=\"n\">{x['per_hour']:.1f}</td><td class=\"n\">{x['first_save_min'] if x['first_save_min'] is not None else ''}</td>"
                    f"<td class=\"n\">{x['gap_median']:.1f} / {x['gap_max']:.0f}</td><td class=\"n\">{x['runs']}</td>"
                    f"<td class=\"n\">{x['uae_minutes']:.0f}</td><td class=\"n\">{num(x['unrendered']) if x['has_text'] else ''}</td>"
                    f"<td class=\"n\">{x['idle']:.0f}</td><td class=\"n\">{x['tail_min'] if x['tail_min'] is not None else ''}</td></tr>")
    return f"""<section id="days"><h2>Every count</h2>
<p>The last 14 counts, newest first. Times are minutes from the chain's first start inside the count (or from 05:30 when it was already running). "Saves/h" divides by the time
the chain was running, not by the clock. "Gap" is the median and the longest wait between two saves. "Stopped" is time inside the count with no run at all (a crash,
an internet outage, a stop by the yield watchdog, or the rest after the limit). "After 35" is how long the chain kept running after the 35th save until LinkedIn's banner stopped it.</p>
<div class="box"><table class="t"><thead><tr><th>Count</th><th>Ran</th><th class="n">Saved</th><th class="n">to 10</th><th class="n">to 20</th><th class="n">to 30</th><th class="n">to 35</th>
<th class="n">Saves/h</th><th class="n">First save after</th><th class="n">Gap median / longest</th><th class="n">Runs</th><th class="n">UAE pass</th><th class="n">Unrendered clicks</th>
<th class="n">Stopped</th><th class="n">After 35</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>"""


# ------------------------------------------------------------------ where each count's time went
_PART_CLASS = {"Clicking": "unr", "Opening": "opn", "Cards seen": "kwn", "Moving": "mov", "Changing": "pag", "Easy Apply forms": "frm", "Easy Apply attempts": "rej",
               "UAE": "uae", "Gaps": "rst", "Stopped": "stp", "Not explained": "nex"}


def _pclass(name: str) -> str:
    return next((c for k, c in _PART_CLASS.items() if name.startswith(k)), "nex")


def budget_section(r: dict) -> str:
    cov = [d for d in r["days"] if d.get("budget")][-4:]
    if not cov:
        return ""
    legend = "".join(f'<span><i class="sw p-{_pclass(p["name"])}"></i>{e(p["name"])}{tag(p["how"]) if p["how"] != "remainder" else ""}</span>' for p in cov[-1]["budget"]["parts"])
    bars = []
    for d in cov:
        b = d["budget"]
        segs = "".join(f'<i class="p-{_pclass(p["name"])}" style="width:{100 * p["min"] / b["wall"]:.2f}%" title="{e(p["name"])}: {hm(p["min"])}, {pc(p["min"], b["wall"])}"></i>'
                       for p in b["parts"] if p["min"] > 0)
        cap = (f'<b>{e(d["label"])}{" (running, so far)" if d["running"] else ""}</b><span>{d["saved"]} saved in {hm(b["wall"])}</span>'
               f'<span class="muted">{num(d["unrendered"])} unrendered clicks = {pc(part(d, "Clicking"), b["wall"])} of the count</span>')
        bars.append(f'<div class="stackrow"><div class="cap">{cap}</div><div class="stack">{segs}</div></div>')
    head = "".join(f'<th class="n">{e(d["label"])}</th>' for d in cov)
    trs = []
    for i, p in enumerate(cov[-1]["budget"]["parts"]):
        cells = "".join(f'<td class="n">{hm(d["budget"]["parts"][i]["min"])} <span class="dim">{pc(d["budget"]["parts"][i]["min"], d["budget"]["wall"])}</span></td>' for d in cov)
        trs.append(f'<tr><th scope="row">{e(p["name"])}{tag(p["how"]) if p["how"] != "remainder" else ""}</th>{cells}</tr>')
    totals = "".join(f'<td class="n">{hm(d["budget"]["wall"])}</td>' for d in cov)
    scales = [d for d in cov if d["budget"]["scale"] < 0.99]
    scale_note = (" The UAE pass runs the same code as the main pass, so its lines are in the same text log; for " + ", ".join(f"{e(d['label'])} (x{d['budget']['scale']})" for d in scales)
                  + " the estimated rows were scaled down so the parts do not add up to more than the count's length.") if scales else ""
    return f"""<section id="budget"><h2>Where each count's time went</h2>
<p>Each bar is one count, from the chain's first start to its last event. Solid segments are <b>measured</b> from timestamps. Hatched segments are <b>estimated</b>: the bot's own text log
has no times, so the number of lines of each kind is multiplied by a per-line cost fitted on {num(r['fit'].get('n', 0))} stretches between two timestamped skip lines
(fit quality R&sup2; {r['fit'].get('r2', 'n/a')}, typical miss {r['fit'].get('sd', 'n/a')} s per stretch). "Not explained" is what is left.{scale_note}</p>
{''.join(bars)}
<div class="legend">{legend}</div>
<div class="box"><table class="t"><thead><tr><th>Part</th>{head}</tr></thead><tbody>{''.join(trs)}
<tr class="sum"><th scope="row">Count length</th>{totals}</tr></tbody></table></div></section>"""


def why_section(r: dict) -> str:
    bs, fu, cost = r["burst_summary"], r["funnel"], r["fit"].get("cost", {})
    if not fu["unrendered"]:
        return ""
    cov_days = [d for d in r["days"] if d["key"] >= r["coverage_from"]]
    known = sum(d["prev_rejected"] + d["already_applied"] for d in cov_days)
    cards_all = fu["title"] + fu["badge"] + fu["pass"] + fu["unrendered"] + known
    return f"""<section id="why"><h2>Why cards are not drawn yet</h2>
<p>What the logs show. LinkedIn draws the job cards of a results list only while they are near the viewport; the rest are empty placeholders with no link inside. In the last {fu['days']} counts
the India pass met {num(fu['unrendered'])} of them, {pc(fu['unrendered'], cards_all)} of the {num(cards_all)} cards it handled. Clicking a placeholder fails ("Job-card click failed"); the bot discards the
dialog, tries the card once more and skips it, after the pacing wait: about {sec(cost.get('click_fail'))} each. They come in bursts, one results page at a time: {bs['burst_n']} stretches of ten or more in a row,
median {hm(bs['burst_median_s'] / 60)} long with {bs['burst_cards_median']} cards.</p>
<p>The cause is not fully pinned down. An independent read of one run (21 Oct) found 64 such stretches, 21 right after a card rejected on its title (rejected in half a second, with no scrolling) and 39 after a
card the bot had opened. The UAE pass, which scrolls each card into view and waits up to 2 s for its link before it decides anything, showed about 0.5% such failures in nine passes, against 28% of the cards
of that India run. So drawing every card first is expected to remove nearly all of them whatever the cause.</p></section>"""


# ------------------------------------------------------------------ what each step costs
def steps_section(r: dict) -> str:
    fit, cost, fu, gb, fl = r["fit"], r["fit"].get("cost", {}), r["funnel"], r["gap_by"], r["flow"]
    tc = r["title_clean"]
    meas = []

    def m(step, what, typ, n, extra=""):
        meas.append(f'<tr><th scope="row" class="w">{e(step)}<span class="id">{e(what)}</span></th><td class="n">{typ}</td><td class="n">{n}</td><td class="w dim">{extra}</td></tr>')

    if tc.get("n"):
        m("Title check", "A card whose title fails the fit rules is skipped without being opened.", f"{sec(tc['mean'])} each", num(fu["title"]),
          f"Two title rejects in a row, {tc['n']} pairs; 9 in 10 under {sec(tc['p90'])}. Timestamps have a one-second grain.")
    if gb.get("badge", {}).get("n"):
        g = gb["badge"]
        m("Opened card, rejected on the activity badge", "Pacing wait, click, read the badge. The gap before its skip line.", f"{sec(g['median'])} median", num(fu["badge"]),
          f"mean {sec(g['mean'])}, 9 in 10 under {sec(g['p90'])}; a longer gap holds unrendered clicks before it.")
    if gb.get("pass", {}).get("n"):
        g = gb["pass"]
        m("Opened card, passed the filters", "The gap before the pass line.", f"{sec(g['median'])} median", num(fu["pass"]),
          f"mean {sec(g['mean'])}, 9 in 10 under {sec(g['p90'])}; a longer gap holds unrendered clicks before it.")
    if fl["saved"].get("n"):
        g = fl["saved"]
        byq = "; ".join(f"{k}: {sec(v['median'])}" for k, v in sorted(fl["by_q"].items()) if v.get("n"))
        m("Passed, then saved", "Easy Apply form: from the pass line to the saved row.", f"{sec(g['median'])} median", num(g["n"]),
          f"9 in 10 under {sec(g['p90'])}, slowest {sec(g['max'])}. By number of questions, median: {byq}.")
    for k, g in sorted(fl["fail"].items(), key=lambda kv: -kv[1].get("n", 0)):
        if g.get("n"):
            m(f"Passed, then rejected: {k}", "From the pass line to the failed row.", f"{sec(g['median'])} median", num(g["n"]), f"9 in 10 under {sec(g['p90'])}.")
    rg = r["restart_gaps"]
    if rg.get("n"):
        m("Restart gap", "From a run's exit to the next run's launch, inside one supervisor session.", f"{sec(rg['median'])} median", num(rg["n"]),
          f"The supervisor waits 60 s; mean {sec(rg['mean'])}, longest {hm(rg['max'] / 60)} (waits for free memory). Since 1 Oct.")
    if r["uae"]:
        mins = [u["minutes"] for u in r["uae"] if u["minutes"] > 1]
        recent = [u["minutes"] for u in r["uae"][-6:] if u["minutes"] > 1]
        m("UAE pass", "A full pass over the UAE search results at the start of every bot start.", f"{statistics.median(recent or mins):.1f} min median, last six", num(len(r["uae"])),
          f"All {len(r['uae'])} passes since 1 Oct: median {statistics.median(mins):.1f} min, total {hm(sum(u['minutes'] for u in r['uae']))}.")
    covered = [d for d in r["days"] if d["key"] >= r["coverage_from"]]
    occ = {"click_fail": sum(d["unrendered"] for d in covered), "discard_extra": sum(d.get("discard_extra", 0) for d in covered), "trying": sum(d["opened"] for d in covered),
           "page": sum(d["pages"] for d in covered), "next_fail": sum(d.get("next_fail", 0) for d in covered), "ai_answer": sum(d.get("ai_answers", 0) for d in covered),
           "prev_rejected": sum(d.get("prev_rejected", 0) for d in covered), "already_applied": sum(d.get("already_applied", 0) for d in covered)}
    labels = [("click_fail", "Click on a card that is not drawn yet", "The click finds no link and fails; the bot discards, tries once more, then skips the card."),
              ("discard_extra", "Extra Discard attempts", "Discard buttons the bot searched for beyond the two a failed click needs."),
              ("trying", "Opening a card", "Pacing wait (8 to 20 s, average 14 s), click, read the badge."),
              ("prev_rejected", "Card the bot rejected before", "Opened after the pacing wait, then skipped: \"Skipping previously rejected\"."),
              ("already_applied", "Card the bot applied to before", "Opened after the pacing wait, then skipped: \"Already applied to\"."),
              ("page", "Changing the result page", "A search term's next results page."),
              ("next_fail", "Failed Next/Review lookup", "A form step whose button was not found in time."),
              ("ai_answer", "AI answer to a form question", "The answering call, per question.")]
    est_rows = []
    for k, step, what in labels:
        c = cost.get(k)
        if c is None:
            continue
        est_rows.append(f'<tr><th scope="row" class="w">{e(step)}<span class="id">{e(what)}</span></th><td class="n">{each(c)}</td><td class="n">{num(occ[k])}</td>'
                        f'<td class="n">{hm(occ[k] * c / 60) if c >= 0.5 else "n/a"}</td></tr>')
    est_rows.append(f'<tr><th scope="row" class="w">Moving on after a title or badge verdict<span class="id">What is left in each stretch once the lines above are counted.</span></th>'
                    f'<td class="n">{sec(fit.get("base_title"))} per title stretch, {sec(fit.get("base_badge"))} per badge stretch</td><td class="n">{num(fu["title"] + fu["badge"])}</td>'
                    f'<td class="n">{hm((fu["title"] * fit.get("base_title", 0) + fu["badge"] * fit.get("base_badge", 0)) / 60)}</td></tr>')
    cov_date = datetime.strptime(r["coverage_from"], "%Y-%m-%d")
    return f"""<section id="steps"><h2>What each step costs</h2>
<p>Two kinds of number. The first table is read straight from timestamps. The second comes from the fit: the bot's text log shows <i>that</i> a step happened but not <i>when</i>, so
each line type gets the per-line cost that best explains the time between timestamped lines. Counts are for the {fu['days']} counts since {cov_date:%d %b}, when the card-decision logs begin.</p>
<h3>Measured{tag('measured')}</h3>
<div class="box"><table class="t"><thead><tr><th>Step</th><th class="n">Time</th><th class="n">Times</th><th class="w">Detail</th></tr></thead><tbody>{''.join(meas)}</tbody></table></div>
<h3>Estimated per line{tag('estimated')}</h3>
<div class="box"><table class="t"><thead><tr><th>Step</th><th class="n">Cost per line</th><th class="n">Lines counted</th><th class="n">Time in total</th></tr></thead><tbody>{''.join(est_rows)}</tbody></table></div>
<p class="note">A failed Next/Review lookup and an AI answer get no cost of their own from the fit: once the measured form seconds are taken out, they explain nothing more.</p></section>"""


# ------------------------------------------------------------------ funnel
def funnel_section(r: dict) -> str:
    fu = r["funnel"]
    cards = fu["title"] + fu["badge"] + fu["pass"]
    if not cards:
        return ""
    rej = sum(fu["failed"].values())

    def row(label, n, cls=""):
        return (f'<div class="lab">{e(label)}</div><div><div class="bar {cls}" style="width:{100 * n / cards:.2f}%"></div></div>'
                f'<div class="val">{num(n)} &middot; {pc(n, cards)}</div>')

    bars = [row("Cards decided", cards, "dim"), row("Failed the title check", fu["title"]), row("Opened", fu["badge"] + fu["pass"], "dim"),
            row("Rejected on the activity badge", fu["badge"]), row("Passed the filters, tried", fu["pass"]), row("Saved", fu["saved"], "sign"), row("Rejected after opening", rej)]
    reasons = ", ".join(f"{e(k)} {v}" for k, v in sorted(fu["failed"].items(), key=lambda kv: -kv[1]))
    s = fu["saved"] or 1
    per = [("Cards decided", cards / s), ("Failed the title check", fu["title"] / s), ("Rejected on the badge", fu["badge"] / s), ("Passed and tried", fu["pass"] / s),
           ("Rejected after opening", rej / s), ("Clicks on cards not drawn yet", fu["unrendered"] / s), ("Result pages changed", fu["pages"] / s),
           ("Minutes the chain ran", fu["minutes"] / s)]
    ptr = "".join(f'<tr><th scope="row">{e(k)}</th><td class="n">{v:.1f}</td></tr>' for k, v in per)
    return f"""<section id="funnel"><h2>From cards to saved applications</h2>
<p>Everything the bot decided in the last {fu['days']} counts. Percentages are of all cards decided. "Rejected after opening" are cards that passed the badge check and then failed a
later rule or the form: {e(reasons)}.</p>
<div class="box"><div class="hb" style="padding:12px 0">{''.join(bars)}</div></div>
<h3>Per saved application</h3>
<div class="box"><table class="t" style="min-width:0;max-width:520px"><tbody>{ptr}</tbody></table></div></section>"""


# ------------------------------------------------------------------ gaps between saves
def gaps_section(r: dict) -> str:
    days = [d for d in r["days"] if d["key"] >= r["coverage_from"]]
    gaps = [g for d in days for g in d["gaps"]]
    if not gaps:
        return ""
    edges = [(0, 1), (1, 2), (2, 5), (5, 10), (10, 20), (20, 40), (40, 80), (80, 100000)]
    tot = sum(gaps)
    rows = []
    for lo, hi in edges:
        g = [x for x in gaps if lo <= x < hi]
        label = f"{lo} to {hi} min" if hi < 1000 else f"{lo} min or more"
        w = 100 * sum(g) / tot
        rows.append(f'<div class="lab">{label}</div><div><div class="bar {"sign" if lo >= 20 else ""}" style="width:{w:.2f}%"></div></div>'
                    f'<div class="val">{len(g)} gaps &middot; {hm(sum(g))} &middot; {w:.0f}%</div>')
    bs = r["burst_summary"]
    brows = "".join(f"<tr><td>{b['from']:%a %d %b %H:%M:%S}</td><td class=\"n\">{hm(b['seconds'] / 60)}</td><td class=\"n\">{b['unrendered']}</td><td class=\"n\">{b['opened']}</td><td class=\"n\">{b['pages']}</td></tr>"
                    for b in r["bursts"])
    return f"""<section id="gaps"><h2>Waiting between saves</h2>
<p>Every wait between two consecutive saves in the last {len(days)} counts ({len(gaps)} waits, {hm(tot)} in all), grouped by length. The bar is the share of the waiting <i>time</i>,
not of the number of waits: a few long waits hold most of it.</p>
<div class="box"><div class="hb" style="padding:12px 0">{''.join(rows)}</div></div>
<p>The long waits are mostly unrendered cards. The text log can be lined up with timestamps for {hm(bs['aligned_min'])} of running time; in it, {bs['burst_n']} stretches with ten
or more unreadable cards in a row took {hm(bs['burst_min'])} ({pc(bs['burst_min'], bs['aligned_min'])}). The median such stretch lasted {hm(bs['burst_median_s'] / 60)} and held {bs['burst_cards_median']} cards.
The ten longest stretches between two timestamped skip lines:</p>
<div class="box"><table class="t"><thead><tr><th>Started</th><th class="n">Length</th><th class="n">Unrendered clicks inside</th><th class="n">Cards opened inside</th><th class="n">Page changes inside</th></tr></thead>
<tbody>{brows}</tbody></table></div></section>"""


# ------------------------------------------------------------------ timeline
def timeline_svg(r: dict, days: list[dict]) -> str:
    now = datetime.now()
    w0s = {d["key"]: w0_of(d) for d in days}
    span_h = min(26, max(1, max(math.ceil((d["end"] - w0s[d["key"]]).total_seconds() / 3600) for d in days)))
    W, L, R, lane = 1100, 100, 14, 116
    H = 26 + lane * len(days) + 10
    pw = W - L - R

    def X(t, w0):
        return L + max(0.0, min(span_h * 3600.0, (t - w0).total_seconds())) / (span_h * 3600.0) * pw

    s = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Timeline of the latest counts: runs, UAE passes, unrendered stretches, saves">',
         '<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="var(--paper)"/>'
         '<line x1="0" y1="0" x2="0" y2="6" stroke="var(--ink)" stroke-width="2"/></pattern></defs>']
    base = w0s[days[0]["key"]]
    for h in range(0, span_h + 1, 2):
        x = L + h / span_h * pw
        s.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="22" y2="{H - 6}" stroke="var(--grid)" stroke-width="1"/>')
        s.append(f'<text x="{x:.1f}" y="14" text-anchor="middle">{(base + timedelta(hours=h)):%H:%M}</text>')
    for i, d in enumerate(days):
        y0 = 26 + i * lane
        w0 = w0s[d["key"]]
        s.append(f'<text x="{L}" y="{y0 + 12}" class="b">{e(d["label"])} &middot; {d["saved"]} saved &middot; {e(hm(d["wall"]))}</text>')
        for off, cap in ((20, "runs"), (38, "UAE pass"), (56, "unrendered"), (80, "saves")):
            s.append(f'<text x="{L - 6}" y="{y0 + off + 9}" text-anchor="end">{cap}</text>')
        for a in d["attempts"]:
            x1, x2 = X(a["start"], w0), X(a["end"] or now, w0)
            tip = f"run {a['start']:%H:%M}-{(a['end'] or now):%H:%M}, exit {'running' if a['running'] else a['rc']}, saved {a['saved'] if a['saved'] is not None else '?'}"
            s.append(f'<g><title>{e(tip)}</title><rect x="{x1:.1f}" y="{y0 + 20}" width="{max(1.5, x2 - x1):.1f}" height="12" fill="var(--ink)" opacity=".85"/></g>')
        for u in d["uae"]:
            x1, x2 = X(u["start"], w0), X(u["end"], w0)
            s.append(f'<g><title>UAE pass {u["start"]:%H:%M}, {u["minutes"]} min, {u["cards"]} cards</title><rect x="{x1:.1f}" y="{y0 + 38}" width="{max(1.5, x2 - x1):.1f}" height="12" fill="url(#hatch)" stroke="var(--ink)"/></g>')
        for b in r["bands"]:
            if w0 <= b["from"] < w0 + timedelta(hours=24):
                x1, x2 = X(b["from"], w0), X(b["to"], w0)
                s.append(f'<g><title>{b["unrendered"]} unrendered clicks, {b["from"]:%H:%M:%S}-{b["to"]:%H:%M:%S}</title><rect x="{x1:.1f}" y="{y0 + 56}" width="{max(1.5, x2 - x1):.1f}" height="12" fill="var(--sign)" stroke="var(--ink)" stroke-width=".6"/></g>')
        for t in d["failed_times"]:
            x = X(t, w0)
            s.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y0 + 94}" y2="{y0 + 102}" stroke="var(--muted)" stroke-width="1"/>')
        for t in d["saved_times"]:
            x = X(t, w0)
            s.append(f'<g><title>saved {t:%H:%M:%S}</title><line x1="{x:.1f}" x2="{x:.1f}" y1="{y0 + 76}" y2="{y0 + 102}" stroke="var(--ink)" stroke-width="1.6"/></g>')
        s.append(f'<line x1="{L}" x2="{W - R}" y1="{y0 + lane - 4}" y2="{y0 + lane - 4}" stroke="var(--grid)" stroke-width="1"/>')
    s.append("</svg>")
    return "".join(s)


def timeline_section(r: dict) -> str:
    days = [d for d in r["days"] if d["key"] >= r["coverage_from"]][-3:]
    if not days:
        return ""
    return f"""<section id="timeline"><h2>The latest counts, minute by minute</h2>
<p>One lane per count, all on the same clock starting at 05:30. Dark bars are runs of the bot (hover for the exit code and saves), hatched bars are UAE passes, yellow marks stretches
where ten or more cards in a row could not be clicked, and the ticks are saves (tall) and rejected applications (short).</p>
<div class="box">{timeline_svg(r, days)}</div></section>"""


# ------------------------------------------------------------------ how a count fills up
def curves_svg(r: dict, days: list[dict]) -> str:
    W, H, L, R, T, B = 1100, 330, 46, 170, 16, 30
    xmax = 600
    s = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Saved applications against minutes since the chain started, one line per count">']

    def X(m):
        return L + min(m, xmax) / xmax * (W - L - R)

    def Y(v):
        return H - B - v / 36 * (H - T - B)

    for v in (0, 10, 20, 30, 35):
        s.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--grid)"/><text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{v}</text>')
    for m in range(0, xmax + 1, 60):
        s.append(f'<line x1="{X(m):.1f}" x2="{X(m):.1f}" y1="{T}" y2="{H - B}" stroke="var(--grid)"/><text x="{X(m):.1f}" y="{H - 10}" text-anchor="middle">{m // 60} h</text>')
    labels = []
    for i, d in enumerate(days):
        steps = d["saved_min"]
        pts = [f"M{X(0):.1f},{Y(0):.1f}"]
        for k, m in enumerate(steps, start=1):
            pts.append(f"L{X(m):.1f},{Y(k - 1):.1f} L{X(m):.1f},{Y(k):.1f}")
        path = " ".join(pts)
        latest = i == len(days) - 1
        op = 0.25 + 0.75 * (i + 1) / len(days)
        if latest:
            s.append(f'<path d="{path}" fill="none" stroke="var(--ink)" stroke-width="5" stroke-linejoin="round"/><path d="{path}" fill="none" stroke="var(--sign)" stroke-width="2.6" stroke-linejoin="round"/>')
        else:
            s.append(f'<path d="{path}" fill="none" stroke="var(--ink)" stroke-width="1.6" opacity="{op:.2f}" stroke-linejoin="round"/>')
        end_m, end_v = (steps[-1] if steps else 0), len(steps)
        short = d["label"].split(" ", 1)[1]
        if end_m > xmax:
            at = sum(1 for m in steps if m <= xmax)
            labels.append([Y(at), X(xmax) + 6, f"{short}: {end_v} at {hm(end_m)} >", latest])
        else:
            labels.append([Y(end_v), X(end_m) + 6, short, latest])
    labels.sort(key=lambda t: t[0])
    prev = -99.0
    for lab in labels:
        lab[0] = max(lab[0], prev + 13)
        prev = lab[0]
        cls = ' class="b"' if lab[3] else ""
        s.append(f'<text x="{lab[1]:.1f}" y="{lab[0] + 4:.1f}"{cls}>{e(lab[2])}</text>')
    s.append("</svg>")
    return "".join(s)


def curves_section(r: dict) -> str:
    days = [d for d in r["days"] if d["saved"] >= 10][-8:]
    if not days:
        return ""
    return f"""<section id="curves"><h2>How a count fills up</h2>
<p>Saved applications against minutes since the chain started, for the last {len(days)} counts that saved 10 or more. A steep line is a fast count, a flat stretch is a wait. The newest count is yellow;
a line that leaves the right edge needed more than ten hours, and its label says when it got to the end.</p>
<div class="box">{curves_svg(r, days)}</div></section>"""


# ------------------------------------------------------------------ hour of day
def hours_section(r: dict) -> str:
    h = r["heat"]
    if not h["rows"]:
        return ""
    mx = max([max(x["cells"]) for x in h["rows"]] + [1])

    def cell(v):
        a = 0 if v == 0 else 12 + 70 * v / mx
        col = "var(--paper)" if a > 48 else "var(--ink)"
        return f'<td style="background:color-mix(in srgb, var(--ink) {a:.0f}%, transparent);color:{col}">{v if v else ""}</td>'

    head = "".join(f"<th>{x:02d}</th>" for x in h["hours"])
    rows = "".join(f'<tr><td class="l">{e(x["label"])}</td>{"".join(cell(v) for v in x["cells"])}</tr>' for x in h["rows"])
    dmx = max(h["decisions"] + [1])
    drow = "".join(f'<td style="background:color-mix(in srgb, var(--muted) {100 * v / dmx * .7:.0f}%, transparent);color:var(--ink)">{v if v else ""}</td>' for v in h["decisions"])
    return f"""<section id="hours"><h2>Saves by hour of day</h2>
<p>Saved applications per clock hour for the last {len(h['rows'])} counts, then the cards decided in the same hours (all counts since {r['coverage_from']}). The count starts at 05:30, so the first column is a half hour.</p>
<div class="box"><table class="t heat"><thead><tr><th class="l">Count</th>{head}</tr></thead><tbody>{rows}
<tr><td class="l muted">Cards decided</td>{drow}</tr></tbody></table></div></section>"""


# ------------------------------------------------------------------ runs, UAE, freshness
def runs_section(r: dict) -> str:
    rows = []
    for d in r["days"][-4:][::-1]:
        for a in d["attempts"][::-1]:
            end = "running" if a["running"] else f"{a['end']:%H:%M}" if a["end"] else ""
            rc = "" if a["running"] else (f"{a['rc']} {lispeed.RC_WORDS.get(a['rc'], '')}".strip() if a["rc"] is not None else "no exit line (killed)")
            carry = " (started before 05:30)" if a.get("carry") else ""
            rows.append(f"<tr><td>{e(d['label'])}</td><td>{a['start']:%H:%M}&ndash;{end}{carry}</td><td class=\"n\">{hm(a['minutes'])}</td><td>{e(rc)}</td>"
                        f"<td class=\"n\">{a['saved'] if a['saved'] is not None else ''}</td><td class=\"n\">{num(a['click_fail']) if a['click_fail'] is not None else ''}</td>"
                        f"<td class=\"n\">{num(a['opened']) if a['opened'] is not None else ''}</td><td class=\"w dim\">{e(a.get('note') or '')}</td></tr>")
    return f"""<section id="runs"><h2>Runs and restarts</h2>
<p>The supervisor starts the bot, and starts it again when it exits. Exit 0 means all search terms were worked through; 44 means LinkedIn's banner stopped the day; 1 means the supervisor stopped
a run (see the note). Every start repeats the UAE pass, the sign-in check and the first search results.</p>
<div class="box"><table class="t"><thead><tr><th>Count</th><th>Run</th><th class="n">Length</th><th>Exit</th><th class="n">Saved</th><th class="n">Unrendered clicks</th>
<th class="n">Cards opened</th><th class="w">Note</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>"""


def uae_section(r: dict) -> str:
    u = r["uae"]
    if not u:
        return ""
    rows = "".join(f"<tr><td>{x['start']:%a %d %b %H:%M}</td><td class=\"n\">{x['minutes']}</td><td class=\"n\">{x['cards'] if x['cards'] is not None else ''}</td>"
                   f"<td class=\"n\">{x['visits'] if x['visits'] is not None else ''}</td><td class=\"n\">{x['applied']}</td><td>{e(str(x['mode'] or ''))}</td></tr>" for x in u[-10:][::-1])
    mins = [x["minutes"] for x in u if x["minutes"] > 1]
    return f"""<section id="uae"><h2>The UAE pass</h2>
<p>At the start of every bot start the bot works through the UAE results before the India search. {len(u)} passes since 1 Oct, {hm(sum(x['minutes'] for x in u))} in all, median {statistics.median(mins):.1f} min,
{sum(x['applied'] for x in u)} applications. The last ten:</p>
<div class="box"><table class="t"><thead><tr><th>Started</th><th class="n">Minutes</th><th class="n">Cards</th><th class="n">Visits</th><th class="n">Applied</th><th>Mode</th></tr></thead><tbody>{rows}</tbody></table></div></section>"""


def fresh_section(r: dict) -> str:
    f = r["fresh"]
    if not f["n"]:
        return ""
    bars = "".join(f'<div class="lab">posted within {k} h</div><div><div class="bar" style="width:{v}%"></div></div><div class="val">{v}%</div>' for k, v in sorted(f["within"].items()))
    return f"""<section id="fresh"><h2>How old the jobs are when they are applied to</h2>
<p>Hours from LinkedIn's posting time to the saved application, last 21 days, {f['n']} saves. Median {f['median_h']} h. A bot that is slow to reach a card applies to older jobs.</p>
<div class="box"><div class="hb" style="padding:12px 0">{bars}</div></div></section>"""


def settings_section(r: dict) -> str:
    rows = "".join(f'<tr><th scope="row" class="w">{e(s["name"])}</th><td class="n">{e(s["value"])}</td><td class="w dim">{e(s["what"])}</td><td class="dim">{e(s["source"])}</td></tr>' for s in r["settings"])
    h = r["hardening"]
    return f"""<section id="settings"><h2>Waits and limits built into the bot</h2>
<p>Read from the bot's source files each time this page is computed, so they cannot drift from what runs. Limits that LinkedIn sets: 35 Easy Apply submissions per count, the count resets at 05:30 IST.</p>
<div class="box"><table class="t"><thead><tr><th>Setting</th><th class="n">Value</th><th class="w">What it does</th><th>File</th></tr></thead><tbody>{rows}</tbody></table></div>
<p class="note">Since 1 Oct the hardening log recorded {h['reconcile']} official count checks, {h['discard_reload']} page reloads after a failed Discard, {h['easy_click']} Easy Apply click retries and {h['uae_failed']} failed UAE passes.</p></section>"""


def ideas_section(r: dict) -> str:
    cov = [d for d in r["days"] if d.get("what_if")]
    if not cov:
        return ""
    fu, cc = r["funnel"], r["card_costs"]
    rows = []
    for d in cov:
        w = d["what_if"]
        rows.append(f"<tr><th scope=\"row\">{e(d['label'])}</th><td class=\"n\">{hm(w['to35_now'])}</td>"
                    f"<td class=\"n\">{num(w['extra_cards'])}</td><td class=\"n\">{hm(w['unrendered_min'])} &rarr; {hm(w['if_min'])}</td><td class=\"n\">about {hm(w['to35_without'])}</td>"
                    f"<td class=\"n\">{num(w['known_n'])}</td><td class=\"n\">{hm(w['known_min'])} &rarr; {hm(w['known_if_min'])}</td><td class=\"n\">about {hm(w['to35_both'])}</td></tr>")
    full = cov[-1]
    pace = full["opened"] * 14 / 60
    uae_day = [d for d in r["days"] if d["uae_minutes"]][-1:] or [None]
    uae_line = (f" The UAE pass cost {hm(uae_day[0]['uae_minutes'])} on {e(uae_day[0]['label'])} and applied {uae_day[0]['uae_applied']} times." if uae_day[0] else "")
    miss = r["hardening"].get("render_miss", 0)
    return f"""<section id="ideas"><h2>What the numbers say would change the speed</h2>
<p><b>1. Draw each card before clicking it.</b> A card the bot reaches before LinkedIn has drawn it has no link to click, so the click fails, the bot discards and tries again, and skips it. If those cards were drawn
first they would cost what an ordinary card costs: about {sec(cc['avg'])} on average ({sec(cc['title'])} when the title fails, {sec(cc['badge'])} when opened), plus about 1 s to draw it. They would also be
{num(sum(d['what_if']['extra_cards'] for d in cov))} more candidates, of which about {fu['saves_per_card'] * 100:.1f}% have ended up saved.</p>
<p><b>2. Skip a card the bot has seen before, before the pacing wait.</b> A card the bot already applied to or rejected earlier is only recognised after it has been opened, so it still waits out the 8 to 20 s pacing wait;
the UAE pass decides these from the card's job id first. They show up because the same searches run again and again over a limited supply of new jobs.</p>
<div class="box"><table class="t"><thead><tr><th>Count</th><th class="n">Time to 35 now</th><th class="n">Cards not drawn yet</th><th class="n">Minutes on them: now &rarr; if ordinary</th><th class="n">Time to 35 after change 1</th>
<th class="n">Cards seen before</th><th class="n">Minutes on them: now &rarr; if skipped at once</th><th class="n">Time to 35 after both</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="note">All of this is an estimate from the fitted costs, and the two changes overlap a little. Each is a separate change to the live bot, so they go in on different days.
The pacing wait itself (8 to 20 s before every opened card) is deliberate; it was {hm(pace)} of the {e(full['label'])} count (about {full['opened']} cards at 14 s). Shortening it would be faster and is a trade against LinkedIn
noticing a pattern, so it is a decision, not a fix.{uae_line}{f' The card-render step has logged {miss} cards that stayed undrawn after 2 s.' if miss else ''}</p></section>"""


def method_section(r: dict) -> str:
    f = r["fit"]
    rows = "".join(f'<tr><th scope="row">{e(k.replace("_", " "))}</th><td class="n">{v:.1f} s</td></tr>' for k, v in f.get("cost", {}).items())
    cov_date = datetime.strptime(r["coverage_from"], "%Y-%m-%d")
    return f"""<section id="method"><h2>How this is measured</h2>
<details class="fold" id="method-details"><summary>Sources, method and limits</summary>
<p><b>Measured</b> means a timestamp pair: the supervisor log (when each run started and ended), the applied and failed history files, the logs of every card decision (title check,
activity badge, pass), the UAE pass log and the hardening log. <b>Estimated</b> means the bot's untimestamped text log, lined up with those timestamps through the job id printed on every
skip line; between two timestamped skip lines the text log shows which kinds of lines occurred, and a non-negative least-squares fit gives each kind its cost in seconds. The
form seconds (pass line to saved row) are measured and taken out of the stretches before fitting.</p>
<p>Fit: {num(f.get('n', 0))} stretches ({f.get('dropped', 0)} outliers over 3 standard deviations dropped), R&sup2; {f.get('r2', 'n/a')}, typical miss {f.get('sd', 'n/a')} s per stretch. Per-line costs:</p>
<div class="box"><table class="t" style="min-width:0;max-width:420px"><tbody>{rows}</tbody></table></div>
<ul class="short">
<li>A count is LinkedIn's day for the 35-submission limit, 05:30 IST to 05:30 IST (measured: the banner appeared after exactly 35 in every count that reached it). A chain that is running at 05:30 is split there.</li>
<li>The card-decision logs begin on {cov_date:%d %b}; the minutes split and the per-line costs only exist from then. The count of unrendered clicks reaches back to 1 Oct, because it needs no timestamps.</li>
<li>Each supervisor attempt is paired with its run in the text log by order, and the pair is dropped when its "Successfully saved" lines disagree with the supervisor's saved count by more than one ({r['dropped_pairs']} dropped).</li>
<li>The per-click cost was fitted on the last three counts. For earlier counts the page shows the number of unrendered clicks, never minutes.</li>
<li>The UAE pass runs the same code as the main pass, so its lines are inside the text log too; where the parts would add up to more than the count, the estimated rows are scaled down.</li>
<li>Timestamps have a one-second grain, so a 0.5 s title check is an average over many.</li>
<li>All times are IST, this PC's clock. Computed from the logs every 10 minutes; the numbers on this page are from {r['generated']:%H:%M:%S}.</li>
</ul></details></section>"""


NAV = [("path", "Path of a card"), ("trend", "Time to 35"), ("days", "Every count"), ("budget", "Where time went"), ("why", "Undrawn cards"), ("steps", "Step costs"),
       ("funnel", "Funnel"), ("gaps", "Waits"), ("timeline", "Timeline"), ("curves", "Filling up"), ("hours", "Hours"), ("runs", "Runs"), ("uae", "UAE pass"),
       ("fresh", "Freshness"), ("settings", "Built-in waits"), ("ideas", "What would help"), ("method", "Method")]


# ------------------------------------------------------------------ page
def page() -> bytes:
    data, at, err, busy = lispeed.get()
    if data is None:
        msg = (f"<p class=\"sign\" style=\"padding:8px\">The first computation failed: {e(err)}</p>" if err else
               "<p>Reading about 40 MB of the bot's logs for the first time since the board started. This takes about 15 seconds; the page reloads itself.</p>")
        return quality._frame(f"<h1>LinkedIn speed</h1>{msg}<script>setTimeout(function(){{location.reload()}},{'60000' if err else '6000'})</script>",
                              datetime.now().strftime("%H:%M:%S"), title="LinkedIn speed", extra_css=_CSS)
    r = data
    stale = (f'<p class="sign" style="padding:8px">The last refresh failed ({e(err)}); these numbers are from {at:%H:%M:%S}.</p>' if err else "")
    nav = " ".join(f'<a href="#{k}">{e(v)}</a>' for k, v in NAV)
    inner = f"""<header><h1>LinkedIn speed</h1>
<p class="lede">Where the LinkedIn auto-applier's time goes, from the bot's own logs. A <b>count</b> is LinkedIn's day for its limit of 35 Easy Apply submissions, 05:30 to 05:30 IST.
Everything is IST. Computed <span id="computed">{r['generated']:%H:%M:%S}</span>, again every 10 minutes. {tag('measured')} is read from timestamps, {tag('estimated')} is the text log's line counts times a fitted cost; the last section explains how.
See also <a href="/week">Week report</a> and <a href="/quality">Match quality</a>.</p></header>
{stale}
<nav class="jump" aria-label="Sections">{nav}</nav>
<section><h2>In short</h2>{short_section(r)}</section>
<section>{cards_section(r)}</section>
{path_section(r)}
{trend_section(r)}
{days_section(r)}
{budget_section(r)}
{why_section(r)}
{steps_section(r)}
{funnel_section(r)}
{gaps_section(r)}
{timeline_section(r)}
{curves_section(r)}
{hours_section(r)}
{runs_section(r)}
{uae_section(r)}
{fresh_section(r)}
{settings_section(r)}
{ideas_section(r)}
{method_section(r)}"""
    return quality._frame(inner, r["generated"].strftime("%H:%M:%S"), title="LinkedIn speed", extra_css=_CSS)


def report_json() -> dict:
    data, at, err, busy = lispeed.get()
    return {"computed_at": at, "error": err, "computing": busy, "data": data}
