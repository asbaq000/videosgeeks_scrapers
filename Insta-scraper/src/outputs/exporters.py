"""
Writers for the scraped records.

JSON is the primary format (that's what the README documents). CSV is here
because spreadsheets are where a lot of this data actually ends up — the
nested location_data is flattened into location_city / location_latitude /
location_longitude columns so the file stays rectangular.
"""

import csv
import json
import os

CSV_FIELDS = [
    "username",
    "full_name",
    "biography",
    "external_url",
    "category",
    "follower_count",
    "following_count",
    "is_verified",
    "media_count",
    "profile_pic_url_hd",
    "account_type",
    "location_city",
    "location_latitude",
    "location_longitude",
]


def _ensure_parent_dir(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_json(records, path, pretty=True):
    _ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2 if pretty else None, ensure_ascii=False)
    return path


def write_jsonl(records, path):
    """One JSON object per line — friendlier for streaming into a pipeline."""
    _ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def _flatten(record):
    location = record.get("location_data") or {}
    flat = {key: record.get(key, "") for key in CSV_FIELDS}
    flat["location_city"] = location.get("city_name", "")
    flat["location_latitude"] = location.get("latitude", 0)
    flat["location_longitude"] = location.get("longitude", 0)
    return flat


def write_csv(records, path):
    _ensure_parent_dir(path)
    # utf-8-sig, not utf-8: without the BOM, Excel mangles every emoji and
    # accented character in the bios.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow(_flatten(record))
    return path


WRITERS = {
    "json": lambda records, path, pretty: write_json(records, path, pretty),
    "jsonl": lambda records, path, pretty: write_jsonl(records, path),
    "csv": lambda records, path, pretty: write_csv(records, path),
}


def export(records, path, fmt="json", pretty=True):
    fmt = (fmt or "json").lower()
    if fmt not in WRITERS:
        raise ValueError(f"unsupported output format: {fmt} (use one of {sorted(WRITERS)})")
    return WRITERS[fmt](records, path, pretty)
