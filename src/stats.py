"""Local archive stats for the idle ticker.

Reads the per-run meta.json files under the local archive and aggregates them
into a short scrolling message - no network calls, just what this box has
actually captured and uploaded.
"""
import logging
import os

import storage

log = logging.getLogger("wardrive.stats")


def local_stats(local_dir):
    """Aggregate all runs in the local archive. Returns counts dict."""
    runs = 0
    nets = 0
    pending = 0
    last_stamp = ""
    last_nets = 0
    if os.path.isdir(local_dir):
        for device in os.listdir(local_dir):
            dpath = os.path.join(local_dir, device)
            if not os.path.isdir(dpath):
                continue
            for run in os.listdir(dpath):
                meta = storage.load_meta(os.path.join(dpath, run))
                if not meta:
                    continue
                runs += 1
                kept = int((meta.get("stats") or {}).get("kept_rows") or 0)
                nets += kept
                if not meta.get("upload_complete"):
                    pending += 1
                stamp = meta.get("stamp", "")
                if stamp >= last_stamp:           # stamps sort chronologically
                    last_stamp, last_nets = stamp, kept
    return {"runs": runs, "nets": nets, "last": last_nets, "pending": pending}


def build_message(cfg):
    """Build the idle ticker from local archive stats, e.g.
    'NETS 2239616   RUNS 9   LAST 26053'. Empty archive -> a friendly prompt."""
    s = local_stats(cfg.get("archive", "local_dir"))
    if s["runs"] == 0:
        return "NO DATA YET"
    parts = ["NETS {}".format(s["nets"]),
             "RUNS {}".format(s["runs"]),
             "LAST {}".format(s["last"])]
    if s["pending"]:
        parts.append("PENDING {}".format(s["pending"]))
    msg = "   ".join(parts)
    log.info("stats ticker: %s", msg)
    return msg
