# Security policy

## Reporting a vulnerability

Report suspected vulnerabilities and leaked values privately through GitHub's private vulnerability reporting: **[open a security advisory](../../security/advisories/new)**. Please do not open a public issue or pull request for them.

A useful report names the file and line, the input that triggers the problem and its effect. Do not attach a raw Scrypted add-on log, engine-log archive files, a Home Assistant backup or a `.pseudonym.key`: they hold credentials or personal data. Describe the shape of a line with placeholders instead.

In scope: the scripts, packages and automations under `ha/`, the CI under `.github/`, documented procedures that would expose a credential, and any instance value (address, token, id, name) found in the tree or its history. Problems in Scrypted itself belong to its maintainers ([koush/scrypted](https://github.com/koush/scrypted)); problems in the Ring service belong to Ring.

This is a reference repository maintained on a best-effort basis, with no response-time commitment. Only the current `main` branch is supported; there are no releases.

## What this repository ships and where it runs

| Path | Deployed to | Runs as |
|---|---|---|
| `ha/scripts/*.py` (six scripts) | `/config/scripts/` | five `command_line` sensors, plus `partial_loss.py`, a module that `cam_motion.py` imports; all run **inside the Home Assistant Core container**, with Core's privileges and environment, including `SUPERVISOR_TOKEN` |
| `ha/packages/*.yaml` | `/config/packages/` | Home Assistant configuration: `command_line` sensors with fixed command strings (`python3 /config/scripts/<name>.py`, no templated arguments), template binary sensors, one `input_button` |
| `ha/automations/*.json` | Home Assistant storage automations | state, template, time, time-pattern, start-up and `persistent_notification` triggers; **no webhook trigger** |
| `README.md`, `docs/` | - | documentation |
| `.github/` | GitHub Actions | CI only |

The repository ships **no add-on, no container image, no Scrypted plugin and no AppArmor profile**. Scrypted runs as a third-party Home Assistant add-on whose code, configuration and confinement belong to its maintainers; the documentation configures it through Scrypted's own UI, and the scripts read its log. The one exception is a documented local patch to the Ring plugin's bundled push receiver ([Operations §3](docs/operations.md#3-motion--doorbell-press--home-assistant-mqtt)). It needs access to the Scrypted volume, so keep the original file, and a plugin update replaces the patched copy.

The scripts use the Python standard library, plus Pillow from the Core container in `cam_vision.py`. None of them opens a listening socket, spawns a shell or a subprocess, or deserialises anything but JSON (no `pickle`, `yaml.load` or `eval`). Every network call is plain HTTP to one of two local endpoints: the Supervisor (`http://supervisor`) and the host's own Scrypted HTTP port (`http://<HA_HOST_IP>:11080`). Files are written under `/config` (each monitor's own dotfile state and the engine-log archive directory) and under `/dev/shm/cam_snap/` in the Core container's tmpfs, a directory created with mode `0700` where `cam_health.py` leaves the latest good snapshot of each camera and a cycle marker for `cam_vision.py`.

## Trust boundaries and untrusted input

- **Scrypted engine log**, read by `cam_flap.py` and `cam_logarchive.py` through the Supervisor log API. Its text comes from the Ring cloud (push-notification and signalling dumps), from Scrypted plugins, and from any client that can reach Scrypted's webhook endpoint: the endpoint logs each request, URL and headers included, before it checks the token. Every line is treated as untrusted. `cam_flap.py` only matches fixed patterns and publishes counts, camera and door names and times. `cam_logarchive.py` redacts before writing and withholds what still looks like an identifier (below); a line whose redaction raises is withheld rather than archived and does not stop the run; the archive's own marker lines are written by the script, never copied from the log. Each archive run stops at a 60 s work budget, checked between journal entries.
- **Snapshots** from the Scrypted webhooks: `cam_health.py` hashes the body and writes each good one to `/dev/shm/cam_snap/<cam>.jpg` (a temporary file, then a rename); `cam_vision.py` decodes a thumbnail of that file with Pillow, and fetches from the webhook itself only while those frames are missing or stale. Bodies are read whole from the host's own Scrypted port.
- **Recorder database**: `cam_motion.py` opens it read-only (`mode=ro`) with bound query parameters.
- **State files** under `/config`: written and read back as JSON by the monitors themselves.

## Credentials the scripts use

| Credential | Used by | Sent to | Stored |
|---|---|---|---|
| `SUPERVISOR_TOKEN` | `cam_flap.py`, `cam_logarchive.py` | `http://supervisor/addons/<scrypted_addon_slug>/logs`, as a Bearer header | never: read from the Core container's environment; not written, logged or published |
| Scrypted webhook tokens | `cam_health.py`, `cam_vision.py` | the URL path of `http://<HA_HOST_IP>:11080/endpoint/@scrypted/webhook/public/<device_id>/<webhook_token>/takePicture` | in the **deployed** copies under `/config/scripts/`, and therefore in Home Assistant backups; placeholders in this repository |
| Home Assistant long-lived access token | the operator, for the REST deploy steps in [`ha/README.md`](ha/README.md) | Home Assistant's REST API | not used or stored by any script |

`SUPERVISOR_TOKEN` is Home Assistant Core's own Supervisor token and carries Core's broad Supervisor API access. Anything that can modify `/config/scripts/` therefore runs code with it; see the hardening list below.

Scrypted logs every webhook request with its URL, so the raw add-on log holds the webhook tokens in clear.

## Sensitive data

### The raw Scrypted add-on log

It is **not redacted**. It holds Ring session JWTs, webhook tokens and request URLs, ICE credentials, DTLS fingerprints, IPv4 and IPv6 addresses (the startup banner prints the host's global IPv6 address) and Ring device and location ids. Home Assistant administrators can read it on the add-on's log page, and so can anything holding a Supervisor token with log access. Never paste it into an issue; [`docs/operations.md`](docs/operations.md) shows how to measure it without printing its content.

### The engine-log archive (`cam_logarchive.py`)

- **Where.** `/config/cam_engine.log.d/`, one gzip file per UTC day, 35 days. An archive run sets the process umask to `077` and creates the directory with mode `0700` when it does not exist; day files, `state.json` and the lock file are created `0600`. The directory name matches the Supervisor's core-backup exclusion `*.log.*`, so the archive is not included in Home Assistant backups; a core restore deletes it.
- **Redaction before anything is written**, in a fixed order: JWTs and e-mail addresses; IPv6 (including `::ffff:a.b.c.d` and an address with its port glued on) before IPv4; `Authorization` and `Cookie` values, bearer and basic tokens, ICE credentials, password, username, token, secret and api-key values; RTSP session ids; DTLS fingerprints and MACs; Ring ids by key and as the `id` of a location, device, doorbot or ding object, and location ids in Ring REST paths and errors; ding and push epoch-millisecond stamps (replaced by offsets from the line's own time); UUIDs, HomeKit setup codes and coordinates; runs of 16 or more hex digits and of 7 or more digits; last, runs of eight or more characters mixing letters and digits in quoted values under keys ending in `id` or `_key`. A probe request dump that matches a strict grammar is collapsed to its url line plus a note of its method and user-agent; its other lines are not written.
- **Residual scan.** An independent, broader scan runs over the whole archived line. Any hit writes `<WITHHELD residual=...>` in place of the text, keeping the line's prefix (timestamp, host and writer; only the timestamp if the prefix itself trips the scan). Withheld lines are counted and recorded as a persisted event.
- **Keyed pseudonyms.** Camera, ding, media-cell and session ids become `<DEV:...>`, `<DING:...>`, `<CELL:...>` and `<SES:...>`: HMAC-SHA256 over the id class and value under a 32-byte key from `os.urandom`, written in the letters `g` to `z`: 8 letters for a camera, 12 for the others. Account, location, user and hardware ids stay the constant `<RID>`.
- **The key file.** `.pseudonym.key` in the archive directory. It is written to a temporary file created with `O_CREAT | O_EXCL` and mode `0600` (then `fchmod` to `0600`), fsynced and renamed over the key file, so it never exists with wider permissions; a failed save removes the temporary file, and stale temporary files are deleted under the run lock. A new key is saved only after the run's archive lines are on disk, and the run that starts a new key heads every gzip member it appends with a `#~camlog~ REKEY` line (a brand-new archive starts with `START` instead), so tokens made under different keys are never mistaken for one another. A key file of the wrong size is replaced the same way; any other error reading it fails the run before anything is written or repaired. `--dry-run` uses a throwaway key and writes nothing. The key is never logged or published. It is a dotfile so that a `*` copy of the day files leaves it behind: keep it on the host, because with the key a camera's 9-digit Ring id can be recovered from its token by brute force (about 10^9 HMACs).
- **What is published.** The sensor's attributes carry counts, timestamps and status text, never archived line content. `--scan` reports counts; `--dry-run` reports counts and the names of keys whose values were replaced, never values.
- **Still personal data.** The archive keeps camera names, door names, times, the host name and the writer's identifier, and so shows when people come and go. Do not attach it to issues or share it.
- **Limits.** Redaction is pattern-based. An identifier with no key, shorter than the opaque and digit floors and not a JWT, IP address, UUID or colon-hex run passes both scans; a `withheld` event or a nonzero `residual_run` is the signal to add a rule. A rule change applies to new lines only.

### Other monitor state

`/config/.cam_health_state.json` (frame hashes), `/config/.cam_vision_state.json` (per-camera 48 x 30 grayscale baseline frames, base64, and change times), `/config/.cam_motion_state.json` (motion statistics, verdict latches and recorder-gap history) and `/config/.cam_flap_door_state.json` (door-trip verdicts with times) are ordinary `/config` files and are included in Home Assistant backups.

`/dev/shm/cam_snap/` in the Home Assistant Core container holds the latest good snapshot of every camera (`<cam>.jpg`) and a `CYCLE` marker. It is tmpfs (memory), outside `/config`, not in any backup, and emptied when Home Assistant restarts. Until then it is the current picture of every view, readable by anything with root access to the Core container.

### What leaves the host

Push notifications (`notify.<your_mobile_app_target>`) carry camera and door names, counts, times and monitor status text, and pass through the mobile app's push service. They carry no tokens, credentials or addresses. Every other alert is a persistent notification inside Home Assistant. The doorbell announcement sends one fixed phrase to the configured TTS engine, which may be a cloud service.

## The Scrypted add-on's posture

As deployed with Home Assistant Core 2026.10.1 and Scrypted add-on v0.147.0:

- confined by the Supervisor's **default AppArmor profile**; protection mode **on**; `full_access: false`; no privileged capabilities;
- `host_network: true`: Scrypted's server listens on every host interface (HTTPS admin port `10443`; the probes use HTTP port `11080`), and its startup banner prints a Remote URL on the host's **global IPv6 address**;
- `hassio_api: true` (default role) and `homeassistant_api: true`: Scrypted can call the Supervisor and Home Assistant's API;
- device access: `/dev/mem`, `/dev/dri/*`, `/dev/apex_0` to `/dev/apex_3` (Coral), `/dev/vchiq`, `/dev/video*`, plus USB, UART, GPIO and video.

Scrypted holds the account's Ring refresh token, and every Scrypted plugin runs inside it. Treat it as a trusted, high-value component.

### Owner-side hardening

1. **Keep protection mode on** for the Scrypted add-on, so full hardware or Docker API access that a future release might request stays blocked.
2. **Keep AppArmor in force.** The add-on runs under the Supervisor's default profile; keep AppArmor enabled on the host (Home Assistant OS enables it). This repository needs no profile of its own.
3. **Do not grant full access**: do not install a modified add-on build that requests `full_access`, privileged capabilities or Docker API access.
4. **Close inbound IPv6.** On IPv6 the host is reachable without any port-forward if the router lets traffic in. Confirm that the router drops unsolicited inbound IPv6 to the host, at least TCP `10443`, `11080` and the pinned RTSP rebroadcast port, and test it from outside the LAN, for example from a mobile connection against the banner's Remote URL. For remote access, use a VPN.
5. **Scrypted login and plugins.** Use a strong, unique Scrypted admin password, and install only the plugins the setup needs, from trusted sources.
6. **Ring account.** Keep two-factor authentication on and review Authorized Client Devices in the Ring app; remove anything unknown.
7. **Restrict `/config`.** Add-ons that map the configuration folder (Samba, SSH, file editors) can read the archive and its key and can change the scripts that run with `SUPERVISOR_TOKEN`. Install them only when needed and protect their credentials.
8. **Backups.** They contain the deployed scripts with their webhook tokens and the monitors' state: keep them encrypted and off untrusted storage.
9. **Stay current.** Keep Home Assistant, the Supervisor and the Scrypted add-on updated.

## Repository controls

- **CodeQL** code scanning (`.github/workflows/codeql.yml`, `security-extended` queries) for Python and the GitHub Actions workflows, on push, pull request and weekly.
- **Secret scanning** with **push protection**.
- **Dependabot security updates.**
- **Private vulnerability reporting** (see above).
- **CI** (`.github/workflows/validate.yml`) runs with a read-only token (`permissions: contents: read`) on pushes to `main`, pull requests and manual dispatch. It has no `pull_request_target` trigger. Every action is pinned to a full commit SHA, checkout keeps no credentials (`persist-credentials: false`), and PyYAML is installed by version and hash from `.github/requirements-ci.txt`; `.github/dependabot.yml` keeps the pins current. Its one secret goes to the hygiene sweep, which runs in a job of its own with no third-party code but the pinned checkout, so nothing an earlier step installs can rewrite the sweep or read its environment. The validate job compiles the scripts, parses the packages with `yaml.safe_load`, checks each automation's JSON and that its id matches its filename, checks every package's `json_attributes` against the keys its script emits, and runs the hygiene sweep.
- **Hygiene sweep** (`.github/hygiene-sweep.sh`): generic patterns (private IPv4 ranges, IPv6 addresses, runs of 16 or more hex digits, add-on slugs, home-directory paths, config-entry ids, a real notify target) plus instance names and ids loaded from a CI secret or an untracked local file, never from the repository. It reports `file:line` only, never the matched text. It scans the working tree, not history; in CI it runs after a push is already public, and without the secret (for example on a pull request from a fork) only the generic checks run.
- **Branch ruleset.** Deleting `main` and force-pushing to it are blocked, with no bypass, so published history cannot be rewritten or dropped by mistake.
- **Placeholders.** Every instance-specific address, token, device id and name in the repository is a placeholder such as `<HA_HOST_IP>`, `<device_id_N>`, `<webhook_token>` or `<cam_N>`. `.gitignore` excludes secrets files, root-level Home Assistant configuration, and engine-log archive pulls, the pseudonym key and the monitors' state files (gzip would hide an archive's content from the sweep).
