# Third-party notices

This repository contains no third-party source files: everything here was
written for it. What it does carry is **inherited behaviour**, and the terms
attached to that behaviour are listed below. Nothing in this project's MIT
license replaces them.

## Nintendo connectivity-check responses (GPL-2.0-or-later)

`pi/bridge_mode.py` answers a fixed list of Nintendo connectivity hostnames
with the specific status codes, headers and bodies the console expects, so it
believes the network is usable. That host list and those responses come from:

- **90DNS** -- GPL-2.0-or-later. <https://gitlab.com/a/90dns>
- by way of **fernando-granco/switch2-homebrew** (MIT)
- by way of **favas0/CivSwitch-Pico2W-SerialBridge** (MIT), where the same
  attribution is preserved

The GPL-2.0-or-later terms apply to that derived behaviour.

## Ported design (MIT)

The bridge concept -- an isolated open AP, DHCP, wildcard DNS pointing at the
board, the connectivity portal, and the serial command protocol with its
`SEND`/`END` framing -- is the design of:

- **CivSwitch-SerialBridge** by Jack Spence, MIT.
  <https://github.com/jackspence7/CivSwitch-SerialBridge>
- **CivSwitch-Pico2W-SerialBridge** by favas0, MIT.
  <https://github.com/favas0/CivSwitch-Pico2W-SerialBridge>

This project reimplements it on Linux, with `hostapd`, `dnsmasq` and Python in
place of the Arduino core. The protocol is kept compatible so their senders
still work; `RELAY` and `TUNNEL` are additions.

## Referenced but not included

These are linked from the documentation and used as separate tools. None of
their code is here, and their own licenses govern them:

| Project | License | Role |
| --- | --- | --- |
| [Gezine/CivSwitch](https://github.com/Gezine/CivSwitch) | see repo | The exploit, the Remote JS Loader, the persistent installer |
| [itsPLK/CivSwitchPiZero](https://github.com/itsPLK/CivSwitchPiZero) | see repo | Nexmon-based image for the LDN/USB injection |
| [ItsNerisma/CivSwitchLDN](https://github.com/ItsNerisma/CivSwitchLDN) | GPL-3.0-only | LDN injection from a Linux PC |
| [s8n/CivSwitchFS-kaiHEN](https://github.com/s8n/CivSwitchFS-kaiHEN) | GPL-3.0-only | FS access chained into a kaiHEN boot |
| [kinnay/LDN](https://github.com/kinnay/LDN) | GPL-3.0 | LDN protocol library |
| [seemoo-lab/nexmon](https://github.com/seemoo-lab/nexmon) | see repo | Patched Broadcom firmware enabling monitor mode |

## Runtime dependencies

- **pyserial** (BSD-3-Clause) -- on the PC and the Pi.
- **hostapd** and **dnsmasq** (GPL-2.0) -- installed from Raspberry Pi OS and
  invoked as separate processes, not linked or redistributed here.

## Trademarks

Nintendo, Nintendo Switch and related names are trademarks of Nintendo.
This project is unaffiliated with and unendorsed by Nintendo or by any of the
projects listed above.
