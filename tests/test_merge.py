"""Offline tests for WiGLE CSV detection, merge, dedup and normalization.

Run from the repo root:   python3 -m pytest tests/ -q
                or:        python3 tests/test_merge.py
These only import merge.py (stdlib-only) so they run anywhere - no Pi needed.
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import merge  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(__file__), "samples")
M1 = os.path.join(SAMPLES, "marauder1.csv")
M2 = os.path.join(SAMPLES, "marauder2.csv")
P1 = os.path.join(SAMPLES, "piglet1.csv")


def _read(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


def test_detect_version_and_device():
    assert merge.detect_version(M1) == "1.4"
    assert merge.detect_version(P1) == "1.6"
    assert merge.device_for_version("1.4") == "marauder"
    assert merge.device_for_version("1.6") == "piglet"


def test_is_wigle_csv():
    assert merge.is_wigle_csv(M1)
    assert not merge.is_wigle_csv(__file__)


def test_discover_finds_only_wigle(tmp_path=None):
    base = tmp_path or tempfile.mkdtemp()
    base = str(base)
    # Lay down a wigle file, a decoy, and an excluded archive dir.
    import shutil
    shutil.copy(M1, os.path.join(base, "wardrive_1.csv"))
    with open(os.path.join(base, "notes.csv"), "w") as f:
        f.write("hello,world\n1,2\n")
    os.makedirs(os.path.join(base, "archive"), exist_ok=True)
    shutil.copy(M2, os.path.join(base, "archive", "old.csv"))
    found = merge.discover(base, ["archive"])
    assert len(found) == 1
    assert found[0].endswith("wardrive_1.csv")


def test_merge_lines_default_dedups_exact():
    # Default "lines" mode: M2's EE:02 row is byte-identical to M1's -> removed.
    out = tempfile.mktemp(suffix=".csv")
    stats = merge.merge([M1, M2], out)
    rows = _read(out)
    assert rows[0][0] == "WigleWifi-1.4"
    data = rows[2:]
    macs_times = {(r[0], r[3]) for r in data}
    assert ("AA:BB:CC:DD:EE:02", "2024-06-01 10:01:00") in macs_times
    # EE:01 at 11:00 is a different line, so it's kept (not a byte-dup).
    assert ("AA:BB:CC:DD:EE:01", "2024-06-01 11:00:00") in macs_times
    assert stats["dedup_mode"] == "lines"
    assert stats["duplicates_removed"] == 1
    assert stats["device"] == "marauder"
    assert stats["source_version"] == "1.4"


def test_merge_none_concatenates():
    out = tempfile.mktemp(suffix=".csv")
    stats = merge.merge([M1, M2], out, dedup="none")
    assert stats["duplicates_removed"] == 0
    assert stats["kept_rows"] == stats["total_rows"] == 6  # 3 + 3, nothing dropped


def test_merge_fields_dedups_mac_firstseen():
    out = tempfile.mktemp(suffix=".csv")
    stats = merge.merge([M1, M2], out, dedup="fields")
    assert stats["dedup_mode"] == "fields"
    assert stats["duplicates_removed"] == 1
    assert stats["device"] == "marauder"


def test_normalize_14_to_16():
    out = tempfile.mktemp(suffix=".csv")
    stats = merge.merge([M1], out, normalize_to="1.6")
    rows = _read(out)
    assert rows[0][0] == "WigleWifi-1.6"
    header = rows[1]
    assert header == merge.COLUMNS_16
    # First data row: Frequency column (index 5) should be empty, Type last.
    first = rows[2]
    assert len(first) == len(merge.COLUMNS_16)
    assert first[5] == ""          # Frequency unknown from 1.4
    assert first[-1] == "WIFI"     # Type preserved
    assert stats["out_version"] == "1.6"
    assert stats["dedup_mode"] == "fields"   # normalization forces field path


def test_piglet_16_passthrough():
    out = tempfile.mktemp(suffix=".csv")
    stats = merge.merge([P1], out)
    rows = _read(out)
    assert rows[0][0] == "WigleWifi-1.6"
    assert stats["device"] == "piglet"
    assert stats["kept_rows"] == 2


if __name__ == "__main__":
    # Minimal runner without pytest.
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            print(f"ERROR {fn.__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
