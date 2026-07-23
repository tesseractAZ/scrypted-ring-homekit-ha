# Migration runbook — make Scrypted the single Ring engine (HomeKit HKSV + Home Assistant)

Stage‑by‑stage cutover from a stock‑Ring Home Assistant setup to a single Scrypted Ring session that feeds **both** Apple HomeKit (with HKSV) and Home Assistant — with **zero camera downtime** and a clean rollback checkpoint.

**Starting point assumed:** HA (2024.11+ so go2rtc is built in) with the **stock Ring integration** active, an existing **HA HomeKit bridge**, and however you currently surface Ring stills/cameras (e.g. a custom snapshot component + `local_file` still cameras + a couple of helper automations). Adapt the specifics to your setup.

**Goal:** Scrypted becomes the ONE Ring client, fanning out to Apple HomeKit (HKSV) and Home Assistant. Retire the stock Ring integration and any custom snapshot/still scaffolding — proving the new path green **before** disabling the old one.

**Core principle:** Stand Scrypted up **alongside** the working system. New Scrypted‑fed HA cameras must be proven green BEFORE any old path is disabled. Scrypted is a **distinct Ring session** (its own Control Center device + persisted session id), so deleting HA's Ring entry does not log Scrypted out.

**Legend:** 🔴 **USER‑GATED** (a human must click/type — add‑on install, Ring password + live 2FA, iOS HomeKit QR scan) · 🟢 **AUTOMATABLE** (API/file/config) · 🟡 **CONFIRM LIVE** (version‑dependent — verify on your box, don't assume).

> Placeholders: `<HA_HOST_IP>` = the address where HA / the Scrypted add‑on is reachable on your LAN · `<device_id>`, `<token>`, `<rtspServerPath>` = per‑camera / per‑stream values you copy from the Scrypted console (never construct them).

---

## Stage 0 — Pre-flight snapshot & backup
**WHO:** 🟢 AUTOMATABLE

1. Back up (keep until the soak fully passes, so rollback is a file‑copy + restart): any custom snapshot component, the `packages/*.yaml` that defines your still cameras / snapshot pipeline, and the helper automations.
2. Record which Lovelace cards point at the old `camera.<name>` / `camera.<name>_still` entities — repointed in Stage 7.
3. Confirm your HA HomeKit bridge config location (`homekit:` YAML or the UI HomeKit Bridge entry) and whether it currently exposes the Ring cameras.

**VERIFY:** backups exist off the live path; a written list of every card / automation / HomeKit‑bridge reference to the old entities.

---

## Stage 1 — Install the Scrypted add-on
**WHO:** 🔴 **USER‑GATED** — the install click must happen in the HA admin UI. Supervisor add‑on management authenticates with the Supervisor's own token, not a user token (a user token against `/api/hassio/` returns **HTTP 401**), so it can't be scripted.

**Clicks:** Settings → Add‑ons → Add‑on Store → ⋮ → Repositories → add `https://github.com/koush/scrypted` → search **Scrypted** → **Install** → Show in sidebar **ON** → **Start**. First boot auto‑creates an internal admin used transparently via Ingress.

**Ports (hardcoded defaults):** HTTPS/admin **`10443`** (self‑signed) · HTTP/insecure **`11080`** · integration host later `127.0.0.1:10443`.

**Notes:**
- HAOS is **not recommended for Scrypted NVR** (limited local storage). Not a blocker if HKSV records to iCloud rather than locally.
- 🟡 If the sidebar shows a blank page, log in with the dedicated admin from Stage 2.

**VERIFY:** opening Scrypted from the sidebar auto‑logs‑in and the Management Console loads.

---

## Stage 2 — Create a dedicated Scrypted admin user
**WHO:** 🔴 USER‑GATED (Scrypted UI)

Scrypted → Settings/Users → create a **new admin user** (your own username/password). The auto‑created Ingress user can't be used by external integrations/plugins that need to authenticate — you need a real admin.

**VERIFY:** you can log into `https://<HA_HOST_IP>:10443` (or the sidebar) as the new admin.

---

## Stage 3 — ⚠️ RATE-LIMIT GUARD: pause any second Ring client, then log Scrypted into Ring
**WHO:** Step A 🟢 AUTOMATABLE · Step B 🔴 USER‑GATED (Ring email + password + **live 2FA**)

**Ordering is mandatory** — quiet any existing Ring poller/client **immediately before** the Scrypted Ring login and keep it off through cutover, so two clients don't hammer Ring during login.

- **Step A (first):** turn off any existing snapshot/poller automation and leave it off for the whole cutover. (Keep the stock Ring integration *loaded* for now if a custom component still borrows its session.)
- **Step B (immediately after):** Scrypted → install the **Ring Plugin** → enter Ring **Email + Password** → enter the **2FA code** Ring sends → Scrypted stores its own auto‑rotating refresh token and registers as a distinct Control Center device.
  - Fallback if in‑app login won't complete: generate a token with `npx -p ring-client-api ring-auth-cli` and paste it into the **Refresh Token** field (leave email/password blank).
  - Set a recognizable Control Center display name; leave **Legacy RTSP Streaming** off.

**VERIFY:** each camera shows a live snapshot and plays live **inside the Scrypted console**; the old poller is off.

---

## Stage 4 — Per-camera stream config in Scrypted (battery vs wired)
**WHO:** 🔴 USER‑GATED (Scrypted toggles)

**Battery / motion‑event trap (from the Ring plugin guidance):**
- On **battery** cameras: **do not enable Prebuffer**, PAM‑DIFF, or OpenCV — a persistent stream *stops motion‑event delivery* and *drains the battery faster than it charges*, and clutters the Ring app with Live‑View recordings. An always‑open RTSP pull has the same effect; prefer **snapshot‑only** Generic Cameras for battery cams.
- On **wired / hardline** cameras: none of that applies. **Prebuffer is safe and desirable** here — it's what makes live view instant (see [operations.md §1](operations.md#1-instant-live-view-prebuffer-wired-cameras-only)). Leave it off during the migration and enable it as a post‑cutover step once each camera's wired status is confirmed.
- **Codec must be H.264** (H.265/MJPEG break WebRTC).

> This corrects the blanket "never prebuffer Ring" advice — that warning is **battery‑specific**. Classify each camera wired‑vs‑battery before choosing.

**VERIFY:** on battery cams, Prebuffer/PAM‑DIFF/OpenCV are off; snapshots + live still work in the console for all cameras.

---

## Stage 5 — Build the Scrypted → HA feed URLs (still + stream)
**WHO:** 🔴 USER‑GATED to read per‑camera URLs from the console (tokens are per‑stream — copy, don't guess) · 🟢 AUTOMATABLE to test them.

### 5a. Still image (Webhook plugin)
1. Install `@scrypted/webhook`; enable it as a mixin on each camera.
2. The camera **Console** prints an **Insecure Local Base URL** = `<endpoint>/<device_id>/<token>`. Append `takePicture`.
3. Use the **insecure (http, 11080)** endpoint so HA doesn't reject the self‑signed cert. A wrong token returns HTTP 401 `Invalid Token`.

```
http://<HA_HOST_IP>:11080/endpoint/@scrypted/webhook/public/<device_id>/<token>/takePicture
```

### 5b. Live stream (Rebroadcast / prebuffer-mixin)
- Plugin: `@scrypted/prebuffer-mixin` ("Rebroadcast Plugin") — auto‑enabled mixin on every camera.
- **⚠️ PIN A STABLE PORT FIRST:** Rebroadcast Plugin → Manage → set **Rebroadcast Port** to a fixed value (e.g. `8554`). Blank → `listen(0)` picks a **random port that changes every restart** and silently breaks the HA URL.
- Per camera, copy the read‑only **RTSP Rebroadcast Url**:

```
rtsp://<HA_HOST_IP>:<rebroadcastPort>/<rtspServerPath>
```
`<rtspServerPath>` is a random hex token **persisted per stream — copy exactly, do not construct.** Replace any `localhost` with the address reachable **from HA**.

**VERIFY:** each `/takePicture` URL returns a JPEG in a browser; each `rtsp://…/<token>` plays in VLC — **before** touching HA.

---

## Stage 6 — Create the NEW Generic Camera entities in HA (stock Ring untouched)
**WHO:** 🟢 AUTOMATABLE. Runs alongside stock Ring (distinct sessions).

Settings → Devices & Services → **+ Add Integration → Generic Camera**, per camera:
- **Still Image URL** = the `takePicture` webhook URL (5a)
- **Stream Source** = the `rtsp://…/<token>` URL (5b) — or blank for snapshot‑only battery cams
- **RTSP transport** = `tcp` · **Authentication** = blank · **Verify SSL** = OFF (you used the http endpoint)

**Live view:** HA's built‑in **go2rtc** auto‑proxies RTSP → WebRTC/HLS; no `go2rtc.yaml` edit needed. Note that **an HA restart can later leave go2rtc unable to serve these sources** until it's reloaded — see [operations.md §2](operations.md#2-go2rtc-webrtc-survives-with-a-reload-on-restart-automation) for the reload‑on‑start automation.

**🟡 CONFIRM LIVE (transport):** on some HA releases there's a regression where Scrypted‑native **TCP** into Generic Camera fails "Stream never started." Start with `tcp`; if it fails, switch HA's stream backend to **FFmpeg** (FFmpeg TCP).

**VERIFY:** the new `camera.<generic_name>` entities render snapshot + live for **all** cameras before Stage 7.

---

## Stage 7 — Repoint dashboard + automations to the NEW entities (before any teardown)
**WHO:** 🟢 AUTOMATABLE

**⚠️ HARD ORDERING:** if a custom snapshot component borrows the stock Ring integration's authenticated session, disabling stock Ring instantly breaks it. So repoint **everything** to the Scrypted‑fed Generic Cameras first.

1. Edit every Lovelace card at the old entities → point at `camera.<generic_name>`.
2. Old helper automations → repoint to the new entities or mark for deletion (Stage 10).

**VERIFY (gate):** every card renders live + snapshot from Scrypted; all cameras green on the NEW entities. Do not proceed until so.

---

## Stage 8 — Expose Scrypted → Apple HomeKit + HKSV, and de-conflict the HA HomeKit bridge
**WHO:** plugin config 🔴 USER‑GATED · **iOS QR pairing per camera** 🔴 USER‑GATED · HA bridge de‑conflict 🟢 AUTOMATABLE

**8a. Prep:** HKSV needs **H.264 + a working motion sensor**; for Ring cloud cameras rely on the plugin‑provided motion.

**8b. Pair per camera:** install the **HomeKit Plugin**. Cameras default to **Accessory (Standalone) Mode** — **each camera has its own QR code**. In iOS Home → Add Accessory → scan that camera's QR (Scrypted → camera → HomeKit → Pairing). Repeat per camera. **Do not** scan the HomeKit plugin's **bridge** QR for cameras — the bridge is only for non‑camera devices.

**8c. Enable HKSV (iPhone):** Home app → camera → gear → enable **Stream & Recording** → set rules.

**HKSV hard requirements:** H.264 + motion sensor + a **Home Hub** (Apple TV / HomePod) + **iCloud+**, with Scrypted on the **same subnet** as the hub (cross‑subnet recording silently fails). iCloud+ HKSV camera limits scale with plan tier (recordings themselves don't count against storage); size your plan to your camera count.

**8d. De‑conflict the HA bridge (critical):** HA's own HomeKit bridge may expose the same cameras → duplicates. Switch the HA HomeKit bridge to **Include mode** listing only the entities you want (Ring cameras left out), or use `homekit:` → `filter:` → `exclude_domains: [camera]`. A known HA bug re‑duplicates on restart — Include mode + deleting leftover ghosts in Apple Home is the durable fix.

**8e. Ghost cleanup:** delete pre‑existing HA‑bridge Ring accessories in Apple Home; keep only the Scrypted accessory‑mode cameras. If pairing/recording fails: verify the hub is online/updated and on the **same subnet/VLAN**, enable LAN multicast, try a different mDNS advertiser, and use **Reset Pairing** to retry.

**VERIFY:** each camera appears **once** in Apple Home, live view works, `Stream & Recording` on with recordings appearing; the same camera is **not** exposed by both the HA bridge and Scrypted.

---

## Stage 9 — Disable (reversibly) the stock Ring integration — ROLLBACK CHECKPOINT
**WHO:** 🟢 AUTOMATABLE. Only after Stages 6–8 are green.

Settings → Devices & Services → **Ring → ⋮ → Disable.** This unloads the entry; any custom component borrowing its session goes away and its poll loop stops — **expected**, which is why the dashboard was repointed first. The old entities go `unavailable`; no restart needed; history preserved.

**⚠️ Leave DISABLED for a soak (hours → a day). This disabled state is the rollback checkpoint.**

**VERIFY (soak gate):** all Scrypted‑fed cameras stay green, HKSV records motion, Scrypted's Ring session healthy, no dark cards.

---

## Stage 10 — Retire the old custom component / packages / automations / still cameras
**WHO:** 🟢 AUTOMATABLE (after a clean soak)

Delete the obsolete automations; remove the custom snapshot component + its packages YAML (which defined the `local_file` still cameras + any ffmpeg pipeline); the still entities disappear with their YAML. **Check Configuration**, then **restart HA** to cleanly unload the deleted custom component.

**VERIFY:** Check Config passes; post‑restart there are no leftover still entities and no errors from the removed component; all Scrypted cameras green.

---

## Stage 11 — Delete the stock Ring integration → exactly ONE Ring client
**WHO:** delete 🟢 AUTOMATABLE · Ring‑app verification 🔴 USER‑GATED

1. Settings → Devices & Services → **Ring → ⋮ → Delete** → confirm. **Restart HA.**
2. **Ring app → Account → Control Center → Authorized Client Devices:** confirm only the **Scrypted** device remains and the old Home Assistant client is gone (a stale HA client could later invalidate Scrypted's refresh token).

**Now proceed to [operations.md](operations.md)** to enable instant live view, restore motion/doorbell to HA, and stand up the health watchdog.

---

## Single-client proof checklist (after Stage 11)
1. **Ring app:** Control Center → Authorized Client Devices = only the Scrypted device.
2. **HA:** no Ring integration; no leftover still entities; no snapshot‑component log errors.
3. **Scrypted:** all cameras online; on battery cams, Prebuffer/PAM‑DIFF/OpenCV off; motion events arriving.
4. **HomeKit:** each camera once in Apple Home; HKSV recording within tier; the HA bridge exposes no Ring camera.
5. **Battery cams (if any):** the Ring app is not accumulating stray Live‑View recordings.

## Rollback
- **At Stage 9 (only DISABLED):** Ring → ⋮ → **Enable** — restores the borrowed session, the old poll loop, and the old stills/dashboard. Fully reversible, no restart, no data loss.
- **After Stage 11 (DELETED):** re‑add via + Add Integration → Ring with fresh 2FA — this introduces a **second** client and may rotate Scrypted's token (you may then need to re‑2FA Scrypted).
- **If Scrypted is the problem:** re‑run its Ring 2FA login (Stage 3B); HA's Ring entry is unaffected.
- Keep the old custom component + packages YAML backed up until the soak passes.

## Exact-values reference
| Item | Value |
|---|---|
| Add‑on repo | `https://github.com/koush/scrypted` |
| HTTPS/admin port | `10443` (self‑signed) |
| HTTP/insecure port | `11080` |
| Integration host (add‑on) | `127.0.0.1:10443` |
| Still Image URL | `http://<HA_HOST_IP>:11080/endpoint/@scrypted/webhook/public/<device_id>/<token>/takePicture` |
| RTSP rebroadcast URL | `rtsp://<HA_HOST_IP>:<rebroadcastPort>/<rtspServerPath>` (pin the port; path = random hex, copy exactly) |
| Rebroadcast plugin | `@scrypted/prebuffer-mixin` ("Rebroadcast Plugin") |
| Webhook plugin | `@scrypted/webhook` |
| MQTT plugin (motion/doorbell) | `@scrypted/mqtt` → see operations.md §3 |
| Ring 2FA field | `Two Factor Code` |
| Ring token fallback | `npx -p ring-client-api ring-auth-cli` |
| HA Generic Camera transport | `tcp` (fallback: FFmpeg backend on some releases — confirm live) |
| HA go2rtc | built‑in since 2024.11, auto WebRTC proxy |
| HomeKit cameras | Accessory Mode (per‑camera QR); do **not** scan the bridge QR |
| HKSV toggle | `Stream & Recording` (needs a Home Hub + iCloud+ on the same subnet) |

**Confirm‑live items (version‑dependent):** RTSP `tcp` vs FFmpeg backend · sidebar white‑page fix · Generic‑Camera / go2rtc WebRTC behavior after restart · your iCloud+ tier vs camera count · per‑camera battery‑vs‑wired classification.
