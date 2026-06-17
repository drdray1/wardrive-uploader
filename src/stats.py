"""Pull account stats from WiGLE and wdgowars for the idle ticker.

WiGLE:    GET https://api.wigle.net/api/v2/stats/user   (HTTP Basic auth)
wdgowars: GET https://wdgwars.pl/api/me                  (X-API-Key header)

Everything is best-effort: any failure just drops that platform from the
message so the ticker still shows whatever it could fetch.
"""
import logging

import requests
from requests.auth import HTTPBasicAuth

log = logging.getLogger("wardrive.stats")

WIGLE_STATS_URL = "https://api.wigle.net/api/v2/stats/user"
WDGOWARS_ME_URL = "https://wdgwars.pl/api/me"


def _find(d, *keys):
    """Return the first present key from a (possibly nested) dict."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    for v in d.values():                      # one level of nesting
        if isinstance(v, dict):
            got = _find(v, *keys)
            if got is not None:
                return got
    return None


def wigle_stats(api_name, api_token, timeout=10):
    """Return {'nets': int, 'rank': int} or None."""
    try:
        r = requests.get(WIGLE_STATS_URL, headers={"Accept": "application/json"},
                         auth=HTTPBasicAuth(api_name, api_token), timeout=timeout)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("wigle stats fetch failed: %s", e)
        return None
    if not data.get("success", True):
        log.warning("wigle stats: %s", data.get("message", "unsuccessful"))
        return None
    nets = _find(data, "discoveredWiFiGPS", "discoveredWiFi", "discoveredWiFiGPSPercent")
    rank = _find(data, "rank")
    return {"nets": _int(nets), "rank": _int(rank)}


def wdgowars_stats(api_key, timeout=10):
    """Return {'total': int, 'wifi': int, 'today': int} or None."""
    try:
        r = requests.get(WDGOWARS_ME_URL, headers={"X-API-Key": api_key,
                         "Accept": "application/json"}, timeout=timeout)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("wdgowars stats fetch failed: %s", e)
        return None
    if not data.get("ok", True):
        log.warning("wdgowars stats: not ok")
        return None
    return {"total": _int(data.get("total")), "wifi": _int(data.get("wifi")),
            "today": _int(data.get("recent_today"))}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fmt(n):
    return str(n) if n is not None else "?"


def build_message(cfg):
    """Build the scrolling idle message from both platforms. Returns a string
    (may be empty if nothing could be fetched)."""
    parts = []
    if cfg.getbool("wigle", "enabled"):
        name, token = cfg.get("wigle", "api_name"), cfg.get("wigle", "api_token")
        if name and token:
            s = wigle_stats(name, token)
            if s:
                parts.append("WIGLE {} #{}".format(_fmt(s["nets"]), _fmt(s["rank"])))
    if cfg.getbool("wdgowars", "enabled"):
        key = cfg.get("wdgowars", "api_key")
        if key:
            s = wdgowars_stats(key)
            if s:
                parts.append("WDG {} +{}".format(_fmt(s["total"]), _fmt(s["today"])))
    msg = "   ".join(parts)
    if msg:
        log.info("stats ticker: %s", msg)
    return msg
