#!/usr/bin/env python3
"""
Fetch GCMT earthquake catalog and save in CMTSOLUTION format.

Usage:
    # Search via GlobalCMT website:
    python fetch_gcmt.py -s 2000 -e 2010 -mn 6.0 -zn 150 -zx 1000 -o CMTSOLUTIONS

    # Import from ID list file:
    python fetch_gcmt.py -i gcmt_id.txt -o CMTSOLUTIONS

The ID-list file should have one GCMT ID per line. The first whitespace-separated
token is treated as the ID; the rest of the line is ignored. Both the new format
(200502020230A) and the old short format (010800G) are accepted.

Partial dates are expanded automatically:
    2020       -> 2020-01-01
    2020-03    -> 2020-03-01
    2020-03-15 -> 2020-03-15
"""

import argparse
import sys
import os
import re
import requests
from datetime import datetime
from obspy import read_events


def parse_time(s):
    """Accept YYYY, YYYY-MM, or YYYY-MM-DD strings."""
    s = s.strip()
    if re.fullmatch(r"\d{4}", s):
        return datetime(int(s), 1, 1)
    elif re.fullmatch(r"\d{4}-\d{2}", s):
        y, m = s.split("-")
        return datetime(int(y), int(m), 1)
    else:
        y, m, d = s[:10].split("-")
        return datetime(int(y), int(m), int(d))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fetch GCMT catalog events and save in CMTSOLUTION format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-s", "--start", default=None,
                        help="Start time: YYYY, YYYY-MM, or YYYY-MM-DD "
                             "(required unless --ids-file is given)")
    parser.add_argument("-e", "--end", default=None,
                        help="End time: YYYY, YYYY-MM, or YYYY-MM-DD "
                             "(required unless --ids-file is given)")
    parser.add_argument("-i", "--ids-file", default=None,
                        help="Path to a file containing GCMT IDs (one per line). "
                             "When given, the web search is skipped.")
    parser.add_argument("-mn", "--minmag", type=float, default=6.0,
                        help="Minimum magnitude (default: 6.0, web search only)")
    parser.add_argument("-mx", "--maxmag", type=float, default=10.0,
                        help="Maximum magnitude (default: 10.0, web search only)")
    parser.add_argument("--magtype", choices=["Mw", "Ms", "Mb"], default="Mw",
                        help="Magnitude type for --minmag/--maxmag filter: "
                             "Mw (moment, default), Ms (surface wave), Mb (body wave). "
                             "Web search only.")
    parser.add_argument("-zn", "--mindepth", type=float, default=0.0,
                        help="Minimum depth in km (default: 0, web search only)")
    parser.add_argument("-zx", "--maxdepth", type=float, default=1000.0,
                        help="Maximum depth in km (default: 1000, web search only)")
    parser.add_argument("-yn", "--minlat", type=float, default=-90.0,
                        help="Minimum latitude (default: -90, web search only)")
    parser.add_argument("-yx", "--maxlat", type=float, default=90.0,
                        help="Maximum latitude (default: 90, web search only)")
    parser.add_argument("-xn", "--minlon", type=float, default=-180.0,
                        help="Minimum longitude (default: -180, web search only)")
    parser.add_argument("-xx", "--maxlon", type=float, default=180.0,
                        help="Maximum longitude (default: 180, web search only)")
    parser.add_argument("-o", "--output", default="CMTSOLUTIONS",
                        help="Output filename (default: CMTSOLUTIONS)")
    return parser.parse_args()


def fetch_gcmt_ids_web(start, end, minmag, maxmag, magtype, mindepth, maxdepth,
                       minlat, maxlat, minlon, maxlon):
    """Query GlobalCMT search form and return a list of event IDs."""
    url = "https://www.globalcmt.org/cgi-bin/globalcmt-cgi-bin/CMT5/form"

    # Only the selected magnitude type is filtered; the others are left at full range.
    mag_keys = {"Mw": ("lmw", "umw"), "Ms": ("lms", "ums"), "Mb": ("lmb", "umb")}
    mag_params = {"lmw": 0, "umw": 10, "lms": 0, "ums": 10, "lmb": 0, "umb": 10}
    lo, hi = mag_keys[magtype]
    mag_params[lo] = minmag
    mag_params[hi] = maxmag

    params = {
        "itype": "ymd",
        "yr": start.year, "mo": start.month, "day": start.day,
        "otype": "ymd",
        "oyr": end.year, "omo": end.month, "oday": end.day,
        "jyr": 1976, "jday": 1, "ojyr": 1976, "ojday": 1, "nday": 1,
        **mag_params,
        "llat": minlat, "ulat": maxlat,
        "llon": minlon, "ulon": maxlon,
        "lhd": mindepth, "uhd": maxdepth,
        "lts": -9999, "uts": 9999,
        "lpe1": 0, "upe1": 90, "lpe2": 0, "upe2": 90,
        "list": 1,
    }
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()

    # Strip HTML tags, then pick lines whose first token looks like a GCMT ID.
    # Old format: 6 digits + letter (e.g. 010800G)
    # New format: 12 digits + letter (e.g. 200502020230A)
    text = re.sub(r"<[^>]+>", " ", r.text)
    ids = []
    for line in text.splitlines():
        token = line.split()[0] if line.split() else ""
        if re.fullmatch(r"\d{6}[A-Z]|\d{12}[A-Z]", token):
            ids.append(token)
    return ids


def read_ids_file(path):
    """Read GCMT IDs from a text file (first token per non-empty, non-comment line)."""
    ids = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ids.append(line.split()[0])
    return ids


def decode_old_id_date(gcmt_id):
    """Decode the date encoded in an old-format GCMT ID (MMDDYY + letter, e.g. '121800A').
    Returns (year, month, day) or None for new-format IDs."""
    if not re.fullmatch(r"\d{6}[A-Z]", gcmt_id):
        return None
    mm, dd, yy = int(gcmt_id[0:2]), int(gcmt_id[2:4]), int(gcmt_id[4:6])
    year = 2000 + yy if yy <= 30 else 1900 + yy
    return year, mm, dd


def clean_xml_file(path):
    with open(path, "r") as f:
        data = f.read()
    for old, new in [
        ("&lt;", "<"), ("&gt;", ">"),
        ("<pre>", ""), ("</pre>", ""),
        ("<html>", ""), ("</html>", ""),
        ("<body>", ""), ("</body>", ""),
    ]:
        data = data.replace(old, new)
    with open(path, "w") as f:
        f.write(data)


def fetch_by_id(gcmt_id, tmp_path):
    """Download the QuakeML for a specific GCMT ID. Returns True on success."""
    url = f"http://ds.iris.edu/spudservice/momenttensor/gcmtid/{gcmt_id}/quakeml"
    r = requests.get(url, allow_redirects=True, timeout=30)
    r.raw.decode_content = True
    with open(tmp_path, "wb") as f:
        f.write(r.content)
    clean_xml_file(tmp_path)
    with open(tmp_path, "r") as f:
        first = f.readline()
    return not first.startswith("Error 4")


def ev_to_cmt_lines(ev, gcmt_id):
    """Convert an ObsPy Event to 13 CMTSOLUTION lines. Returns None on missing data."""
    if not ev.focal_mechanisms:
        return None
    mt = ev.focal_mechanisms[0].moment_tensor
    tensor = mt.tensor

    Mwc, Mb = 0.0, 0.0
    for mag in ev.magnitudes:
        if mag.magnitude_type == "Mwc":
            Mwc = mag.mag
        elif mag.magnitude_type == "Mb":
            Mb = mag.mag

    t0 = ev.origins[0].time
    micro = str(t0.microsecond)
    line01 = (
        " PDEQ "
        + str(t0.year).rjust(4) + " " + str(t0.month).rjust(2) + " "
        + str(t0.day).rjust(2) + " " + str(t0.hour).rjust(2) + " "
        + str(t0.minute).rjust(2) + " " + str(t0.second).rjust(2)
        + "." + micro[:2] + " "
        + str(f"{ev.origins[0].latitude:-.4f}").rjust(8) + " "
        + str(f"{ev.origins[0].longitude:-.4f}").rjust(8) + " "
        + str(ev.origins[1].depth / 1000) + "  "
        + str(f"{Mb:-.1f}").rjust(3) + "  "
        + str(f"{Mwc:-.1f}").rjust(3) + " "
        + ev.event_descriptions[0].text
    )
    line02 = "event name:      " + gcmt_id
    t_centroid = ev.origins[1].time
    timeshift = t_centroid - t0
    line03 = "time shift:      " + str(f"{timeshift:.4f}").rjust(7)
    line04 = "half duration:   " + str(f"{mt.source_time_function.duration / 2:.4f}").rjust(7)
    line05 = "latitude:       " + str(f"{ev.origins[1].latitude:-.4f}").rjust(8)
    line06 = "longitude:      " + str(f"{ev.origins[1].longitude:-.4f}").rjust(8)
    line07 = "depth:          " + str(f"{ev.origins[1].depth / 1000:.4f}").rjust(8)
    line08 = "Mrr:       " + str(f"{tensor.m_rr * 1e7:-.6e}").rjust(13)
    line09 = "Mtt:       " + str(f"{tensor.m_tt * 1e7:-.6e}").rjust(13)
    line10 = "Mpp:       " + str(f"{tensor.m_pp * 1e7:-.6e}").rjust(13)
    line11 = "Mrt:       " + str(f"{tensor.m_rt * 1e7:-.6e}").rjust(13)
    line12 = "Mrp:       " + str(f"{tensor.m_rp * 1e7:-.6e}").rjust(13)
    line13 = "Mtp:       " + str(f"{tensor.m_tp * 1e7:-.6e}").rjust(13)
    return [line01, line02, line03, line04, line05, line06,
            line07, line08, line09, line10, line11, line12, line13]


def parse_cmt_lines(tmp_path, gcmt_id):
    """Parse a QuakeML file from IRIS SPUD. Returns lines, None, or 'date_mismatch'.

    For old-format IDs (e.g. '121800A'), validates the fetched event's date against
    the date encoded in the ID to guard against IRIS SPUD substring matches.
    """
    with open(tmp_path, "r") as f:
        if f.readline().startswith("Error 4"):
            return None
    cat = read_events(tmp_path, "quakeml")
    if not cat or not cat[0].focal_mechanisms:
        return None
    ev = cat[0]
    expected = decode_old_id_date(gcmt_id)
    if expected is not None:
        t0 = ev.origins[0].time
        year, month, day = expected
        if not (t0.year == year and t0.month == month and t0.day == day):
            return "date_mismatch"
    return ev_to_cmt_lines(ev, gcmt_id)


def fetch_by_old_id_gcmt(gcmt_id, year, month, day):
    """Fallback for old-format IDs: query GlobalCMT (list=4 → CMTSOLUTION format)
    and return the 13-line CMTSOLUTION block for gcmt_id, or None on failure."""
    from datetime import timedelta
    end_dt = datetime(year, month, day) + timedelta(days=1)
    url = "https://www.globalcmt.org/cgi-bin/globalcmt-cgi-bin/CMT5/form"
    params = {
        "itype": "ymd", "yr": year, "mo": month, "day": day,
        "otype": "ymd", "oyr": end_dt.year, "omo": end_dt.month, "oday": end_dt.day,
        "jyr": 1976, "jday": 1, "ojyr": 1976, "ojday": 1, "nday": 1,
        "lmw": 0, "umw": 10, "lms": 0, "ums": 10, "lmb": 0, "umb": 10,
        "llat": -90, "ulat": 90, "llon": -180, "ulon": 180,
        "lhd": 0, "uhd": 1000,
        "lts": -9999, "uts": 9999,
        "lpe1": 0, "upe1": 90, "lpe2": 0, "upe2": 90,
        "list": 4,  # CMTSOLUTION format output
    }
    r = requests.get(url, params=params, timeout=60)
    if not r.ok:
        return None

    # Strip HTML tags, keep non-empty lines
    lines = [l for l in re.sub(r"<[^>]+>", "", r.text).splitlines() if l.strip()]

    # Each CMTSOLUTION block is 13 lines; the event name appears in line 2 (i+1).
    # Search for the line containing "event name: ... gcmt_id"
    for i, line in enumerate(lines):
        if "event name:" in line and gcmt_id in line:
            block_start = i - 1  # step back to the PDE line
            if block_start >= 0 and block_start + 12 < len(lines):
                return lines[block_start:block_start + 13]
    return None


def download_and_parse(ids, tmp_path):
    """Fetch QuakeML and build CMTSOLUTION records for each ID."""
    records, failed, skipped = [], 0, 0
    total = len(ids)
    for i, gid in enumerate(ids):
        try:
            lines = None
            if not fetch_by_id(gid, tmp_path):
                failed += 1
                print(f"  [!] {gid}: not found")
            else:
                lines = parse_cmt_lines(tmp_path, gid)
                if lines == "date_mismatch":
                    # IRIS SPUD returned the wrong event via substring match.
                    # Fall back to GlobalCMT which outputs CMTSOLUTION directly.
                    date = decode_old_id_date(gid)
                    lines = fetch_by_old_id_gcmt(gid, *date) if date else None
                    if lines is None:
                        failed += 1
                        print(f"  [!] {gid}: not found on GlobalCMT fallback")
                    else:
                        records.append(lines)
                elif lines is None:
                    skipped += 1
                else:
                    records.append(lines)
        except Exception as exc:
            failed += 1
            print(f"  [!] {gid}: exception – {exc}")

        pct = (i + 1) / total * 100 if total else 100.0
        print(
            f"  ok={len(records):>5}  skipped={skipped:>4}  failed={failed:>4}"
            f"  total={i+1:>5}/{total}  ({pct:.1f}%)",
            end="\r",
        )
    print()
    return records, failed, skipped


def main():
    args = parse_args()

    if not args.ids_file and (not args.start or not args.end):
        print("Error: --start/--end are required unless --ids-file is given.",
              file=sys.stderr)
        sys.exit(2)

    tmp_path = "temp_gcmt.xml"
    print("Global CMT solutions downloader")

    if args.ids_file:
        ids = read_ids_file(args.ids_file)
        print(f"  Mode    : ID list ({args.ids_file})")
        print(f"  IDs     : {len(ids)}")
        print(f"  Output  : {args.output}")
        print()
    else:
        start = parse_time(args.start)
        end = parse_time(args.end)
        print(f"  Mode    : GlobalCMT web search")
        print(f"  Time    : {start.date()}  ->  {end.date()}")
        print(f"  Mag     : {args.minmag} – {args.maxmag}  ({args.magtype})")
        print(f"  Depth   : {args.mindepth} – {args.maxdepth} km")
        print(f"  Lat/Lon : [{args.minlat}, {args.maxlat}] / [{args.minlon}, {args.maxlon}]")
        print(f"  Output  : {args.output}")
        print()
        print("Fetching event IDs from GlobalCMT...")
        ids = fetch_gcmt_ids_web(
            start, end,
            args.minmag, args.maxmag, args.magtype,
            args.mindepth, args.maxdepth,
            args.minlat, args.maxlat,
            args.minlon, args.maxlon,
        )
        print(f"{len(ids)} event(s) found\n")

    records, failed, skipped = download_and_parse(ids, tmp_path)

    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    if not records:
        print("No events retrieved. Output file not written.")
        sys.exit(1)

    with open(args.output, "w") as f:
        for lines in records:
            for line in lines:
                f.write(line + "\n")
            f.write("\n")

    print(f"\nDone. {len(records)} written, {skipped} skipped, "
          f"{failed} failed  →  '{args.output}'.")


if __name__ == "__main__":
    main()
