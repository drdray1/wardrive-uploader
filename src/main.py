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
import tempfile
import time

# Allow running both as `python src/main.py` and as an installed module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import display as disp
import merge
import storage
import upload
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
    def __init__(self, cfg, display, dry_run=False):
        self.cfg = cfg
        self.display = display
        self.dry_run = dry_run
        self.uploaders = upload.build_uploaders(cfg)
        self.required = set(cfg.getlist("upload", "required"))
        names = [u.name for u in self.uploaders]
        log.info("uploaders enabled: %s (required: %s)%s",
                 names or "NONE", sorted(self.required) or "none",
                 " [DRY-RUN]" if dry_run else "")

    # -- the full pipeline for one inserted card ----------------------------
    def process(self, devnode):
        mountpoint = None
        stamp = _utc_stamp()
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

            # Merge.
            self.display.set_state(disp.MERGING)
            combined_name = f"wardrive_combined_{stamp}.csv"
            combined_path = os.path.join(tempfile.gettempdir(), combined_name)
            stats = merge.merge(sources, combined_path)
            device = stats["device"]

            # Local archive FIRST (logs safe even if upload later fails).
            free = storage.free_mb("/")
            log.info("local free space: %s MB", free)
            meta = {
                "stamp": stamp, "device": device, "devnode": devnode,
                "stats": stats, "uploads": [], "dry_run": self.dry_run,
            }
            local_dir = self.cfg.get("archive", "local_dir")
            archive_dir = storage.write_local_archive(
                local_dir, device, stamp, sources, combined_path, meta)
            storage.prune_local_archive(
                local_dir,
                self.cfg.getint("archive", "retention_runs"),
                self.cfg.getint("archive", "retention_mb"))

            if stats["oversize"]:
                log.warning("combined file exceeds WiGLE 180MiB limit (%s bytes)",
                            stats["bytes"])

            # Upload.
            if self.dry_run:
                log.info("[DRY-RUN] skipping upload and on-card archive")
                self.display.set_state(disp.SUCCESS)
                return

            self.display.set_state(disp.UPLOADING, progress=0.0)
            results, required_ok = self._upload(combined_path)
            meta["uploads"] = [r.as_dict() for r in results]
            storage.update_meta(archive_dir, meta)

            if not required_ok:
                log.error("required upload(s) failed; leaving card untouched")
                self.display.set_state(disp.ERROR)
                return

            # Success -> move originals into on-card archive.
            folder = self.cfg.get("archive", "oncard_folder")
            storage.archive_on_card(sources, mountpoint, folder, stamp)
            self.display.set_state(disp.SUCCESS)
            log.info("DONE - safe to remove card")

        except Exception as e:
            log.exception("pipeline error: %s", e)
            self.display.set_state(disp.ERROR)
        finally:
            storage.unmount(mountpoint)

    def _upload(self, path):
        results = []
        retries = self.cfg.getint("upload", "retries") or 1
        succeeded = set()
        total = len(self.uploaders)
        for i, up in enumerate(self.uploaders):
            res = upload.upload_with_retry(up, path, retries=retries)
            results.append(res)
            if res.ok:
                succeeded.add(up.name)
            self.display.set_progress((i + 1) / max(1, total))
        # Required uploaders must all have succeeded.
        required_ok = self.required.issubset(succeeded) if self.required else bool(succeeded)
        return results, required_ok


# ---------------------------------------------------------------------------
# udev-driven service loop
# ---------------------------------------------------------------------------
def run_service(cfg, display):
    import pyudev
    appliance = Appliance(cfg, display)
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by("block")
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
            for state in (disp.IDLE, disp.SCANNING, disp.MERGING,
                          disp.UPLOADING, disp.SUCCESS, disp.ERROR, disp.NONE_FOUND):
                display.set_state(state, progress=0.6 if state == disp.UPLOADING else None)
                time.sleep(3)
            return 0
        if args.dry_run:
            Appliance(cfg, display, dry_run=True).process(args.dry_run)
            time.sleep(3)
            return 0
        if args.once:
            Appliance(cfg, display).process(args.once)
            time.sleep(3)
            return 0
        run_service(cfg, display)
    finally:
        display.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
