# CrewHex Screen Client ↔ Server Protocol (v2)

Server implementation: `teamHub_resell/api/routers/signage.py`. All paths are
under `api_base` + `/api/v1`. v1 endpoints (section 7) remain for 1.1.x clients
and as a fallback; a v1 device token is valid on v2 endpoints.

## 1. Pairing

The **server** issues the code. The screen never chooses it.

```
POST /hub-device/v2/register            (public, rate-limited per IP)
{ "player_kind": "pi", "client_version": "2.0.0" }
```
```json
{ "registration_id": "uuid", "pairing_code": "K7M 4PX", "device_secret": "<256-bit>",
  "expires_at": "2026-09-23T07:30:00+00:00", "poll_seconds": 3 }
```

The screen shows `pairing_code` (6 characters from `34679ACDEFGHJKMNPQRTUVWXY`,
no 0/O/1/I/5/S/2/Z/8/B) and keeps `device_secret` private. A tenant admin types
the code in **Screens → Add screen**; that binds the screen to their tenant.

```
POST /hub-device/v2/claim-status        (public, rate-limited per IP)
{ "registration_id": "uuid", "device_secret": "..." }
```
| Status | Meaning | Screen does |
|---|---|---|
| `200 {"status":"pending"}` | not claimed yet | keep polling |
| `200 {"status":"paired","device_token":"chx_dev_…","display_id":"uuid","display_name":"…"}` | claimed | store token 0600, start syncing |
| `404` | unknown id or wrong secret | register again |
| `409` | token already collected | register again |
| `410` | code expired | register again |

The token is released **once**, only to the holder of the secret. A guessed
code is worthless without the secret.

## 2. Manifest

```
GET /hub-device/v2/manifest
Authorization: Bearer <device_token>
If-None-Match: "<etag>"                 -> 304 when unchanged
```
```json
{
  "schema": 2, "etag": "…",
  "display":  { "id","name","status","timezone","operating_days","open_time","close_time","holiday_dates",
                "always_on","after_hours_mode","after_hours_message","after_hours_page_key","orientation",
                "group_id","default_playlist_id","group_default_playlist_id" },
  "branding": { "name","logo_url","primary_colour","site_label" },
  "schedules": [ { "id","playlist_id","target_type","target_id","days","start_time","end_time",
                   "start_date","end_date","priority","enabled","created_at" } ],
  "overrides": [ { "id","target_type","target_id","playlist_id","message","tone","starts_at","ends_at" } ],
  "playlists": { "<id>": { "name", "items": [ { "id","kind","media_id","page_key","url","config",
                                                "duration_seconds","valid_from","valid_to","enabled" } ] } },
  "media":     { "<id>": { "kind","content_type","bytes","sha256","url":"/hub-device/v2/media/<id>" } },
  "pages":     { "<page_key>": { "title","enabled","sort_order","rotation_seconds","blocks","message" } }
}
```

* The manifest holds the **whole week**, not "what's on now": the screen decides
  locally (`kiosk-src/resolver.js`, identical rules to the server's
  `signage_resolver.py`) so schedules keep switching while offline.
* Download every `media[].url` and page image **before** switching, and verify
  each file's SHA-256. Never show a partial or unverified file.
* Page image blocks point at `/hub-device/v2/page-image?key=hub-page/<tenant>/<uuid>.<ext>`.
* `401` anywhere: token revoked (screen removed) — wipe token + cache, re-pair.

## 3. Live numbers

`GET /hub-device/v2/live` → `{"live": {"fuel_dollars","fuel_litres","receipts","vehicles_due"}}`.
Kept out of the manifest so a new fuel receipt doesn't invalidate every screen's cache.

## 4. Heartbeat (every 60 s)

```
POST /hub-device/v2/heartbeat
{ "client_version","manifest_etag","mode","current_item","uptime_s","cache_bytes",
  "screen_w","screen_h","errors":[…], "plays":[{"day":"2026-09-23","ref":"<item id>","plays":3,"seconds":45}] }
```
```json
{ "ok": true, "manifest_etag": "…", "commands": [{"id":"…","command":"identify|reload|clear_cache"}],
  "update": {"version": "2.0.1"} | null, "status": "online|paused" }
```
If `manifest_etag` differs from the screen's, fetch the manifest now. Commands
are delivered once. Unsent plays are kept for the next heartbeat.

## 5. Token rotation

`POST /hub-device/v2/rotate-token` → `{"device_token": "…"}`. Screens rotate
weekly. The old token keeps working for 10 minutes.

## 6. OTA

When the heartbeat returns `update.version` (or v1 `update-check` does), the
client downloads from the **same host as `api_base` only**:

```
{api_base}/public/screen-client-<version>.tar.gz
{api_base}/public/screen-client-<version>.tar.gz.sha256
{api_base}/public/screen-client-<version>.tar.gz.sig     (ed25519, raw)
```

Checks, in order: version matches `^\d+\.\d+\.\d+$`; SHA-256 matches; signature
verifies against the bundled `client/ota_pubkey.pem` (**required** when that file
exists); archive holds only regular files under `client/`; `client/VERSION`
equals the pushed version. The build is installed to
`/var/lib/crewhex-screen/app/<version>/` and `launch.py` starts it next boot,
rolling back to the bundled build after 3 failed starts. A version that rolled
back is not reinstalled.

## 7. v1 (legacy, 1.1.x clients)

`POST /hub-device/register {pin}`, `GET /hub-device/pair/{pin}`, `GET /hub-device/content`,
`POST /hub-device/heartbeat`, `GET /hub-device/update-check`, `POST /hub-device/update-applied`.
Retire once every screen reports 2.x in **Screens**.

## 8. Local kiosk API (127.0.0.1 only, Pi)

The Chromium kiosk talks only to the local client, never to CrewHex, and never
sees the token. Every `/api/*` call needs the header `X-CrewHex-Kiosk: 1`
(which forces a CORS preflight the server never grants) and a loopback `Host`
(DNS-rebinding guard).

| Route | Purpose |
|---|---|
| `GET /` | kiosk (one self-contained file, CSP with script hashes) |
| `GET /api/status` | mode, pairing code, link health, pending commands |
| `GET /api/manifest`, `GET /api/live` | local copies |
| `GET /media/<id>`, `GET /page-image?key=` | verified files from disk (Range supported) |
| `POST /api/report` | kiosk telemetry + proof of play, forwarded in the heartbeat |
| `GET /api/wifi/scan`, `POST /api/wifi/connect` | only while unpaired or offline |
