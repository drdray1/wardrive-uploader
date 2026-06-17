#!/usr/bin/env python3
"""Wardrive upload appliance - main service.

Loop forever: wait for a USB SD card -> mount -> discover WiGLE CSVs -> merge ->
write local archive -> upload (WiGLE + wdgowars) -> archive on card -> show DONE
-> wait for removal -> repeat.

Run modes:
  (no args)        run as the appliance service (udev-driven)
  --once DEV       process a single device node and exit (e.g. /dev/sda1)
  --dry-run DEV    process but do NOT upload or move/archive originals
  --test-display   cycle the Scroll pHAT through every status, then exit
"""
import argparse
import logging
import os
import signal
import sys
import time

# Allow running both as `python src/main.py` and as an installed module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import display as disp
import merge
import storage
import upload
from upload_manager import UploadManager
from wuconfig import Config

log = logging.getLogger("wardrive.main")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _utc_stamp():
    # No Date.now() worries here - plain service runtime.
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


class Appliance:
    """Handles only the FAST, card-mounted stage: copy the logs off the card,
    move originals into the on-card archive, unmount, and hand off to the
    background UploadManager for merge + upload. Goal: minimal card time."""

    SAFE_REMOVE_HOLD = 2.0   # seconds to show "safe to remove" before handoff

    def __init__(self, cfg, display, manager, dry_run=False):
        self.cfg = cfg
        self.display = display
        self.manager = manager
        self.dry_run = dry_run
        self.required = set(cfg.getlist("upload", "required"))

    def process(self, devnode):
        mountpoint = None
        stamp = _utc_stamp()
        archive_dir = None
        copied = []
        device = "unknown"
        ok = False
        try:
            self.display.set_state(disp.SCANNING)
            mountpoint = storage.mount(devnode)

            exclude = self.cfg.getlist("scan", "exclude_dirs")
            sources = merge.discover(mountpoint, exclude)
            if not sources:
                log.info("no WiGLE CSVs found on %s", devnode)
                self.display.set_state(disp.NONE_FOUND)
                return
            log.info("found %s WiGLE CSV file(s)", len(sources))
            device = merge.device_for_version(merge.detect_version(sources[0]))
            log.info("device=%s, local free space: %s MB", device, storage.free_mb("/"))

            # FAST: copy raw logs off the card to the local archive.
            self.display.set_state(disp.COPYING)
            meta = {
                "stamp": stamp, "device": device, "devnode": devnode,
                "sources": [os.path.basename(s) for s in sources],
                "uploads": [], "attempts": 0, "upload_complete": False,
                "required": sorted(self.required), "dry_run": self.dry_run,
            }
            local_dir = self.cfg.get("archive", "local_dir")
            archive_dir, copied = storage.start_local_archive(
                local_dir, device, stamp, sources, meta)
            storage.prune_local_archive(
                local_dir,
                self.cfg.getint("archive", "retention_runs"),
                self.cfg.getint("archive", "retention_mb"))

            # Move originals into the on-card archive (instant same-fs rename),
            # leaving the wardrive folder empty.
            if not self.dry_run:
                folder = self.cfg.get("archive", "oncard_folder")
                storage.archive_on_card(sources, mountpoint, folder, stamp)
            ok = True
        except Exception as e:
            log.exception("card-stage error: %s", e)
            self.display.set_state(disp.ERROR)
        finally:
            storage.unmount(mountpoint)

        if not ok:
            return

        if self.dry_run:
            log.info("[DRY-RUN] merging locally, skipping upload")
            try:
                out = os.path.join(archive_dir, f"wardrive_combined_{stamp}.csv")
                meta["stats"] = merge.merge(copied, out, dedup=self.cfg.get("merge", "dedup"))
                meta["combined_file"] = os.path.basename(out)
                storage.update_meta(archive_dir, meta)
                self.display.set_result(disp.SUCCESS, {})
            except Exception as e:
                log.exception("dry-run merge failed: %s", e)
                self.display.set_state(disp.ERROR)
            return

        # Card is safe to remove NOW - the rest happens off-card.
        self.display.set_state(disp.SAFE_REMOVE)
        log.info("SAFE TO REMOVE card (%s) - merge + upload continue in background", device)
        time.sleep(self.SAFE_REMOVE_HOLD)
        self.manager.enqueue(archive_dir)


# ---------------------------------------------------------------------------
# udev-driven service loop
# ---------------------------------------------------------------------------
def run_service(cfg, display):
    import pyudev
    manager = UploadManager(cfg, display)
    names = [u.name for u in manager.uploaders]
    log.info("uploaders enabled: %s (required: %s)",
             names or "NONE", sorted(manager.required) or "none")
    appliance = Appliance(cfg, display, manager)
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by("block")
    if not manager.is_active():
        display.set_state(disp.IDLE)
    log.info("waiting for SD card insertion...")

    busy = False
    for device in iter(monitor.poll, None):
        action = device.action
        devtype = device.get("DEVTYPE")
        devnode = device.device_node
        if devtype != "partition" or not devnode:
            continue
        if action == "add" and not busy:
            busy = True
            log.info("card inserted: %s", devnode)
            # Give the kernel a moment to settle the partition.
            time.sleep(1)
            appliance.process(devnode)
        elif action == "remove":
            log.info("card removed: %s", devnode)
            busy = False
            # Don't clobber the display if a background upload is running.
            if not manager.is_active():
                display.set_state(disp.IDLE)
            log.info("waiting for SD card insertion...")


def main(argv=None):
    setup_logging()
    parser = argparse.ArgumentParser(description="Wardrive upload appliance")
    parser.add_argument("--once", metavar="DEV", help="process one device node and exit")
    parser.add_argument("--dry-run", metavar="DEV",
                        help="process DEV but do not upload or archive originals")
    parser.add_argument("--test-display", action="store_true",
                        help="cycle the Scroll pHAT through all states and exit")
    args = parser.parse_args(argv)

    cfg = Config()
    display = disp.Display(
        brightness=cfg.getint("display", "brightness") or 128,
        rotate=cfg.getint("display", "rotate"))

    # Clean shutdown clears the panel.
    def _sig(_s, _f):
        display.stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    try:
        if args.test_display:
            for state in (disp.IDLE, disp.SCANNING, disp.COPYING, disp.MERGING,
                          disp.UPLOADING, disp.SAFE_REMOVE, disp.NONE_FOUND):
                display.set_state(state, progress=0.6 if state == disp.UPLOADING else None)
                time.sleep(3)
            # Show result states with per-uploader marks (left=wigle, right=wdgowars).
            display.set_result(disp.SUCCESS, {"wigle": True, "wdgowars": True})
            time.sleep(3)
            display.set_result(disp.ERROR, {"wigle": True, "wdgowars": False})
            time.sleep(3)
            return 0
        if args.dry_run:
            Appliance(cfg, display, manager=None, dry_run=True).process(args.dry_run)
            time.sleep(3)
            return 0
        if args.once:
            manager = UploadManager(cfg, display)
            Appliance(cfg, display, manager).process(args.once)
            # Wait for the background merge+upload to finish before exiting.
            deadline = time.time() + 1800
            while manager.is_active() and time.time() < deadline:
                time.sleep(1)
            time.sleep(3)
            return 0
        run_service(cfg, display)
    finally:
        display.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
