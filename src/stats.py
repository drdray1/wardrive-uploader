"""Pull account stats from WiGLE and wdgowars for the idle ticker.

WiGLE:    GET https://api.wigle.net/api/v2/stats/user   (HTTP Basic auth)
wdgowars: GET https://wdgwars.pl/api/team/me            (X-API-Key header)

Best-effort: a fetch that fails falls back to the last successful value (cached
in-process), so a transient API timeout never blanks the ticker.
"""
import logging

import requests
from requests.auth import HTTPBasicAuth

log = logging.getLogger("wardrive.stats")

WIGLE_STATS_URL = "https://api.wigle.net/api/v2/stats/user"
WDGOWARS_TEAM_URL = "https://wdgwars.pl/api/team/me"
TIMEOUT = 15

# Characters the 3x5 ticker font can render (others -> space).
_TICKER_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 #+-:./?")

# Reused connection + last-good results so a blip doesn't drop a platform.
_session = requests.Session()
_last_good = {"wigle": None, "wdgowars": None}


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


def wigle_stats(api_name, api_token, timeout=TIMEOUT):
    """Return {'nets', 'rank', 'month_rank'} or None."""
    try:
        r = _session.get(WIGLE_STATS_URL, headers={"Accept": "application/json"},
                         auth=HTTPBasicAuth(api_name, api_token), timeout=timeout)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("wigle stats fetch failed: %s", e)
        return None
    if not data.get("success", True):
        log.warning("wigle stats: %s", data.get("message", "unsuccessful"))
        return None
    return {
        "nets": _int(_find(data, "discoveredWiFiGPS", "discoveredWiFi")),
        "rank": _int(_find(data, "rank")),
        "month_rank": _int(_find(data, "monthRank")),
    }


def wdgowars_team(api_key, timeout=TIMEOUT):
    """Return {'name': str, 'rank': int} for the caller's team, or None
    (e.g. not on a team -> 404)."""
    try:
        r = _session.get(WDGOWARS_TEAM_URL, headers={"X-API-Key": api_key,
                         "Accept": "application/json"}, timeout=timeout)
        if r.status_code == 404:
            return None
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("wdgowars team fetch failed: %s", e)
        return None
    return {"name": data.get("name"), "rank": _int(_find(data, "rank"))}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fmt(n):
    return str(n) if n is not None else "?"


def _ticker_safe(s):
    return "".join(c if c in _TICKER_OK else " " for c in (s or "").upper())


def build_message(cfg):
    """Build the idle ticker: WiGLE monthly rank + wdgowars team rank. On a fetch
    failure, fall back to the last successful value so the ticker stays complete."""
    parts = []
    if cfg.getbool("wigle", "enabled"):
        name, token = cfg.get("wigle", "api_name"), cfg.get("wigle", "api_token")
        if name and token:
            s = wigle_stats(name, token)
            if s:
                _last_good["wigle"] = s
            s = s or _last_good["wigle"]
            if s:
                # Monthly rank is what we care about (fall back to all-time rank).
                rank = s["month_rank"] if s["month_rank"] is not None else s["rank"]
                parts.append("WIGLE MO #{}".format(_fmt(rank)))
    if cfg.getbool("wdgowars", "enabled"):
        key = cfg.get("wdgowars", "api_key")
        if key:
            t = wdgowars_team(key)
            if t and t["rank"] is not None:
                _last_good["wdgowars"] = t
            t = t if (t and t["rank"] is not None) else _last_good["wdgowars"]
            if t and t["rank"] is not None:
                name = _ticker_safe(t["name"]).strip() or "TEAM"
                parts.append("WDG {} #{}".format(name, _fmt(t["rank"])))
    msg = "   ".join(parts)
    if msg:
        log.info("stats ticker: %s", msg)
    return msg
