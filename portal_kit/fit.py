"""One definition of a well-matched job for every auto-applier (Naukri 1/2, Instahyre 1/2, LinkedIn India/UAE).

The rules live outside every repo (~/.secrets/fit_rules.json or $FIT_RULES; this repo is public). Decisions,
2026-10-08: a job's minimum required experience must be <= 5 years; "Senior" titles are fine when the range fits;
Lead / Staff / Principal / Head / Manager / Architect (and Director / VP / Chief) never; screening answers about
years in a skill stay "3", so the applier must never open a job built on skills the user does not have; any Indian
city or remote (relocation is fine), Bengaluru and remote preferred. Each Naukri / Instahyre account keeps to its own
identity (Data Platform Engineer or AI Engineer); LinkedIn is one profile and may take either.

Regions (2026-10-14). Where a job may be is a region's rule. `india` holds today's words (the top-level abroad_words and
preferred_location_words): any location except one naming a place abroad. Other regions come from the rules file's
"regions" block; `gulf` holds the location words of the enabled countries, and only a location naming one of them passes.
A region may also rule out title words and make some titles stretch-only for an identity (the user's Gulf decision of
2026-10-13: Power BI titles out, Data Analyst and Data Modeler stretch only). An applier entry in "appliers" is a plain
identity (judged in India) or {"identity": ..., "region": ...}; an applier the rules do not name raises RulesUnavailable.

    python portal_kit/fit.py check "<title>" [--identity data_platform|ai_engineer] [--region india|gulf] [--exp 3-5] [--tags a,b,c]
    python portal_kit/fit.py audit [--since YYYY-MM-DD] [--sample 20] [--html PATH]

Appliers load this file by path (importlib), the way linkedin_uae_overlay.py loads facts.py. Stdlib only, Python 3.11.
"""
# (No `from __future__ import annotations`: appliers load this file by path without registering it in
# sys.modules, and dataclasses then cannot resolve string annotations.)
import argparse
import csv
import hashlib
import html
import json
import os
import random
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

RULES_PATH = Path(os.environ.get("FIT_RULES") or Path.home() / ".secrets" / "fit_rules.json")
GITHUB = Path(os.environ.get("JOB_TRACKER_HOME") or Path.home() / "GitHub")
NK = Path(os.environ.get("JOB_DASHBOARD_NK") or GITHUB / "Naukri-autoapply-bot-for-claude")
JT = Path(os.environ.get("JOB_DASHBOARD_JT") or GITHUB / "job-tracker")
LI = Path(os.environ.get("JOB_DASHBOARD_LI") or GITHUB / "FORKED_Auto_job_applier_linkedIn")


class RulesUnavailable(RuntimeError):
    """The rules file is missing or unreadable, or names no rules for this applier or region. Appliers stop (they never
    apply without the gate)."""


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\u2013", "-").replace("\u2014", "-")).strip().lower()


def _alt(words) -> re.Pattern:
    words = sorted({_norm(w) for w in words if w and _norm(w)}, key=len, reverse=True)
    if not words:
        return re.compile(r"(?!x)x")
    return re.compile(r"(?<![a-z0-9])(" + "|".join(map(re.escape, words)) + r")(?![a-z0-9+#])")


@dataclass
class Job:
    source: str                      # naukri | instahyre | linkedin
    title: str
    company: str = ""
    location: str = ""
    exp_min: float | None = None
    exp_max: float | None = None
    tags: list[str] = field(default_factory=list)   # the site's skill tags for the job
    text: str = ""                   # description text, when the site gives one (LinkedIn)
    url: str = ""
    job_id: str = ""
    site_score: float = 0.0


@dataclass
class Verdict:
    apply: bool
    bucket: str                      # good | stretch | reject
    score: float
    reasons: list[str]
    identity: str
    rules_version: str
    skill_hits: list[str] = field(default_factory=list)


class Identity:
    def __init__(self, key: str, d: dict):
        self.key, self.label = key, d.get("label", key)
        self.target_re = _alt(d.get("target_titles", []))
        self.stretch_re = _alt(d.get("stretch_titles", []))
        self.skills = sorted({_norm(s) for s in d.get("skills", [])}, key=len, reverse=True)
        self.skill_re = _alt(self.skills)
        self.domain_re = _alt(d.get("domain_words", []))


_NEVER = _alt([])


class Region:
    """Where a job may be, and the region's own title rules. A deny list (abroad_words: India) rejects a location naming a
    place abroad; an allow list (location_words: the Gulf) rejects a location naming none of the region's places. A
    preferred word always passes. An empty location is never a reason to reject, except in a region with an allow list: there
    only a location naming one of its places passes, so an empty location is rejected ("no location")."""

    def __init__(self, key: str, d: dict):
        self.key, self.label = key, d.get("label", key)
        self.abroad_re = _alt(d.get("abroad_words", []))
        self.allow_list = bool(d.get("location_words"))
        self.allowed_re = _alt(d.get("location_words", []))
        self.preferred_re = _alt(d.get("preferred_location_words", []))
        self.off_title_re = _alt(d.get("off_title_words", []))
        self.stretch_only = {k: _alt(v) for k, v in (d.get("stretch_only_titles") or {}).items()}

    def stretch_only_re(self, identity: str) -> re.Pattern:
        return self.stretch_only.get(identity, _NEVER)


class Rules:
    def __init__(self, d: dict):
        self.raw = d
        self.version = hashlib.sha1(json.dumps(d, sort_keys=True).encode("utf-8")).hexdigest()[:10]
        self.max_min = float(d["max_experience_min_years"])
        self.min_max = float(d.get("min_experience_max_years", 0))
        self.senior_re = _alt(d["senior_title_words"])
        self.stretch_word_re = _alt(d.get("stretch_title_words", []))
        self.off_role_re = _alt(d["off_role_words"])
        self.off_stack_re = _alt(d["off_stack_words"])
        self.agency_re = _alt(d.get("agency_markers", []))
        self.avoid = {k: _alt(v + [k]) for k, v in (d.get("avoid_companies") or {}).items()}
        self.min_tag_hits = int(d.get("min_tag_hits", 2))
        self.role_re = _alt(d.get("generic_role_words", []))
        self.neutral_tags = {_norm(x) for x in d.get("neutral_tags", [])}
        self.min_tag_share = float(d.get("min_tag_share", 0.34))
        self.w = d.get("score_weights") or {}
        self.identities = {k: Identity(k, v) for k, v in d["identities"].items()}
        self.appliers = d.get("appliers") or {}
        # India is built from today's top-level words unless the file gives a "regions" -> "india" block of its own.
        blocks = {k: v for k, v in (d.get("regions") or {}).items() if not k.startswith("_") and isinstance(v, dict)}
        india = dict(blocks.pop("india", None) or {"abroad_words": d.get("abroad_words", []),
                                                  "preferred_location_words": d.get("preferred_location_words", [])})
        india.setdefault("label", "India")
        self.regions = {"india": Region("india", india), **{k: Region(k, v) for k, v in blocks.items()}}
        self.abroad_re, self.preferred_re = self.regions["india"].abroad_re, self.regions["india"].preferred_re

    def region(self, key: str) -> Region:
        try:
            return self.regions[key]
        except KeyError:
            raise RulesUnavailable(f"the fit rules define no region {key!r} (they have: {', '.join(self.regions)})") from None

    def applier(self, applier: str) -> tuple[str, str]:
        """(identity, region) of an applier. Its entry is a plain identity (or 'best_of_both'), judged in India, or an object
        {"identity": ..., "region": ...}. An applier, identity or region the rules do not define raises RulesUnavailable."""
        spec = self.appliers.get(applier)
        if isinstance(spec, str):
            ident, region = spec, "india"
        elif isinstance(spec, dict):
            ident, region = str(spec.get("identity") or ""), str(spec.get("region") or "india")
        else:
            raise RulesUnavailable(f"the fit rules name no applier {applier!r} (they have: {', '.join(sorted(self.appliers))})")
        if ident != "best_of_both" and ident not in self.identities:
            raise RulesUnavailable(f"applier {applier!r} is set to identity {ident!r}, which the fit rules do not define")
        self.region(region)
        return ident, region

    def company_key(self, company: str) -> str | None:
        """The avoid-list entry a company name matches (aliases: 'Tata Consultancy Services' is 'tcs')."""
        c = _norm(company)
        for key, rx in self.avoid.items():
            if rx.search(c):
                return key
        return None


def load_rules(path: Path | str | None = None) -> Rules:
    p = Path(path) if path else RULES_PATH
    try:
        return Rules(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError) as e:
        raise RulesUnavailable(f"fit rules unreadable at {p}: {e}") from e


# ------------------------------------------------------------------ experience bands
_NAUKRI_URL_BAND = re.compile(r"-(\d{1,2})-to-(\d{1,2})-years?(?:-|$)")
_CARD_BAND = re.compile(r"(?<![\d.])(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\s*(?:yrs?|years?)\b")
_GENERAL = re.compile(r"(?<![\d.])(\d{1,2})(?:\s*(?:-|to)\s*(\d{1,2}))?\s*\+?\s*(?:yrs?|years?)(?:'s)?"
                      r"(?:\s+of)?(?:\s+(?:total|overall|professional|relevant|industry|hands[- ]on|work|working|"
                      r"proven|solid|strong))*\s+(?:experience|exp)\b(?!\s+(?:in|with|on|using)\b)")
_MINIMUM = re.compile(r"(?:minimum|min\.?|at least|atleast)\s+(?:of\s+)?(\d{1,2})\s*\+?\s*(?:yrs?|years?)")


def band_from_naukri_url(url: str) -> tuple[float | None, float | None]:
    m = _NAUKRI_URL_BAND.search(url or "")
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def band_from_card(text: str) -> tuple[float | None, float | None]:
    """A Naukri card's '5-10 Yrs' (also 'X to Y years')."""
    m = _CARD_BAND.search(_norm(text))
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def band_from_text(text: str) -> tuple[float | None, float | None]:
    """The job's overall requirement from description text: the strictest general statement ('5+ years of
    experience', '3-6 years of professional experience', 'minimum 4 years'). Skill-specific phrases ('2+ years of
    experience in Spark') are not the job's bar and are ignored. None when nothing general is stated."""
    t = _norm(text)
    found = []
    for m in _GENERAL.finditer(t):
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else None
        if lo <= 30:
            found.append((lo, hi))
    for m in _MINIMUM.finditer(t):
        found.append((float(m.group(1)), None))
    if not found:
        return None, None
    lo, hi = max(found, key=lambda x: x[0])
    return lo, hi


# ------------------------------------------------------------------ verdict
def _tags_norm(tags) -> list[str]:
    return [_norm(t) for t in (tags or []) if _norm(t)]


def _title_flags(t: str, reg: Region, rules: Rules) -> list[str]:
    """Reasons from the rules that look at a title alone and do not depend on the identity (t is already normalised)."""
    out: list[str] = []
    m = rules.senior_re.search(t)
    if m:
        out.append(f"too-senior title ({m.group(1)})")
    m = rules.off_role_re.search(t)
    if m:
        out.append(f"off-role title ({m.group(1)})")
    m = rules.off_stack_re.search(t)
    if m:
        out.append(f"title built on a stack you don't use ({m.group(1)})")
    m = reg.off_title_re.search(t)          # the region's own ruled-out titles (none in India)
    if m:
        out.append(f"title ruled out for {reg.label} ({m.group(1)})")
    return out


def _title_kind(t: str, ident: Identity, identity: str, reg: Region, rules: Rules) -> str | None:
    """'target', 'stretch', or None (outside this identity's roles) for a normalised title."""
    if reg.stretch_only_re(identity).search(t):
        return "stretch"     # the region's stretch-only titles for this identity: never "good", never outside the roles
    if ident.target_re.search(t) or (ident.domain_re.search(t) and rules.role_re.search(t)):
        return "target"      # e.g. "AI Enablement Engineer", "Software Development Engineer III - Data"
    if ident.stretch_re.search(t):
        return "stretch"
    return None


def evaluate(job: Job, identity: str, rules: Rules, region: str = "india") -> Verdict:
    if identity not in rules.identities:
        raise RulesUnavailable(f"the fit rules define no identity {identity!r} (they have: {', '.join(rules.identities)})")
    ident = rules.identities[identity]
    reg = rules.region(region)
    w = rules.w
    t = _norm(job.title)
    rej: list[str] = []
    notes: list[str] = []
    score = 0.0

    rej.extend(_title_flags(t, reg, rules))
    title_kind = _title_kind(t, ident, identity, reg, rules)
    if title_kind is None:
        rej.append(f"title outside the {ident.label} roles")
    else:
        score += w.get("title_target", 40) if title_kind == "target" else w.get("title_stretch", 20)

    # experience band (user: minimum <= 5 years)
    stretch = title_kind == "stretch" or bool(rules.stretch_word_re.search(t))
    if job.exp_min is not None:
        if job.exp_min > rules.max_min:
            rej.append(f"needs {job.exp_min:g}+ years (limit {rules.max_min:g})")
        elif job.exp_max is not None and job.exp_max < rules.min_max:
            rej.append(f"entry-level (max {job.exp_max:g} years)")
        else:
            fit = w.get("exp_fit_max", 20)
            if job.exp_min <= 3:
                score += fit
            else:
                score += fit * (0.6 if job.exp_min <= 4 else 0.3)
                stretch = True
                notes.append(f"minimum {job.exp_min:g} years")
    else:
        score += w.get("exp_fit_max", 20) / 2
        notes.append("experience not stated")

    # skills fit (user: never open a job built on skills you don't have)
    # Generic tags ("Machine Learning", "AI", "Data") name a field, not a skill a screener asks years for.
    tags = [x for x in _tags_norm(job.tags) if x not in rules.neutral_tags]
    hits: list[str] = []
    if tags:
        hit_tags = [x for x in tags if ident.skill_re.search(x)]
        stack_tags = [x for x in tags if rules.off_stack_re.search(x) and not ident.skill_re.search(x)]
        hits = hit_tags
        share = len(hit_tags) / len(tags)
        if len(stack_tags) > len(hit_tags):
            rej.append(f"skills: built on {', '.join(stack_tags[:3])}")
        elif len(hit_tags) < min(rules.min_tag_hits, len(tags)) or share < rules.min_tag_share:
            rej.append(f"skills: {len(hit_tags)} of {len(tags)} listed skills are yours")
    elif job.text:
        txt = _norm(job.text)
        hits = sorted({m.group(1) for m in ident.skill_re.finditer(txt)})
        stack = sorted({m.group(1) for m in rules.off_stack_re.finditer(txt)})
        # A thin description is no evidence of other skills; it only rejects a generic title (Software Engineer)
        # that shows nothing of the identity, or a description built on another stack.
        if len(stack) > len(hits):
            rej.append(f"skills: description built on {', '.join(stack[:3])}")
        elif len(hits) < rules.min_tag_hits:
            if title_kind != "target":
                rej.append(f"skills: only {len(hits)} of your skills in the description")
            else:
                notes.append("few of your skills in the description")
    else:
        notes.append("skills not listed")
    score += min(w.get("skills_max", 25), w.get("per_skill", 5) * len(hits))

    # location. India: any Indian city or remote, Bengaluru / remote preferred. An allow-list region (the Gulf): only its
    # places. The "location_india" weight is the score of an ordinary (not preferred) place in whichever region.
    loc = _norm(job.location)
    preferred = bool(loc) and bool(reg.preferred_re.search(loc))
    if not loc and reg.allow_list:
        rej.append("no location")          # an allow-list region passes only a named place; an empty location names none
    elif loc and not preferred and (reg.abroad_re.search(loc) or (reg.allow_list and not reg.allowed_re.search(loc))):
        rej.append(f"outside {reg.label} ({job.location})")
    elif not loc or preferred:
        score += w.get("location_preferred", 10)
    else:
        score += w.get("location_india", 6)

    # company
    ck = rules.company_key(job.company)
    if ck:
        rej.append(f"avoided company ({ck})")
    comp = _norm(job.company)
    if comp and comp != "unknown":
        if rules.agency_re.search(comp):
            notes.append("agency / aggregator")
        else:
            score += w.get("direct_company", 5)

    score = round(min(100.0, score), 1)
    if rej:
        return Verdict(False, "reject", score, rej + notes, identity, rules.version, hits)
    return Verdict(True, "stretch" if stretch else "good", score, notes, identity, rules.version, hits)


def evaluate_for(applier: str, job: Job, rules: Rules) -> Verdict:
    """The applier's identity and region from the rules; 'best_of_both' (LinkedIn) takes the better of the two identities.
    An applier the rules do not name raises RulesUnavailable (it never applies without the gate)."""
    ident, region = rules.applier(applier)
    if ident != "best_of_both":
        return evaluate(job, ident, rules, region)
    vs = [evaluate(job, k, rules, region) for k in rules.identities]
    order = {"good": 2, "stretch": 1, "reject": 0}
    return max(vs, key=lambda v: (order[v.bucket], v.score))


def title_reasons(title: str, applier: str, rules: Rules) -> list[str]:
    """Why a job's TITLE alone rules it out for this applier: an empty list means the title passes. For a job card that has not been
    opened yet: only the title rules of evaluate() (too senior, off-role, built on a stack the user does not use, the region's own
    ruled-out titles, a title outside the roles of every identity the applier may take); nothing about experience, skills, location or
    company. An applier or region the rules do not name raises RulesUnavailable (it never applies without the gate)."""
    ident, region = rules.applier(applier)
    reg = rules.region(region)
    t = _norm(title)
    out = _title_flags(t, reg, rules)
    keys = list(rules.identities) if ident == "best_of_both" else [ident]
    if all(_title_kind(t, rules.identities[k], k, reg, rules) is None for k in keys):
        out.append("title outside the " + " and ".join(rules.identities[k].label for k in keys) + " roles")
    return out


def record_fit(con: sqlite3.Connection, applier: str, job: Job, v: Verdict, decision: str = "") -> None:
    """One row per job and applier in a separate table (applied_jobs is written with INSERT OR REPLACE and a
    fixed column list, so it cannot carry extra columns)."""
    con.execute("""CREATE TABLE IF NOT EXISTS job_fit (
        applier TEXT, job_id TEXT, source TEXT, title TEXT, company TEXT, location TEXT, exp_min REAL, exp_max REAL,
        score REAL, bucket TEXT, reasons TEXT, identity TEXT, rules_version TEXT, decision TEXT, ts TEXT,
        PRIMARY KEY (applier, job_id))""")
    con.execute("INSERT OR REPLACE INTO job_fit VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (applier, str(job.job_id or job.url), job.source, job.title, job.company, job.location, job.exp_min,
                 job.exp_max, v.score, v.bucket, json.dumps(v.reasons), v.identity, v.rules_version, decision,
                 datetime.now().isoformat(timespec="seconds")))
    con.commit()


# ------------------------------------------------------------------ audit of stored applications
def _naukri_rows(db: Path, since: str) -> list[Job]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    out = []
    for jid, url, slug, comp, when, notes in con.execute(
            "SELECT job_id, job_url, job_title, company_name, application_date, notes FROM applied_jobs "
            "WHERE status LIKE 'Applied%' AND application_date >= ? ORDER BY application_date", (since,)):
        parts = (notes or "").split(" - ")
        title = parts[0].strip() if len(parts) >= 3 else re.sub(r"^job listings ", "", slug or "")
        location = parts[1].strip() if len(parts) >= 3 else ""
        company = comp or ""
        if _norm(company) in ("", "unknown") and len(parts) >= 3:
            company = parts[2].strip()
        lo, hi = band_from_naukri_url(url or "")
        out.append(Job("naukri", title, company, location, lo, hi, [], "", url or "", str(jid)))
    return out


def _instahyre_rows(db: Path, since: str) -> list[Job]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    out = []
    for jid, title, comp, locs, kw, score, url in con.execute(
            "SELECT job_id, title, company, locations, keywords, score, url FROM applications "
            "WHERE decision='applied' AND ts >= ? ORDER BY ts", (since,)):
        try:
            tags = json.loads(kw or "[]")
        except ValueError:
            tags = []
        out.append(Job("instahyre", title or "", comp or "", locs or "", None, None, tags, "", url or "", str(jid),
                       float(score or 0)))
    return out


_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")


def _linkedin_rows(csv_path: Path, since: str) -> list[Job]:
    out = []
    with open(csv_path, encoding="utf-8", errors="replace", newline="") as fh:
        for r in csv.DictReader(fh):
            # 41 old rows (2025 format) hold "Pending" or a resume name in Date Applied, which sorts after any
            # date as text; only a real timestamp counts.
            applied = (r.get("Date Applied") or "").strip()
            if not _DATE_RX.match(applied) or applied < since:
                continue
            text = r.get("About Job") or ""
            lo, hi = band_from_text(text)
            loc = ", ".join(x for x in (r.get("Work Location"), r.get("Work Style")) if x)
            out.append(Job("linkedin", r.get("Title") or "", r.get("Company") or "", loc, lo, hi, [], text,
                           r.get("Job Link") or "", r.get("Job ID") or ""))
    return out


def stored_applications(since: str) -> dict[str, list[Job]]:
    return {
        "naukri1": _naukri_rows(NK / "naukri_jobs.db", since),
        "naukri2": _naukri_rows(NK / "profiles" / "naukri2" / "naukri_jobs.db", since),
        "instahyre1": _instahyre_rows(JT / "state" / "instahyre_1.db", since),
        "instahyre2": _instahyre_rows(JT / "state" / "instahyre_2.db", since),
        "linkedin": _linkedin_rows(LI / "all excels" / "all_applied_applications_history.csv", since),
    }


def audit(since: str, sample: int, html_path: str | None, seed: int = 8) -> dict:
    rules = load_rules()
    data = stored_applications(since)
    summary, samples = {}, {}
    rnd = random.Random(seed)
    for applier, jobs in data.items():
        verdicts = [(j, evaluate_for(applier, j, rules)) for j in jobs]
        counts = {"good": 0, "stretch": 0, "reject": 0}
        reasons: dict[str, int] = {}
        for _, v in verdicts:
            counts[v.bucket] += 1
            if v.bucket == "reject":
                key = re.sub(r"\s*\(.*\)$", "", v.reasons[0]) if v.reasons else "?"
                key = re.sub(r"\d+(\.\d+)?", "N", key)
                reasons[key] = reasons.get(key, 0) + 1
        summary[applier] = {"applied": len(jobs), **counts,
                            "top_reject_reasons": sorted(reasons.items(), key=lambda x: -x[1])[:6]}
        samples[applier] = rnd.sample(verdicts, min(sample, len(verdicts)))
    if html_path:
        Path(html_path).write_text(_review_html(since, rules, summary, samples), encoding="utf-8")
    return {"since": since, "rules_version": rules.version, "summary": summary}


def _review_html(since: str, rules: Rules, summary: dict, samples: dict) -> str:
    e = html.escape
    names = {"naukri1": "Naukri 1 (Data Platform)", "naukri2": "Naukri 2 (AI Engineer)",
             "instahyre1": "Instahyre 1 (Data Platform)", "instahyre2": "Instahyre 2 (AI Engineer)",
             "linkedin": "LinkedIn (either identity)"}
    rows = []
    for a, s in summary.items():
        rr = "; ".join(f"{e(k)} {n}" for k, n in s["top_reject_reasons"])
        rows.append(f"<tr><th scope=row>{e(names.get(a, a))}</th><td class=n>{s['applied']}</td><td class=n>{s['good']}</td>"
                    f"<td class=n>{s['stretch']}</td><td class=n>{s['reject']}</td><td>{rr}</td></tr>")
    sections = []
    for a, items in samples.items():
        lis = []
        for j, v in items:
            band = "" if j.exp_min is None else f"{j.exp_min:g}-{j.exp_max:g} yrs" if j.exp_max is not None else f"{j.exp_min:g}+ yrs"
            why = "; ".join(v.reasons) or "fits"
            lis.append(f"<tr class={v.bucket}><td><span class=b>{v.bucket}</span></td><td>{e(j.title)}</td>"
                       f"<td>{e(j.company)}</td><td>{e(band)}</td><td class=n>{v.score:g}</td><td>{e(why)}</td></tr>")
        sections.append(f"<section><h2>{e(names.get(a, a))}</h2><div class=box><table><thead><tr><th>Verdict</th>"
                        f"<th>Title</th><th>Company</th><th>Experience</th><th>Score</th><th>Why</th></tr></thead>"
                        f"<tbody>{''.join(lis)}</tbody></table></div></section>")
    dp, ai = rules.raw["identities"]["data_platform"], rules.raw["identities"]["ai_engineer"]
    ask = (f"<p><b>Data Platform</b>: {e(', '.join(dp.get('not_on_resume_ask_user', [])))}</p>"
           f"<p><b>AI Engineer</b>: {e(', '.join(ai.get('not_on_resume_ask_user', [])))}</p>")
    skills = (f"<p><b>Data Platform</b>: {e(', '.join(dp['skills']))}</p><p><b>AI Engineer</b>: {e(', '.join(ai['skills']))}</p>")
    return f"""<title>Job Fit Review</title>
<style>
:root {{ --bg:#f7f7f4; --fg:#1d2226; --muted:#5f6b70; --line:#dcdfd8; --surface:#fff; --good:#1f6f43; --good-bg:#e1f3e8;
  --str:#7a5a00; --str-bg:#fbf0d2; --rej:#8f2d22; --rej-bg:#f8e2df; color-scheme: light; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#15191b; --fg:#e7ebe9; --muted:#9aa6a8; --line:#2c3437;
  --surface:#1c2124; --good:#9ee0b8; --good-bg:#183526; --str:#f0d58a; --str-bg:#3a3117; --rej:#f2b1a8; --rej-bg:#45221e; color-scheme: dark; }} }}
:root[data-theme="dark"] {{ --bg:#15191b; --fg:#e7ebe9; --muted:#9aa6a8; --line:#2c3437; --surface:#1c2124; --good:#9ee0b8;
  --good-bg:#183526; --str:#f0d58a; --str-bg:#3a3117; --rej:#f2b1a8; --rej-bg:#45221e; color-scheme: dark; }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 "Segoe UI",system-ui,sans-serif; padding-inline:16px; padding-block:24px 48px; }}
main {{ max-width:1180px; margin:0 auto; display:grid; gap:28px; }}
h1,h2 {{ margin:0; text-wrap:balance; }} h1 {{ font-size:1.8rem; }} h2 {{ font-size:1.15rem; }}
p {{ max-width:80ch; margin:6px 0; }} .muted {{ color:var(--muted); }}
.box {{ overflow-x:auto; background:var(--surface); border:1px solid var(--line); border-radius:8px; }}
table {{ border-collapse:collapse; width:100%; min-width:760px; }}
th,td {{ text-align:left; vertical-align:top; padding:8px 12px; border-top:1px solid var(--line); }}
thead th {{ border-top:0; font-size:.75rem; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); }}
.n {{ font-variant-numeric:tabular-nums; text-align:right; }}
.b {{ display:inline-block; padding:1px 8px; border-radius:999px; font-size:.8rem; }}
tr.good .b {{ background:var(--good-bg); color:var(--good); }} tr.stretch .b {{ background:var(--str-bg); color:var(--str); }}
tr.reject .b {{ background:var(--rej-bg); color:var(--rej); }}
section {{ display:grid; gap:10px; min-width:0; }}
</style>
<main>
<header><h1>Job Fit Review</h1>
<p class=muted>Every application since {e(since)}, re-graded by the shared fit rules (version {e(rules.version)}). Nothing was
applied or changed. Good and stretch would be applied to; reject would be skipped before any Apply click.</p></header>
<section><h2>What I need from you</h2><ol>
<li>Look through the 20 sample verdicts per applier below and tell me any that are wrong (a good job marked reject, or a
mismatch marked good or stretch).</li>
<li>Check the two skill lists at the bottom. A job only passes if enough of its listed skills are on your list, so add any
skill from the "not on your resumes" lists that you really have (LangChain, PyTorch, FastAPI, GCP/BigQuery, Azure,
Terraform, Hadoop/Hive...).</li>
<li>Stretch titles: should Data Scientist (AI identity) and Data Analyst / SQL Developer (Data Platform identity) stay as
stretch, or be skipped?</li></ol>
<p class=muted>Older Naukri rows saved only the job's URL text, so some titles below read like "data engineer cgi hyderabad
...": the live gate reads the card's real title. LinkedIn titles before 10-02 lost their last letter (a parser bug fixed
that day).</p></section>
<section><h2>Totals</h2><div class=box><table><thead><tr><th>Applier</th><th class=n>Applied</th><th class=n>Good</th>
<th class=n>Stretch</th><th class=n>Reject</th><th>Top reasons for reject</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
{''.join(sections)}
<section><h2>Your skill lists (from the two resumes)</h2>{skills}</section>
<section><h2>Skills not on your resumes: add any you really have</h2>{ask}</section>
</main>"""


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("title")
    c.add_argument("--identity", default="data_platform")
    c.add_argument("--region", default="india")
    c.add_argument("--exp", default="")
    c.add_argument("--tags", default="")
    c.add_argument("--location", default="")
    c.add_argument("--company", default="")
    a = sub.add_parser("audit")
    a.add_argument("--since", default=(date.today() - timedelta(days=7)).isoformat())
    a.add_argument("--sample", type=int, default=20)
    a.add_argument("--html", default=None)
    args = ap.parse_args(argv)
    if args.cmd == "check":
        rules = load_rules()
        lo = hi = None
        if args.exp:
            p = args.exp.split("-")
            lo = float(p[0])
            hi = float(p[1]) if len(p) > 1 and p[1] else None
        job = Job("cli", args.title, args.company, args.location, lo, hi,
                  [x.strip() for x in args.tags.split(",") if x.strip()])
        v = evaluate(job, args.identity, rules, args.region)
        print(json.dumps(v.__dict__, indent=1))
        return 0
    out = audit(args.since, args.sample, args.html)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(_main())
