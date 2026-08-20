"""
Google Sheets delivery.

The sheet is the source of truth for "never fetch this account twice": every
run reads the usernames already in it and refuses to look at them again. A
local mirror in the leads dir backs that up, so deleting a row by hand
doesn't resurrect an old lead.

Setup, once:

  1. Google Cloud console -> create (or pick) a project.
  2. Enable the "Google Sheets API" and the "Google Drive API".
  3. Credentials -> Create credentials -> Service account. Create a JSON key
     and save it somewhere private, e.g. src/config/google-credentials.json.
  4. Open the JSON and copy the "client_email" value — it looks like
     something@your-project.iam.gserviceaccount.com
  5. Share your spreadsheet with that email, with Editor permission.

Then:

  python src/find_creators.py -k data/keywords.txt \
      --sheet https://docs.google.com/spreadsheets/d/<id>/edit \
      --credentials src/config/google-credentials.json

The service account is its own identity — it never sees your Google password,
and it can only touch spreadsheets you have explicitly shared with it.
"""

import os
import re

try:
    import gspread
except ImportError:  # pragma: no cover - optional dependency
    gspread = None

HEADER = [
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

DEFAULT_WORKSHEET = "Leads"

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


class SheetError(Exception):
    """Anything that stops us reaching or writing the sheet."""


def sheet_key(spreadsheet):
    """Accept a full edit URL or a bare spreadsheet id."""
    match = _SHEET_ID_RE.search(spreadsheet or "")
    return match.group(1) if match else (spreadsheet or "").strip()


def connect(spreadsheet, credentials_path, worksheet=DEFAULT_WORKSHEET):
    """
    Open (or create) the worksheet and make sure row 1 is the header.

    Raises SheetError with something actionable rather than letting a
    gspread stack trace escape.
    """
    if gspread is None:
        raise SheetError("gspread is not installed:  pip install gspread google-auth")
    if not credentials_path or not os.path.exists(credentials_path):
        raise SheetError(
            f"Service account key not found at {credentials_path!r}. "
            "See the setup steps at the top of src/outputs/sheets.py."
        )

    try:
        client = gspread.service_account(filename=credentials_path)
    except Exception as exc:  # noqa: BLE001 — surface the cause, don't dump a trace
        raise SheetError(f"could not load the service account key: {exc}") from exc

    key = sheet_key(spreadsheet)
    try:
        book = client.open_by_key(key)
    except Exception as exc:  # noqa: BLE001
        raise SheetError(
            f"could not open spreadsheet {key!r}: {exc}\n"
            "Most often this means the sheet hasn't been shared with the "
            "service account's client_email as an Editor."
        ) from exc

    try:
        ws = book.worksheet(worksheet)
    except Exception:  # noqa: BLE001 — worksheet missing is the normal first run
        ws = book.add_worksheet(title=worksheet, rows=1000, cols=len(HEADER))
        ws.append_row(HEADER, value_input_option="RAW")
        return ws

    first_row = ws.row_values(1)
    if not first_row:
        ws.append_row(HEADER, value_input_option="RAW")
    return ws


def existing_usernames(ws):
    """Every handle already in the sheet — the accounts we must never refetch."""
    try:
        values = ws.get_all_values()
    except Exception as exc:  # noqa: BLE001
        raise SheetError(f"could not read the sheet: {exc}") from exc

    if not values:
        return set()

    header = [h.strip().lower() for h in values[0]]
    try:
        column = header.index("username")
    except ValueError:
        return set()

    return {
        row[column].strip().lower()
        for row in values[1:]
        if len(row) > column and row[column].strip()
    }


def to_row(record, stamp):
    """One lead as a flat list in HEADER order."""
    keywords = record.get("keywords") or []
    values = {
        "profile_url": f"https://www.instagram.com/{record.get('username', '')}/",
        "username": record.get("username", ""),
        "full_name": record.get("full_name", ""),
        "follower_count": record.get("follower_count", ""),
        "posts_last_week": record.get("posts_last_week", ""),
        "latest_post": record.get("latest_post", ""),
        "category": record.get("category", ""),
        "biography": (record.get("biography") or "").replace("\n", " ").strip(),
        "external_url": record.get("external_url", ""),
        "is_verified": record.get("is_verified", ""),
        "media_count": "" if record.get("media_count") is None else record["media_count"],
        # One keyword per lead now — the niche this account was hunted for.
        "keyword": record.get("matched_keyword") or (keywords[0] if keywords else ""),
        "found_via": record.get("found_via", ""),
        "delivered_on": stamp,
    }
    return [values[column] for column in HEADER]


def append_leads(ws, records, stamp):
    """Append leads to the bottom of the sheet. Returns the rows written."""
    rows = [to_row(r, stamp) for r in records]
    if not rows:
        return rows
    try:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    except Exception as exc:  # noqa: BLE001
        raise SheetError(f"could not append to the sheet: {exc}") from exc
    return rows
