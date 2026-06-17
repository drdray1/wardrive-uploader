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
WDGOWARS_TEAM_URL = "https://wdgwars.pl/api/team/me"

# Characters the 3x5 ticker font can render (others -> space).
_TICKER_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 #+-:./?")


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
    """Return {'nets', 'rank', 'month_rank'} or None."""
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
    return {
        "nets": _int(_find(data, "discoveredWiFiGPS", "discoveredWiFi")),
        "rank": _int(_find(data, "rank")),
        "month_rank": _int(_find(data, "monthRank")),
    }


def wdgowars_team(api_key, timeout=10):
    """Return {'name': str, 'rank': int} for the caller's team, or None
    (e.g. not on a team -> 404)."""
    try:
        r = requests.get(WDGOWARS_TEAM_URL, headers={"X-API-Key": api_key,
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
    """Build the idle ticker: WiGLE monthly rank + wdgowars team rank.
    Best-effort; returns '' if nothing could be fetched."""
    parts = []
    if cfg.getbool("wigle", "enabled"):
        name, token = cfg.get("wigle", "api_name"), cfg.get("wigle", "api_token")
        if name and token:
            s = wigle_stats(name, token)
            if s:
                # Monthly rank is what we care about (fall back to all-time rank).
                rank = s["month_rank"] if s["month_rank"] is not None else s["rank"]
                parts.append("WIGLE MO #{}".format(_fmt(rank)))
    if cfg.getbool("wdgowars", "enabled"):
        key = cfg.get("wdgowars", "api_key")
        if key:
            t = wdgowars_team(key)
            if t and t["rank"] is not None:
                name = _ticker_safe(t["name"]).strip() or "TEAM"
                parts.append("WDG {} #{}".format(name, _fmt(t["rank"])))
    msg = "   ".join(parts)
    if msg:
        log.info("stats ticker: %s", msg)
    return msg
