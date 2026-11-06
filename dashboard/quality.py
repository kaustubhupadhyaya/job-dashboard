"""Match quality: every saved application of the last N days, graded by the shared fit rules
(portal_kit/fit.py, rules in ~/.secrets/fit_rules.json), plus how the Naukri chat windows were answered.

Replaces the static audit.html of 2026-10-05, whose numbers were a one-off snapshot (and partly invented). Everything
here is computed from the bots' own files on each request; nothing is written. An open page re-reads itself every
minute (see _LIVE_JS), the way the board polls /api/status, and says so plainly when it cannot.
"""
from __future__ import annotations

import html
import importlib.util
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import paths
except ImportError:     # imported as part of the package (python -m dashboard.quality)
    from . import paths

HERE = Path(__file__).resolve().parent
FIT_PATH = HERE.parent / "portal_kit" / "fit.py"
NK = paths.NK
NAMES = {"naukri1": "Naukri 1", "naukri2": "Naukri 2", "instahyre1": "Instahyre 1", "instahyre2": "Instahyre 2",
         "linkedin": "LinkedIn"}
IDENT = {"naukri1": "Data Platform", "naukri2": "AI Engineer", "instahyre1": "Data Platform", "instahyre2": "AI Engineer",
         "linkedin": "either"}
CHAT_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) [\d:,]+ - \w+ - \[chat-v2\] q#\d+ kind=(\S+) src=(\S+) verify=(\S+) "
                       r"ms=\d+ q='(.*)' a='(.*)'$")
YEARS_Q = re.compile(r"how many years|years of experience|number of years|years? (?:of )?exp", re.I)


def _fit():
    """Loaded fresh on each request, so a rules or code change shows without restarting the board."""
    spec = importlib.util.spec_from_file_location("jt_fit_live", FIT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tail_text(path: Path, max_bytes: int) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def chat_answers(since: str) -> dict:
    """Counts from the Naukri bots' own [chat-v2] log lines (both accounts)."""
    out = {}
    for key, log in (("naukri1", NK / "naukri_bot.log"), ("naukri2", NK / "profiles" / "naukri2" / "naukri_bot.log")):
        c = {"questions": 0, "years_questions": 0, "answered_3_years": 0, "default_yes": 0, "by_source": {}}
        for ln in _tail_text(log, 6_000_000):
            m = CHAT_LINE.match(ln)
            if not m or m.group(1) < since:
                continue
            _, kind, src, _ok, q, a = m.groups()
            c["questions"] += 1
            c["by_source"][src] = c["by_source"].get(src, 0) + 1
            if YEARS_Q.search(q):
                c["years_questions"] += 1
                if a.strip().lower().startswith("3"):
                    c["answered_3_years"] += 1
            if a.strip() == "Yes" and re.search(r"experience|worked|familiar|knowledge|hands[- ]on", q, re.I):
                c["default_yes"] += 1
        out[key] = c
    return out


def report(days: int = 7) -> dict:
    fit = _fit()
    since = (date.today() - timedelta(days=days)).isoformat()
    rules = fit.load_rules()
    data = fit.stored_applications(since)
    rows = {}
    for applier, jobs in data.items():
        counts = {"good": 0, "stretch": 0, "reject": 0}
        reasons: dict[str, int] = {}
        examples: dict[str, list[str]] = {}
        per_day: dict[str, dict] = {}
        for j in jobs:
            v = fit.evaluate_for(applier, j, rules)
            counts[v.bucket] += 1
            if v.bucket == "reject" and v.reasons:
                k = re.sub(r"\s*\(.*\)$", "", v.reasons[0])
                k = re.sub(r"\d+(\.\d+)?", "N", k)
                reasons[k] = reasons.get(k, 0) + 1
                ex = examples.setdefault(k, [])
                if len(ex) < 3:
                    ex.append(j.title[:60])
        rows[applier] = {"applied": len(jobs), **counts,
                         "reasons": [{"reason": k, "count": n, "examples": examples.get(k, [])}
                                     for k, n in sorted(reasons.items(), key=lambda x: -x[1])[:6]]}
    return {"since": since, "days": days, "rules_version": rules.version, "generated": datetime.now().isoformat(timespec="seconds"),
            "appliers": rows, "chat": chat_answers(since)}


# The saved theme of the board (static/app.js), applied before the first paint.
_THEME_JS = ("<script>try { var t = localStorage.getItem('board-theme'); "
             "if (t === 'dark' || t === 'light') document.documentElement.dataset.theme = t; } "
             "catch (e) { /* private window */ }</script>")

_LIVE_CSS = """.q .bar { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; color: var(--muted); }
.q .bar .textbtn { margin-left: 0; }
.q .live { font-size: 14px; }
.q .live.sign { padding: 2px 8px; }
.q #body { display: grid; gap: 24px; }"""

_LIVE_JS = r"""<script>
/* Keeps an open page current: reads /quality again every minute and swaps in the new numbers, the way the board polls
   /api/status. A failed read is shown, never hidden; three minutes without a success turn the status into the board's
   yellow sign. Polling pauses while the tab is hidden. */
(function () {
  var POLL_MS = 60000, STALE_MS = 180000, TICK_MS = 15000;
  var live = document.getElementById('live'), btn = document.getElementById('refresh');
  var lastOk = Date.now(), failedAt = 0, busy = false;
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function clock(ms) { var d = new Date(ms); return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()); }
  function sig(root) {   // the report without its "Computed" stamp and open/closed state, so an unchanged report is not rebuilt
    var c = root.cloneNode(true), s = c.querySelector('#computed');
    if (s) s.textContent = '';
    Array.prototype.forEach.call(c.querySelectorAll('details[open]'), function (d) { d.removeAttribute('open'); });
    return c.innerHTML;
  }
  function paint() {
    var age = Date.now() - lastOk;
    if (busy) { live.className = 'live'; live.textContent = 'Updating...'; return; }
    if (age > STALE_MS) {
      live.className = 'live sign';
      live.textContent = 'Stopped updating at ' + clock(lastOk) + '. Reload this page, or start the board with job-tracker\\dashboard.cmd.';
    } else if (failedAt) {
      live.className = 'live';
      live.textContent = 'Could not refresh at ' + clock(failedAt) + '; showing the ' + clock(lastOk) + ' numbers.';
    } else {
      live.className = 'live';
      live.textContent = 'Updated ' + clock(lastOk) + (age >= 10000 ? ', ' + Math.round(age / 1000) + ' s ago' : '') + '. Refreshes every minute.';
    }
  }
  async function refresh() {
    if (busy) return;
    busy = true; btn.disabled = true; paint();
    try {
      var res = await fetch(location.pathname, { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var doc = new DOMParser().parseFromString(await res.text(), 'text/html');
      var fresh = doc.getElementById('body'), mine = document.getElementById('body');
      if (!fresh) throw new Error('not the report page');
      if (sig(fresh) === sig(mine)) {
        var a = doc.getElementById('computed'), b = document.getElementById('computed');
        if (a && b) b.textContent = a.textContent;
      } else {
        var openIds = Array.prototype.map.call(mine.querySelectorAll('details[open][id]'), function (d) { return d.id; });
        mine.innerHTML = fresh.innerHTML;
        openIds.forEach(function (id) { var d = document.getElementById(id); if (d) d.open = true; });
      }
      lastOk = Date.now(); failedAt = 0;
    } catch (e) {
      failedAt = Date.now();
    }
    busy = false; btn.disabled = false; paint();
  }
  btn.addEventListener('click', refresh);
  document.addEventListener('visibilitychange', function () { if (!document.hidden && Date.now() - lastOk >= POLL_MS) refresh(); });
  setInterval(function () { if (!document.hidden) refresh(); }, POLL_MS);
  setInterval(paint, TICK_MS);
  paint();
})();
</script>"""


def _frame(inner: str, clock: str, title: str = "Match quality", extra_css: str = "") -> bytes:
    """The page around the report. The 'unavailable' message uses it too, so an open page recovers by itself once the rules
    are readable again. Without JavaScript this is the plain, server-rendered page it always was. The week report (week.py)
    reuses it with its own title and CSS; the refresh script reads whatever page it sits on."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><link rel="stylesheet" href="/app.css">
{_THEME_JS}
<style>
.q {{ max-width: 1180px; margin: 0 auto; padding-block: 16px 48px; display: grid; gap: 24px; }}
.q h1 {{ margin: 0; font-size: 36px; line-height: 40px; font-weight: 600; }}
.q h2 {{ margin: 0; font-size: 20px; font-weight: 600; }}
.q p {{ margin: 0; max-width: 90ch; color: var(--muted); }}
.box {{ overflow-x: auto; border-top: 2px solid var(--ink); }}
.q table {{ border-collapse: collapse; width: 100%; min-width: 760px; }}
.q th, .q td {{ text-align: left; vertical-align: top; padding: 8px 10px; border-bottom: 1px solid var(--grid); }}
.q thead th {{ font-size: 12px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); font-weight: 600; }}
.q .n {{ text-align: right; white-space: nowrap; }}
.q .pc {{ color: var(--muted); font-size: 12px; }}
.q .id {{ display: block; color: var(--muted); font-weight: 400; font-size: 12px; }}
.q .sign {{ background: var(--sign); color: var(--sign-ink); }}
.q .sign .pc {{ color: var(--sign-ink); }}
.q ul.why {{ margin: 0; padding-left: 18px; }}
.q .ex {{ color: var(--muted); font-size: 12px; }}
{_LIVE_CSS}
{extra_css}
</style></head><body><main class="q">
<div class="bar"><a href="/">Back to the board</a><span id="live" class="live">Computed {html.escape(clock)}. Reload to update.</span><button id="refresh" type="button" class="textbtn">Refresh now</button></div>
<div id="body">
{inner}
</div></main>
{_LIVE_JS}
</body></html>""".encode("utf-8")


def page(days: int = 7) -> bytes:
    e = html.escape
    try:
        r = report(days)
    except Exception as ex:   # rules missing or unreadable: say so plainly
        return _frame(f"<h1>Match quality is unavailable</h1><p>{e(str(ex))}</p>", datetime.now().strftime("%H:%M:%S"))
    trs = []
    for a, s in r["appliers"].items():
        n = s["applied"] or 1
        pct = lambda k: f"{100 * s[k] / n:.0f}%"
        why = "".join(f"<li>{e(x['reason'])}: <b>{x['count']}</b> <span class=ex>{e('; '.join(x['examples']))}</span></li>"
                      for x in s["reasons"])
        trs.append(f"<tr><th scope=row>{e(NAMES.get(a, a))}<span class=id>{e(IDENT.get(a, ''))}</span></th>"
                   f"<td class=n>{s['applied']}</td><td class=n>{s['good']} <span class=pc>{pct('good')}</span></td>"
                   f"<td class=n>{s['stretch']} <span class=pc>{pct('stretch')}</span></td>"
                   f"<td class='n {'sign' if s['reject'] / n > 0.1 else ''}'>{s['reject']} <span class=pc>{pct('reject')}</span></td>"
                   f"<td><ul class=why>{why or '<li>none</li>'}</ul></td></tr>")
    chat = []
    for a, c in r["chat"].items():
        src = ", ".join(f"{k} {v}" for k, v in sorted(c["by_source"].items(), key=lambda x: -x[1]))
        chat.append(f"<tr><th scope=row>{e(NAMES[a])}</th><td class=n>{c['questions']}</td><td class=n>{c['years_questions']}</td>"
                    f"<td class=n>{c['answered_3_years']}</td><td class=n>{c['default_yes']}</td><td>{e(src)}</td></tr>")
    inner = f"""<header><h1>Match quality</h1>
<p>Every application saved since {e(r['since'])}, graded by the shared fit rules (version {e(r['rules_version'])}): minimum
experience at most 5 years, no Lead/Staff/Principal/Head/Manager/Architect titles, no job built on skills you don't have,
each Naukri and Instahyre account in its own identity. <b>Reject</b> is what the gate skips before any Apply click once it
is live in that bot; until then it measures what still gets through. Computed <span id="computed">{e(r['generated'][11:19])}</span> from the bots'
own files.</p></header>
<section><h2>Applications, last {r['days']} days</h2><div class="box"><table>
<thead><tr><th>Applier</th><th class="n">Saved</th><th class="n">Good</th><th class="n">Stretch</th><th class="n">Reject</th>
<th>Main reasons for reject, with examples</th></tr></thead><tbody>{''.join(trs)}</tbody></table></div></section>
<section><h2>Naukri screening answers, last {r['days']} days</h2>
<p>From the bots' own chat-window log lines. Years-of-skill questions are answered "3" by your decision; the fit gate's job
is to keep such questions about skills you lack from appearing at all.</p>
<div class="box"><table><thead><tr><th>Account</th><th class="n">Questions</th><th class="n">Years questions</th>
<th class="n">Answered 3</th><th class="n">Skill "Yes"</th><th>By source</th></tr></thead><tbody>{''.join(chat)}</tbody></table></div></section>"""
    return _frame(inner, r["generated"][11:19])
