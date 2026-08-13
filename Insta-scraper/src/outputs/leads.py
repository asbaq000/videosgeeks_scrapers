"""
Daily lead delivery.

The unit of work is "10 fresh leads today", so two things have to persist
between runs:

  * a ledger of every account ever delivered, so tomorrow's ten are ten
    accounts you haven't seen before rather than the same ten again;
  * today's CSV, so re-running after a rate-limit block tops the day up to
    ten instead of starting a second batch.
"""

import csv
import os
from datetime import datetime, timezone

LEAD_FIELDS = [
    "profile_url",
    "username",
    "full_name",
    "follower_count",
    "posts_last_week",
    "latest_post",
    "category",
    "biography",
    "external_url",
    "is_verified",
    "media_count",
    "keyword",
    "found_via",
    "delivered_on",
]

LEDGER_NAME = "delivered.txt"


def today_stamp(now=None):
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def day_file(leads_dir, stamp):
    return os.path.join(leads_dir, f"leads-{stamp}.csv")


def ledger_file(leads_dir):
    return os.path.join(leads_dir, LEDGER_NAME)


def load_ledger(leads_dir):
    """Every username delivered on any previous day."""
    path = ledger_file(leads_dir)
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}


def append_ledger(leads_dir, usernames):
    if not usernames:
        return
    os.makedirs(leads_dir, exist_ok=True)
    with open(ledger_file(leads_dir), "a", encoding="utf-8") as f:
        for name in usernames:
            f.write(name + "\n")


def load_today(leads_dir, stamp):
    """Rows already written for today, so a resumed run tops up rather than restarts."""
    path = day_file(leads_dir, stamp)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def to_row(record, stamp):
    row = {k: record.get(k, "") for k in LEAD_FIELDS}
    row["profile_url"] = f"https://www.instagram.com/{record.get('username', '')}/"
    keywords = record.get("keywords") or []
    row["keyword"] = record.get("matched_keyword") or (keywords[0] if keywords else "")
    row["biography"] = (record.get("biography") or "").replace("\n", " ").strip()
    row["media_count"] = "" if record.get("media_count") is None else record["media_count"]
    row["delivered_on"] = stamp
    return row


def write_day(leads_dir, stamp, rows):
    os.makedirs(leads_dir, exist_ok=True)
    path = day_file(leads_dir, stamp)
    # utf-8-sig so Excel renders the emoji in bios instead of mojibake.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=LEAD_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in LEAD_FIELDS})
    return path
