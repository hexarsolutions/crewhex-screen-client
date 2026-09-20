# CrewHex Screen Client ↔ Server Protocol (v1)

The screen client is server-agnostic about *content* but expects these
endpoints on the CrewHex API (`api_base`, default `https://api.crewhex.com`).
This document is the contract the web-app (Phase 2) build implements.

## 1. Pairing

A screen that has no stored device token polls:

```
GET /api/v1/hub-device/pair/{code}
```

`code` is what the screen displays, format `XXX-XXX` from the alphabet
`A-Z` minus `I,O` plus `2-9` minus `0,1` (e.g. `K7T-QM4`).

Responses:

* `404` — unknown code (screen treats as "keep waiting", shows generic
  waiting state; only a `410` resets the code).
* `410` — code expired or invalidated (screen mints a new code).
* `200`

Pairing pending:
```json
{ "status": "pending" }
```

Paired (returned once the tenant links the code; the endpoint may keep
returning this for a grace window, the client stores the token idempotently):
```json
{
  "status": "paired",
  "device_token": "<opaque bearer token>",
  "device_id": "<uuid>",
  "display_name": "Wynnum workshop TV"
}
```

Server-side expectations: pairing codes are single-screen, expire after
~15 minutes, and a tenant admin creates them by entering the code shown on
the screen into the web app's **Screens** section (which binds the display
to the admin's tenant and issues the `hub_device_tokens` row).

## 2. Content

```
GET /api/v1/hub-device/content
Authorization: Bearer <device_token>
```

```json
{
  "version": "2026-09-21T02:11:04+00:00",
  "display": {
    "name": "Wynnum workshop TV",
    "timezone": "Australia/Brisbane",
    "operating_days": [1,2,3,4,5],
    "open_time": "06:00",
    "close_time": "18:00",
    "holiday_dates": ["2026-12-25"],
    "after_hours_mode": "page" | "blank" | "content",
    "after_hours_page_key": "announcement",
    "after_hours_message": "This display is outside operating hours."
  },
  "pages": [
    {
      "page_key": "announcement",
      "title": "Announcements",
      "enabled": true,
      "rotation_seconds": 30,
      "published_config": {
        "message": "Multibuild Friday - site closed to visitors.",
        "items": [ { "title": "...", "subtitle": "..." } ]
      }
    }
  ]
}
```

Notes:

* `version` is the newest `published_at` across enabled pages; the client
  only rebuilds its slides when it changes.
* Only pages with `enabled: true` are included by the server (the client
  also filters defensively).
* `401` means the token is revoked/unknown — the client wipes its state
  and re-enters pairing mode.
* Page bodies are the *published* configs. `message` renders as the main
  text; `items` (optional list of `{title, subtitle}`) renders as rows.

## 3. Heartbeat

```
POST /api/v1/hub-device/heartbeat
Authorization: Bearer <device_token>
{ "version_seen": "2026-09-21T02:11:04+00:00" }
```

Response `{"ok": true}`. Sent every 60 s; failures are non-fatal. The
server records `last_heartbeat_at` on the display so the **Screens** page
can show online/offline status.

## 4. Client behaviour summary

| Situation | Screen shows |
|---|---|
| No token yet | Pairing screen: big `XXX-XXX` code + instructions |
| Token rejected (401) | Clears token, back to pairing |
| API unreachable > 90 s | "Reconnecting…" veil with the API host |
| Outside operating hours / holiday | After-hours veil (or blank, per mode) |
| Normal | Fullscreen rotation of enabled pages |

The client never needs a human login; the device token is its only
credential and it is stored at `/var/lib/crewhex-screen/device.json`
(0600, service-user owned).
