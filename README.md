# Scrypted as the Single Ring Engine — HomeKit (HKSV) + Home Assistant

Run **one** Ring cloud client and share its cameras with everything else. [Scrypted](https://github.com/koush/scrypted) holds the only session logged into Ring, then re‑exposes each camera locally to both Apple HomeKit — with HomeKit Secure Video — and Home Assistant. Nothing else talks to Ring directly.

> **The problem this solves.** Every independent Ring client — Home Assistant's Ring integration, a HomeKit bridge, the Ring app, a second hub — opens its own cloud session, and they contend for the same account. You feel it as rate‑limited snapshots, stale thumbnails, and live views that drop. Consolidate to a single Scrypted session and the contention disappears; HKSV and fast local snapshots come along for free.

This is the field reference from building and running the setup: a stage‑by‑stage migration runbook and an operations guide for everything that comes after. Every address, token, device id, and credential in it is a placeholder — substitute your own.

---

## What you get

| Capability | How |
|---|---|
| **Single Ring session** | Scrypted is the only client logged into Ring; HomeKit and HA both read from Scrypted |
| **HomeKit Secure Video** | Each camera is exposed to Apple Home in accessory mode; HKSV records to iCloud |
| **Home Assistant cameras** | HA *Generic Camera* entities fed by Scrypted's local snapshot endpoint + RTSP rebroadcast |
| **Fast snapshots** | Local webhook snapshot (~0.1–0.5 s) instead of a per‑request Ring cloud round‑trip |
| **Instant live view** (wired cams) | Optional prebuffer keeps a warm stream, so live view opens in ~0.1 s instead of ~3 s |
| **Motion + doorbell in HA** | Scrypted's MQTT bridge publishes motion / doorbell‑press as HA `binary_sensor`s via MQTT discovery |
| **Doorbell announcements** | HA automation on the doorbell sensor → TTS / multi‑room announcement |
| **Self‑probing health watchdog** | Active snapshot probe catches down / frozen / slow cameras a cached proxy would miss |

---

## How it fits together

```
                         ┌──────────────────────────────┐
   Ring cloud  ──────────►          SCRYPTED             │  (the ONE Ring client)
   (single session)      │  @scrypted/ring              │
                         │  ├─ @scrypted/homekit ────────┼──► Apple Home + HKSV (iCloud)
                         │  ├─ @scrypted/prebuffer-mixin ─┼──► RTSP rebroadcast  ┐
                         │  ├─ @scrypted/webhook ────────┼──► HTTP snapshot URL  │
                         │  └─ @scrypted/mqtt ───────────┼──► MQTT (motion/ding) │
                         └──────────────────────────────┘                       │
                                                                                ▼
                         ┌──────────────────────────────────────────────────────────────┐
                         │                     HOME ASSISTANT                             │
                         │  Generic Camera (still = webhook URL, stream = RTSP)           │
                         │    └─ built‑in go2rtc → WebRTC/HLS live view                   │
                         │  MQTT integration → binary_sensor.<cam>_motion / _doorbell     │
                         │  Automations: doorbell announce · health watchdog · alerts     │
                         └──────────────────────────────────────────────────────────────┘
```

The one idea worth keeping in your head: **Scrypted owns the Ring session; everything downstream is a local reader.** Because Scrypted is its own Control Center device with its own persisted login, deleting Home Assistant's Ring integration doesn't log Scrypted out — which is what makes the cutover safe and reversible.

---

## Requirements

- **Home Assistant** with the built‑in go2rtc (2024.11+), ideally HAOS/Supervised so Scrypted can run as an add‑on.
- **Scrypted** (add‑on or standalone) with the Ring, HomeKit, Rebroadcast (prebuffer‑mixin), Webhook, and MQTT plugins.
- **An MQTT broker** reachable by both Scrypted and HA (e.g. the Mosquitto add‑on) — for motion and doorbell events.
- **Apple HKSV** (optional): a Home Hub (Apple TV / HomePod) on the **same subnet** as Scrypted, plus an iCloud+ plan sized to your camera count.
- A **Ring account with 2FA** — you log Scrypted in interactively once.

---

## Getting started

Read in order:

1. **[`docs/migration-runbook.md`](docs/migration-runbook.md)** — the cutover from a stock‑Ring setup to the single‑Scrypted engine, with **zero camera downtime** and a rollback checkpoint at every stage.
2. **[`docs/operations.md`](docs/operations.md)** — everything that runs afterward: instant live view, motion/doorbell into HA, the health watchdog, doorbell announcements, snapshot freshness, diagnosing stream flapping, and backups.
3. **[`ha/`](ha/)** — the deployable artifacts themselves: the watchdog script, package YAML, and every automation as sanitized, placeholder-ready files. Rebuilding on fresh hardware starts here.

---

## Hard-won lessons

The condensed version of what actually bites in practice. Full detail lives in the docs.

**Streaming & live view**
- **Pin the RTSP rebroadcast port.** Left blank it's randomized on every restart, which silently breaks the HA stream URL.
- **Prebuffer is the instant‑live‑view lever on wired cameras — and poison on battery ones**, where a continuous stream suppresses motion events and drains the battery. This reverses the old blanket "never prebuffer Ring" advice, which was really battery‑specific.
- **One camera may refuse to warm** (`Unable to find sync frame`) if its encoder emits keyframes on demand. That's a hardware/GOP trait, not tunable — leave that camera on‑demand.
- **The prebuffer window length is not a lever.** Current prebuffer‑mixin hardcodes ~10 s (`prebufferDurationMs` constant) — there is no duration setting, and writing a legacy storage key is a silent no‑op. Read the running version's `getSettings()` before writing any setting; vanished options don't error, they just stop mattering. (HKSV only uses a few seconds of pre‑roll, so the cap costs little.)
- **An HA restart can break go2rtc WebRTC** for Generic Cameras until go2rtc is reloaded. Automate the reload on HA start so live view self‑heals.

**Health monitoring**
- **Probe the real snapshot source, not the HA camera proxy.** The proxy returns a *stale cached* frame on failure, so it can't tell a dead camera from a live one.
- **A snapshot‑based watchdog is blind to stream flapping.** A camera can restart its stream dozens of times an hour and still pass every periodic snapshot probe. Measure stream stability at the *log* layer, not the snapshot layer — and the root cause is usually Wi‑Fi (weak signal, interference, or mesh‑roaming behavior) or Ring's cloud relay, not Scrypted.

**HomeKit & MQTT**
- **HomeKit cameras pair in accessory mode** — one QR per camera. Don't scan the HomeKit *bridge* QR for cameras.
- **De‑conflict the HA HomeKit bridge** so the same camera isn't exposed by both HA and Scrypted.
- **Bring motion/doorbell back with Scrypted's MQTT plugin** (publishing via HA MQTT discovery) — *not* the reverse‑direction "Home Assistant" plugin, which imports HA entities into Scrypted, the wrong way.

**Operating it**
- **The supervisor add‑on‑log API returns the *oldest* entries by default.** `Range: entries=:0:N` is a frozen head slice that never advances; use `entries=:-N:N` for the live tail, or you'll analyze hours‑old data and reach the wrong conclusion.
- **Use the insecure HTTP snapshot endpoint** so HA doesn't reject Scrypted's self‑signed certificate.
- **Home Assistant stores backups locally by default.** That protects you against a bad update, not against a dead SD card — arrange an off‑site copy, or a hardware failure takes your only rebuild image with it.

---

## Security & privacy

This is a personal home‑automation setup, published as a reference. Before adapting it:

- Every address, port‑path token, device id, pairing code, and credential in these docs is a **placeholder** (`<HA_HOST_IP>`, `<device_id>`, `<token>`, `<mqtt_user>`, …). Never commit real tokens, Ring credentials, HomeKit pairing codes, or rebroadcast path tokens.
- The Scrypted↔Ring session is bound to your account — treat the Scrypted admin login and its stored Ring refresh token as secrets.
- The local snapshot/RTSP endpoints are unauthenticated on the LAN by design (they carry per‑stream tokens). Keep them off untrusted networks and reach them remotely over a VPN, not a port‑forward.

---

## Contributing & security

Issues and PRs are welcome, though this is a personal reference repo and response cadence varies. Report suspected security issues via [GitHub Security Advisories](../../security/advisories/new) - see [`SECURITY.md`](SECURITY.md).

## License / disclaimer

Licensed under the **MIT License** — Copyright © 2026 Eric Paschal. See [`LICENSE`](LICENSE) for the full text.

Not affiliated with or endorsed by Ring, Amazon, Apple, or the Scrypted project. Ring, HomeKit, HKSV, and Home Assistant behaviors change across versions — treat every version‑specific note as "verify on your box."
