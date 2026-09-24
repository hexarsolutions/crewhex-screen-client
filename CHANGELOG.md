# Changelog

## 2.0.8 (security + supply chain)

- `install.sh`: KIOSK_HOME was read before it was set, so every fresh install
  aborted with "unbound variable" under `set -u`. Fixed (order swapped).
- `install.sh` now pins the version, requires the checksum *and* the ed25519
  signature, verifies it against the public key embedded in the script, and
  refuses to install on any failure. A missing `.sha256` is no longer a silent skip.
- Banner colour: the player read `display.primary_colour`; the manifest publishes
  `branding.primary_colour`. Every tenant banner had been falling back to amber.
- Version drift: player.js hard-coded 2.0.6 while VERSION said 2.0.7, so the pairing
  screen showed the wrong version. The build now injects `client/VERSION` and CI
  fails if the two disagree.
- Test bundles are signed with a throwaway key, so the suite passes with signatures
  mandatory (CI had been red since the OTA public key shipped, which blocked package
  and release publishing).

## 2.0.7

- Broadcast overlay fix: a text-only broadcast is a banner over the programme, and the
  "has anything changed?" check now compares the *playing* resolution instead of the
  overlay. Before, every live broadcast made the wall flick a slide each second no
  matter what the playlist durations said. Slides now hold their schedule durations
  (playlist items, page rotation_seconds) with the banner rolling on top.

## 2.0.6

- Stop restarting a slide on transient status blips: the local status poll no longer
  bumps the generation on a single miss (two consecutive misses required). This was
  the earlier 1s flicker.
- waitChange keeps a hard 1s floor per slide and only cuts short on a real content
  change (playing resolution or manifest etag).

## 2.0.5

- Banner wording enters from the right immediately and loops seamlessly (was parked
  off-screen for most of the cycle, so only the NOTICE tag showed).
- All rotation items play 15s.

## 2.0.4

- Banner text colour auto-contrasts against the banner background (black on light
  brand colours, white on dark; black always on the red alert bar).

## 2.0.3

- Banner text enters from the right (was invisible until late in the loop); soft
  cross-fade between items.


## 2.0.0

**Protocol v2** (needs the CrewHex server signage release; falls back to v1 automatically).

- Pairing: server-issued code + device-only secret. Admins type just the code: no business name.
- Playlists, schedules, locations, broadcasts, images and video, not just Hub pages.
- Offline-first: manifest and media cached on the SD card, SHA-256 verified. The TV keeps
  playing through outages instead of showing "Reconnecting…". A small status chip appears instead.
- One renderer shared with the hosted web player (Fire TV / Android / browser). Hub pages
  render with the v1.1.5 geometry.
- Heartbeat: telemetry, proof of play, remote Identify / Reload / Clear cache.
- Weekly device-token rotation.

**Security fixes**
- Kiosk XSS: v1 inserted tenant data with `innerHTML` and an `esc()` that did not escape
  quotes inside `src`/`style` attributes. Now DOM APIs only, plus a strict CSP with script hashes.
- Wi-Fi endpoints could be called by any page loaded in Chromium. Now they need a custom header,
  a loopback Host and our own Origin, and work only while unpaired or offline.
- `device.json` could become world-readable after a re-pair. Now written atomically as 0600.
- OTA: no integrity check, `tarfile.extractall` without path checks, and updates written into
  `/opt`, which is read-only under `ProtectSystem=strict`, so v1 OTA could not swap files on
  installer-built Pis. Now: same-host HTTPS, SHA-256, ed25519 signature, safe extraction,
  install to `/var/lib/.../app/<version>`, launcher with 3-strike rollback.
- Only `/` is served from the kiosk directory; nothing else on disk is exposed.

**Upgrading from 1.1.x**: see README "Upgrading to 2.0". The token is kept, so there's no re-pairing.
