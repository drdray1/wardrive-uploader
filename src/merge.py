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
import gzip
import io
import logging
import os
import time

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


# Always skipped (OS metadata/trash), in addition to configured exclude_dirs.
_SYSTEM_DIRS = {
    "system volume information", ".spotlight-v100", ".fseventsd",
    ".trashes", ".trash-1000", ".documentrevisions-v100", ".temporaryitems",
}
# WiGLE logs come as .csv, .wiglecsv, or .log (Marauder/Bruce write .log).
_WIGLE_EXTS = (".csv", ".wiglecsv", ".log")


def discover(mountpoint, exclude_dirs):
    """Recursively find WiGLE log files under mountpoint. Matches by extension
    AND content (first line starts with 'WigleWifi-'), skipping excluded and
    system dirs plus macOS '._' AppleDouble sidecar files."""
    exclude = {d.lower() for d in exclude_dirs} | _SYSTEM_DIRS
    found = []
    for root, dirs, files in os.walk(mountpoint):
        dirs[:] = [d for d in dirs if d.lower() not in exclude]
        for name in files:
            if name.startswith("._"):                 # macOS AppleDouble junk
                continue
            if not name.lower().endswith(_WIGLE_EXTS):
                continue
            full = os.path.join(root, name)
            if is_wigle_csv(full):
                found.append(full)
    found.sort()
    return found


def _read_header(path):
    """Cheaply read just the two header lines (raw, including newlines).
    Returns (preheader_line, colheader_line) or (None, None)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            pre = f.readline()
            col = f.readline()
    except OSError:
        return None, None
    if not pre.startswith(WIGLE_SIGNATURE):
        return None, None
    return pre, col


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


def _ensure_nl(s):
    return s if s.endswith("\n") else s + "\n"


def _resolve(paths, normalize_to, dedup):
    """Work out versions + the two header lines, and whether to use the
    field-parse path (needed for normalization). Returns a dict."""
    versions = {detect_version(p) for p in paths}
    versions.discard(None)
    if len(versions) > 1:
        log.warning("multiple WiGLE versions on one card %s; normalizing to 1.6", versions)
        normalize_to = "1.6"
    source_version = sorted(versions)[0] if versions else "1.6"
    out_version = normalize_to or source_version
    use_fields = normalize_to is not None or dedup == "fields"

    # Header lines (raw strings ending in \n).
    pre, col = None, None
    for p in paths:
        rp, rc = _read_header(p)
        if rp is not None:
            pre, col = rp, rc
            break
    default_col = ",".join(COLUMNS_16 if out_version == "1.6" else COLUMNS_14) + "\r\n"
    if pre is None:
        pre = "WigleWifi-" + out_version + "\r\n"
        col = default_col
    if use_fields:
        # Canonicalize: fix version token in pre-header, use canonical columns.
        parts = pre.rstrip("\r\n").split(",")
        parts[0] = "WigleWifi-" + out_version
        pre = ",".join(parts) + "\r\n"
        col = default_col
    elif not col:
        col = default_col

    return {
        "source_version": source_version,
        "out_version": out_version,
        "normalize_to": normalize_to,
        "use_fields": use_fields,
        "dedup": "fields" if use_fields else dedup,
        "pre": _ensure_nl(pre),
        "col": _ensure_nl(col),
    }


def _row_to_line(row):
    buf = io.StringIO()
    csv.writer(buf).writerow(row)
    return buf.getvalue()


def _stream(paths, info, emit):
    """Iterate data rows across all files, calling emit(line) for each kept row.
    Returns (total_rows, kept_rows). Constant memory aside from the dedup set."""
    total = kept = 0
    if info["use_fields"]:
        seen = set()
        normalize_to = info["normalize_to"]
        for p in paths:
            ver = detect_version(p)
            _, _, data = _read_parts(p)
            for row in data:
                total += 1
                if normalize_to == "1.6" and ver == "1.4":
                    row = _row_14_to_16(row)
                key = _dedup_key(row)
                if key in seen:
                    continue
                seen.add(key)
                emit(_row_to_line(row))
                kept += 1
        return total, kept

    do_dedup = info["dedup"] != "none"
    seen = set()              # hashes of data lines; a few MB at most
    for p in paths:
        try:
            f = open(p, "r", encoding="utf-8", errors="replace", newline="")
        except OSError as e:
            log.error("cannot read %s: %s", p, e)
            continue
        with f:
            f.readline()      # skip pre-header
            f.readline()      # skip column header
            for line in f:
                if not line.strip():
                    continue
                total += 1
                if do_dedup:
                    key = hash(line.rstrip("\n").rstrip("\r"))
                    if key in seen:
                        continue
                    seen.add(key)
                emit(_ensure_nl(line))
                kept += 1
    return total, kept


def _base_stats(paths, info):
    return {
        "source_version": info["source_version"],
        "out_version": info["out_version"],
        "device": device_for_version(info["source_version"]),
        "input_files": len(paths),
        "dedup_mode": info["dedup"],
    }


def merge(paths, out_path, normalize_to=None, dedup="lines"):
    """Merge WiGLE CSVs into a single out_path. Returns a stats dict.

    dedup: "lines" (fast exact-line dedup, default), "none" (concatenate),
    "fields" (parse + dedup on MAC+FirstSeen). normalize_to='1.6' upconverts a
    1.4 card and forces the field path. For size-limited uploads use merge_split.
    """
    if not paths:
        raise ValueError("no input files to merge")
    started = time.monotonic()
    info = _resolve(paths, normalize_to, dedup)
    with open(out_path, "w", encoding="utf-8", errors="replace", newline="") as out:
        out.write(info["pre"])
        out.write(info["col"])
        total, kept = _stream(paths, info, out.write)

    nbytes = os.path.getsize(out_path)
    stats = _base_stats(paths, info)
    stats.update(total_rows=total, kept_rows=kept, duplicates_removed=total - kept,
                 bytes=nbytes, oversize=nbytes > WIGLE_MAX_BYTES)
    log.info("merged %s files -> %s/%s rows (%s dupes removed), %s bytes, device=%s, "
             "mode=%s in %.1fs", stats["input_files"], kept, total,
             stats["duplicates_removed"], nbytes, stats["device"],
             stats["dedup_mode"], time.monotonic() - started)
    return stats


class _PartWriter:
    """Streams lines into rolling part files, each kept under max_bytes, every
    part starting with the same two header lines. Constant memory.

    With gzip_out=True the parts are gzip-compressed (.csv.gz) and rollover is
    decided by the COMPRESSED size, so each .gz is guaranteed under the cap
    regardless of how well the data compresses. Compressed size is sampled every
    `check_every` rows (a flush + fstat) to keep compression efficient."""

    def __init__(self, out_dir, base, header_lines, max_bytes, gzip_out=False,
                 check_every=4000):
        self.out_dir = out_dir
        self.base = base
        self.header = [h.encode("utf-8") for h in header_lines]
        self.header_bytes = sum(len(h) for h in self.header)
        self.max_bytes = max_bytes
        self.gzip_out = gzip_out
        self.ext = ".csv.gz" if gzip_out else ".csv"
        self.check_every = check_every
        self.parts = []
        self._raw = None        # underlying binary file
        self._f = None          # gzip wrapper or same as _raw
        self._bytes = 0         # uncompressed bytes (plain-mode rollover)
        self._since_check = 0
        self._has_data = False
        self._idx = 0

    def _open_new(self):
        self._idx += 1
        path = os.path.join(self.out_dir, f"{self.base}_part{self._idx:03d}{self.ext}")
        self._raw = open(path, "wb")
        self._f = gzip.GzipFile(fileobj=self._raw, mode="wb") if self.gzip_out else self._raw
        for h in self.header:
            self._f.write(h)
        self._bytes = self.header_bytes
        self._since_check = 0
        self._has_data = False
        self.parts.append(path)

    def _compressed_size(self):
        self._f.flush()
        self._raw.flush()
        return os.fstat(self._raw.fileno()).st_size

    def _should_roll(self, nbytes):
        if not self._has_data:
            return False
        if self.gzip_out:
            self._since_check += 1
            if self._since_check < self.check_every:
                return False
            self._since_check = 0
            return self._compressed_size() >= self.max_bytes
        return self._bytes + nbytes > self.max_bytes

    def write(self, line):
        data = line.encode("utf-8")
        if self._f is None:
            self._open_new()
        elif self._should_roll(len(data)):
            self.close()
            self._open_new()
        self._f.write(data)
        self._bytes += len(data)
        self._has_data = True

    def close(self):
        if self._f is not None:
            self._f.close()                       # flushes gzip trailer
            if self.gzip_out:
                self._raw.close()
            self._f = self._raw = None


def merge_split(paths, out_dir, base, max_bytes, normalize_to=None, dedup="lines",
                gzip_out=False):
    """Like merge(), but writes one or more part files each <= max_bytes (for
    services that cap upload size). gzip_out compresses parts (.csv.gz) and caps
    by compressed size. Returns (part_paths, stats)."""
    if not paths:
        raise ValueError("no input files to merge")
    started = time.monotonic()
    info = _resolve(paths, normalize_to, dedup)
    pw = _PartWriter(out_dir, base, [info["pre"], info["col"]], max_bytes, gzip_out=gzip_out)
    total, kept = _stream(paths, info, pw.write)
    pw.close()
    if not pw.parts:                       # no data rows at all - still emit one
        pw._open_new(); pw.close()

    sizes = [os.path.getsize(p) for p in pw.parts]
    stats = _base_stats(paths, info)
    stats.update(total_rows=total, kept_rows=kept, duplicates_removed=total - kept,
                 bytes=sum(sizes), num_parts=len(pw.parts), gzip=gzip_out,
                 max_part_bytes=max(sizes) if sizes else 0,
                 oversize=any(s > max_bytes for s in sizes))
    log.info("merged %s files -> %s/%s rows (%s dupes removed) into %s %spart(s), "
             "%s bytes total, device=%s, mode=%s in %.1fs",
             stats["input_files"], kept, total, stats["duplicates_removed"],
             stats["num_parts"], "gz " if gzip_out else "", stats["bytes"],
             stats["device"], stats["dedup_mode"], time.monotonic() - started)
    return pw.parts, stats
