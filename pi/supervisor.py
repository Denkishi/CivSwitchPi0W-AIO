#!/usr/bin/env python3
"""CivSwitch Pi Zero W Supervisor.

Listens on the USB gadget serial port (/dev/ttyGS0) for commands from the
PC GUI and runs bridge mode (AP + DHCP/DNS + connectivity portal).

Serial protocol (115200 baud, UTF-8):
  INFO              → JSON status blob, then END
  BRIDGE            → switch to bridge mode, then END
  SEND ip port len  → (bridge mode) relay payload to Switch, then END
  STATUS            → AP client count, then END
  SCAN              → client MACs, then END
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

SERIAL_PORT = "/dev/ttyGS0"
BAUD = 115200
AP_IP = "192.168.4.1"

# Paths — adjust if installed elsewhere
BRIDGE_SCRIPT = os.path.join(os.path.dirname(__file__), "bridge_mode.py")

# Bridge mode is the resting state: the Pi comes up as the AP and waits for
# the Switch, with no PC action needed. Send STOP over serial to hand the
# radio back to NetworkManager when you want Wi-Fi and SSH on the Pi again.
AUTOSTART_BRIDGE = True

# Escape hatch. Create this file and the bridge will not autostart, so the Pi
# boots onto normal Wi-Fi with SSH reachable. It lives on the FAT32 boot
# partition on purpose: you can create or delete it from Windows or macOS by
# putting the SD card in a reader, with no need to edit cmdline.txt or touch
# the Linux filesystem.
NOAUTOSTART_FLAGS = (
    "/boot/firmware/civswitch-noautostart",
    "/boot/civswitch-noautostart",
)


def autostart_blocked():
    for flag in NOAUTOSTART_FLAGS:
        if os.path.exists(flag):
            return flag
    return None


def log(msg):
    """Write to stdout, which systemd captures into the journal."""
    print(f"[civswitch] {msg}", flush=True)


class Supervisor:
    def __init__(self, serial_path=SERIAL_PORT):
        self.serial_path = serial_path
        self.mode = "idle"  # idle | bridge
        self.bridge_proc = None
        self._ser = None
        self._running = True
        # Stay silent until the PC speaks first. Writing to the gadget
        # TTY with no host attached only produces write timeouts, and
        # after those the link stopped carrying inbound commands: the
        # supervisor looked wedged while the journal showed it healthy.
        self._host_seen = False

    # ── Serial I/O ───────────────────────────────────────────────
    def open_serial(self):
        """Wait for the USB gadget TTY, then open it.

        The gadget appears only once dwc2 is in peripheral mode and g_serial
        has loaded, which can be later than this service starts -- and may
        never happen if the overlay is missing. Waiting here (instead of
        raising) keeps systemd from crash-looping the unit.
        """
        import serial as pyserial

        waited = 0.0
        while self._running:
            if os.path.exists(self.serial_path):
                try:
                    # write_timeout is essential: a USB gadget TTY blocks on
                    # write once its buffer fills and no host drains it.
                    # Without it, autostarting the bridge with no PC
                    # attached wedges the supervisor inside write().
                    self._ser = pyserial.Serial(self.serial_path, BAUD,
                                                timeout=0.1, write_timeout=2)
                    self._ser.reset_input_buffer()
                    log(f"opened {self.serial_path}")
                    return
                except OSError as e:
                    log(f"cannot open {self.serial_path}: {e}")
            elif waited % 30 < 2:
                log(f"waiting for {self.serial_path} "
                    f"(is dtoverlay=dwc2,dr_mode=peripheral set in config.txt?)")
            time.sleep(2)
            waited += 2

    def write(self, text):
        """Send a line to the PC, dropping it if nobody is listening.

        Never raises and never blocks: log() already puts everything in the
        journal, so a detached PC must not be able to stall the supervisor.
        """
        if not (self._ser and self._ser.is_open) or not self._host_seen:
            return
        try:
            self._ser.write((text + "\n").encode("utf-8"))
            # Deliberately NO flush(): pyserial's flush() is tcdrain(), which
            # waits for the bytes to actually leave the port and honours no
            # timeout. On a USB gadget TTY with no host reading, that blocks
            # forever -- which is what wedged the supervisor on autostart.
            # write_timeout alone does not help, because the stall is in the
            # drain, not the write. The bytes are already in the kernel
            # buffer; they go out as soon as a host attaches.
        except Exception:
            try:
                self._ser.reset_output_buffer()
            except Exception:
                pass

    def write_raw(self, data):
        """Write bytes verbatim -- no encoding, no newline, no rstrip."""
        if not (self._ser and self._ser.is_open) or not self._host_seen:
            return
        try:
            self._ser.write(data)
        except Exception:
            try:
                self._ser.reset_output_buffer()
            except Exception:
                pass

    def write_end(self):
        self.write("\nEND")

    def readline(self):
        if self._ser and self._ser.is_open:
            raw = self._ser.readline()
            if raw:
                # A host is clearly attached now, so replies can go out.
                self._host_seen = True
                return raw.decode("utf-8", "replace").rstrip("\r\n")
        return None

    # ── Info ──────────────────────────────────────────────────────
    def get_info(self):
        clients = self._get_client_count()
        return {
            "mode": self.mode,
            "ap_ip": AP_IP if self.mode == "bridge" else None,
            "clients": clients,
            "lan_ip": self._get_lan_ip(),
            "hostname": os.uname().nodename,
        }

    def _get_lan_ip(self):
        """wlan0's address on the home network, or None while bridging.

        Beats pinning a static lease: the Pi always knows where it is, so the
        GUI can just show the address to SSH to instead of hunting for it.
        """
        try:
            out = subprocess.check_output(
                ["ip", "-o", "-4", "addr", "show", "wlan0"],
                timeout=4, text=True, stderr=subprocess.DEVNULL,
            )
        except Exception:
            return None
        for token in out.split():
            if "/" in token and token.count(".") == 3:
                addr = token.split("/")[0]
                # 192.168.4.1 is our own AP address, reported as ap_ip.
                if not addr.startswith("192.168.4."):
                    return addr
        return None

    def _get_client_count(self):
        if self.mode != "bridge":
            return 0
        try:
            out = subprocess.check_output(
                ["iw", "dev", "wlan0", "station", "dump"],
                timeout=3, text=True, stderr=subprocess.DEVNULL,
            )
            return out.count("Station ")
        except Exception:
            return 0

    def _get_client_macs(self):
        if self.mode != "bridge":
            return []
        try:
            out = subprocess.check_output(
                ["iw", "dev", "wlan0", "station", "dump"],
                timeout=3, text=True, stderr=subprocess.DEVNULL,
            )
            macs = []
            for line in out.splitlines():
                if line.startswith("Station "):
                    macs.append(line.split()[1])
            return macs
        except Exception:
            return []

    # ── Bridge mode ──────────────────────────────────────────────
    def start_bridge(self):
        self.stop_all()
        self.write("[SUPERVISOR] Starting bridge mode...")
        # -u matters: without it the child's print() is block-buffered on a
        # pipe, so its startup lines only surface when it exits.
        self.bridge_proc = subprocess.Popen(
            [sys.executable, "-u", BRIDGE_SCRIPT],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        t = threading.Thread(target=self._stream_output,
                             args=(self.bridge_proc, "bridge"), daemon=True)
        t.start()

        # Verify the radio really is an AP instead of sleeping and assuming so.
        if self._wait_for_ap(timeout=25):
            self.mode = "bridge"
            log("bridge mode active, wlan0 is in AP mode")
            self.write("[SUPERVISOR] Bridge mode active.")
        else:
            self.write("[ERROR] Bridge failed: wlan0 never entered AP mode")
            log("bridge failed: wlan0 never entered AP mode")
            self.stop_all()
        self.write_end()

    def _wait_for_ap(self, timeout=25):
        """True once `iw dev wlan0 info` reports type AP."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.bridge_proc and self.bridge_proc.poll() is not None:
                return False  # the child died; no point waiting
            try:
                out = subprocess.check_output(
                    ["iw", "dev", "wlan0", "info"],
                    timeout=4, text=True, stderr=subprocess.DEVNULL,
                )
                if "type AP" in out:
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def _stream_output(self, proc, label):
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line:
                    self.write(f"[{label.upper()}] {line}")
        except Exception:
            pass

    # ── Payload relay (bridge mode) ──────────────────────────────
    def read_exact(self, length, timeout=60):
        """Read exactly `length` bytes of payload from the serial link."""
        buf = b""
        deadline = time.monotonic() + timeout
        while len(buf) < length and time.monotonic() < deadline:
            chunk = self._ser.read(length - len(buf))
            if chunk:
                buf += chunk
                deadline = time.monotonic() + 20
        return buf

    def relay_binary(self, ip, port, length):
        """Relay a payload and return the console's reply byte-for-byte.

        Unlike SEND, nothing is decoded, line-trimmed or mixed with [RELAY]
        status text: the reply comes back as `RESP <n>` plus exactly n bytes.
        That is what a transparent TCP proxy needs -- the tools that drive the
        loader parse its output line by line, so reformatting corrupts it.
        Progress goes to the journal instead of the wire.
        """
        if self.mode != "bridge":
            self.write("ERR not in bridge mode")
            self.write_end()
            return

        payload = self.read_exact(length)
        if len(payload) < length:
            self.write(f"ERR short payload {len(payload)}/{length}")
            self.write_end()
            return

        import socket as _socket
        log(f"relay {length}B -> {ip}:{port}")
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((ip, port))
            sock.sendall(payload)
            sock.shutdown(_socket.SHUT_WR)

            reply = b""
            sock.settimeout(120)
            while True:
                try:
                    chunk = sock.recv(8192)
                except _socket.timeout:
                    break
                if not chunk:
                    break
                reply += chunk
                sock.settimeout(10)
            sock.close()
        except Exception as e:
            log(f"relay failed: {e}")
            self.write(f"ERR {e}")
            self.write_end()
            return

        log(f"relay reply {len(reply)}B")
        self.write(f"RESP {len(reply)}")
        self.write_raw(reply)
        self.write_end()

    def relay_payload(self, ip, port, length):
        if self.mode != "bridge":
            self.write("[ERROR] Not in bridge mode")
            self.write_end()
            return

        # Read exactly `length` bytes of payload from serial
        payload = b""
        deadline = time.monotonic() + 15
        while len(payload) < length and time.monotonic() < deadline:
            remaining = length - len(payload)
            chunk = self._ser.read(remaining)
            if chunk:
                payload += chunk

        if len(payload) < length:
            self.write(f"[ERROR] Only got {len(payload)} of {length} bytes")
            self.write_end()
            return

        # TCP relay to Switch
        import socket
        self.write(f"[RELAY] Connecting to {ip}:{port} ({length} bytes)...")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(8)
            sock.connect((ip, port))
            sock.sendall(payload)
            sock.shutdown(socket.SHUT_WR)

            # Read response
            self.write("[RELAY] Sent. Reading response...")
            response_data = []
            sock.settimeout(120)
            got_data = False
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    self.write(chunk.decode("utf-8", "replace").rstrip("\n"))
                    got_data = True
                    sock.settimeout(5)
                except socket.timeout:
                    break

            if not got_data:
                self.write("[RELAY] (no response)")
            sock.close()
        except Exception as e:
            self.write(f"[RELAY] ERROR: {e}")

        self.write_end()

    # ── Cleanup ──────────────────────────────────────────────────
    def stop_all(self):
        if self.bridge_proc:
            self.bridge_proc.terminate()
            try:
                # Must outlast the whole teardown. It flushes the AP address
                # and runs `nmcli device connect wlan0`, which can take tens
                # of seconds. A 5s wait here used to SIGKILL the bridge
                # mid-teardown, so wlan0 never rejoined Wi-Fi and the Pi was
                # left unreachable -- the symptom was "All stopped" with none
                # of bridge_mode's own "Stopping bridge" output.
                self.bridge_proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                log("bridge did not exit in 120s; killing it")
                self.bridge_proc.kill()
            self.bridge_proc = None


        # Stop hostapd/dnsmasq if running
        for svc in ("hostapd", "dnsmasq"):
            subprocess.run(["systemctl", "stop", svc],
                           capture_output=True, timeout=5)
        self.mode = "idle"

    # ── Main loop ────────────────────────────────────────────────
    def run(self):
        # Blocks until the gadget TTY exists, so the control channel is always
        # live BEFORE the bridge can take the radio away. If the gadget never
        # appears we stay here with Wi-Fi and SSH up, rather than autostarting
        # the bridge and locking out both ways in.
        self.open_serial()
        self.write("[SUPERVISOR] CivSwitch Pi Zero W ready.")
        self.write(f"[SUPERVISOR] Hostname: {os.uname().nodename}")

        blocked = autostart_blocked()
        if blocked:
            log(f"autostart disabled by {blocked}; staying idle")
            self.write(f"[SUPERVISOR] Autostart disabled by {blocked}")
        elif AUTOSTART_BRIDGE and self._running:
            log("autostarting bridge (serial control channel is up)")
            self.write("[SUPERVISOR] Autostarting bridge...")
            self.start_bridge()

        while self._running:
            line = self.readline()
            if not line:
                time.sleep(0.05)
                continue

            line = line.strip()
            if not line:
                continue

            if line == "INFO":
                info = self.get_info()
                self.write(json.dumps(info))
                self.write_end()

            elif line == "BRIDGE":
                self.start_bridge()


            elif line.startswith("RELAY "):
                parts = line.split()
                if len(parts) != 4:
                    self.write("ERR usage: RELAY <ip> <port> <length>")
                    self.write_end()
                    continue
                try:
                    self.relay_binary(parts[1], int(parts[2]), int(parts[3]))
                except ValueError:
                    self.write("ERR invalid port or length")
                    self.write_end()

            elif line.startswith("SEND "):
                parts = line.split()
                if len(parts) != 4:
                    self.write("[ERROR] Usage: SEND <ip> <port> <length>")
                    self.write_end()
                    continue
                try:
                    ip = parts[1]
                    port = int(parts[2])
                    length = int(parts[3])
                except ValueError:
                    self.write("[ERROR] Invalid port or length")
                    self.write_end()
                    continue
                self.relay_payload(ip, port, length)

            elif line == "STATUS":
                count = self._get_client_count()
                self.write(f"[STATUS] Mode: {self.mode}, AP clients: {count}")
                self.write_end()

            elif line == "SCAN":
                macs = self._get_client_macs()
                self.write(f"[SCAN] {len(macs)} clients")
                for m in macs:
                    self.write(f"  {m}")
                self.write_end()

            elif line == "TUNNEL":
                # Hand the link to the multiplexed tunnel. It owns the port
                # until the PC sends QUIT or the link goes idle, so no line
                # commands are served meanwhile -- and the port is never
                # handed to another process, which is where serial handovers
                # kept going wrong.
                self.write("[SUPERVISOR] Entering tunnel mode")
                try:
                    from tunnel import Tunnel
                    Tunnel(self._ser, log).run()
                except Exception as e:
                    log(f"tunnel failed: {e}")
                self.write("[SUPERVISOR] Tunnel mode ended")
                self.write_end()

            elif line == "STOP":
                self.stop_all()
                self.write("[SUPERVISOR] All stopped.")
                self.write_end()

            else:
                self.write(f"[ERROR] Unknown: {line}")
                self.write_end()

    def shutdown(self, *_):
        self._running = False
        self.stop_all()


def main():
    sv = Supervisor()
    signal.signal(signal.SIGTERM, sv.shutdown)
    signal.signal(signal.SIGINT, sv.shutdown)
    log("supervisor starting")
    # Bridge mode stops sshd/avahi while it runs and restores them on exit.
    # If it was SIGKILLed they would stay down, so make every supervisor start
    # put them back: this runs before any bridge can be started.
    for svc in ("ssh", "avahi-daemon"):  # ssh kept for a stranded-board rescue
        r = subprocess.run(["systemctl", "is-active", "--quiet", svc],
                           capture_output=True)
        if r.returncode != 0:
            log(f"{svc} was down (likely a hard-killed bridge); starting it")
            subprocess.run(["systemctl", "start", svc],
                           check=False, capture_output=True, timeout=20)
    try:
        sv.run()
    except KeyboardInterrupt:
        pass
    except Exception:
        # Log the traceback to the journal instead of dying silently, and do
        # NOT tear down services we may never have started: an early failure
        # here used to run stop_all() on every systemd restart.
        import traceback
        log("supervisor crashed:\n" + traceback.format_exc())
        return 1
    sv.stop_all()
    log("supervisor stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
