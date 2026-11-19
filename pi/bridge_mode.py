#!/usr/bin/env python3
"""Bridge mode: start hostapd + dnsmasq + DNS/HTTP captive portal.

This script is launched by the supervisor. It configures wlan0 as an AP,
starts hostapd and dnsmasq, and runs a small HTTP server that answers
Nintendo connectivity checks so the Switch thinks it's online.

Exits when terminated by the supervisor.
"""

import functools
import os
import signal
import subprocess
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

# Our stdout is a pipe read by the supervisor, so print() would be block
# buffered and nothing would surface until this process exits. The supervisor
# also launches us with -u; this keeps the script correct when run by hand.
print = functools.partial(print, flush=True)  # noqa: A001

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")
AP_IP = "192.168.4.1"


# ── Nintendo connectivity-check HTTP handler ─────────────────────
class ConnectivityHandler(BaseHTTPRequestHandler):
    """Respond to Nintendo connectivity probes; 404 everything else."""

    # Hosts that need the legacy HTML response
    LEGACY = {"conntest.nintendowifi.net"}

    # Hosts that need a plain "ok" response
    MODERN = {
        "ctest.cdn.nintendo.net",
        "ctest.p01.ctest.srv.nintendo.net",
        "ctest-dl-lp1.cdn.nintendo.net",
        "ctest-ul-lp1.cdn.nintendo.net",
        "api.p01.lp1.ctest.srv.nintendo.net",
        "api-ipv6.p01.lp1.ctest.srv.nintendo.net",
    }

    def do_GET(self):
        host = self.headers.get("Host", "").split(":")[0]

        if host in self.LEGACY:
            body = (
                "<html><head><title>HTML Page</title></head>"
                "<body>This is test.html page</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Organization", "Nintendo")
            self.end_headers()
            self.wfile.write(body.encode())
            return

        if host in self.MODERN:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Organization", "Nintendo")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    do_POST = do_GET
    do_HEAD = do_GET

    def log_message(self, format, *args):
        # Quiet — supervisor streams what matters
        pass


def configure_interface():
    """Set wlan0 to the AP static IP."""
    subprocess.run(["ip", "addr", "flush", "dev", "wlan0"], check=False)
    subprocess.run(
        ["ip", "addr", "add", f"{AP_IP}/24", "dev", "wlan0"],
        check=True,
    )
    subprocess.run(["ip", "link", "set", "wlan0", "up"], check=True)


def start_hostapd():
    conf = os.path.join(CONFIG_DIR, "hostapd.conf")
    return subprocess.Popen(
        ["hostapd", conf],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )


def start_dnsmasq():
    """Start dnsmasq for DHCP + wildcard DNS on the AP.

    No --tag-if here: it takes a full tag expression (set:x,tag:y), so the
    bare `--tag-if=civswitch` this used to pass made dnsmasq reject the
    command line and exit at once -- clients associated but got no DHCP
    lease. Identify our own instance by its config path instead.
    """
    conf = os.path.join(CONFIG_DIR, "dnsmasq.conf")

    # Reap a leftover instance of ours from a previous bridge run.
    subprocess.run(["pkill", "-f", f"dnsmasq.*{conf}"], check=False)
    time.sleep(0.3)

    proc = subprocess.Popen(
        ["dnsmasq", "-C", conf, "--keep-in-foreground", "--log-facility=-"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )

    # A bad config or a busy port makes dnsmasq exit immediately. Catch it
    # here rather than discovering it as "the Switch never gets an IP".
    time.sleep(1.5)
    if proc.poll() is not None:
        out = (proc.stdout.read() or "").strip()
        raise RuntimeError(f"dnsmasq exited at startup: {out}")
    return proc


# Services stopped for the life of the bridge.
#
# sshd is deliberately NOT in here. Keeping it up means the Pi always has a
# working admin path: on the home LAN when the bridge is idle, and at
# 192.168.4.1 over Switch2Bridge while it is running. Stopping it made the
# only way in the USB serial link, so every teardown that failed to rejoin
# Wi-Fi left the board unreachable and needing the SD card pulled.
#
# The tradeoff is that a client on the AP can reach port 22. That is exposure
# to the console, not a route to the internet: there is still no default
# route, no forwarding and no NAT. Add "ssh" below if you want it closed and
# accept recovering over serial only.
EXPOSED_SERVICES = ("avahi-daemon",)


def set_services(action):
    for svc in EXPOSED_SERVICES:
        subprocess.run(["systemctl", action, svc],
                       check=False, capture_output=True, timeout=15)


def main():
    print("Configuring wlan0...")
    # Prevent NetworkManager from touching wlan0
    subprocess.run(
        ["nmcli", "device", "set", "wlan0", "managed", "no"],
        check=False, capture_output=True,
    )
    time.sleep(0.5)
    configure_interface()

    print("Starting hostapd...")
    hostapd = start_hostapd()
    time.sleep(1)

    # Check hostapd started
    if hostapd.poll() is not None:
        out = hostapd.stdout.read()
        print(f"hostapd failed: {out}")
        sys.exit(1)

    print("Starting dnsmasq...")
    try:
        dnsmasq = start_dnsmasq()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        hostapd.terminate()
        hostapd.wait(timeout=5)
        subprocess.run(["nmcli", "device", "set", "wlan0", "managed", "yes"],
                       check=False, capture_output=True)
        sys.exit(1)
    print("dnsmasq running (DHCP 192.168.4.10-50)")

    print("Starting HTTP connectivity server on :80...")
    httpd = HTTPServer((AP_IP, 80), ConnectivityHandler)
    httpd.timeout = 1

    # Do this last: if anything above failed we have already exited, and SSH
    # was still up for recovery.
    print(f"Closing {', '.join(EXPOSED_SERVICES)} for the life of the bridge...")
    set_services("stop")

    print(f"Bridge active - SSID: Switch2Bridge, IP: {AP_IP}")

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while running:
            httpd.handle_request()
    finally:
        print("Stopping bridge...")
        httpd.server_close()
        hostapd.terminate()
        dnsmasq.terminate()
        hostapd.wait(timeout=5)
        dnsmasq.wait(timeout=5)
        # Drop the AP address first. Leaving 192.168.4.1 on wlan0 confuses
        # NetworkManager when it takes the device back, and it then sits there
        # neither serving an AP nor joining the home network -- which strands
        # the Pi with no SSH and no way in but this serial link.
        subprocess.run(["ip", "addr", "flush", "dev", "wlan0"], check=False)
        subprocess.run(
            ["nmcli", "device", "set", "wlan0", "managed", "yes"],
            check=False, capture_output=True,
        )
        # NetworkManager does not reliably autoconnect a device it has just
        # regained, so ask explicitly instead of hoping.
        subprocess.run(["nmcli", "device", "connect", "wlan0"],
                       check=False, capture_output=True, timeout=60)
        set_services("start")
        print(f"Restored {', '.join(EXPOSED_SERVICES)}.")
        print("Bridge stopped.")


if __name__ == "__main__":
    main()
