#!/usr/bin/env python3
"""TCP-to-serial proxy: run console tools from the PC through the Pi bridge.

Tools like CivSwitchFS-kaiHEN and CivSwitch's own senders talk plain TCP to
the Remote JS Loader, so they cannot be pointed at a COM port. This listens
locally, tunnels each connection over the USB serial link, and has the Pi open
the real TCP connection to the console:

    tool -> 127.0.0.1:9026 -> USB serial -> Pi -> 192.168.4.2:9026

So instead of moving the PC's Wi-Fi onto Switch2Bridge, point the tool at
localhost and keep the PC on your own network:

    python tcp_proxy.py --console 192.168.4.2
    npm run persist:chain -- 127.0.0.1

It works because those tools use one connection per request: send everything,
half-close, then read until the console closes. That maps exactly onto the
supervisor's RELAY command, which returns the reply byte-for-byte.

Requirements: the bridge must be running, and nothing else may hold the COM
port -- disconnect the GUI first, since the port allows a single reader.
"""

import argparse
import socket
import sys
import threading
import time

import serial
import serial.tools.list_ports

BAUD = 115200
GADGET = (0x0525, 0xA4A7)   # Linux Foundation / Gadget Serial
READ_IDLE = 0.4             # client is done once it half-closes or goes quiet


def find_gadget():
    return next((p.device for p in serial.tools.list_ports.comports()
                 if (p.vid, p.pid) == GADGET), None)


class Bridge:
    """Serialised access to the Pi. One request at a time, by design."""

    def __init__(self, port):
        self.ser = serial.Serial(port, BAUD, timeout=0.5, write_timeout=15)
        self.lock = threading.Lock()
        time.sleep(1.0)
        self.ser.reset_input_buffer()

    def _readline(self, timeout):
        deadline = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < deadline:
            ch = self.ser.read(1)
            if not ch:
                continue
            if ch == b"\n":
                return buf.decode("utf-8", "replace").strip()
            buf += ch
        return None

    def relay(self, console_ip, console_port, payload, verbose=False):
        """Send payload to the console, return its reply bytes."""
        with self.lock:
            self.ser.reset_input_buffer()
            cmd = f"RELAY {console_ip} {console_port} {len(payload)}\n"
            self.ser.write(cmd.encode())
            self.ser.write(payload)
            self.ser.flush()

            # Wait for the framing header. The console may take minutes.
            deadline = time.monotonic() + 420
            while time.monotonic() < deadline:
                line = self._readline(10)
                if line is None:
                    continue
                if line.startswith("RESP "):
                    n = int(line.split()[1])
                    reply = b""
                    sub = time.monotonic() + 120
                    while len(reply) < n and time.monotonic() < sub:
                        chunk = self.ser.read(n - len(reply))
                        if chunk:
                            reply += chunk
                            sub = time.monotonic() + 30
                    self._readline(10)        # trailing END
                    if verbose:
                        print(f"  reply {len(reply)}B", file=sys.stderr)
                    return reply
                if line.startswith("ERR "):
                    raise RuntimeError(line[4:])
                if line and verbose:
                    print(f"  pi: {line}", file=sys.stderr)
            raise RuntimeError("the Pi did not answer in time")


def read_request(conn):
    """Collect what the tool sends, until it half-closes or falls quiet."""
    conn.settimeout(30)
    data = b""
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:          # client half-closed: the normal path
                break
            data += chunk
            conn.settimeout(READ_IDLE)
    except socket.timeout:
        pass
    return data


def serve(bridge, listen_host, listen_port, console_ip, console_port, verbose):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((listen_host, listen_port))
    srv.listen(4)
    print(f"proxy su {listen_host}:{listen_port} -> {console_ip}:{console_port} "
          f"via seriale", flush=True)
    print(f"punta il tool a {listen_host}", flush=True)

    while True:
        conn, _ = srv.accept()
        try:
            payload = read_request(conn)
            if not payload:
                conn.close()
                continue
            print(f"-> {len(payload)} byte", flush=True)
            try:
                reply = bridge.relay(console_ip, console_port, payload, verbose)
            except Exception as e:
                print(f"   errore: {e}", flush=True)
                conn.close()
                continue
            print(f"<- {len(reply)} byte", flush=True)
            if reply:
                conn.sendall(reply)
            # Close so the tool's "end" handler fires, as a real console would.
            conn.shutdown(socket.SHUT_WR)
        except Exception as e:
            print(f"   connessione interrotta: {e}", flush=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="COM port of the Pi (auto-detected by VID/PID)")
    ap.add_argument("--console", default="192.168.4.2",
                    help="console IP on the bridge network (default: 192.168.4.2)")
    ap.add_argument("--console-port", type=int, default=9026)
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--listen-port", type=int, default=9026)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    dev = args.port or find_gadget()
    if not dev:
        print("Pi non trovato: specifica --port COMx", file=sys.stderr)
        return 1
    try:
        bridge = Bridge(dev)
    except Exception as e:
        print(f"{dev}: {e}", file=sys.stderr)
        print("la GUI tiene la porta? disconnettila prima.", file=sys.stderr)
        return 1
    print(f"seriale: {dev}", flush=True)

    try:
        serve(bridge, args.listen, args.listen_port,
              args.console, args.console_port, args.verbose)
    except KeyboardInterrupt:
        print("\nchiuso", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
