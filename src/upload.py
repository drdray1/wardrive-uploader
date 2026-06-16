"""Pluggable uploaders for WiGLE and wdgowars.

Each uploader exposes .name and .upload(path) -> Result. Failures are returned,
not raised, so the pipeline can decide what's required vs. best-effort.
"""
import logging
import os
import time

import requests
from requests.auth import HTTPBasicAuth

log = logging.getLogger("wardrive.upload")


class Result:
    def __init__(self, name, ok, message="", transid=None, status=None):
        self.name = name
        self.ok = ok
        self.message = message
        self.transid = transid
        self.status = status

    def as_dict(self):
        return {
            "uploader": self.name,
            "ok": self.ok,
            "message": self.message,
            "transid": self.transid,
            "status": self.status,
        }


class WigleUploader:
    name = "wigle"
    URL = "https://api.wigle.net/api/v2/file/upload"

    def __init__(self, api_name, api_token, donate=True, timeout=120):
        self.auth = HTTPBasicAuth(api_name, api_token)
        self.donate = donate
        self.timeout = timeout

    def upload(self, path):
        fname = os.path.basename(path)
        with open(path, "rb") as f:
            files = {"file": (fname, f, "text/csv")}
            data = {"donate": "true" if self.donate else "false"}
            resp = requests.post(
                self.URL, headers={"Accept": "application/json"},
                auth=self.auth, files=files, data=data, timeout=self.timeout,
            )
        ok = False
        message = f"HTTP {resp.status_code}"
        transid = None
        try:
            body = resp.json()
            ok = bool(body.get("success"))
            message = body.get("message", message)
            results = body.get("results") or []
            if results and isinstance(results, list):
                transid = results[0].get("transid")
            transid = transid or body.get("transid")
        except ValueError:
            message = resp.text[:200]
        return Result(self.name, ok and resp.ok, message, transid, resp.status_code)


class WdgowarsUploader:
    """UNVERIFIED API. Endpoint/field names come from config and may need
    adjusting against your logged-in wdgowars docs. Kept non-blocking by default.
    """
    name = "wdgowars"

    def __init__(self, api_key, endpoint, field="file", timeout=120):
        self.api_key = api_key
        self.endpoint = endpoint
        self.field = field
        self.timeout = timeout

    def upload(self, path):
        fname = os.path.basename(path)
        with open(path, "rb") as f:
            files = {self.field: (fname, f, "text/csv")}
            # Send the API key both as a header and form field to cover variants.
            headers = {"Accept": "application/json", "X-API-Key": self.api_key}
            data = {"api_key": self.api_key}
            resp = requests.post(
                self.endpoint, headers=headers, files=files, data=data,
                timeout=self.timeout,
            )
        ok = resp.ok
        message = f"HTTP {resp.status_code}"
        try:
            body = resp.json()
            if isinstance(body, dict):
                ok = bool(body.get("success", resp.ok))
                message = body.get("message", message)
        except ValueError:
            message = resp.text[:200]
        return Result(self.name, ok, message, None, resp.status_code)


def upload_with_retry(uploader, path, retries=3, backoff=5):
    """Try an uploader up to `retries` times with linear backoff."""
    last = None
    for attempt in range(1, retries + 1):
        try:
            res = uploader.upload(path)
            if res.ok:
                log.info("%s upload ok (attempt %s): %s", uploader.name, attempt, res.message)
                return res
            log.warning("%s upload failed (attempt %s/%s): %s",
                        uploader.name, attempt, retries, res.message)
            last = res
        except (requests.RequestException, OSError) as e:
            log.warning("%s upload error (attempt %s/%s): %s",
                        uploader.name, attempt, retries, e)
            last = Result(uploader.name, False, str(e))
        if attempt < retries:
            time.sleep(backoff * attempt)
    return last or Result(uploader.name, False, "no attempts made")


def build_uploaders(cfg):
    """Instantiate enabled uploaders from config."""
    uploaders = []
    if cfg.getbool("wigle", "enabled"):
        name = cfg.get("wigle", "api_name")
        token = cfg.get("wigle", "api_token")
        if name and token:
            uploaders.append(WigleUploader(
                name, token, donate=cfg.getbool("wigle", "donate"),
                timeout=cfg.getint("upload", "timeout")))
        else:
            log.warning("wigle enabled but api_name/api_token missing - skipping")
    if cfg.getbool("wdgowars", "enabled"):
        key = cfg.get("wdgowars", "api_key")
        if key:
            uploaders.append(WdgowarsUploader(
                key, cfg.get("wdgowars", "endpoint"),
                field=cfg.get("wdgowars", "field"),
                timeout=cfg.getint("upload", "timeout")))
        else:
            log.warning("wdgowars enabled but api_key missing - skipping")
    return uploaders
