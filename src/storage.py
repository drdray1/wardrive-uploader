"""Mounting, on-card archiving, and the permanent local archive on the Pi."""
import json
import logging
import os
import shutil
import subprocess
import time

log = logging.getLogger("wardrive.storage")

MOUNT_BASE = "/run/wardrive-mnt"


def mount(devnode):
    """Mount a partition (vfat/exfat/ext*) read-write. Returns the mountpoint."""
    os.makedirs(MOUNT_BASE, exist_ok=True)
    mountpoint = os.path.join(MOUNT_BASE, os.path.basename(devnode))
    os.makedirs(mountpoint, exist_ok=True)
    # Let the kernel auto-detect the filesystem; exfat/vfat/ext4 all supported
    # once exfatprogs is installed.
    subprocess.run(
        ["mount", devnode, mountpoint],
        check=True, capture_output=True, text=True,
    )
    log.info("mounted %s at %s", devnode, mountpoint)
    return mountpoint


def unmount(mountpoint):
    if not mountpoint:
        return
    for attempt in range(3):
        try:
            subprocess.run(["sync"], check=False)
            subprocess.run(["umount", mountpoint], check=True,
                           capture_output=True, text=True)
            log.info("unmounted %s", mountpoint)
            return
        except subprocess.CalledProcessError as e:
            log.warning("umount attempt %s failed: %s", attempt + 1,
                        e.stderr.strip() if e.stderr else e)
            time.sleep(1)
    log.error("could not unmount %s", mountpoint)


def archive_on_card(files, mountpoint, folder, stamp):
    """Move source files into <mountpoint>/<folder>/<stamp>/, leaving the
    wardrive folder empty. Returns the destination dir."""
    dest = os.path.join(mountpoint, folder, stamp)
    os.makedirs(dest, exist_ok=True)
    for src in files:
        try:
            shutil.move(src, os.path.join(dest, os.path.basename(src)))
        except OSError as e:
            log.error("failed to move %s -> %s: %s", src, dest, e)
            raise
    log.info("archived %s files on card -> %s", len(files), dest)
    return dest


def start_local_archive(local_dir, device, stamp, source_files, meta):
    """FAST path while the card is mounted: copy the raw source files to the Pi
    and write an initial meta.json. Merge + combined file come later, off-card.
    Returns (archive_dir, list_of_copied_source_paths)."""
    dest = os.path.join(local_dir, device, stamp)
    src_dir = os.path.join(dest, "sources")
    os.makedirs(src_dir, exist_ok=True)
    copied = []
    for src in source_files:
        target = os.path.join(src_dir, os.path.basename(src))
        try:
            shutil.copy2(src, target)
            copied.append(target)
        except OSError as e:
            log.error("local archive copy failed for %s: %s", src, e)
            raise
    _write_meta(dest, meta)
    log.info("copied %s source file(s) to local archive -> %s", len(copied), dest)
    return dest, copied


def load_meta(archive_dir):
    path = os.path.join(archive_dir, "meta.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def update_meta(archive_dir, meta):
    _write_meta(archive_dir, meta)


def _write_meta(archive_dir, meta):
    # Atomic write: a truncated meta.json (power loss mid-write) would make the
    # run unreadable and silently un-resumable. Write a temp file then replace.
    os.makedirs(archive_dir, exist_ok=True)
    path = os.path.join(archive_dir, "meta.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def free_mb(path):
    try:
        usage = shutil.disk_usage(path)
        return usage.free // (1024 * 1024)
    except OSError:
        return -1


def prune_local_archive(local_dir, keep_runs, max_mb):
    """Enforce retention so the Pi never fills its own card. Removes oldest
    run directories beyond keep_runs, then by total size over max_mb."""
    if not os.path.isdir(local_dir):
        return
    runs = []  # (mtime, path)
    for device in os.listdir(local_dir):
        dpath = os.path.join(local_dir, device)
        if not os.path.isdir(dpath):
            continue
        for run in os.listdir(dpath):
            rpath = os.path.join(dpath, run)
            if os.path.isdir(rpath):
                runs.append((os.path.getmtime(rpath), rpath))
    runs.sort()  # oldest first

    # Retention by count.
    while keep_runs > 0 and len(runs) > keep_runs:
        _, victim = runs.pop(0)
        _rmtree(victim)

    # Retention by total size.
    if max_mb > 0:
        total = sum(_dir_size(p) for _, p in runs)
        max_bytes = max_mb * 1024 * 1024
        while runs and total > max_bytes:
            _, victim = runs.pop(0)
            total -= _dir_size(victim)
            _rmtree(victim)


def _rmtree(path):
    log.info("retention: removing old archive %s", path)
    shutil.rmtree(path, ignore_errors=True)


def _dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total
