# Homebrew launcher package

This folder turns the hub into a tile on the PS5 home screen: press it and the console
opens straight into the dashboard on your TV, with no manual browser navigation.

**What is here and what is not.** Everything the wrapper needs is here — the boot page,
the manifest template, the asset specification and the build steps. Producing the
actual `.pkg` requires the PS5 SDK / payload toolchain on your machine, which is not
bundled and cannot be: it is large, licence-encumbered, and version-specific to your
firmware and exploit chain. The build is one command once the toolchain is installed;
step 4 below is where you run it.

---

## The three ways to launch, in increasing effort

### 1. Browser bookmark — works right now, no build

In the PS5 browser, open `http://<pc-ip>:8080` and bookmark it. Zero setup. The
downside is that reaching the browser at all takes several menu steps on a stock
console, which is the whole reason the other two options exist.

### 2. A loader shortcut — no .pkg, no SDK

Most exploit host pages let you register a custom URL as a payload entry. Point it at
`http://<pc-ip>:8080` and it becomes one selection from your loader. This is the
cheapest real improvement and needs nothing from this folder.

### 3. A real homebrew tile — what the rest of this document is for

A signed FSELF application package whose only job is to open a WebView at your hub's
URL. This is the version that looks and behaves like an installed app.

---

## How the wrapper works

```
  home screen tile
        │
        ▼
  eboot.bin  ──▶  WebView  ──▶  file:///app0/boot.html  ──▶  http://<pc-ip>:8080
                                      │
                                      └─ retries, shows a diagnostic if the PC is off
```

`boot.html` is a local page shipped inside the package. It does not contain your
dashboard — it is a launcher that redirects to the hub and, crucially, handles the
case where the PC is asleep or on a different address. A bare redirect would leave you
staring at a WebKit error with no way to tell what went wrong from the sofa.

Keeping the real UI on the PC rather than inside the package matters: you update the
hub by editing files on the PC, not by rebuilding and reinstalling a package.

---

## Files

| File | Purpose |
|---|---|
| `boot.html` | The launcher page. Edit `HUB_URL` (or pass it via `param.json`). |
| `param.json.template` | Application metadata: title ID, name, version, category. |
| `assets.md` | Exact icon and background image sizes the console expects. |

---

## Build steps

### 1. Choose a title ID

Homebrew convention is a non-retail ID so it can never collide with a real title.
Use something in the `XXXX00000`-style homebrew range your toolchain documents —
commonly `PPSA00000`-prefixed custom IDs or the `HOMEBREW`/`BREW` ranges used by the
scene tooling. Put the same value in `param.json` and in the eventual package name.

### 2. Fill in `param.json`

Copy the template, then replace every `<...>` placeholder:

```bash
cp param.json.template param.json
```

- `titleId` — the ID from step 1
- `titleName` — what appears under the tile ("PS5 Command Hub")
- `contentVersion` / `masterVersion` — your own versioning
- `hubUrl` — `http://<your-pc-ip>:8080`, the address `start_hub.bat` prints

Hard-coding the IP here is the simplest approach, and it is why a DHCP reservation for
the PC matters: if the PC's address changes, the tile stops working until you rebuild.
If your toolchain lets you read a value from storage at launch, prefer that.

### 3. Produce the icon and background

See `assets.md`. The console will not display a tile with missing or wrongly sized
art, and it fails quietly, which is a frustrating way to lose an hour.

### 4. Build and sign

This is the toolchain-dependent part. With a PS5 payload SDK (for example
`ps5-payload-dev` / `ps5-payload-sdk` and the community packaging tools) the shape is:

```bash
# 1. compile the stub that opens a WebView at file:///app0/boot.html
make                      # produces eboot.bin / the FSELF

# 2. stage the package contents
mkdir -p pkg/app0/sce_sys
cp boot.html         pkg/app0/
cp param.json        pkg/app0/sce_sys/
cp icon0.png         pkg/app0/sce_sys/
cp pic1.png          pkg/app0/sce_sys/
cp eboot.bin         pkg/app0/

# 3. build the .pkg with your packaging tool
<pkg-tool> --create pkg/ --out PS5CommandHub.pkg
```

Consult your toolchain's own documentation for the exact invocation — the flags differ
between tools and firmware targets, and a wrong one produces a package that installs
and then refuses to launch.

### 5. Install it

Put `PS5CommandHub.pkg` in your packages folder and install it **through this hub**.
The dashboard installing its own launcher is a fitting first job for it.

---

## Troubleshooting the tile

**Tile launches to a blank or error page.** The PC is off, asleep, or on a different
IP. `boot.html` shows a diagnostic with the URL it tried; check that address from the
console browser. Give the PC a static DHCP reservation.

**Tile does not appear after installing.** Rebuild the database: Safe Mode option 5
(*Clear Cache and Rebuild Database*). It is non-destructive. The Recovery tab in the
hub has the full Safe Mode guide.

**It launches but the controller does nothing.** The WebView may not expose the
Gamepad API even where the browser does. The hub detects this and says so on load;
everything remains reachable with a USB keyboard. Check the hub's log drawer.

**Changing the hub's port.** The URL is baked into `boot.html` / `param.json`. Either
keep the hub on the port you built against, or rebuild the package.
