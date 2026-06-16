"""WiGLE CSV discovery, version detection, merge and dedup.

WiGLE CSV files have two header lines:
  line 1: pre-header, e.g. "WigleWifi-1.4,appRelease=...,model=..."
  line 2: column header, e.g. "MAC,SSID,AuthMode,FirstSeen,Channel,..."
followed by data rows.

On this appliance a single card holds logs from ONE device, so every file on a
card shares one WiGLE version (Marauder -> 1.4, Piglet -> 1.6). We still detect
the version and can normalize 1.4 -> 1.6, but never mix versions in one output.
"""
import csv
import io
import logging
import os

log = logging.getLogger("wardrive.merge")

WIGLE_SIGNATURE = "WigleWifi-"
WIGLE_MAX_BYTES = 180 * 1024 * 1024  # WiGLE per-file upload limit (180 MiB)

# 1.6 added columns after the original 1.4 set. We pad 1.4 rows to 1.6 width.
COLUMNS_14 = [
    "MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "RSSI",
    "CurrentLatitude", "CurrentLongitude", "AltitudeMeters", "AccuracyMeters", "Type",
]
COLUMNS_16 = [
    "MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "Frequency", "RSSI",
    "CurrentLatitude", "CurrentLongitude", "AltitudeMeters", "AccuracyMeters",
    "RCOIs", "MfgrId", "Type",
]

DEVICE_BY_VERSION = {"1.4": "marauder", "1.6": "piglet"}


def is_wigle_csv(path):
    """True if the file's first line looks like a WiGLE pre-header."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline()
        return first.startswith(WIGLE_SIGNATURE)
    except OSError:
        return False


def detect_version(path):
    """Return version string like '1.4' / '1.6', or None if not a WiGLE file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
    except OSError:
        return None
    if not first.startswith(WIGLE_SIGNATURE):
        return None
    token = first.split(",", 1)[0]          # "WigleWifi-1.6"
    return token[len(WIGLE_SIGNATURE):] or None


def device_for_version(version):
    return DEVICE_BY_VERSION.get(version, "unknown")


def discover(mountpoint, exclude_dirs):
    """Recursively find WiGLE CSV files under mountpoint, skipping excluded dirs."""
    exclude = {d.lower() for d in exclude_dirs}
    found = []
    for root, dirs, files in os.walk(mountpoint):
        dirs[:] = [d for d in dirs if d.lower() not in exclude]
        for name in files:
            if not name.lower().endswith((".csv", ".wiglecsv")):
                continue
            full = os.path.join(root, name)
            if is_wigle_csv(full):
                found.append(full)
    found.sort()
    return found


def _read_parts(path):
    """Return (preheader_line, column_header_line, list_of_data_rows)."""
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        content = f.read()
    reader = csv.reader(io.StringIO(content))
    rows = list(reader)
    if len(rows) < 2:
        return None, None, []
    preheader = rows[0]
    colheader = rows[1]
    data = [r for r in rows[2:] if r and any(c.strip() for c in r)]
    return preheader, colheader, data


def _row_14_to_16(row):
    """Map a 1.4 data row to 1.6 column order (insert empty Frequency/RCOIs/MfgrId)."""
    # 1.4: MAC,SSID,Auth,First,Chan,RSSI,Lat,Lon,Alt,Acc,Type
    # 1.6: MAC,SSID,Auth,First,Chan,Freq,RSSI,Lat,Lon,Alt,Acc,RCOIs,MfgrId,Type
    r = (row + [""] * len(COLUMNS_14))[: len(COLUMNS_14)]
    return [
        r[0], r[1], r[2], r[3], r[4],   # MAC..Channel
        "",                             # Frequency (unknown in 1.4)
        r[5],                           # RSSI
        r[6], r[7], r[8], r[9],         # Lat,Lon,Alt,Acc
        "", "",                         # RCOIs, MfgrId
        r[10],                          # Type
    ]


def _dedup_key(row):
    # MAC + FirstSeen uniquely identifies an observation across files.
    mac = row[0].strip().upper() if len(row) > 0 else ""
    first_seen = row[3].strip() if len(row) > 3 else ""
    return (mac, first_seen)


def merge(paths, out_path, normalize_to=None):
    """Merge same-version WiGLE CSVs into out_path. Returns a stats dict.

    normalize_to: optional '1.6' to upconvert a 1.4 card. Default keeps the
    card's native version (no mixing happens because one card = one device).
    """
    if not paths:
        raise ValueError("no input files to merge")

    versions = {detect_version(p) for p in paths}
    versions.discard(None)
    if len(versions) > 1:
        # Shouldn't happen (one device per card) but be safe: normalize to 1.6.
        log.warning("multiple WiGLE versions on one card %s; normalizing to 1.6", versions)
        normalize_to = "1.6"
    source_version = sorted(versions)[0] if versions else "1.6"
    out_version = normalize_to or source_version

    # Choose output header from the first file matching the output version,
    # else synthesize a minimal pre-header.
    preheader, colheader = None, None
    for p in paths:
        ph, ch, _ = _read_parts(p)
        if ph is None:
            continue
        if detect_version(p) == out_version or normalize_to is None:
            preheader = ph
            colheader = COLUMNS_16 if out_version == "1.6" else COLUMNS_14
            # Keep the real column header if present and we're not upconverting.
            if normalize_to is None and ch:
                colheader = ch
            break
    if preheader is None:
        preheader = ["WigleWifi-" + out_version, "appRelease=wardrive-uploader"]
        colheader = COLUMNS_16 if out_version == "1.6" else COLUMNS_14

    seen = set()
    total_rows = 0
    kept_rows = 0
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(preheader)
    writer.writerow(colheader)

    for p in paths:
        ver = detect_version(p)
        _, _, data = _read_parts(p)
        for row in data:
            total_rows += 1
            if normalize_to == "1.6" and ver == "1.4":
                row = _row_14_to_16(row)
            key = _dedup_key(row)
            if key in seen:
                continue
            seen.add(key)
            writer.writerow(row)
            kept_rows += 1

    payload = buf.getvalue().encode("utf-8")
    with open(out_path, "wb") as f:
        f.write(payload)

    stats = {
        "source_version": source_version,
        "out_version": out_version,
        "device": device_for_version(source_version),
        "input_files": len(paths),
        "total_rows": total_rows,
        "kept_rows": kept_rows,
        "duplicates_removed": total_rows - kept_rows,
        "bytes": len(payload),
        "oversize": len(payload) > WIGLE_MAX_BYTES,
    }
    log.info("merged %s files -> %s rows (%s dupes removed), %s bytes, device=%s",
             stats["input_files"], stats["kept_rows"],
             stats["duplicates_removed"], stats["bytes"], stats["device"])
    return stats
