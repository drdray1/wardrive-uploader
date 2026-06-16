# Wardrive Uploader

A standalone, plug-and-walk-away appliance built on a **Raspberry Pi Zero W** with a
**Pimoroni Scroll pHAT**. Insert an SD card full of wardrive logs, and it automatically
finds them, merges them into one file, uploads to **WiGLE** and **wdgowars**, then archives
the originals — all signalled live on the little LED matrix. No keyboard, no monitor, no menu.

Plug card in → watch the lights → unplug when it shows the ✓. That's the whole workflow.

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
git clone https://github.com/<you>/wardrive-uploader.git
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
| `[upload] required` | Uploaders that **must** succeed before originals are archived (default `wigle`). |
| `[archive] oncard_folder` | Folder created on the card for uploaded originals (default `archive`). |
| `[archive] local_dir` | Permanent copy kept on the Pi. |
| `[archive] retention_runs` / `retention_mb` | Prune oldest local runs so the Pi never fills up. |
| `[display] brightness` / `rotate` | Panel brightness (0–255) and `180` if mounted upside‑down. |

> ⚠️ The wdgowars API is **unverified** (docs are behind login). It's non‑blocking by default,
> so WiGLE always works even if the wdgowars endpoint/field names need tweaking in the config.

---

## Usage

1. Power the Pi. It boots into the idle animation (a dot sweeping left→right).
2. Insert a card. Watch the status:

| State | Scroll pHAT shows |
|-------|-------------------|
| Idle / waiting | Dim dot sweeping across |
| Scanning card | A dot circling a ring |
| Merging files | Pulsing dots growing 1→2→3 |
| Uploading | A **progress bar** filling left→right |
| ✅ Success — safe to remove | Bright **✓** with a slow pulse |
| ❌ Error (originals untouched) | Fast‑blinking **✗** |
| No logs found | Dim centre dash blinking |

3. When you see the **✓**, pull the card. Insert the next one; it repeats.

On error, nothing on the card is moved — just fix the issue (Wi‑Fi, keys) and re‑insert to retry.

---

## Where your files go

- **On the card:** uploaded originals are moved to `…/archive/<UTC-timestamp>/`, leaving the
  wardrive folder empty.
- **On the Pi:** a permanent copy of the originals + the merged file + a `meta.json` (device,
  version, row counts, upload results) under `/var/lib/wardrive-uploader/archive/<device>/<ts>/`.
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
| Uploads fail with auth error | Re‑check WiGLE API **name + token** in `/etc/wardrive-uploader/config.ini`. |
| Uploads fail, clock looks wrong | Zero W has no RTC — give it a minute on Wi‑Fi to sync NTP (`timedatectl`). |
| Want to test without a card | `sudo .venv/bin/python src/main.py --test-display` cycles every status. |
| Dry run a real card | `sudo .venv/bin/python src/main.py --dry-run /dev/sda1` (no upload, no archive). |

---

## How it works

```
IDLE ──card in──▶ MOUNT ──▶ DISCOVER ──▶ MERGE ──▶ LOCAL ARCHIVE ──▶ UPLOAD ──▶ ON-CARD ARCHIVE ──▶ DONE
  ▲                                          │(none found)                  │(required upload fails)      │
  └───────────────card out──────────────────┴──────────────────────────────┴── ERROR (card untouched) ──┘
```

- **Discovery** scans the whole card and matches files whose first line starts with
  `WigleWifi-`, so it finds logs regardless of folder.
- **Merge** keeps one header pair, concatenates data rows, and de‑dups on `MAC + FirstSeen`.
  Marauder writes WigleWifi‑1.4 and Piglet writes 1.6; since one card = one device, no mixing
  happens (a 1.4→1.6 normalizer exists for safety).
- The **local copy is written before upload**, so your logs are safe even if the upload fails.

Run the offline tests with `python3 tests/test_merge.py`.

---

## Credits / License

Built for archiving wardrive logs from ESP32 Marauder and Piglet devices.
Uploads to [WiGLE](https://wigle.net) and [wdgowars](https://wdgwars.pl).
Licensed under the MIT License — see [LICENSE](LICENSE).
