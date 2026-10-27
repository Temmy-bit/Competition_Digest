#!/usr/bin/env python3
"""Pull Kaggle + Zindi competitions, score them against your profile, email a Gmail report.

Usage:
    python competition_digest.py            # fetch, build report, send email
    python competition_digest.py --dry-run  # fetch, build report, write report.html + competitions.csv, no email
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import logging
import os
import smtplib
import sqlite3
import ssl
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("digest")

# --------------------------------------------------------------------------- config
# Edit weights to change what counts as "relevant" to you.
PROFILE_KEYWORDS = {
    "nlp": 3, "language": 3, "speech": 3, "translation": 3, "yoruba": 4, "hausa": 4,
    "igbo": 4, "swahili": 4, "african": 3, "afri": 2, "low-resource": 4,
    "agent": 3, "llm": 3, "rag": 3, "retrieval": 3, "generative": 2,
    "vision": 3, "image": 2, "detection": 3, "yolo": 3, "segmentation": 2,
    "mlops": 2, "agricultur": 3, "crop": 3, "farm": 3, "climate": 2,
}
CLOSING_SOON_DAYS = 7
DEFAULT_RECENT_DAYS = int(os.getenv("DIGEST_RECENT_DAYS", "30"))
STATE_DB = Path(os.getenv("STATE_DB", "state.db"))
MAX_PAGES = 30
UA = {"User-Agent": "Mozilla/5.0 (competition-digest)"}


@dataclass
class Comp:
    platform: str
    key: str
    title: str
    url: str
    deadline: datetime | None = None
    reward: str = ""
    category: str = ""
    teams: int | None = None
    tags: list[str] = field(default_factory=list)
    description: str = ""
    score: int = 0
    is_new: bool = False
    deadline_changed: bool = False

    @property
    def days_left(self) -> int | None:
        if not self.deadline:
            return None
        return (self.deadline - datetime.now(timezone.utc)).days


# --------------------------------------------------------------------------- helpers
def g(obj, *names, default=None):
    """Get first available attribute/key from an object or dict."""
    for n in names:
        if isinstance(obj, dict):
            if obj.get(n) not in (None, ""):
                return obj[n]
        else:
            v = getattr(obj, n, None)
            if v not in (None, ""):
                return v
    return default


def to_dt(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        s = str(v).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------- sources
def fetch_kaggle() -> list[Comp]:
    from kaggle.api.kaggle_api_extended import KaggleApi  # needs KAGGLE_USERNAME / KAGGLE_KEY

    api = KaggleApi()
    api.authenticate()
    out: dict[str, Comp] = {}
    for page in range(1, MAX_PAGES + 1):
        items = api.competitions_list(page=page)
        # print(f"Kaggle page {page}: {(items)} competitions,")
        if not items:
            break
        for c in items.competitions:
            ref = str(g(c, "ref", "url", default=""))
            url = ref if ref.startswith("http") else f"https://www.kaggle.com/competitions/{ref.rsplit('/', 1)[-1]}"
            tags = g(c, "tags", default=[]) or []
            tag_names = [str(g(t, "name", default=t)) for t in tags]
            out[ref] = Comp(
                platform="Kaggle",
                key=f"kaggle:{ref}",
                title=str(g(c, "title", default=ref)),
                url=url,
                deadline=to_dt(g(c, "deadline")),
                reward=str(g(c, "reward", default="")),
                category=str(g(c, "category", default="")),
                teams=g(c, "team_count", "teamCount"),
                tags=tag_names,
                description=str(g(c, "description", default="")),
            )
    return list(out.values())


ZINDI_API = "https://api.zindi.africa/v1/competitions"  # unofficial; may change or require auth


def fetch_zindi() -> list[Comp]:
    out: dict[str, Comp] = {}
    for page in range(0, MAX_PAGES):
        r = requests.get(ZINDI_API, params={"page": page, "per_page": 50}, headers=UA, timeout=30)
        r.raise_for_status()
        body = r.json()
        items = body.get("data", body) if isinstance(body, dict) else body
        if not items:
            break
        for c in items:
            attrs = c.get("attributes", c) if isinstance(c, dict) else {}
            slug = str(g(c, "id", "slug", default=g(attrs, "slug", "id", default="")))
            if not slug:
                continue
            out[slug] = Comp(
                platform="Zindi",
                key=f"zindi:{slug}",
                title=str(g(attrs, "title", "name", default=slug)),
                url=f"https://zindi.africa/competitions/{slug}",
                deadline=to_dt(g(attrs, "end_time", "end_date", "deadline", "ends_at")),
                reward=str(g(attrs, "prize", "reward", "prize_pool", default="")),
                category=str(g(attrs, "kind", "type", "category", default="")),
                teams=g(attrs, "participants_count", "teams_count", "participant_count"),
                tags=[str(t) for t in (g(attrs, "tags", default=[]) or [])],
                description=str(g(attrs, "subtitle", "description", default="")),
            )
        if len(items) < 50:
            break
    return list(out.values())


# --------------------------------------------------------------------------- scoring + state
def score(c: Comp) -> int:
    text = " ".join([c.title, c.category, c.description, " ".join(c.tags)]).lower()
    s = sum(w for k, w in PROFILE_KEYWORDS.items() if k in text)
    if c.platform == "Zindi":
        s += 1
    return s


def apply_state(comps: list[Comp]) -> None:
    db = sqlite3.connect(STATE_DB)
    db.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, first_seen TEXT, deadline TEXT)")
    now = datetime.now(timezone.utc).isoformat()
    first_run = db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
    for c in comps:
        row = db.execute("SELECT deadline FROM seen WHERE key=?", (c.key,)).fetchone()
        dl = c.deadline.isoformat() if c.deadline else ""
        if row is None:
            c.is_new = not first_run
            db.execute("INSERT INTO seen VALUES (?,?,?)", (c.key, now, dl))
        elif row[0] != dl:
            c.deadline_changed = True
            db.execute("UPDATE seen SET deadline=? WHERE key=?", (dl, c.key))
    db.commit()
    db.close()


def filter_recent(comps: list[Comp], max_age_days: int | None) -> list[Comp]:
    if max_age_days is None:
        return list(comps)
    if max_age_days < 0:
        raise ValueError("Recent-day window must be zero or greater")
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    return [c for c in comps if c.deadline is not None and c.deadline >= cutoff]


# --------------------------------------------------------------------------- report
def fmt_dl(c: Comp) -> str:
    if not c.deadline:
        return "n/a"
    d = c.days_left
    return f"{c.deadline:%Y-%m-%d} ({d}d)" if d is not None and d >= 0 else f"{c.deadline:%Y-%m-%d} (closed)"


def table(rows: list[Comp]) -> str:
    if not rows:
        return "<p><i>None.</i></p>"
    head = "".join(f"<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ccc'>{h}</th>"
                   for h in ["Platform", "Competition", "Deadline", "Reward", "Type", "Teams", "Fit"])
    body = ""
    for c in rows:
        flags = (" 🆕" if c.is_new else "") + (" ⚠️deadline changed" if c.deadline_changed else "")
        cells = [c.platform, f"<a href='{html.escape(c.url)}'>{html.escape(c.title)}</a>{flags}",
                 fmt_dl(c), html.escape(c.reward or "n/a"), html.escape(c.category or ""),
                 "" if c.teams is None else str(c.teams), str(c.score)]
        body += "<tr>" + "".join(f"<td style='padding:4px 8px;border-bottom:1px solid #eee'>{x}</td>" for x in cells) + "</tr>"
    return f"<table style='border-collapse:collapse;font-family:sans-serif;font-size:13px'><tr>{head}</tr>{body}</table>"


def build_report(comps: list[Comp], status: dict[str, str], recent_days: int | None = None) -> tuple[str, str]:
    open_ = [c for c in comps if c.days_left is None or c.days_left >= 0]
    by_deadline = lambda c: (c.deadline is None, c.deadline or datetime.max.replace(tzinfo=timezone.utc))
    closing = sorted([c for c in open_ if c.days_left is not None and c.days_left <= CLOSING_SOON_DAYS], key=by_deadline)
    new = [c for c in open_ if c.is_new]
    changed = [c for c in open_ if c.deadline_changed]
    best = sorted([c for c in open_ if c.score > 0], key=lambda c: -c.score)[:10]
    everything = sorted(open_, key=by_deadline)

    date_suffix = f" (last {recent_days}d)" if recent_days is not None else ""
    st = "".join(f"<li><b>{k}</b>: {html.escape(v)}</li>" for k, v in status.items())
    h = f"""<html><body style='font-family:sans-serif'>
 <h2>Competition digest — {datetime.now():%Y-%m-%d}{date_suffix}</h2>
 <p>{len(open_)} open competitions ({sum(c.platform=='Kaggle' for c in open_)} Kaggle, {sum(c.platform=='Zindi' for c in open_)} Zindi).</p>
 <h3>Source status</h3><ul>{st}</ul>
 <h3>Closing within {CLOSING_SOON_DAYS} days</h3>{table(closing)}
 <h3>New since last run</h3>{table(new)}
 <h3>Deadline changed</h3>{table(changed)}
 <h3>Best fit (keyword score)</h3>{table(best)}
 <h3>All open competitions{date_suffix}</h3>{table(everything)}
 </body></html>"""
    t = f"Competition digest {datetime.now():%Y-%m-%d}{date_suffix}: {len(open_)} open, {len(new)} new, {len(closing)} closing soon. See HTML version."
    return h, t


def to_csv(comps: list[Comp]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["platform", "title", "url", "deadline", "days_left", "reward", "category", "teams", "tags", "score", "new", "deadline_changed"])
    for c in comps:
        w.writerow([c.platform, c.title, c.url, c.deadline.isoformat() if c.deadline else "", c.days_left,
                    c.reward, c.category, c.teams, "|".join(c.tags), c.score, c.is_new, c.deadline_changed])
    return buf.getvalue()


# --------------------------------------------------------------------------- email
def send_gmail(html_body: str, text_body: str, csv_text: str) -> None:
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]  # Google account > Security > App passwords (needs 2FA)
    to = os.getenv("REPORT_TO", user)
    msg = EmailMessage()
    msg["Subject"] = f"Competition digest {datetime.now():%Y-%m-%d}"
    msg["From"], msg["To"] = user, to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    msg.add_attachment(csv_text.encode(), maintype="text", subtype="csv", filename="competitions.csv")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pwd)
        s.send_message(msg)


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="write report.html/competitions.csv instead of emailing")
    ap.add_argument("--days", type=int, default=DEFAULT_RECENT_DAYS, help="limit output to competitions with deadlines within the last N days; set to 0 to include only current-day deadlines")
    ap.add_argument("--all-dates", action="store_true", help="include all open competitions instead of limiting to the recent-date window")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    recent_days = None if args.all_dates else max(args.days, 0)

    comps: list[Comp] = []
    status: dict[str, str] = {}
    for name, fn in [("Kaggle", fetch_kaggle), ("Zindi", fetch_zindi)]:
        try:
            got = fn()
            comps += got
            status[name] = f"OK, {len(got)} fetched"
        except Exception as e:  # one source failing must not kill the report
            log.exception("%s failed", name)
            status[name] = f"FAILED: {type(e).__name__}: {e}"

    if not comps:
        log.error("No competitions fetched from any source.")
        return 1

    for c in comps:
        c.score = score(c)
    apply_state(comps)

    filtered = filter_recent(comps, recent_days)
    html_body, text_body = build_report(filtered, status, recent_days=recent_days)
    csv_text = to_csv(filtered)

    if args.dry_run:
        Path("report.html").write_text(html_body, encoding="utf-8")
        Path("competitions.csv").write_text(csv_text, encoding="utf-8")
        log.info("Wrote report.html and competitions.csv")
    else:
        send_gmail(html_body, text_body, csv_text)
        log.info("Email sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
