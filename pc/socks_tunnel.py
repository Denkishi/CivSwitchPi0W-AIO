#!/usr/bin/env python3
"""SOCKS5 proxy on the PC, tunnelled to the console over the USB serial link.

Point any SOCKS5-capable client at this and it reaches the console network as
if it were local -- no Wi-Fi card needed on the PC.

    client -> 127.0.0.1:1080 (SOCKS5) -> USB serial -> Pi -> 192.168.4.x

SOCKS5 rather than a fixed port forward because of passive FTP: the server
answers PASV with an address and a port chosen at runtime, and the client then
opens a second connection there. A forwarder would have to guess those ports;
a SOCKS proxy is simply asked to connect, and it is on the far side of the
link where that address exists.

FileZilla: Edit -> Settings -> Connection -> Generic proxy -> SOCKS 5,
host 127.0.0.1, port 1080. Then open ftp://192.168.4.2:1337 as usual.

Requirements: the bridge must be running, and nothing else may hold the COM
port -- disconnect the GUI, or let the GUI start this for you.
"""

import argparse
import socket
import socketserver
import struct
import sys
import threading
import time

import serial
import serial.tools.list_ports

BAUD = 115200
GADGET = (0x0525, 0xA4A7)

MAGIC = 0xA5
HEADER = 6
MAX_PAYLOAD = 4096

OPEN, OPEN_OK, OPEN_FAIL, DATA, CLOSE, QUIT, PING = 1, 2, 3, 4, 5, 6, 7


def frame(kind, channel, payload=b""):
    return struct.pack(">BBHH", MAGIC, kind, channel, len(payload)) + payload


class Channel:
    def __init__(self, cid, sock):
        self.id = cid
        self.sock = sock
        self.opened = threading.Event()
        self.ok = False
        self.reason = ""
        self.closed = False
        # The console can answer before we have sent the SOCKS reply -- an FTP
        # server greets immediately. Forwarding that first would put "220 ..."
        # where the client expects the SOCKS version byte, and it reports
        # "unknown SOCKS protocol version: 50" (0x32, the "2" of 220). So hold
        # early bytes until the handshake is finished.
        self.ready = threading.Event()
        self.pending = []
        self.plock = threading.Lock()

    def deliver(self, payload):
        if self.ready.is_set():
            self.sock.sendall(payload)
            return
        with self.plock:
            if self.ready.is_set():
                self.sock.sendall(payload)
            else:
                self.pending.append(payload)

    def release(self):
        """Called once the SOCKS reply is on the wire."""
        with self.plock:
            queued, self.pending = self.pending, []
            self.ready.set()
        for chunk in queued:
            self.sock.sendall(chunk)


class Link:
    """Owns the serial port: one reader thread, locked writes, channel table."""

    def __init__(self, port, verbose=False):
        self.verbose = verbose
        self.ser = serial.Serial(port, BAUD, timeout=0.2, write_timeout=20)
        self.wlock = threading.Lock()
        self.channels = {}
        self.clock = threading.Lock()
        self.next_id = 1
        self.running = True
        self.ready = threading.Event()
        self._buf = b""
        # Where to send a client that asks us to dial loopback. FileZilla
        # rewrites the PASV address to its peer -- which through a proxy is
        # 127.0.0.1 -- while keeping the PASV port, so the data channel would
        # aim at the PC. We know the real host from the control connection.
        self.console_host = None
        time.sleep(1.0)

    # -- setup ----------------------------------------------------
    def enter_tunnel_mode(self, timeout=25):
        """Get the Pi into tunnel mode, whatever state it was left in.

        If a previous run was killed without sending QUIT the Pi is still in
        tunnel mode and ignores the text command, because it is reading frames
        now. So probe with a PING first, and only fall back to the text
        command -- after a QUIT to clear any half-dead session.
        """
        self.ser.reset_input_buffer()
        threading.Thread(target=self._reader, daemon=True).start()

        self.send(PING, 0)
        if self.ready.wait(3):
            self.say("already in tunnel mode")
            return True

        self.send(QUIT, 0)
        time.sleep(1.5)
        with self.wlock:
            self.ser.write(b"TUNNEL" + bytes([10]))
        # The Pi sends a PING once it is listening. Its text banner arrives
        # first; the frame reader resyncs past it on the magic byte.
        return self.ready.wait(timeout)

    # -- wire -----------------------------------------------------
    def send(self, kind, channel, payload=b""):
        with self.wlock:
            try:
                self.ser.write(frame(kind, channel, payload))
            except Exception as e:
                self.say(f"write failed: {e}")

    def say(self, msg):
        print(msg, flush=True)

    def _reader(self):
        while self.running:
            try:
                chunk = self.ser.read(2048)
            except Exception as e:
                self.say(f"serial lost: {e}")
                self.running = False
                return
            if chunk:
                self._buf += chunk
            got = self._take_frames()
            if not chunk and not got:
                continue

    def _take_frames(self):
        got = False
        while True:
            while self._buf and self._buf[0] != MAGIC:
                self._buf = self._buf[1:]
            if len(self._buf) < HEADER:
                return got
            _, kind, chan, length = struct.unpack(">BBHH", self._buf[:HEADER])
            if len(self._buf) < HEADER + length:
                return got
            payload = self._buf[HEADER:HEADER + length]
            self._buf = self._buf[HEADER + length:]
            got = True
            self._dispatch(kind, chan, payload)

    def _dispatch(self, kind, chan, payload):
        if kind == PING:
            self.ready.set()
            return
        ch = self.channels.get(chan)
        if kind == OPEN_OK:
            if ch:
                ch.ok = True
                ch.opened.set()
        elif kind == OPEN_FAIL:
            if ch:
                ch.reason = payload.decode("utf-8", "replace")
                ch.opened.set()
        elif kind == DATA:
            if ch and not ch.closed:
                try:
                    ch.deliver(payload)
                except Exception:
                    self.drop(chan)
        elif kind == CLOSE:
            self.drop(chan)

    # -- channels -------------------------------------------------
    def open(self, sock, host, port, timeout=20):
        with self.clock:
            cid = self.next_id
            self.next_id = self.next_id % 65000 + 1
            ch = Channel(cid, sock)
            self.channels[cid] = ch
        self.send(OPEN, cid, f"{host}:{port}".encode())
        if not ch.opened.wait(timeout):
            self.channels.pop(cid, None)
            raise RuntimeError("the Pi did not answer the open")
        if not ch.ok:
            self.channels.pop(cid, None)
            raise RuntimeError(ch.reason or "connection refused")
        return ch

    def drop(self, cid):
        ch = self.channels.pop(cid, None)
        if ch and not ch.closed:
            ch.closed = True
            try:
                ch.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                ch.sock.close()
            except Exception:
                pass

    def close(self):
        self.running = False
        for cid in list(self.channels):
            self.drop(cid)
        try:
            self.send(QUIT, 0)
            time.sleep(0.3)
        except Exception:
            pass
        try:
            self.ser.close()
        except Exception:
            pass


class Socks5Handler(socketserver.BaseRequestHandler):
    link = None

    def handle(self):
        c = self.request
        try:
            if not self._greet(c):
                return
            host, port = self._request_target(c)
            if host is None:
                return
        except Exception:
            return

        try:
            ch = Socks5Handler.link.open(c, host, port)
        except Exception as e:
            print(f"   {host}:{port} -> {e}", flush=True)
            # 0x05 = connection refused by destination
            self._reply(c, 5)
            return

        if not host.startswith("127.") and host != "::1":
            Socks5Handler.link.console_host = host
        print(f"-> {host}:{port} (ch{ch.id})", flush=True)
        self._reply(c, 0)
        try:
            ch.release()          # now anything the console already sent flows
        except Exception:
            Socks5Handler.link.drop(ch.id)
            return
        self._pump(c, ch)

    # -- SOCKS5 ---------------------------------------------------
    def _greet(self, c):
        head = self._recv_exact(c, 2)
        if not head or head[0] != 5:
            return False
        nmethods = head[1]
        self._recv_exact(c, nmethods)
        c.sendall(b"\x05\x00")          # no authentication
        return True

    CMD_NAMES = {1: "CONNECT", 2: "BIND", 3: "UDP ASSOCIATE"}

    def _request_target(self, c):
        head = self._recv_exact(c, 4)
        if not head:
            return None, None
        if head[1] != 1:                # CONNECT only
            name = self.CMD_NAMES.get(head[1], head[1])
            print(f"   SOCKS {name} not supported -- set the client to "
                  f"PASSIVE mode", flush=True)
            self._reply(c, 7)           # command not supported
            return None, None
        atyp = head[3]
        if atyp == 1:
            raw = self._recv_exact(c, 4)
            host = socket.inet_ntoa(raw)
        elif atyp == 3:
            n = self._recv_exact(c, 1)[0]
            host = self._recv_exact(c, n).decode("utf-8", "replace")
        elif atyp == 4:
            raw = self._recv_exact(c, 16)
            host = socket.inet_ntop(socket.AF_INET6, raw)
        else:
            self._reply(c, 8)           # address type not supported
            return None, None
        port = struct.unpack(">H", self._recv_exact(c, 2))[0]
        if host.startswith("127.") or host == "::1":
            real = Socks5Handler.link.console_host
            if real:
                # Passive FTP through a proxy: the client kept the PASV port
                # but replaced the address with its peer. Aim at the console.
                print(f"   {host}:{port} rewritten to {real}:{port}",
                      flush=True)
                host = real
            else:
                print(f"   {host}:{port} is this machine and no console "
                      f"address is known yet", flush=True)
        return host, port

    def _reply(self, c, code):
        try:
            c.sendall(b"\x05" + bytes([code]) + b"\x00\x01"
                      + b"\x00\x00\x00\x00" + b"\x00\x00")
        except Exception:
            pass

    def _recv_exact(self, c, n):
        buf = b""
        while len(buf) < n:
            part = c.recv(n - len(buf))
            if not part:
                return None
            buf += part
        return buf

    # -- data -----------------------------------------------------
    def _pump(self, c, ch):
        """Client -> console. The reader thread handles the other way."""
        c.settimeout(None)
        try:
            while not ch.closed:
                data = c.recv(MAX_PAYLOAD)
                if not data:
                    break
                Socks5Handler.link.send(DATA, ch.id, data)
        except Exception:
            pass
        if not ch.closed:
            Socks5Handler.link.send(CLOSE, ch.id)
        Socks5Handler.link.drop(ch.id)
        print(f"<- ch{ch.id} closed", flush=True)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def find_gadget():
    return next((p.device for p in serial.tools.list_ports.comports()
                 if (p.vid, p.pid) == GADGET), None)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="COM port of the Pi (auto-detected by VID/PID)")
    ap.add_argument("--console",
                    help="console IP to use when a client asks for loopback "
                         "(otherwise learned from the control connection)")
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--listen-port", type=int, default=1080)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    dev = args.port or find_gadget()
    if not dev:
        print("Pi not found: pass --port COMx", file=sys.stderr)
        return 1
    try:
        link = Link(dev, args.verbose)
    except Exception as e:
        print(f"{dev}: {e}", file=sys.stderr)
        print("is the GUI holding the port? disconnect it first.", file=sys.stderr)
        return 1

    link.console_host = args.console
    print(f"serial: {dev}", flush=True)
    if not link.enter_tunnel_mode():
        print("the Pi did not enter tunnel mode (is the bridge running?)",
              file=sys.stderr)
        link.close()
        return 1
    print("tunnel up", flush=True)

    Socks5Handler.link = link
    srv = Server((args.listen, args.listen_port), Socks5Handler)
    print(f"SOCKS5 on {args.listen}:{args.listen_port}", flush=True)
    print("FileZilla: Settings -> Connection -> Generic proxy -> SOCKS 5, "
          f"{args.listen}:{args.listen_port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nclosing", flush=True)
    finally:
        srv.shutdown()
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
