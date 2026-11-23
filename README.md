# CivSwitchPi0W-AIO

An all-in-one Raspberry Pi Zero W companion for [CivSwitch](https://github.com/Gezine/CivSwitch):
the Pi is a Wi-Fi access point for the console and a USB-serial bridge for your
PC, so you can send JS payloads and run console tools **without putting the
console on a network that can reach Nintendo**, and without needing Wi-Fi on
the PC.

```text
PC  --USB serial-->  Pi Zero W  --Wi-Fi AP-->  Switch 2
                     192.168.4.1              192.168.4.2
                     Switch2Bridge            CivSwitch loader :9026
```

This is a port of [CivSwitch-Pico2W-SerialBridge](https://github.com/favas0/CivSwitch-Pico2W-SerialBridge)
by favas0 (itself a port of [CivSwitch-SerialBridge](https://github.com/jackspence7/CivSwitch-SerialBridge)
by Jack Spence) from the Pico 2 W to a Raspberry Pi Zero W running Raspberry Pi
OS, plus a GUI, a TCP proxy and the operational glue around them.

It does **not** contain the exploit. It carries no payloads, no keys and no
kaiHEN build. It is transport and tooling.

---

## Table of contents

- [What this gives you](#what-this-gives-you)
- [What you need](#what-you-need)
- [The repos involved](#the-repos-involved)
- [Read this before you start](#read-this-before-you-start)
- [Step 1 - First injection](#step-1---first-injection)
- [Step 2 - Build the bridge Pi](#step-2---build-the-bridge-pi)
- [Step 3 - Daily use](#step-3---daily-use)
- [Step 4 - Running console tools](#step-4---running-console-tools)
- [Browsing the console filesystem](#browsing-the-console-filesystem)
- [Switch network settings](#switch-network-settings)
- [Turning autostart off](#turning-autostart-off)
- [Serial protocol](#serial-protocol)
- [Project structure](#project-structure)
- [Verified on hardware](#verified-on-hardware)
- [Network isolation, measured](#network-isolation-measured)
- [Troubleshooting](#troubleshooting)
- [Gotchas found while porting](#gotchas-found-while-porting)
- [Credits and licenses](#credits-and-licenses)

---

## What this gives you

- An open AP named **Switch2Bridge** at `192.168.4.1`, with DHCP and a
  wildcard DNS that answers every name with the Pi itself.
- Nintendo connectivity checks answered locally, so the console believes it is
  online and skips the captive-portal nag. It is **not** online: there is no
  default route, no forwarding and no NAT. See
  [Network isolation, measured](#network-isolation-measured).
- The bridge as the **resting state**: plug the Pi into any power source and
  the AP comes up on its own. No PC needed.
- A minimal PC GUI to send payloads, type protocol commands, and read status.
- A **TCP proxy** so tools that speak TCP to the loader (such as
  CivSwitchFS-kaiHEN) run from a PC with no Wi-Fi card at all.

What it does not do: it does not start CivSwitch's Remote JS Loader, and it
cannot perform the LDN injection. See
[Step 1](#step-1---first-injection) for why.

---

## What you need

### Console side

Taken from the upstream projects; check theirs for the current truth.

- A **Nintendo Switch 2**. For the kaiHEN chain, firmware **23.0.1** is the
  tested one -- do not use lower firmware.
- **Civilization VII 1.0.0**, physical cartridge.
- A microSD card for the console, FAT32 or exFAT, if you intend to run kaiHEN.
- CivSwitch's **Remote JS Loader** running on the console whenever you send a
  payload. The bridge does not start it.

### Pi side

- **Raspberry Pi Zero W** (the original, ARMv6, BCM43430 Wi-Fi).
- A micro-USB **data** cable in the port marked **USB**, not **PWR**.
- A microSD card, 4 GB is plenty.
- **Raspberry Pi OS Lite (32-bit)**. Do not use 64-bit: the Zero W is ARMv6
  and cannot run it. This was built and verified on Trixie, kernel
  `6.18.50+rpt-rpi-v6`, Python 3.13.

### PC side

- **Python 3.8+** with `pyserial` for the GUI and proxy.
- **Node.js 20+** and npm, only if you want to run CivSwitchFS-kaiHEN.
- Windows, macOS or Linux. No Wi-Fi card required.

---

## The repos involved

| Repo | What it is | When you need it |
| --- | --- | --- |
| [Gezine/CivSwitch](https://github.com/Gezine/CivSwitch) | The exploit, the Remote JS Loader and the persistent installer | Always, upstream of everything |
| [itsPLK/CivSwitchPiZero](https://github.com/itsPLK/CivSwitchPiZero) | Standalone bare-metal Pi image that performs the **LDN or USB injection** | First injection, or if persistence is lost |
| **this repo** | Pi OS bridge: AP, DHCP, connectivity portal, serial relay, GUI, TCP proxy | Sending payloads and running tools afterwards |
| [s8n/CivSwitchFS-kaiHEN](https://github.com/s8n/CivSwitchFS-kaiHEN) | FS access chained into a kaiHEN boot | When you want homebrew |
| [ItsNerisma/CivSwitchLDN](https://github.com/ItsNerisma/CivSwitchLDN) | LDN injection from a Linux PC | On a PC with a suitable Wi-Fi adapter, not on this Pi |
| [favas0/CivSwitch-Pico2W-SerialBridge](https://github.com/favas0/CivSwitch-Pico2W-SerialBridge) | The Pico 2 W bridge this is a port of | Reference, or if you use a Pico instead |

---

## Read this before you start

**Do not let the console reach Nintendo.** This is the one step where a
mistake is expensive: an online console running this can collect a blacklist
update that locks you out of Civ VII. The bridge is built to prevent that, and
the isolation is measured in
[Network isolation, measured](#network-isolation-measured) -- but read
[90DNS](https://a.gitlab.io/90dns/) and the warnings in the kaiHEN README
anyway, and follow them precisely.

**The FS vulnerability carries real risk.** kaiHEN's own README says it may
corrupt your save file or more. If you want minimal risk, wait.

**This project is transport, not the exploit.** Nothing here is a substitute
for reading the upstream instructions.

---

## Step 1 - First injection

The bridge cannot do this part, and the reason is hardware.

CivSwitchLDN needs to receive and transmit action frames in **monitor mode**.
On a Pi Zero W, `iw list` reports only:

```text
IBSS, managed, AP, P2P-client, P2P-GO, P2P-device
```

No monitor mode. The chip can do it, but the stock `brcmfmac` driver and
firmware do not expose it -- which is exactly why
[CivSwitchPiZero](https://github.com/itsPLK/CivSwitchPiZero) ships
Nexmon-patched firmware and driver instead of using the stock ones. Its
prebuilt modules target kernel `6.12.13`, while current Pi OS runs `6.18.x`,
so you cannot simply drop them onto this card.

So use **a second SD card** with itsPLK's image for the injection:

1. Flash `build/civ7-pizero.img` from
   [CivSwitchPiZero](https://github.com/itsPLK/CivSwitchPiZero) with Raspberry
   Pi Imager or balenaEtcher. It is about 40 MiB and boots entirely in RAM.
2. Copy your own `prod.keys`, dumped from your own Switch 1, to the root of
   the `CIVSWITCH` partition. It must contain `master_key_00`,
   `aes_kek_generation_source` and `aes_key_generation_source`.
3. Open a Civ VII local wireless host lobby on the console.
4. Power the Pi and leave the lobby open. Allow up to four minutes.

With persistence installed you do this **once**: afterwards you restart the
game and use **Load Game** to bring the loader back. Then swap to the bridge
card below.

If you have a Linux PC with a MediaTek or Realtek adapter,
[CivSwitchLDN](https://github.com/ItsNerisma/CivSwitchLDN) does the same job
from the PC.

---

## Step 2 - Build the bridge Pi

### 2.1 Flash Raspberry Pi OS

In **Raspberry Pi Imager**, choose Raspberry Pi OS (other) ->
**Raspberry Pi OS Lite (32-bit)**. Before writing, open the settings
(gear icon, or answer yes to the customisation prompt) and set:

- **Hostname**, for example `pi`
- **Username and password** -- these are the SSH credentials
- **Wi-Fi SSID and password**: it must be a **2.4 GHz** network, since the
  Zero W has no 5 GHz radio
- **Wireless LAN country**
- **Enable SSH** on the Services tab

Save, then write. Recent images configure this through cloud-init
(`network-config`, `user-data`, `meta-data` on the boot partition), not the
old `firstrun.sh` -- so do not go looking for that file.

### 2.2 First boot and find the Pi

Insert the card, power the Pi, and give it **5 to 10 minutes**: cloud-init
expands the filesystem and installs packages on a single ARMv6 core.

`.local` names are unreliable from Windows, so find it by IP instead. Look in
your router's DHCP client list for the hostname you set, or scan for the
Raspberry Pi Foundation MAC prefix `b8:27:eb`.

```sh
ssh pi@<pi-ip>
```

Once the bridge is set up you will not need to hunt again: the GUI shows
`ssh pi@<addr>`, because the Pi reports its own address.

### 2.3 Install dependencies

```sh
sudo apt update && sudo apt install -y hostapd dnsmasq iw python3-serial
```

Node.js is **not** needed on the Pi. Neither is CivSwitchLDN.

### 2.4 Copy this repo and run the installer

From your PC:

```sh
git clone https://github.com/Denkishi/CivSwitchPi0W-AIO
```

```sh
scp -r CivSwitchPi0W-AIO/pi pi@<pi-ip>:~/civswitch
```

Then on the Pi:

```sh
cd ~/civswitch && chmod +x install.sh && sudo ./install.sh
```

The installer:

1. Installs any missing system packages.
2. Adds `dtoverlay=dwc2,dr_mode=peripheral` under `[all]` in `config.txt` and
   `dwc2`, `g_serial` to `/etc/modules`, so the Pi presents a USB serial
   gadget to the PC.
3. Masks `serial-getty@ttyGS0`, which would otherwise fight the supervisor for
   the port.
4. Disables the `hostapd` and `dnsmasq` services -- the bridge starts them
   itself, so they must not come up on their own.
5. Installs `civswitch.service` and enables a **persistent journal**, so a
   failure that takes the Pi off the network still leaves a log you can read
   after rebooting.

Reboot when it finishes:

```sh
sudo reboot
```

### 2.5 Connect the PC

Plug the cable into the port marked **USB**. The Pi appears as a serial
device with USB VID/PID `0525:A4A7` -- a COM port on Windows,
`/dev/ttyACM0` on Linux, `/dev/cu.usbmodem*` on macOS. Windows needs no
driver.

```sh
cd CivSwitchPi0W-AIO/pc
```

```sh
pip install -r requirements.txt
```

```sh
python civswitch_gui.py
```

The port marked **(Pi)** is selected automatically.

---

## Step 3 - Daily use

The bridge is the resting state. Plug the Pi into power and the AP comes up by
itself; the GUI already shows `AP 192.168.4.1` in green when you connect.

1. Start CivSwitch's Remote JS Loader on the console (Civ VII -> Load Game
   with persistence installed).
2. Connect the console to **Switch2Bridge**. No password. See
   [Switch network settings](#switch-network-settings).
3. In the GUI, set the console IP and port, pick a `.js` file, click **Send**.
   The loader's output comes back in the log.

For payloads you use often, the **Payload** tab keeps a small library: write
or import snippets, edit them in place, and **Send** one -- or pick several
with Ctrl-click and **Send selected in order** to fire a chain. The console's
reply streams into the tab. The library is saved to `civswitch_payloads.json`.

**Stop bridge** hands the radio back to NetworkManager, so the Pi rejoins your
Wi-Fi and SSH works again, about 15 seconds later. **Start bridge** puts it
back. **Send** stays disabled until the bridge is actually up, and the bridge
button is locked while the Pi is busy starting or stopping it.

With **Stop bridge on exit** ticked (the default), closing the GUI also stops
the bridge and shuts down any tunnel it started, so nothing is left holding
the COM port. The Pi still autostarts the bridge on its next boot.

The GUI reconnects on its own when the Pi is unplugged and plugged back in,
and the 🌙 / ☀ button in the top bar switches between light and dark.

The command box at the bottom sends raw protocol commands (`INFO`, `STATUS`,
`SCAN`, `BRIDGE`, `STOP`) and prints the reply, which is useful when you want
to ask the Pi directly what it is doing.

---

## Step 4 - Running console tools

Tools like [CivSwitchFS-kaiHEN](https://github.com/s8n/CivSwitchFS-kaiHEN)
open a plain TCP connection to the loader
(`net.createConnection({host, port: 9026})`). **They have no COM port option
and cannot have one**: they speak TCP, not serial. There are two ways to give
them a path to the console.

### 4.1 Through the Pi (works with no Wi-Fi card)

With the bridge running, pick a mode in **Tunnel** and click **Start**. The
GUI hands the COM port over -- a serial port takes a single reader -- and
streams the tunnel output into its log. **Stop** takes the port back.

| Mode | Listens on | Use it for |
| --- | --- | --- |
| **TCP relay** | `127.0.0.1:9026` | npm tools that connect to the loader |
| **SOCKS5** | `127.0.0.1:1080` | FTP, browsers, anything SOCKS-aware |

For the npm tools, pick the TCP relay:

```text
tool -> 127.0.0.1:9026 -> USB serial -> Pi -> 192.168.4.2:9026
```

Then set up and run the tool, pointing it at localhost:

```sh
git clone https://github.com/s8n/CivSwitchFS-kaiHEN
```

```sh
cd CivSwitchFS-kaiHEN && npm install
```

```sh
npm run persist:chain -- 127.0.0.1
```

Download a kaiHEN build for SD card use from that repo's releases, extract it
onto the console's microSD, and insert the card **before** you start. Keep the
game open until the console reports `INSTALL DONE`.

The proxy also runs standalone, if you would rather not keep the GUI open:

```sh
python pc/tcp_proxy.py --console 192.168.4.2
```

This works because those tools use one connection per request -- send
everything, half-close, read until the console closes -- which maps exactly
onto the supervisor's `RELAY` command. The serial link is not the bottleneck
it looks like: see the measured rate under Browsing the console filesystem.

### 4.2 By moving the PC onto the AP

If the PC has Wi-Fi, this is simpler and faster, since nothing crosses the
serial link. Install dependencies first, because the PC has no internet while
it is on the AP:

```sh
npm install
```

Join `Switch2Bridge`, then:

```sh
npm run persist:chain -- 192.168.4.2
```

---

## Browsing the console filesystem

With a homebrew FTP server running on the console (kaiHEN ships one), the GUI
can browse its SD card directly -- no FileZilla, no proxy settings.

1. Start the bridge.
2. Open the **Files** tab and click **Start tunnel and connect** (the FTP port
   defaults to kaiHEN's `1337`). The tab connects on its own whenever the
   SOCKS5 tunnel is up.

It works like a desktop file manager:

- a folder tree on the left and the current folder on the right, with
  sortable Name / Size / Modified / Type columns, back / forward / up, an
  editable path bar and a filter;
- **drag files or folders from Explorer** into the window to upload them into
  the folder you are viewing, or drop them on a folder to upload there;
- **move** items by dragging them onto a folder (in the list or the tree), or
  with Cut / Copy / Paste (Ctrl+X / C / V). Moves are server-side when the FTP
  server allows it, otherwise they go through the PC;
- inline rename (F2), new folder, recursive download / upload / delete, a
  right-click menu, and "Open on this PC";
- a **preview pane** for text and config files (editable in place -- Save
  writes it straight back to the console) and for images;
- **Tools**: search the whole SD by name (results stream in), a disk-usage
  breakdown of any folder with bars, and **bookmarks** for folders you open a
  lot;
- a transfer queue with percentage, rate, time left and **Cancel**. Transfers
  use their own FTP connection, so browsing keeps working meanwhile, resume
  where they left off (`REST`), and raise a desktop notification when they
  finish while the window is in the background.

Throughput is better than the nominal 115200 baud suggests, because a USB CDC
gadget ignores that figure and runs at USB speed. Measured here at about
**2.1 MB/s** pulling an 11.8 MB file off the console, byte-for-byte intact.

`pc/ftp_client.py` also works on its own, with the SOCKS tunnel running:

```sh
python pc/ftp_client.py --list
```

```sh
python pc/ftp_client.py --get /kaihen/payload.js ./payload.js
```

### Using FileZilla instead

Point it at the SOCKS5 tunnel: **Settings -> Connection -> Generic proxy ->
SOCKS 5**, `127.0.0.1:1080`. Then open `ftp://192.168.4.2:1337`.

Two things bite here, and the tunnel handles both. FileZilla rewrites the
address in a `PASV` reply to its peer, which through a proxy is `127.0.0.1`,
while keeping the PASV port -- so the data channel would aim at the PC. The
tunnel rewrites a loopback target back to the console. And if the client is in
**active** mode it asks the proxy to dial a port on the PC, which nothing on
the console side can reach; the log says so explicitly when it happens.

One control connection means one operation at a time. Letting two buttons
overlap desyncs it: the second command lands mid-transfer, the server answers
`503 Invalid command during transfer` and then resets, which leaves the panel
dead until you reconnect. The GUI serializes operations, disables the buttons
while one runs, and drops the session on an unrecoverable error so the next
click reconnects cleanly.

Listing also has to CWD explicitly every time. Relying on the server's current
directory made **Up** change the path label while showing the same contents.

Homebrew FTP servers can also be fragile under rapid reconnection: this one
answers `550 unavailable` for a while after a few aborted sessions. Keeping
one session open, as the GUI does, avoids it.

## Switch network settings

Both addressing modes are verified on this port.

**Automatic** works out of the box: dnsmasq leases `192.168.4.10` to
`192.168.4.50`. Read the address the console got from its Internet settings,
or ask the Pi (`SCAN` lists associated MACs).

**Manual** is verified with these values. `192.168.4.2` sits outside the DHCP
range on purpose, so a static console and a DHCP client can never be handed
the same address:

| Setting | Value |
| --- | --- |
| IP address | `192.168.4.2` |
| Subnet mask | `255.255.255.0` |
| Gateway | `192.168.4.1` |
| Primary DNS | `192.168.4.1` |
| Secondary DNS | blank or `0.0.0.0` |
| Proxy | off |
| MTU | leave unchanged |

The GUI defaults to `192.168.4.2`, matching this table.

If the console cannot see `Switch2Bridge` at all, check that the bridge is
actually running -- the AP exists only while it is.

---

## Turning autostart off

Create an empty file named `civswitch-noautostart` on the SD card's `bootfs`
partition and the bridge will not autostart: the Pi boots onto normal Wi-Fi
with SSH reachable. Delete it to go back.

It lives on the FAT32 boot partition on purpose, so you can create or remove
it from Windows or macOS with the card in a reader -- no editing of
`cmdline.txt`, no touching the Linux filesystem, and no way to make the card
unbootable. The supervisor logs which file stopped it:

```text
[civswitch] autostart disabled by /boot/firmware/civswitch-noautostart; staying idle
```

This is the recovery path if the Pi ever strands itself.

---

## Serial protocol

The supervisor speaks a line protocol on the gadget TTY at 115200 baud. Every
reply ends with `END`.

| Command | Reply |
| --- | --- |
| `INFO` | JSON: mode, `ap_ip`, client count, `lan_ip`, hostname |
| `BRIDGE` | Starts bridge mode, only reporting success once `wlan0` really is an AP |
| `STOP` | Stops the bridge and hands the radio back to NetworkManager |
| `STATUS` | Current mode and AP client count |
| `SCAN` | MAC addresses of associated clients |
| `SEND ip port len` | Then `len` bytes of payload; relays it and prints the reply as text |
| `RELAY ip port len` | Same, but the reply comes back verbatim as `RESP <n>` plus n bytes |

`RELAY` exists alongside `SEND` because `SEND` is built for humans: it decodes
the reply as text, trims newlines and interleaves `[RELAY]` status lines.
Console tools parse the loader output line by line, so that reformatting
corrupts it. `RELAY` is what the proxy uses.

---

## Project structure

```text
pc/
  civswitch_gui.py     PC GUI: bridge, commands, tunnel, Files and Payload tabs
  ftp_browser.py       the Files tab: two-pane FTP file manager
  payload_tab.py       the Payload tab: JS payload library, editor, sender
  gui_extras.py        light/dark theme, toasts, taskbar flash
  ftp_client.py        minimal FTP over SOCKS, also usable standalone
  pilink.py            clean shutdown helpers shared by the GUI and tunnels
  win_drop.py          drag and drop from Explorer, high-DPI support
  tcp_proxy.py         one-shot TCP-to-serial relay for npm tools
  socks_tunnel.py      SOCKS5 proxy over a multiplexed serial tunnel
  requirements.txt
pi/
  supervisor.py        systemd service: serial protocol, mode control
  tunnel.py            multiplexed TCP channels over the serial link
  bridge_mode.py       AP + DHCP/DNS + Nintendo connectivity portal
  install.sh           one-time setup
  requirements.txt
  config/
    hostapd.conf       the AP
    dnsmasq.conf       DHCP and wildcard DNS
```

---

## Verified on hardware

Pi Zero W, Raspberry Pi OS Lite Trixie, kernel `6.18.50+rpt-rpi-v6`,
Python 3.13, driven from Windows 11.

- An 11.8 MB file came off the console's SD card through the tunnel at about
  2.1 MB/s, byte-for-byte intact, with the progress bar tracking it.
- Supervisor starts at boot and opens `/dev/ttyGS0`.
- The Pi enumerates as a serial device, USB VID/PID `0525:A4A7`.
- `INFO`, `STATUS`, `SCAN`, `RELAY` answer and terminate with `END`.
- The bridge **autostarts unattended** with nothing attached to the serial
  port, and the supervisor still answers afterwards.
- `BRIDGE` puts `wlan0` into AP mode: `iw dev wlan0 info` reports `type AP`
  and hostapd logs `AP-ENABLED`.
- A phone associated and dnsmasq leased it `192.168.4.50`; a Switch 2
  associated with the manual settings above and stayed connected.
- The three Nintendo connectivity hosts answer `200` with
  `X-Organization: Nintendo`; everything else answers `404`.
- `STOP` completes its teardown and SSH returns in about 15 seconds.
- The `civswitch-noautostart` flag keeps the bridge down and is logged.
- The GUI hands the COM port to the proxy and takes it back.

Not verified: a full `persist:chain` run end to end (the transport is proven,
the console side is not), maximum-size payloads, and long-running stability.

---

## Network isolation, measured

Collected on the Pi **while bridge mode was active**, since that is the only
state in which a console is attached:

| Check | Result |
| --- | --- |
| Interfaces on the radio | `wlan0` only, `type AP` -- no second station interface |
| IP addresses | `127.0.0.1` and `192.168.4.1` only; the Pi has left the home LAN |
| Routing table | `192.168.4.0/24 dev wlan0` only -- **no default route** |
| `ip_forward` v4 / v6 | `0` / `0`, with no persistent sysctl setting it |
| NAT rules | none |
| `ping 8.8.8.8` | fails |
| DNS from the AP | every name, `google.com` included, answers `192.168.4.1` |
| Listening on the AP | `:53` DNS, `:67` DHCP, `:80` connectivity checks |
| USB gadget | `g_serial` only -- no `g_ether`/RNDIS, so no network over USB |

There is no route off this network: no upstream interface, no default route,
no forwarding, no NAT, and a DNS server that answers every query with the Pi's
own address. A successful connectivity check on the console means only that
the Pi impersonated Nintendo's endpoints.

`avahi-daemon` is stopped for the life of the bridge. **`sshd` is deliberately
left running**, so the Pi always has an admin path: on the home LAN when the
bridge is idle, and at `192.168.4.1` over Switch2Bridge while it runs. Closing
it made the serial link the only way in, and every teardown that failed to
rejoin Wi-Fi then left the board unreachable. The tradeoff is that a client on
the AP can reach port 22 -- exposure to the console, not a route out. Add
`"ssh"` to `EXPOSED_SERVICES` in `pi/bridge_mode.py` if you want it closed and
accept recovering over serial only.

One honest caveat: the radio **does** advertise a concurrent combination
(`#{managed} <= 1, #{AP} <= 1 ... #channels <= 1`), so the hardware could run
a station alongside the AP. This port never creates that second interface --
`iw dev` shows `wlan0` alone in `type AP` -- but adding one would open a path.

Measure it yourself rather than trusting this table: run the checks with the
bridge **up**, and call tools by absolute path (`/usr/sbin/iw`). A
non-interactive SSH shell has no `/usr/sbin`, and `iw: command not found`
produces empty output that reads exactly like a clean result.

---

## Troubleshooting

**No COM port appears.** Check the cable is in the port marked **USB**, not
**PWR**. Then check `config.txt` has `dtoverlay=dwc2,dr_mode=peripheral` under
`[all]` and that `lsmod` lists `dwc2` and `g_serial`.

**The console cannot see Switch2Bridge.** The AP exists only while the bridge
runs. Also note that `netsh wlan show networks` on Windows reads a cache and
can report a live AP as absent -- trust the Pi (`iw dev wlan0 info`) or a real
client instead.

**The GUI says the port is in use.** Only one process may hold a serial port.
Stop the proxy, or close another terminal that has it open.

**The Pi is not on your LAN after stopping the bridge.** Give it 20 seconds.
If it still is not, read the journal: it is persistent, so
`journalctl -u civswitch -b -1` shows the previous boot too.

**The Pi is unreachable entirely.** Put the SD card in your PC and create
`civswitch-noautostart` on `bootfs`. See
[Turning autostart off](#turning-autostart-off).

**`npm install` fails on the Pi.** It should not be run there. Node.js is a PC
requirement only.

---

## Gotchas found while porting

Each of these failed silently, so they are worth knowing if you change this:

- `grep "^dtoverlay=dwc2" config.txt` matches the `dtoverlay=dwc2,dr_mode=host`
  that stock Pi OS ships under `[cm5]`, so a naive check skips adding the
  overlay and no USB gadget ever appears. Use a marker comment instead.
- Pi OS pins the journal to RAM via
  `/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf`. systemd
  sorts drop-ins by **filename across all directories**, so a `/etc` drop-in
  named `10-` loses to that `40-`. Hence `99-civswitch-persistent.conf`, plus
  a `journalctl --flush`.
- `StartLimitIntervalSec` and `StartLimitBurst` belong in `[Unit]`. Under
  `[Service]`, systemd ignores them and logs `Unknown key`.
- The supervisor must not speak before the PC does. Writing to the gadget TTY
  with no host attached only yields write timeouts, and after those the link
  stopped carrying inbound commands -- it looked wedged while the journal
  showed it healthy.
- pyserial `flush()` is `tcdrain()`: it waits for bytes to leave the port and
  honours no timeout, so it blocks forever on a gadget TTY with no reader.
  `write_timeout` does not help, because the stall is in the drain.
- Whatever kills the bridge process must outlast its teardown. A 5s wait then
  SIGKILL cut `nmcli device connect` short, so `wlan0` never rejoined Wi-Fi.
  The tell was `All stopped` with none of bridge_mode's own output.
- Tearing down the AP must flush `192.168.4.1` off `wlan0` before handing the
  device back, then ask NetworkManager to connect explicitly.
- A child's `print()` on a pipe is block-buffered, so its output only surfaces
  when it exits. Run it under `python3 -u`.
- Match the gadget by numeric VID/PID, not by description: on Windows it is a
  generic "USB Serial Device", and pyserial spells `hwid` differently per
  backend.
- Give the host's first write to a freshly opened gadget port a generous
  `write_timeout`. 4s was not enough; a short one looks exactly like a dead Pi.
- A mode that owns the serial link must be able to tell that the other end is
  gone. Killing the PC side of the tunnel without sending QUIT left the Pi
  reading frames and silently ignoring line commands, so the GUI just sat on
  "starting bridge...". The tunnel now watches the bytes it discards while
  resyncing: a newline means someone is talking the line protocol, so it hands
  the port back. The idle timeout is 180s as a second net.
- `dnsmasq --tag-if=civswitch` is invalid (`--tag-if` takes a full tag
  expression), so dnsmasq exited at startup and clients associated but got no
  DHCP lease.

---

## Credits and licenses

This project stands entirely on work by other people.

- **[Gezine](https://github.com/Gezine)** for [CivSwitch](https://github.com/Gezine/CivSwitch):
  the exploit, the Remote JS Loader and the persistent installer.
- **[Jack Spence](https://github.com/jackspence7)** for the original
  [CivSwitch-SerialBridge](https://github.com/jackspence7/CivSwitch-SerialBridge), MIT.
- **[favas0](https://github.com/favas0)** for the
  [Pico 2 W port](https://github.com/favas0/CivSwitch-Pico2W-SerialBridge), MIT,
  which this is a port of. The AP, DHCP, wildcard DNS and connectivity-check
  behaviour are his design; this repo reimplements them on Linux.
- **[itsPLK](https://github.com/itsPLK)** for
  [CivSwitchPiZero](https://github.com/itsPLK/CivSwitchPiZero), the Nexmon-based
  image that does the injection this bridge cannot.
- **[ItsNerisma](https://github.com/ItsNerisma)** for
  [CivSwitchLDN](https://github.com/ItsNerisma/CivSwitchLDN) and CivSwitchFS.
- **[s8n](https://github.com/s8n)** for
  [CivSwitchFS-kaiHEN](https://github.com/s8n/CivSwitchFS-kaiHEN).
- **[Kinnay](https://github.com/kinnay)** for the
  [LDN library](https://github.com/kinnay/LDN), GPL-3.0.
- **[SeanHeuc](https://github.com/SeanHeuc)** for the UDP-over-LDN installer
  transfer method.
- **Nexmon** and **brcmfmac** for the Wi-Fi driver and firmware work that makes
  LDN possible at all.
- **Earle Philhower and contributors** for arduino-pico, which the Pico version
  builds on.

The Nintendo connectivity-check responses are inherited from **90DNS**
(GPL-2.0-or-later) by way of **fernando-granco/switch2-homebrew** (MIT), via
the favas0 port. Those terms are not replaced by this repository's license.

Original code in this repository is MIT. See [LICENSE](LICENSE) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

This is an independent project and is not endorsed by any of the above.
Nintendo and Nintendo Switch are trademarks of Nintendo. Use it on hardware
and software you own, at your own risk.
