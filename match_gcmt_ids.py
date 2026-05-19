#!/usr/bin/env python3
"""
Match earthquake events from a list file to GCMT event IDs.

Reads a flexible input file with date/lon/lat/mag (and optional depth),
queries the GlobalCMT catalog with a narrow window per event, and writes
the matching GCMT event IDs to a file compatible with `fetch_gcmt.py -i`.

Input file (whitespace-separated; '#' marks comments):

    # Canonical (no units):
    2014/08/18  47.53  32.59  6.2  12.0

    # Table style (also accepted — units / "Mw" tag / leading ID / trailing text):
    1  2014/08/18  47.53  32.59  12.0km  Mw 6.2  Iran-Iraq Border  85-110

Required columns: date  lon  lat  mag
Optional        : leading integer ID, depth (with or without "km"),
                  any non-numeric trailing text (region, distance, ...).
Dates accept YYYY/MM/DD or YYYY-MM-DD.

When the canonical 4-number form (lon lat mag depth) has both values in 0-10,
ordering is ambiguous; in that case put magnitude first.

Usage:
    python match_gcmt_ids.py events_input.txt -o gcmt_ids.txt
    python fetch_gcmt.py -i gcmt_ids.txt -o CMTSOLUTIONS
"""

import argparse
import math
import re
import sys
from datetime import datetime
import requests


def parse_date(s):
    """Accept YYYY/MM/DD or YYYY-MM-DD."""
    return datetime.strptime(s.replace("/", "-"), "%Y-%m-%d")


def parse_event_line(line):
    """Parse one event line. Returns dict or None if the line is empty/unparseable."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    tokens = s.split()

    date_idx = None
    for i, t in enumerate(tokens):
        if re.match(r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}$", t):
            date_idx = i
            break
    if date_idx is None:
        return None
    try:
        date = parse_date(tokens[date_idx])
    except ValueError:
        return None
    rest = tokens[date_idx + 1:]

    depth, mag = None, None
    plain_nums = []
    i = 0
    while i < len(rest):
        t = rest[i]
        # "Mw 6.2" / "Ms 7.0" / "Mb 5.5"
        if t.lower() in ("mw", "ms", "mb") and i + 1 < len(rest):
            try:
                mag = float(rest[i + 1])
                i += 2
                continue
            except ValueError:
                pass
        # "12.0km"
        m = re.match(r"^([+-]?\d+(?:\.\d+)?)km$", t, re.IGNORECASE)
        if m:
            depth = float(m.group(1))
            i += 1
            continue
        try:
            plain_nums.append(float(t))
            i += 1
        except ValueError:
            break  # non-numeric → trailing free text, stop scanning

    if len(plain_nums) < 2:
        return None
    lon, lat = plain_nums[0], plain_nums[1]

    # Remaining numbers: assume canonical mag-then-depth order, but use the
    # 0-10 heuristic so we still parse correctly when depth happens to come first.
    for v in plain_nums[2:]:
        if mag is None and 0 < v <= 10:
            mag = v
        elif depth is None:
            depth = v
    if mag is None:
        return None

    return {"date": date, "lon": lon, "lat": lat, "mag": mag, "depth": depth, "raw": s}


def read_events_file(path):
    events = []
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            ev = parse_event_line(line)
            if ev is None:
                if line.strip() and not line.strip().startswith("#"):
                    print(f"  [!] line {lineno}: could not parse: {line.strip()}",
                          file=sys.stderr)
                continue
            ev["lineno"] = lineno
            events.append(ev)
    return events


def query_gcmt(date, lon, lat, mag, dlat, dlon, dmag):
    """Query GlobalCMT for a narrow window around (date, lon, lat, mag).

    Returns a list of {id, lat, lon, mw} for every CMTSOLUTION block in the response.
    """
    url = "https://www.globalcmt.org/cgi-bin/globalcmt-cgi-bin/CMT5/form"
    params = {
        "itype": "ymd",
        "yr": date.year, "mo": date.month, "day": date.day,
        "otype": "ymd",
        "oyr": date.year, "omo": date.month, "oday": date.day,
        "jyr": 1976, "jday": 1, "ojyr": 1976, "ojday": 1, "nday": 1,
        "lmw": max(0.0, mag - dmag), "umw": min(10.0, mag + dmag),
        "lms": 0, "ums": 10, "lmb": 0, "umb": 10,
        "llat": max(-90.0, lat - dlat), "ulat": min(90.0, lat + dlat),
        "llon": max(-180.0, lon - dlon), "ulon": min(180.0, lon + dlon),
        "lhd": 0, "uhd": 1000,
        "lts": -9999, "uts": 9999,
        "lpe1": 0, "upe1": 90, "lpe2": 0, "upe2": 90,
        "list": 4,  # CMTSOLUTION format
    }
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()

    text = re.sub(r"<[^>]+>", "", r.text)
    lines = [l for l in text.splitlines() if l.strip()]

    out = []
    for i, line in enumerate(lines):
        if "event name:" not in line or i < 1 or i + 11 >= len(lines):
            continue
        block = lines[i - 1:i + 12]
        try:
            evid = block[1].split(":", 1)[1].strip().split()[0]
            lat_c = float(block[4].split(":", 1)[1].strip())
            lon_c = float(block[5].split(":", 1)[1].strip())
        except (ValueError, IndexError):
            continue
        out.append({"id": evid, "lat": lat_c, "lon": lon_c})
    return out


def angular_distance(lon1, lat1, lon2, lat2):
    """Great-circle distance in degrees (haversine)."""
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2
    return math.degrees(2 * math.asin(math.sqrt(a)))


def parse_args():
    p = argparse.ArgumentParser(
        description="Match earthquake events to GCMT IDs by date + location + magnitude",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See module docstring for input file format.",
    )
    p.add_argument("input", help="Input event list file")
    p.add_argument("-o", "--output", default="gcmt_ids.txt",
                   help="Output ID file (default: gcmt_ids.txt)")
    p.add_argument("--dlat", type=float, default=1.0,
                   help="Latitude search window in degrees, ± (default: 1.0)")
    p.add_argument("--dlon", type=float, default=1.0,
                   help="Longitude search window in degrees, ± (default: 1.0)")
    p.add_argument("--dmag", type=float, default=0.3,
                   help="Magnitude search window, ± (default: 0.3)")
    return p.parse_args()


def main():
    args = parse_args()
    events = read_events_file(args.input)
    print(f"Loaded {len(events)} event(s) from {args.input}")
    print(f"Search window: ±{args.dlat}° lat, ±{args.dlon}° lon, ±{args.dmag} mag\n")

    out_lines, matched, ambiguous, missing = [], 0, 0, 0
    for ev in events:
        d = ev["date"].strftime("%Y-%m-%d")
        info = f"{d}  lon={ev['lon']:+8.2f}  lat={ev['lat']:+7.2f}  mag={ev['mag']:.1f}"
        try:
            cands = query_gcmt(ev["date"], ev["lon"], ev["lat"], ev["mag"],
                               args.dlat, args.dlon, args.dmag)
        except Exception as e:
            print(f"  [!] {info}: query failed - {e}")
            missing += 1
            continue
        if not cands:
            print(f"  [!] {info}: no GCMT match")
            missing += 1
            continue

        scored = sorted(
            ((angular_distance(ev["lon"], ev["lat"], c["lon"], c["lat"]), c) for c in cands),
            key=lambda x: x[0],
        )
        best_d, best = scored[0]
        if len(scored) > 1:
            ambiguous += 1
            others = ", ".join(f"{c['id']}(Δ={d_:.2f}°)" for d_, c in scored[1:])
            print(f"  [?] {info}: {len(scored)} matches; "
                  f"chose {best['id']} Δ={best_d:.2f}°; others: {others}")
        else:
            print(f"  [+] {info}: {best['id']}  Δ={best_d:.2f}°")
        out_lines.append(f"{best['id']}  # {info}")
        matched += 1

    with open(args.output, "w") as f:
        f.write("\n".join(out_lines) + ("\n" if out_lines else ""))
    print(f"\nDone. matched={matched}, ambiguous={ambiguous}, missing={missing}  →  {args.output}")
    if matched == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
