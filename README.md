# PS5 Command Hub

A local, controller-driven dashboard for a PS5 running etaHEN. It runs on a Windows
PC on your LAN, hosts your `.pkg` files over local HTTP, tells etaHEN's installer
daemon to fetch them, and gives you the whole thing as a TV-legible interface you can
drive from a DualSense — from the console's browser or from a homebrew launcher tile.

Nothing in it contacts the internet. No CDN, no remote font, no analytics, no
catalogue lookups, no art service. The Safety tab re-verifies that claim at startup
by scanning the shipped frontend for remote references, so it is a checked assertion
rather than a promise.

---

## Quick start

1. **Install Python 3.11 or newer.**
   `winget install Python.Python.3.12`, or python.org — tick *Add python.exe to PATH*.
2. **Run `start_hub.bat`.** It creates `.venv`, installs `flask` + `cachetools`, and
   starts the server. First run also writes `configs.json` from the defaults.
3. **Put packages in `packages\`** (or point `library.pkg_directories` elsewhere in
   Settings). Sub-folders are scanned.
4. **Open the URL the banner prints** — e.g. `http://192.168.1.20:8080` — in the PS5
   browser, or on your PC to try it with a mouse.

It starts in **mock mode**: a built-in simulator stands in for etaHEN, so the entire
UI works with the console switched off. Point it at the real console in
Settings → Console when you are ready.

```
start_hub.bat                  # normal start
start_hub.bat --port 9000      # override the port for one run
start_hub.bat --mock           # force the simulator
python run.py --host 127.0.0.1 # bind loopback only
python -m server.mock_etahen   # run the simulator on its own
```

---

## How installing actually works

```
   library scan            install request              console fetches
PC .pkg files ──▶ hub ──────────────────────▶ etaHEN ──────────────────▶ hub
                                 :12800                    GET /pkg/<id>
```

The hub never pushes bytes at the console. It hands etaHEN a URL on this PC
(`http://<your-ip>:8080/pkg/<id>`) and the console pulls it. That is why:

- **the bandwidth throttle genuinely works** — we own the socket, so a token bucket
  on our side is a real ceiling, not a request;
- **pause/resume is possible at all** — pausing cuts the byte stream (the package
  route answers `503`), and because the route honours `Range`, a console that
  re-requests with an offset continues instead of restarting;
- **a pasted link bypasses the hub entirely** — the console fetches it directly, so
  the hub never sees or relays those bytes.

**Progress is attributed.** If the installer daemon exposes a usable progress
endpoint, the row says *reported by etaHEN*. If it does not, progress is inferred
from bytes this hub has served and is labelled *estimated from bytes served* —
which means the package left the PC, not that the console finished writing it. The
UI never passes one off as the other.

---

## Configuration

Everything lives in `configs.json` beside `run.py`. It is created on first run and
documented field-by-field in `configs.example.json`. Any string value may contain
`{app}`, which expands to the folder holding `run.py`.

| Section | What matters |
|---|---|
| `server` | `host_ip` (`"auto"` detects your LAN address), `port`, `subnet_allowlist` |
| `etahen` | `mode` (`mock`/`live`), `console_ip`, ports, and **`endpoints`** |
| `library` | `pkg_directories`, scan interval, `max_items` |
| `transfers` | concurrency, retries, `throttle_mbps`, history size |
| `ui` | performance mode, overscan, cards per row, language, sounds |
| `safety` | `active_probing` (off by default), PSN domains, Pi-hole, log retention |
| `backup` | `source_dirs` for the save-data bundler |
| `wipe` | PIN hash, confirm phrase, lockout, and the **allowlist** |

### `etahen.endpoints` — the important one

etaHEN's wire format is not a stable public contract, and it differs between builds
and firmware. So every request the hub makes is described in config, not in code:

```json
"install": {
  "port": "installer",
  "method": "POST",
  "path": "/api/install",
  "payload_template": { "type": "direct", "packages": ["{url}"] }
}
```

`{url}`, `{title_id}`, `{task_id}` and `{state}` are substituted at call time. If
installs fail against your console, this is the thing to adjust — no Python
changes needed. Compare what the hub sends against what your build expects:

```bash
curl -v http://<console-ip>:9090/api/heartbeat
curl -v -X POST http://<console-ip>:12800/api/install \
     -H 'Content-Type: application/json' \
     -d '{"type":"direct","packages":["http://<pc-ip>:8080/pkg/<id>"]}'
```

Endpoints the hub uses if present and degrades gracefully without:
`progress`, `storage`, `hardware`, `klog`, `task_control`, `delete_path`.

### Changing settings

Settings written from the UI are restricted to an allowlist on the server
(`ALLOWED_SETTING_PATHS` in `server/routes/api_system.py`). Everything under `wipe`
is deliberately excluded: the PIN and the confirmation phrase can only be changed
through the PIN-gated endpoints, so no settings call can weaken the destructive path.

---

## Controller map

| Button | Action |
|---|---|
| D-pad / left stick | Move focus (geometric, with auto-repeat) |
| Cross | Confirm — open the item sheet, press a button, edit a field |
| Circle | Back — close overlay, return to Library, focus the rail |
| Square | Library: pin · Queue: move earlier · Browser: fullscreen |
| Triangle | Library: queue now · Queue: pause/resume · Browser: send link to queue |
| L1 / R1 | Previous / next tab |
| L2 / R2 | Page up / page down |
| Options | Context menu for the focused item |
| Touchpad | Toggle the developer log drawer |

Text fields open an on-screen keyboard: Cross types, Square backspaces, Triangle
spaces, R1 shifts, Options accepts, Circle cancels. Password fields show bullets, so
a PIN is not displayed across the room.

Keyboard parity for desk use: arrows, Enter, Escape, PageUp/PageDown, `[` / `]` for
tabs, `/` for search, `` ` `` for the log drawer.

If your firmware's browser does not expose the Gamepad API, the hub says so once on
load and everything still works from a keyboard.

---

## The Safety tab, and what it does not claim

The panel keeps three kinds of statement strictly apart:

- **Verified here** — machine-checked: the address bound, the subnet allowlist in
  force, the air-gap audit result, the etaHEN heartbeat, and (only if you opt in)
  whether PSN hostnames answer *this PC*.
- **Not verified** — stated plainly. Above all: **whether the console itself can
  reach PSN cannot be determined from here.** The hub talks to etaHEN, not to the
  console's routing table.
- **Your actions** — a persisted checklist of the console and router settings that
  actually determine exposure.

There is no "ban-safe" badge, and the best state the panel will show is *All local
checks pass*. No local dashboard can honestly promise more than that, and a green
light that isn't earned is worse than no light at all.

**Active probing is off by default.** Resolving PSN hostnames would make this machine
emit exactly the outbound traffic the air-gap audit exists to detect. Turning it on
is a deliberate, one-click act with the trade-off stated at the point of decision.

The air-gap audit scans `static/` and `templates/` for `http(s)://` references,
protocol-relative `src`/`href`, and font imports. A remote URL on a line that looks
like a loader is **blocking**; a URL in a comment is a **note**. XML namespace
identifiers are exempt in code, because nothing ever fetches them. The one declared
exception to same-origin is `frame-src *` in the CSP, which the Browser tab needs;
it is listed in the panel rather than hidden.

---

## Emergency wipe

```
  set PIN  ──▶  PIN + typed phrase  ──▶  dry run (real file list)  ──▶  execute
```

- PBKDF2-HMAC-SHA256 with a per-install random salt, 240,000 iterations by default.
- Five wrong attempts triggers a 15-minute lockout **that survives a restart** —
  otherwise someone with physical access would just restart the hub.
- The dry run is not skippable. `execute` only accepts a token a dry run issued, and
  that token expires (120s default).
- **Allowlist only.** `wipe.allowlist` is the entire universe of what can be deleted.
  Inside it, these are refused absolutely: a filesystem root, the app folder or any
  ancestor of it, anything inside the hub's own source, your home folder, system
  directories, and symlinks or junctions.
- Directories are emptied, not removed, so logging keeps working and your allowlist
  stays valid. A file held open by the process is truncated and reported as such.
- **The hub cannot delete itself.** `server/`, `static/`, `templates/`, `run.py` and
  friends are protected and listed in the UI. After a wipe the server stops; bring it
  back with `start_hub.bat`.
- Console-side paths are supported but **ship empty**, and are only acted on if you
  configure both `wipe.console_paths` and an `etahen.endpoints.delete_path`. Without
  that endpoint the plan shows them as explicitly not-deleted, with the reason.

Beside the wipe is a Safe Mode recovery guide: how to enter it, what each of the
seven options does, and the `PS5\UPDATE\PS5UPDATE.PUP` USB reinstall procedure.

---

## Save-data backup

`backup.source_dirs` lists folders **on this PC** to bundle into
`backups/savedata-<timestamp>.zip`, keeping the newest `keep_archives`.

Being straight about the limit: pulling save data off the console needs a console-side
file API, which etaHEN does not expose by default. Point `source_dirs` at wherever you
already land saves (a USB export folder, an FTP mirror) and this packages them.

---

## Project layout

```
run.py                  entry point; threaded WSGI, --host/--port/--mock
start_hub.bat           venv bootstrap + launch
configs.example.json    documented copy of the schema
server/
  config.py             schema, {app} expansion, atomic save, runtime binding
  security.py           subnet allowlist, CSP headers, PBKDF2 PIN
  etahen.py             the daemon client + reconnect watchdog
  mock_etahen.py        offline simulator (downloads for real)
  pkg_parser.py         .pkg -> title/ID/version, layered with provenance
  library.py            scanning, metadata cache, user overrides
  transfers.py          the queue: scheduling, retries, pause, progress
  fileserve.py          Range-capable serving + bandwidth shaper
  safety.py             air-gap audit, killswitch panel, checklist
  wipe.py               PIN gate, refusal rules, dry run, execution
  telemetry.py          storage meters, hardware metrics
  logs.py               session log, ring buffer, activity events
  hub.py                service container + the 1 Hz aggregate state
  routes/               JSON blueprints, one per area
templates/index.html    the whole shell
static/css|js|img       three stylesheets, nineteen scripts, no bundler
tests/                  261 tests
pkg_wrapper/            homebrew launcher template + build notes
```

**Frontend constraints, deliberately.** Classic scripts (no modules), ES2017-level
syntax, no optional chaining or nullish coalescing, no `:has()`, no `aspect-ratio`
(the 3:4 cards use the padding-ratio trick), `clamp()` only behind an `@supports`
guard with a flat fallback. The PS5 browser is WebKit and not always a recent one,
and `tests/test_frontend_contract.py` enforces all of it.

The UI polls one endpoint — `/api/system/state` — once a second for everything the
chrome needs, rather than six endpoints, because the console's browser is happiest
with few requests.

---

## Running the tests

```bash
.venv\Scripts\python.exe -m pip install pytest
.venv\Scripts\python.exe -m pytest tests/ -q
```

They cover the SFO/JSON/content-ID parser against synthetic packages, the queue state
machine (retries, pause, reorder, restart persistence), the bandwidth shaper's actual
rate, Range handling, the subnet gate and PIN hashing, the audit catching a planted
remote URL, every wipe refusal rule, the full HTTP surface, the mock daemon over real
sockets, and the frontend contract (dead ids, missing icons, CSP violations, unsupported
syntax).

Everything writes to a temporary directory — the suite never touches your real
`configs.json`, `data/` or `logs/`.

---

## Troubleshooting

**Library is empty.** Check the banner's *Packages* line. Set
`library.pkg_directories` in Settings and press Rescan. Only files matching
`library.extensions` (default `.pkg`) are picked up.

**Console shows offline.** Normal when it is asleep — the watchdog retries quietly
with backoff. For a real console: confirm `etahen.console_ip`, that etaHEN is running,
and that `curl http://<console-ip>:9090/api/heartbeat` answers from the PC.

**Installs start then fail.** Almost always `etahen.endpoints.install` not matching
your build's expected payload. Compare with `curl` as shown above and edit the
template. Also confirm the console can actually reach the URL in the banner — if you
started the hub with `--port`, the advertised URL follows the override, but a firewall
rule may not.

**403 from the hub.** Your client is outside `server.subnet_allowlist`. Add its
subnet, or set `enforce_subnet_allowlist` to `false` if you understand the
consequence — the Safety tab will flag it as a warning while it is off.

**A site shows "refused to be embedded".** It sent `X-Frame-Options` or a restrictive
`frame-ancestors`. The hub does not proxy pages — that would make this machine a relay
for external traffic and void the air-gap audit — so there is nothing to work around.
The link can still be sent to the install queue.

**The UI feels heavy on the console.** Settings → Interface → Performance mode. It
drops every transition and the ambient wash.

---

## Scope and limits, stated once

- The hub reads package headers locally. It ships **no** package index, catalogue or
  art service, and will not fetch one. Where packages come from is your business and
  your responsibility.
- The killswitch panel **verifies and instructs**; it does not enforce. Without
  router-level control it cannot block anything, and it says so instead of implying
  otherwise.
- Hardware telemetry and console storage appear only if your etaHEN build exposes
  them. Where it does not, the panel says what is missing rather than showing a
  zeroed gauge.
- Mock-mode figures are labelled *simulated* everywhere they appear, so a demo is
  never mistaken for live data.
# ps5
