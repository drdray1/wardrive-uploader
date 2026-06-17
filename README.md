# Wardrive Uploader

A standalone, plug-and-walk-away appliance built on a **Raspberry Pi Zero W** with a
**Pimoroni Scroll pHAT**. Insert an SD card full of wardrive logs, and it automatically
finds them, merges + compresses them, uploads to **WiGLE** and **wdgowars**, and archives
the originals — all signalled live on the little LED matrix. No keyboard, no monitor, no menu.

**Plug card in → wait a few seconds → unplug when it shows the ⬆ (safe to remove).** The
merge and uploads finish on their own in the background, with the card already out.

## Features

- **Minimal card time** — while the card is in, it only copies the logs off and archives the
  originals (an instant rename), then says "safe to remove." Merge + upload happen after.
- **Fast, low-memory merge** — streaming line-dedup, sized for a 512 MB Zero W.
- **gzip + smart splitting** — uploads are gzipped (`~6–8×`) and capped per file, so even a
  200 MB capture is usually a single upload per service.
- **Parallel uploads** to WiGLE + wdgowars, **resumable** across failures and reboots
  (per-part, per-service status tracked on disk).
- **Live status** on the Scroll pHAT, plus an **idle ticker** showing your WiGLE monthly rank
  and wdgowars team rank.
- **Two copies of every run** — archived on the card *and* permanently on the Pi.
- **One-command install** + systemd service; runs headless on boot.

---

## What you need

| Item | Notes |
|------|-------|
| Raspberry Pi Zero W | ARMv6, runs Raspberry Pi OS **Bookworm or Trixie 32‑bit** |
| Pimoroni Scroll pHAT | [Adafruit #3017](https://www.adafruit.com/product/3017) — 11×5 **white** LEDs (no color) |
| USB SD‑card reader | Plugs into the Pi's data **micro‑USB OTG** port (with an OTG adapter) |
| Wi‑Fi | 2.4 GHz network the Pi can reach to upload |
| WiGLE account | API token from <https://wigle.net/account> |
| wdgowars account | API key from <https://wdgwars.pl/profile> *(optional / non‑blocking)* |

Works with WiGLE‑CSV cards from an **ESP32 Marauder** (WigleWifi‑1.4) and a **Piglet**
(WigleWifi‑1.6). Use **one card at a time** — one device per card.

---

## Wiring

1. Seat the **Scroll pHAT** on the Pi's 40‑pin GPIO header (it uses I²C).
2. Plug the **USB SD‑card reader** into the Pi's **data** micro‑USB port (the inner one,
   via a micro‑USB‑OTG → USB‑A adapter).
3. Power the Pi from the **PWR** micro‑USB port.

Uploads go over the Pi's built‑in Wi‑Fi, so the single USB port is dedicated to the card
reader.

---

## Install

```bash
ssh <user>@wardrive-uploader.local
git clone https://github.com/drdray1/wardrive-uploader.git
cd wardrive-uploader
sudo ./install.sh
```

The installer is idempotent and does everything: apt deps, a Python virtualenv, enables I²C,
creates the config, and installs + starts the systemd service. It prompts for your WiGLE /
wdgowars keys (you can skip and edit later). **If it enables I²C for the first time, reboot
once** so the display lights up:

```bash
sudo reboot
```

---

## Configure

Secrets live only in `/etc/wardrive-uploader/config.ini` (mode `600`) — never in this repo.

- **WiGLE token:** <https://wigle.net/account> → *Show my token* → copy **API Name** and
  **API Token** into the `[wigle]` section.
- **wdgowars key:** <https://wdgwars.pl/profile> → *Generate API key* → put it in `[wdgowars]`.

Key options (see `config.example.ini` for the full list):

| Option | Meaning |
|--------|---------|
| `[upload] required` | Uploaders that **must** succeed for a run to count as done (default `wigle`; wdgowars is best‑effort). |
| `[upload] max_upload_mb` / `gzip` | Per‑file cap (default 55) and gzip on/off (default on). gzip caps the *compressed* size. |
| `[upload] retries` / `max_attempts` | Per‑pass retries (default 3) and background retry passes before giving up (default 10). |
| `[merge] dedup` | `lines` (fast exact‑line, default), `none` (concatenate), or `fields` (MAC+FirstSeen, smallest/slowest). |
| `[wdgowars] min_interval_seconds` | Cooldown the uploader waits between wdgowars uploads (default 60). |
| `[archive] oncard_folder` | Folder created on the card for uploaded originals (default `archive`). |
| `[archive] local_dir` | Permanent copy kept on the Pi (default `/var/lib/wardrive-uploader/archive`). |
| `[archive] retention_runs` / `retention_mb` | Prune oldest local runs so the Pi never fills up. |
| `[display] brightness` / `rotate` | Panel brightness (0–255, scales the whole UI) and `180` if mounted upside‑down. |
| `[stats] enabled` / `refresh_minutes` | Scroll your WiGLE monthly rank + wdgowars team rank when idle (default on, refreshed every 15 min). |

> wdgowars uses its documented REST API (`POST /api/upload-csv`, header `X-API-Key`, field
> `file`, 60 MB cap, `.gz` accepted). It's non‑blocking by default, so even if it has a hiccup
> WiGLE still succeeds and your logs are safe in the archives.

---

## Usage

1. Power the Pi. When idle it scrolls your stats (or a dim dot if stats are off/unavailable).
2. Insert a card. The card is only needed for a few seconds — long enough to copy
   the logs off and archive the originals. Then it shows the **eject ⬆** and the
   rest (merge + upload) happens in the background while the card is already out.

| State | Scroll pHAT shows | Card needed? |
|-------|-------------------|:---:|
| Idle / waiting | Scrolls your stats (e.g. `WIGLE MO #678  WDG LAB5 #5` — WiGLE monthly rank + wdgowars team rank), or a dim dot if stats are off/unavailable | — |
| Scanning card | Dot circling a ring | yes |
| Copying logs off | Bright comet sweeping right | yes |
| **Safe to remove** | Steady **⬆** arrow, gentle pulse | **pull it now** |
| Merging (background) | Dots growing 1→2→3 | no |
| Uploading (background) | **Progress bar** filling left→right | no |
| ✅ Success | Bright **✓** + side bars (see below) | no |
| ❌ Upload failed | Bright **✗** + side bars; auto‑retries | no |
| No logs found | Dim steady centre dash | — |

**Which service uploaded?** On the ✓/✗ result, the two edge columns are per‑service
flags: **left edge lit = WiGLE OK**, **right edge lit = wdgowars OK**. So ✓ with both
edges lit = both succeeded; ✓ with only the left edge = WiGLE went up but wdgowars
didn't (it'll keep retrying in the background).

3. As soon as you see the **⬆**, pull the card and insert the next one — you don't
   have to wait for the upload. Originals are archived on the card *before* upload,
   so a failed upload never strands your logs; it just retries (every 10 min, and on
   reboot) until it succeeds or hits `max_attempts`.

---

## Where your files go

- **On the card:** uploaded originals are moved to `…/archive/<UTC-timestamp>/`, leaving the
  wardrive folder empty.
- **On the Pi:** a permanent copy of the originals (`sources/`) + the merged part file(s) + a
  `meta.json` (device, version, row counts, per‑part upload results) under
  `/var/lib/wardrive-uploader/archive/<device>/<ts>/`.
- **Logs:** `journalctl -u wardrive-uploader -f`

---

## Updating

```bash
cd wardrive-uploader
git pull
sudo systemctl restart wardrive-uploader
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Display stays dark | `i2cdetect -y 1` should show `74`. If not, enable I²C (`sudo raspi-config`) and **reboot**. |
| `scrollphat` import error | Harmless — the built‑in `smbus2` fallback driver takes over. Status still shows. |
| Card not detected / won't mount | Ensure it's FAT32/exFAT/ext4; `exfatprogs` is installed by the installer. Check `dmesg`. |
| "No logs found" but logs are there | It matches `.csv`/`.wiglecsv`/`.log` with a `WigleWifi-` first line. Logs inside `archive/` or `combined/` (or `[scan] exclude_dirs`) are skipped by design. |
| Uploads fail with auth error | Re‑check WiGLE API **name + token** in `/etc/wardrive-uploader/config.ini`. |
| `413 Payload Too Large` | Uploads are gzipped + capped at `[upload] max_upload_mb` (55) and auto‑split; if it still trips, lower that value. |
| wdgowars `429` / cooldown | Expected — it self‑paces `min_interval_seconds` (default 60) between uploads. |
| Uploads fail, clock looks wrong | Zero W has no RTC — give it a minute on Wi‑Fi to sync NTP (`timedatectl`). |
| Want to test without a card | `sudo .venv/bin/python src/main.py --test-display` cycles every status. |
| Dry run a real card | `sudo .venv/bin/python src/main.py --dry-run /dev/sda1` (no upload, no archive). |

---

## How it works

```
 CARD MOUNTED (seconds)                    BACKGROUND (card already removed)
┌───────────────────────────────┐        ┌──────────────────────────────────────┐
│ MOUNT ▶ DISCOVER ▶ COPY to Pi  │        │ MERGE ▶ UPLOAD(WiGLE+wdgowars,retry)  │
│        ▶ archive on card       │──────▶ │   ▶ update meta ▶ ✓/✗                  │
│        ▶ unmount ▶ ⬆ SAFE      │ enqueue│   (retries every 10 min + on reboot)  │
└───────────────────────────────┘        └──────────────────────────────────────┘
```

- **Discovery** scans the whole card for `.csv` / `.wiglecsv` / `.log` files whose first line
  starts with `WigleWifi-` (Marauder writes `wardrive_*.log`), so it finds logs regardless of
  folder. OS metadata/trash dirs and macOS `._` sidecar files are ignored automatically.
- **Card time is minimized**: while mounted it only *copies* the logs to the Pi and
  moves the originals into the on‑card `archive/` (an instant same‑filesystem rename),
  then unmounts and tells you it's safe to remove. The slow **merge and upload run
  afterwards**, off‑card.
- **Merge** streams the logs together, de‑duping (default `[merge] dedup = lines`: drop
  byte‑identical rows, fast + constant‑memory; or `none`/`fields`), then writes **gzipped,
  size‑capped parts** (`.csv.gz`, default ≤55 MB each — wdgowars caps at 60 MB, WiGLE at
  ~180 MiB; both accept gzip). gzip shrinks WiGLE CSV ~6–8×, so a typical capture — even a
  200 MB one — fits in **a single part / single upload**. Marauder writes WigleWifi‑1.4 and
  Piglet 1.6; one card = one device so no mixing (a 1.4→1.6 normalizer exists for safety).
- **Uploads** run the two services **in parallel** (one thread each). wdgowars enforces a
  cooldown, so its thread self‑paces (`[wdgowars] min_interval_seconds`, default 60) — only
  relevant when a capture is big enough to need multiple parts. Per‑part, per‑service success
  is tracked in `meta.json`, so retries only re‑send what actually failed.
- **Durable uploads**: each run's status is tracked in `meta.json`. Failed/incomplete
  uploads are re‑queued automatically (periodically and on reboot) until they succeed
  or hit `max_attempts`, so a flaky network never loses data.

Run the offline tests with `python3 tests/test_merge.py`.

---

## Credits / License

Built for archiving wardrive logs from ESP32 Marauder and Piglet devices.
Uploads to [WiGLE](https://wigle.net) and [wdgowars](https://wdgwars.pl).
Licensed under the MIT License — see [LICENSE](LICENSE).
