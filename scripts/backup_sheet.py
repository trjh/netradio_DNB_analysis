#!/usr/bin/env python3
"""Back up every tab of the Google Sheet to data/sheet/<tab>.csv.

A quick safety net (not the comprehensive solution) so the spreadsheet's data is
version-controlled in the repo. Downloads the workbook's xlsx export and converts
each worksheet to CSV. (The public gviz CSV endpoint mis-selects tabs by name, so
we read the xlsx directly.)

Stdlib only (uses `curl` for the download). Usage:
    python3 scripts/backup_sheet.py [--sheet ID]
"""
import argparse
import csv
import io
import os
import re
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "data" / "sheet"
DEFAULT_SHEET = os.environ.get("NETRADIO_SHEET_ID", "1bQ8S1v-IgOMpJAHftaSZdtLXCz3wHAfufQk8F6A3nck")
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# Never export tabs that hold credentials. Override via env (comma-separated).
SKIP_TABS = {t.strip().lower() for t in
             os.environ.get("NETRADIO_SHEET_SKIP_TABS", "SECRETS,baseurl").split(",") if t.strip()}


def download_xlsx(sheet_id):
    res = subprocess.run(
        ["curl", "-sL", "--max-time", "40",
         "https://docs.google.com/spreadsheets/d/%s/export?format=xlsx" % sheet_id],
        check=False, capture_output=True, timeout=50)
    blob = res.stdout
    if res.returncode != 0 or blob[:2] != b"PK":
        raise SystemExit("could not download the workbook xlsx (auth/sharing?)")
    return zipfile.ZipFile(io.BytesIO(blob))


def shared_strings(zf):
    try:
        xml = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    out = []
    for si in ET.fromstring(xml).findall(NS + "si"):
        out.append("".join(t.text or "" for t in si.iter(NS + "t")))
    return out


def col_index(cell_ref):
    letters = re.match(r"[A-Z]+", cell_ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def sheet_to_rows(zf, part, strings):
    root = ET.fromstring(zf.read(part))
    rows = []
    for row in root.iter(NS + "row"):
        cells = {}
        width = 0
        for c in row.findall(NS + "c"):
            idx = col_index(c.get("r"))
            ctype = c.get("t")
            if ctype == "s":
                v = c.find(NS + "v")
                value = strings[int(v.text)] if v is not None and v.text else ""
            elif ctype == "inlineStr":
                value = "".join(t.text or "" for t in c.iter(NS + "t"))
            else:
                v = c.find(NS + "v")
                value = v.text if v is not None and v.text is not None else ""
            cells[idx] = value
            width = max(width, idx + 1)
        rows.append([cells.get(i, "") for i in range(width)])
    # Trim trailing fully-empty rows.
    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()
    return rows


def worksheet_parts(zf):
    """Ordered [(tab_name, part_path)] from workbook.xml + its rels."""
    rels = {}
    for r in ET.fromstring(zf.read("xl/_rels/workbook.xml.rels")):
        rels[r.get("Id")] = "xl/" + r.get("Target").lstrip("/").replace("xl/", "", 1)
    out = []
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    for s in wb.find(NS + "sheets").findall(NS + "sheet"):
        out.append((s.get("name"), rels[s.get(RNS + "id")]))
    return out


def safe_filename(name):
    return re.sub(r'[\\/:*?"<>|]+', "-", name).strip() or "sheet"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet", default=DEFAULT_SHEET, help="Google Sheet ID")
    args = parser.parse_args()

    zf = download_xlsx(args.sheet)
    strings = shared_strings(zf)
    parts = worksheet_parts(zf)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("sheet %s — %d tabs" % (args.sheet, len(parts)))
    for name, part in parts:
        if name.strip().lower() in SKIP_TABS:
            print("  SKIP %-28s (excluded — may hold secrets)" % name)
            continue
        rows = sheet_to_rows(zf, part, strings)
        path = OUT_DIR / (safe_filename(name) + ".csv")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
        print("  ok   %-28s -> %s (%d rows)" % (name, path.name, len(rows)))
    print("backed up %d tabs into %s" % (len(parts), OUT_DIR))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
