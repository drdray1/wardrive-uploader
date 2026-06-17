"""Background post-processing: merge + upload, decoupled from the SD card.

Once the card has been copied to the local archive and unmounted, a run is
queued here. A worker thread merges the copied sources off-card, then uploads
the combined file to all enabled uploaders with retries. Progress/result drive
the Scroll pHAT. Incomplete runs are persisted (meta.json) and re-queued on
startup and on a periodic timer, so uploads survive failures and reboots.
"""
import glob
import logging
import os
import queue
import threading
import time

import display as disp
import merge
import stats
import storage
import upload

log = logging.getLogger("wardrive.uploadmgr")

RESCAN_SECONDS = 600          # re-check for pending runs every 10 min
RESULT_HOLD_SECONDS = 6       # how long to show the final ✓/✗ before idle


class UploadManager:
    def __init__(self, cfg, display):
        self.cfg = cfg
        self.display = display
        self.uploaders = upload.build_uploaders(cfg)
        self.required = set(cfg.getlist("upload", "required"))
        self.local_dir = cfg.get("archive", "local_dir")
        self.retries = cfg.getint("upload", "retries") or 3
        self.max_attempts = cfg.getint("upload", "max_attempts") or 10
        self._q = queue.Queue()
        self._inflight = set()          # archive_dirs queued or processing
        self._lock = threading.Lock()
        self._active = threading.Event()  # set while a job is being processed

        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._periodic_rescan, daemon=True).start()
        self.scan_pending()

    # -- public API ---------------------------------------------------------
    def enqueue(self, archive_dir):
        with self._lock:
            if archive_dir in self._inflight:
                return
            self._inflight.add(archive_dir)
        self._q.put(archive_dir)
        log.info("queued for upload: %s", archive_dir)

    def is_active(self):
        """True if a job is being processed or any are queued."""
        return self._active.is_set() or not self._q.empty()

    def scan_pending(self):
        """Find local-archive runs whose upload isn't complete and queue them."""
        if not os.path.isdir(self.local_dir):
            return
        for device in sorted(os.listdir(self.local_dir)):
            dpath = os.path.join(self.local_dir, device)
            if not os.path.isdir(dpath):
                continue
            for run in sorted(os.listdir(dpath)):
                rpath = os.path.join(dpath, run)
                meta = storage.load_meta(rpath)
                if meta and not meta.get("upload_complete"):
                    self.enqueue(rpath)

    # -- worker -------------------------------------------------------------
    def _periodic_rescan(self):
        while True:
            time.sleep(RESCAN_SECONDS)
            self.scan_pending()

    def _run(self):
        while True:
            archive_dir = self._q.get()
            self._active.set()
            try:
                self._process(archive_dir)
            except Exception as e:
                log.exception("upload job error for %s: %s", archive_dir, e)
                self.display.set_result(disp.ERROR, {})
                time.sleep(RESULT_HOLD_SECONDS)
                self.display.set_state(disp.IDLE)
            finally:
                with self._lock:
                    self._inflight.discard(archive_dir)
                self._active.clear()

    def _process(self, archive_dir):
        meta = storage.load_meta(archive_dir)
        if not meta:
            log.error("no meta.json in %s; skipping", archive_dir)
            return
        if meta.get("upload_complete"):
            return

        # Merge off-card into size-capped parts (if not already merged).
        parts = meta.get("parts")
        part_paths = [os.path.join(archive_dir, n) for n in parts] if parts else []
        if not part_paths or not all(os.path.exists(p) for p in part_paths):
            sources = sorted(glob.glob(os.path.join(archive_dir, "sources", "*")))
            if not sources:
                log.error("no source files in %s; cannot merge", archive_dir)
                return
            self.display.set_state(disp.MERGING)
            base = f"wardrive_{meta.get('device', 'run')}_{meta.get('stamp', 'run')}"
            max_bytes = max(1, self.cfg.getint("upload", "max_upload_mb")) * 1024 * 1024
            part_paths, stats = merge.merge_split(
                sources, archive_dir, base, max_bytes,
                dedup=self.cfg.get("merge", "dedup"),
                gzip_out=self.cfg.getbool("upload", "gzip"),
                compresslevel=self.cfg.getint("upload", "gzip_level") or 6)
            meta["parts"] = [os.path.basename(p) for p in part_paths]
            meta["stats"] = stats
            meta.pop("combined_file", None)
            storage.update_meta(archive_dir, meta)
        parts = meta["parts"]

        # Upload every part to every enabled uploader, skipping (part, uploader)
        # pairs that already succeeded. Each uploader runs in its OWN thread so
        # the services upload in parallel (WiGLE overlaps wdgowars' cooldown).
        # wdgowars still self-paces its own cooldown within its thread.
        status = meta.get("uploads")
        if not isinstance(status, dict):
            status = {}                       # migrate/ignore any old list format
        meta["attempts"] = meta.get("attempts", 0) + 1
        self.display.set_state(disp.UPLOADING, progress=0.0)
        units = max(1, len(parts) * len(self.uploaders))
        progress = {"done": 0}
        lock = threading.Lock()

        def _upload_one(up):
            for pname in parts:
                with lock:
                    already = status.get(pname, {}).get(up.name, {}).get("ok")
                if not already:
                    res = upload.upload_with_retry(
                        up, os.path.join(archive_dir, pname), retries=self.retries)
                    with lock:
                        status.setdefault(pname, {})[up.name] = res.as_dict()
                        storage.update_meta(archive_dir, {**meta, "uploads": status})
                with lock:
                    progress["done"] += 1
                    frac = progress["done"] / units
                self.display.set_progress(frac)

        threads = [threading.Thread(target=_upload_one, args=(up,), daemon=True)
                   for up in self.uploaders]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        meta["uploads"] = status
        ok_names = {u.name for u in self.uploaders
                    if all(status.get(pn, {}).get(u.name, {}).get("ok") for pn in parts)}
        all_ok = ok_names.issuperset({u.name for u in self.uploaders})
        required_ok = self.required.issubset(ok_names) if self.required else bool(ok_names)

        # Stop retrying when everything succeeded, or we've exhausted attempts.
        meta["upload_complete"] = all_ok or meta["attempts"] >= self.max_attempts
        storage.update_meta(archive_dir, meta)

        marks = {u.name: (u.name in ok_names) for u in self.uploaders}
        if required_ok:
            log.info("upload result for %s: %s (attempt %s)", os.path.basename(archive_dir),
                     marks, meta["attempts"])
            self.display.set_result(disp.SUCCESS, marks)
        else:
            log.warning("required upload failed for %s: %s (attempt %s/%s)",
                        os.path.basename(archive_dir), marks,
                        meta["attempts"], self.max_attempts)
            self.display.set_result(disp.ERROR, marks)
        time.sleep(RESULT_HOLD_SECONDS)
        # Refresh the idle ticker so the new run's count shows immediately.
        try:
            self.display.set_message(stats.build_message(self.cfg))
        except Exception as e:
            log.debug("ticker refresh failed: %s", e)
        self.display.set_state(disp.IDLE)
