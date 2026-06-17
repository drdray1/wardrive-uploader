"""Configuration loading for the wardrive uploader.

Reads /etc/wardrive-uploader/config.ini (overridable via WARDRIVE_CONFIG env var
for testing). All secrets live in that file only - never in the repo.
"""
import configparser
import os

DEFAULT_CONFIG_PATH = "/etc/wardrive-uploader/config.ini"

# Defaults applied when a key is missing from the ini file.
_DEFAULTS = {
    "wigle": {
        "enabled": "true",
        "api_name": "",
        "api_token": "",
        "donate": "true",
    },
    "wdgowars": {
        "enabled": "true",
        "api_key": "",
        # Verified against wdgwars.pl Developer API docs: POST /api/upload-csv,
        # header X-API-Key, multipart field "file" (.csv/.log/.gz), 60 MB cap.
        "endpoint": "https://wdgwars.pl/api/upload-csv",
        "field": "file",
        "min_interval_seconds": "60",   # cooldown wdgowars enforces between uploads
    },
    "upload": {
        # Comma-separated uploaders that MUST succeed (others are best-effort).
        "required": "wigle",
        "retries": "3",          # per-attempt retries within one upload pass
        "max_attempts": "10",    # background passes before giving up a run
        "timeout": "120",
        # Cap per uploaded file. wdgowars=60MB, WiGLE~180MiB -> 55 is safe for
        # both. With gzip on, this caps the COMPRESSED size, so most runs fit in
        # one part.
        "max_upload_mb": "55",
        "gzip": "true",          # gzip each part before upload (both services accept .gz)
    },
    "archive": {
        "oncard_folder": "archive",
        "local_dir": "/var/lib/wardrive-uploader/archive",
        "retention_runs": "50",
        "retention_mb": "500",
    },
    "scan": {
        # Directories (by name) skipped during the recursive card scan. (Common
        # OS metadata/trash dirs are always skipped regardless of this setting.)
        # 'archive' = our on-card archive; 'combined' = a common manual-merge folder.
        "exclude_dirs": "archive,combined",
    },
    "merge": {
        # lines = fast streaming exact-line dedup (default); none = concatenate;
        # fields = parse + dedup on MAC+FirstSeen (smallest, slowest).
        "dedup": "lines",
    },
    "stats": {
        # Scroll WiGLE + wdgowars account stats on the panel when idle.
        "enabled": "true",
        "refresh_minutes": "15",
    },
    "display": {
        "brightness": "64",
        "rotate": "0",
    },
}


class Config:
    def __init__(self, path=None):
        self.path = path or os.environ.get("WARDRIVE_CONFIG", DEFAULT_CONFIG_PATH)
        self._cp = configparser.ConfigParser()
        # Seed defaults so reads never KeyError.
        self._cp.read_dict(_DEFAULTS)
        if os.path.exists(self.path):
            self._cp.read(self.path)

    def get(self, section, key):
        return self._cp.get(section, key, fallback=_DEFAULTS.get(section, {}).get(key, ""))

    def getbool(self, section, key):
        return self._cp.getboolean(section, key, fallback=False)

    def getint(self, section, key):
        try:
            return int(self.get(section, key))
        except (TypeError, ValueError):
            return 0

    def getlist(self, section, key):
        raw = self.get(section, key)
        return [x.strip() for x in raw.split(",") if x.strip()]
