# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository automates deployment of applications on Raspberry Pi running DietPi OS. The system is designed for unattended installation and configuration of systemd services with external dependencies.

## Common Commands

### Main Deployment
```bash
# Run complete bootstrap (idempotent)
sudo ./bootstrap.sh

# Bootstrap only the named apps; selection is additive
sudo ./bootstrap.sh lexacube nfc-control
sudo ./bootstrap.sh lexacube nfc-control knockstrip
```

### Service Management
```bash
# Check service status
sudo systemctl status lexacube

# View live logs
sudo journalctl -u lexacube -f

# Restart service
sudo systemctl restart lexacube
```

### Testing Configuration Changes
```bash
# Validate YAML syntax
yq eval apps.yaml

# Check systemd service file
cat /etc/systemd/system/lexacube.service
```

## Architecture

### Configuration-Driven Deployment System

The entire deployment is driven by `apps.yaml`, which defines:
- Application repository, branch, and installation path
- Dependencies between selectable apps (`requires`)
- System package dependencies (`apt_packages`)
- External library dependencies (repos, build commands, Python bindings)
- Systemd service configuration (exec command, user)

### Bootstrap Flow

`bootstrap.sh` orchestrates the complete setup sequence:

1. **Parse Configuration**: Extract all settings from `apps.yaml` using `yq`
2. **Install System Dependencies**: APT packages required for compilation/runtime
3. **Build External Libraries**: Clone, build C/C++ dependencies (e.g., rpi-rgb-led-matrix)
4. **Deploy Application**: Clone/update app repo, checkout branch
5. **Python Environment**: Create venv, install requirements.txt
6. **Install Python Bindings**: Link Python wrappers for C libraries into venv
7. **System Configuration**: ALSA audio, CPU isolation (isolcpus=3)
8. **Service Setup**: Generate systemd service and optional service-address unit, set permissions, enable/start

Key characteristic: **Path substitution pattern** in dependency build commands uses `{path}` placeholder that gets replaced with actual installation path at runtime.

### Privilege Model

The rpi-rgb-led-matrix library requires root for GPIO but drops to `daemon` user:
- Systemd service runs as `User=root`
- Application output directory owned by `daemon:daemon`
- This dual-privilege pattern is critical for LED matrix operation

### CPU Isolation for Real-Time Performance

The system configures `isolcpus=3` in `/boot/cmdline.txt` to dedicate CPU core 3 exclusively to LED matrix rendering. This prevents scheduler jitter that causes display artifacts. The bootstrap script is idempotent on this setting.

## File Structure

- `apps.yaml` - Central configuration for all deployment aspects
- `bootstrap.sh` - Main automation script that reads apps.yaml and performs deployment
- `dietpi.template.txt` - Pre-boot DietPi configuration (copy to /boot before first boot)
- `dietpi-wifi.template.txt` - WiFi credentials template (copy to /boot before first boot)
- `knockstrip.env.template.txt` - knockstrip credentials template (fill in, copy to
  /boot as `knockstrip.env`; bootstrap installs it to /etc/knockstrip.env)

## Important Context

### Working with apps.yaml

When modifying application configuration:
- Always update `apps.yaml` first, then run `bootstrap.sh`
- The `dependencies` array supports build_cmd, install_python_cmd and
  toolchain_cmd with `{path}` substitution. They run at three different points,
  which is the whole reason they are separate fields:
  - `build_cmd` — right after the dependency is cloned, before any venv exists
  - `install_python_cmd` — inside the app's venv, after requirements are installed
  - `toolchain_cmd` — inside the venv, after `install_python_cmd`, for
    cross-compiler toolchains and embedded SDKs fetched by a tool that
    `install_python_cmd` just installed. Must be idempotent: it runs on every
    bootstrap and should be a no-op once the toolchain is cached.
- Python bindings are installed into the app's venv, not system-wide
- APT packages are system-wide, installed before dependency builds

### Apps

**lexacube** — LED matrix game
- Lives in: `/opt/lexacube`
- Runs: `/opt/lexacube/runpygame.sh`
- Claims `192.168.8.247/24` as a secondary address through
  `lexacube-address.service`; the Pi's ordinary DHCP address remains available
  for administration
- `/etc/network/if-up.d/50-lexacube-address` re-claims that address after any
  `ifup`. `ifdown` flushes every address on the interface and the oneshot unit
  never runs again, so without the hook any interface bounce silently strips
  `.247` while systemd still reports the unit active. Every cube hardcodes
  `.247`, so they all drop offline at once. The hook re-adds the address through
  `service-address` directly rather than restarting the unit, because
  `lexacube.service` has `Requires=` on it and a restart would bounce the
  running game.
  DietPi's WiFi monitor used to be the main source of those bounces —
  `scripts/reliability.sh` now masks it (see there for why) — but the hook is
  still required: any `ifup`, from a manual `ifdown`/`ifup` to a DHCP-driven
  reconfiguration, has the same effect. Note also what the hook does *not*
  cover: it re-adds `.247` only when the address is missing from every
  interface, and losing carrier is not an `ifup` event, so a dead cable leaves
  `.247` stranded on a NO-CARRIER interface with the cubes offline
- `/etc/dhcp/dhclient-exit-hooks.d/50-lexacube-address` covers the third path,
  which neither of the other two reach. `if-up.d` is run by `ifup` and **not**
  by dhclient: `/sbin/dhclient-script` handles `RENEW`/`REBIND` itself and
  calls the exit-hooks directory. On the path where a lease returns a
  *different* address it runs `ip -4 addr flush dev $interface label
  $interface`, and a secondary service address carries the interface's own
  label — so the flush takes `.247` with it. Measured on the rig: `eth0` was
  left with **no addresses at all**. The `ifup` hook does not run (no `ifup`
  happened) and the failover watcher does not fire (the carrier never
  dropped), so before this hook existed the address was simply gone while
  `systemctl` still reported `lexacube-address.service` active.
  The exit hook is *sourced* by dhclient-script, so it uses `return`, never
  `exit`; it is mode 644 with no shebang, and its name has no dot because
  run-parts skips those. All three are ways to install a hook that silently
  never runs or breaks the hooks after it.
- The shared decision — *does this event mean the address needs reclaiming?* —
  lives once in `/usr/local/sbin/lexacube-address-reclaim-if-missing`, which
  both hooks call. It checks the owning unit is still active and the address
  is genuinely absent, then detaches the reclaim, because the `arping` probe
  takes up to ~20s and neither `ifup` nor dhclient may block on it
- Depends on: rpi-rgb-led-matrix library for LED control
- Uses: Python venv at `/opt/lexacube/cube_env`
- Output: Written to `/opt/lexacube/output/` (owned by daemon user)

- Member of the `game` exclusive group, and its `default_in_group`

**nfc-control** — NFC admin action daemon
- Lives in: `/opt/nfc-control`
- Runs: `/opt/lexacube/cube_env/bin/python3 /opt/nfc-control/nfc_control_daemon.py`
- Reuses lexacube's venv (aiomqtt already installed there)
- `After=mosquitto.service` plus `Requires=mosquitto.service` and a start limit
  of 5 attempts per 300s (`requires_units` / `start_limit` in apps.yaml). The
  daemon raises `MqttError` and exits the instant the broker refuses a
  connection, so without these a stopped mosquitto meant a restart every 5s
  indefinitely
- `bound_to: lexacube`, so it starts and stops with lexacube instead of at boot

**knockstrip** — LED strip game
- Lives in: `/home/dietpi/knockstrip`
- Member of the `game` exclusive group, so it never runs alongside lexacube
- **Owns its own unit file.** `apps.yaml` points at `ops/knockstrip.service` in
  the app repo via `unit_source` rather than describing the service with
  `exec`/`environment` keys. That unit carries `ExecStartPre` steps (grant
  outbox directory, compiled song build), the pursuit `Environment` settings
  and `Restart=always`, and `game/tests/test_deployment.py` in that repo
  enforces that it stays byte-identical to `ops/knockstrip-preflight.service`
  on every execution directive. Do not reintroduce `exec:` for this app — a
  generated unit would be a lossy copy that silently drifts.
- Reads secrets from `/etc/knockstrip.env`, installed by bootstrap from
  `/boot/knockstrip.env` if present (see Per-Rig Files and Secrets)
- `station_ids.yaml` and `config.local.yaml` are provisioned from `rig_files`

### Per-Rig Files and Secrets

Some values describe *this installation* and are gitignored in the app repo, so a
reflash loses them and the app comes up wrong or refuses to start. Two mechanisms
cover them, split by whether the value is a secret.

**`rig_files`** (in apps.yaml) — non-secret per-rig values, stored inline:

```yaml
rig_files:
  - path: station_ids.yaml        # relative to the app path
    content: |
      station_ids: [3, 4, 6, 8, 12, 13, 24, 20, 22, 23]
```

Recording the rig's real values here is not the app "guessing" a default — the
knockstrip warning against a guessed `station_ids` map is about the *software*
inventing one, which would drive the wrong boxes. This is the deployment repo
remembering what is actually wired. Update it when the rig is rewired.

Policy is **write if absent, warn if different**: an existing file is never
clobbered (someone may be mid-way through tuning on the box), but a difference
is reported with a diff on every run so drift is visible rather than silent.

**`/boot/<name>.env`** — secrets, which cannot be defaulted or committed. Drop the
file on the boot partition during SD prep, the same way `dietpi.txt` and
`dietpi-wifi.txt` are placed; bootstrap installs it to `/etc/<name>.env` (mode
0600), which the generated unit already reads via `EnvironmentFile`. See
`knockstrip.env.template.txt` and `lexacube.env.template.txt`. Same
write-if-absent policy, and the diff is never printed.

### Gameplay Analytics (lexacube)

The Pi records gameplay to an on-disk outbox during an event, when it has no
route out, and uploads afterwards. Three pieces have to agree:

- **`LEXACUBE_ANALYTICS_DIR=/var/lib/lexacube/analytics`** in lexacube's
  `environment`. Recording is opt-in by this variable's *presence*, so that a
  developer run and the test suite cannot litter an outbox. Remove it and the
  Pi plays normally and records nothing — no error, no data.
- **`deploy/lexacube-analytics-upload.{service,timer}`** (cubes repo), declared
  as `extra_units`. The service is a `Type=oneshot` with no `[Install]`
  section; only the **timer** is enabled. It polls `OnCalendar=hourly` with
  `Persistent=true` rather than hooking network-up, because the uploader
  already treats an unreachable vendor as *deferred* — it keeps the files and
  exits 0 — so a run with no route is a no-op and the first run after the Pi is
  home drains everything.
- **`POSTHOG_API_KEY` and `ANALYTICS_PRODUCT`** in `/boot/lexacube.env`.

`ANALYTICS_PRODUCT` is the one that must not be got wrong, and the uploader
**refuses to send** without it. PostHog's free tier allows a single project, so
several products share one event namespace and event names are not disjoint
between them (`session_started` means something different per product). There
is no error for a wrong value, only numbers that are wrong and are found much
later. `knockstrip.env.template.txt` carries the same `POSTHOG_API_KEY`; if
both games ever report to one project, both need a distinct product.

The outbox path lives in two repositories and agrees only by literal, so
`tests/test_analytics_upload_wiring.py` pins it on this side.

### Systemd Service Pattern

Services created by bootstrap.sh have:
- Auto-restart on failure (5 sec delay)
- `After` dependencies from the `after` field in apps.yaml (defaults to `network.target`)
- WorkingDirectory set to app path
- ExecStart pointing to configured exec script

Apps may declare `requires_units` (a list of systemd units) and `start_limit`
(`interval_sec`, `burst`). These exist because `after:` alone is not a
dependency: with `Restart=on-failure` and `RestartSec=5`, an app whose
dependency is missing retries forever, and five retries take ~25s — which never
fits systemd's default 10s `StartLimitIntervalSec`, so the burst counter never
trips. The loop then floods the journal and, on a Pi with volatile logs, rotates
away the entry explaining what actually broke. `requires_units` emits
`Requires=`, which covers the whole lifecycle: it pulls the dependency in on
start, and per systemd.unit(5) the dependent "will be stopped (or restarted) if
one of the other units is explicitly stopped (or restarted)" — so a routine
`systemctl restart mosquitto` restarts the app rather than leaving it dead.
Do not add `PartOf=` alongside it: `PartOf=` is documented as "similar to
`Requires=`, but limited to stopping and restarting", a strict subset of what
`Requires=` already does. `start_limit` widens the window enough that the burst
is reachable, turning an endless retry into a `failed` unit. nfc-control uses
both against `mosquitto.service`. Note `requires_units` is unrelated to the
top-level `requires`, which selects which *apps* bootstrap installs.

Apps may define `service_address.address` and an optional
`service_address.interface` (`auto` by default). Bootstrap installs a separate
oneshot unit that claims the address before the app starts. The address unit is
part of the application lifecycle: stopping the app releases the address, and
starting it claims the address again. The helper refuses to claim an address
already in use by another host. Legacy hosts where the service address is still
the primary static address must be migrated to DHCP administratively before
deployment.

`service_address.failover: true` additionally installs
`<app>-address-failover.service`, which watches the interface holding the
address and re-homes it when that interface loses carrier. This is not covered
by the `if-up.d` hook: that hook fires on `ifup` and only when the address is
missing from *every* interface, whereas losing carrier is neither. Without it a
pulled cable leaves the address advertised on a `NO-CARRIER` interface while the
box is still perfectly reachable elsewhere — for lexacube that means all six
cubes offline, because they hardcode `.247`. The watcher does not pick an
interface itself; it hands the address back to `service-address ... auto`, which
resolves one with `ip route get`. If that re-claim fails — no alternate path is
up yet, or `arping` trips transiently — the address is left configured nowhere,
so the watcher remembers it owes the address a home and keeps retrying. Nothing
else would: losing carrier fires no `ifup`, so the reclaim hook never runs. It
retries only an address it released itself; one that was simply never claimed
belongs to the hook, and racing it would be worse. That works because `scripts/reliability.sh`
sets `ignore_routes_with_linkdown`, so routing already skips link-down
interfaces — the two are a pair, and the watcher is a no-op without it.

It fails over but deliberately does **not** fail back: when the cable returns
the address stays put until the app restarts. Chasing the "best" interface would
flap the address, and every move drops all MQTT sessions.

Apps may declare `extra_units` — sibling unit files shipped in the app repo,
installed to `/etc/systemd/system` regardless of whether the app's main unit
is generated or repo-owned (`unit_source`). A plain string entry is installed
only; the main unit is what wires it in (knockstrip's preflight). A map entry
`{source: ..., enable: true}` is also enabled and restarted on every
bootstrap, for units that stand on their own — lexacube's admin page uses
this so the page outlives the game restarts it triggers. Missing declared
units fail the bootstrap fast; for lexacube that means cubes master must
contain `deploy/lexacube-admin.service` before bootstrapping.

### Exclusive App Groups

Every app is always *installed*; whether it *runs* is separate. Apps sharing an
`exclusive_group` in apps.yaml are mutually exclusive — exactly one member runs
at a time. This exists because the Pi has one audio output and 4GB of RAM, so
lexacube and knockstrip must never run together. (They drive different LED
hardware — matrix and strip — so that is not the conflict.)

Three mechanisms, deliberately layered:

1. **`Conflicts=` drop-in** at `/etc/systemd/system/<name>.service.d/10-exclusive.conf`.
   Starting one member makes systemd stop the others, so even a manual
   `systemctl start knockstrip` cannot leave two games running. It is a drop-in
   rather than a generated directive so it composes with repo-owned units
   (see knockstrip's `unit_source`).
2. **Enable-state is the source of truth.** There is no state file to drift.
   Bootstrap reads `systemctl is-enabled` for each member and *preserves*
   whichever is already active, so re-running bootstrap never changes which
   game is live. `default_in_group: true` breaks the tie only on a fresh flash
   where no member has been enabled yet.
3. **`pi-game`** (`scripts/select-app.sh`, installed to `/usr/local/bin`)
   switches members: `sudo pi-game knockstrip`. With no argument it prints the
   current member and the alternatives.

The inactive member is stopped *and* disabled, so it consumes no memory and no
cycles. Note that `isolcpus=3` still reserves a core for the LED matrix
regardless of which game is active; changing that requires a reboot.

Apps may also declare `bound_to: <app>`, which emits `PartOf=` and
`WantedBy=<app>.service` so the app starts and stops with its parent rather
than at boot. nfc-control uses this to follow lexacube.

Pass one or more app names to bootstrap only those apps during initial
provisioning, for example `sudo ./bootstrap.sh lexacube nfc-control knockstrip`.
Group activation is skipped unless every member of that group was selected, so
a partial run cannot silently switch games.
With no app names, bootstrap installs every configured app. Selection is
additive: bootstrap never disables services installed by an earlier run, and
an unknown app name or incomplete `requires` selection fails before system
configuration begins.

### Wired-Preferred Networking

`scripts/network-interfaces.sh` makes the wire the primary path and leaves WiFi
as an automatic fallback. Three parts, all idempotent:

- **`eth0` is brought up at boot.** DietPi ships `#allow-hotplug eth0`
  commented out, so ifupdown never touched it: a plugged cable did nothing and
  the interface sat `DOWN` while the Pi talked over WiFi.
- **Both route metrics, not one.** `dhclient` honours `metric` for the *default*
  route (`IF_METRIC`, see `/sbin/dhclient-script`), but the cubes are *on-link*
  in the same `/24` and that is decided by the **subnet** route, which dhclient
  does not set. Preferring only the default route leaves cube traffic on WiFi
  while the wire idles — measured: `ip route get <cube>` chose `wlan0` until the
  subnet metric was fixed. `/usr/local/sbin/pi-deploy-route-metrics` applies
  that half, installed in **two** places: an `if-up.d` hook for every `ifup`,
  and an `/etc/dhcp/dhclient-exit-hooks.d` hook for lease changes. `if-up.d` is
  run by `ifup` and **not** by dhclient — `/sbin/dhclient-script` handles
  `RENEW`/`REBIND` itself and calls the exit-hooks directory instead. Without
  the second hook, a lease that returns a *different* address makes
  dhclient-script `ip -4 addr flush` and re-add, destroying the metric route
  and leaving the kernel's fresh metric-0 one, so the wired preference
  silently reverts until the next `ifup`. The ordinary renewal, where the
  address is unchanged, takes an `ip addr change` path that leaves routes
  alone — which is why this is easy to miss. Note the exit hook is *sourced*
  by dhclient-script, so it must use `return`, never `exit`.

  That hook **deletes before it adds**, and must. The kernel creates a metric-0
  route for the prefix automatically whenever an address is assigned, on both
  interfaces, and `ip route replace` cannot overwrite it with a different
  metric — metric is part of the route key, so a replace *adds* a second route.
  Measured after a reboot with the replace-based version: four routes for the
  one prefix (kernel metric-0 on `eth0` **and** `wlan0`, plus 100 and 600), the
  metric-0 pair outranking both of ours and tying with each other, so the
  winner was boot insertion order rather than anything configured. It happened
  to pick the wire; nothing made it. Nor can that route be removed on its own:
  iproute2 treats an unspecified metric and an explicit `metric 0` alike as
  "match any", so either form deletes whichever route is found first. Deleting
  until none remain, then adding one at the intended metric, is the only
  selective-enough operation available.
- **ARP scoped to the owning interface.** Both interfaces hold an address in one
  subnet, and Linux answers ARP for any local address on any interface, so
  `wlan0` would answer for a service address living on `eth0`.

The stanzas are rewritten in the main `interfaces` file rather than dropped into
`interfaces.d/`: the stanzas already exist there and ifupdown rejects a
duplicate `iface`, so a drop-in would break networking rather than override it.
The original is backed up to `interfaces.before-pi-deploy.<timestamp>`.

This pairs with `ignore_routes_with_linkdown` from `reliability.sh` — metrics
decide preference, that sysctl is what makes the switch happen when a cable
dies — so bootstrap runs this script first.

Why it is worth doing at all: every cube message crosses the air twice
(`cube -> AP -> Pi`). Wiring the Pi removes one of the two wireless hops for
every message in both directions, and takes the Pi's own traffic off the 2.4GHz
band the single-band ESP32 cubes cannot leave.

### WiFi: two bands, two consumers

The Pi is dual-band; the ESP32 cubes are **2.4GHz only**. One `dietpi-wifi.db`
serves both, and each needs a different network out of it. Both are named
explicitly in `apps.yaml` rather than inferred:

- **`wifi.preferred_ssid`** — the network the Pi should use (5GHz here).
  `scripts/wifi-preference.sh` writes `priority=` into each
  `wpa_supplicant.conf` network block, because DietPi's own generator
  (`/boot/dietpi/func/dietpi-wifidb`) emits `ssid`, `scan_ssid`, `key_mgmt` and
  `psk` and **no priority at all**. Without it wpa_supplicant chooses on signal
  strength, which at range means 2.4GHz — the band the cubes cannot leave and
  every watt of Pi traffic on it is airtime taken from them.
  The script then **reloads the running daemon** (`wpa_cli reconfigure`) and
  verifies the association, because wpa_supplicant keeps its network
  configuration in memory and never re-reads the file on its own — without
  that it reports success while the Pi stays on the other band until it
  happens to reboot. It reloads only when the file actually changed: doing it
  unconditionally would drop the association on every bootstrap, and on a
  WiFi-only rig that is the path bootstrap is running over.
- **`dependencies[].secret_file.ssid`** — the network compiled into cube
  firmware. The generator otherwise takes the **lowest-numbered configured
  entry**, which is a guess: a 5GHz SSID at entry 0 produces cubes that flash
  fine and can never associate, with nothing on the Pi to say why. A named SSID
  that is not configured fails bootstrap loudly rather than silently falling
  back to some other network.

`psk` is **PBKDF2(passphrase, SSID)**, so the same password hashes differently
per network — a psk copied from one SSID to another will not authenticate.
Put the plaintext passphrase in `aWIFI_KEY` and let `dietpi-wifidb` derive each
one (it runs `wpa_passphrase "$ssid" "$key"` per entry); a 64-hex value is
passed through unchanged, so it must be the hash for *that* SSID.

What this does **not** cover: DietPi rewrites `wpa_supplicant.conf` wholesale
when its WiFi settings change (`dietpi-config`), dropping the priorities until
the next bootstrap. The credentials survive, because they live in
`dietpi-wifi.db` — which is the point. Previously the 5GHz network was
hand-written into `wpa_supplicant.conf` only, so a regenerate removed it
outright and a reflash never had it.

### Idempotency

The bootstrap script can be run multiple times safely:
- Git repos are updated (pull), not re-cloned
- Venv creation skipped if exists
- CPU isolation config only added if not present
- Systemd service overwritten and restarted
- The active member of an exclusive group is preserved, never reset to the default
- PlatformIO toolchains are cached in `/root/.platformio`; re-runs re-resolve but
  re-download nothing

### Building ESP32 Firmware

Bootstrap pre-fetches the ESP32 toolchain (`espressif32`, Xtensa GCC, the Arduino
framework and every `lib_deps` entry) via the cube-pn5180 dependency's
`toolchain_cmd`, so a freshly bootstrapped Pi can build firmware without a ~1GB
first-build download. Build with the venv's PlatformIO, as root — `/opt/cube-pn5180`
is root-owned:

```bash
sudo /opt/lexacube/cube_env/bin/pio run -d /opt/cube-pn5180 -e v6
```

Environments: `v1`, `v6`, `v6_with_hall`, `v6_with_hall_analog` (firmware) and
`native` (Unity tests). `src/secrets.h` is generated by bootstrap from the DietPi
WiFi profile; it is 0600 and must never be committed.

### Claude CLI Backends

`bootstrap.sh` installs the `claude-ant` / `claude-zai` aliases into `.bashrc`
for **both** `root` and `dietpi`, alongside `~/.claude-switch/use-anthropic.sh`
and `use-zai.sh`. The aliases source a switch script and then exec `claude`, so
each alias picks a backend for that one session. This block lives in the
unconditional system-configuration section, so it runs on every bootstrap
regardless of which apps were selected.

Because the aliases are useless without the CLI, bootstrap also:
- installs Claude Code per user via the native installer into
  `~/.local/bin/claude` (skipped when that binary already exists), and appends
  `~/.local/bin` to PATH in `.bashrc`. Each home gets its own copy rather than
  sharing one across the root/dietpi privilege boundary. An install failure
  warns rather than aborting — a first-boot game deployment must not hinge on
  reaching the installer.
- consumes the Z.ai key from the boot partition. `provisioning.env` may set
  `ZAI_API_KEY`; `render_dietpi_provisioning.py` renders it to
  `rendered/lexacube-zai-key` and `prepare_dietpi_sd.sh` stages it on the boot
  partition. Bootstrap installs it as `~/.claude-switch/zai-key` at 0600 and
  **deletes the boot copy**, because FAT32 cannot hold permissions. Later runs
  find no boot copy and leave the installed key alone, so re-bootstrapping never
  clobbers a hand-edited key. Without a key, `claude-zai` warns and returns.

Anthropic needs no key — `use-anthropic.sh` just unsets the Z.ai overrides.

## DietPi Templates

These files are used during SD card preparation (before first boot):
- Customize `dietpi.template.txt` and copy to `/boot/dietpi.txt` for unattended setup
- Customize `dietpi-wifi.template.txt` and copy to `/boot/dietpi-wifi.txt` for WiFi.
  Configure **both** bands when the rig has them — see the notes at the top of
  that file and "WiFi: two bands, two consumers" above
- Customize `knockstrip.env.template.txt` and copy to `/boot/knockstrip.env` so the
  game's credentials survive a reflash
- Must be placed on boot partition before powering on the Pi

## Reliability & Observability

`scripts/reliability.sh` (run at the end of `bootstrap.sh`, idempotent) hardens
the Pi against the live-event failure mode where it hung and needed a manual
power-cycle. Four independent measures:

1. **Hardware watchdog** — `/etc/systemd/system.conf.d/10-watchdog.conf` sets
   `RuntimeWatchdogSec=15`. If pid1 stops petting `/dev/watchdog0` (a true
   hang), the BCM2835 hardware resets the Pi in 15s. No more running to the box.
2. **Persistent journald** — `/var/log` is a DietPi RAMlog tmpfs, so the
   journal is volatile and a reboot erases the logs that would explain a hang.
   A `nofail` bind mount ties `/var/log/journal` to SSD-backed
   `/var/lib/journal-persist` (with `x-systemd.before=dietpi-ramlog.service`
   ordering), and a `journald.conf.d` drop-in sets `Storage=persistent`. RAMlog
   still handles the rest of `/var/log`.
3. **zram swap** — no swap + `cgroup_disable=memory` means a RAM spike hangs the
   whole box. `dietpi-set_swapfile 1 zram` adds ~50%-of-RAM compressed swap
   (zero SSD wear). Fresh flashes get this from `dietpi.template.txt`
   (`AUTO_SETUP_SWAPFILE_LOCATION=zram`).
4. **Health logger** — `pi-health-watch.service` runs `pi-health-watch.sh`,
   polling the firmware throttle/undervoltage bitmask + temp into the (now
   persistent) journal every 5s. The "since boot" bits latch, so even a
   sub-second brownout transient is caught. Inspect with `journalctl -t pi-health`.

Background: a bench load test (all cores + SSD write bursts) could **not**
reproduce a 5V-rail brownout, so rather than chase an unproven power fault
these measures make the Pi self-heal (1, 3) and self-diagnose (2, 4). The
definitive power test is running the real game engine at max stations with
audio while watching `vcgencmd get_throttled`.
