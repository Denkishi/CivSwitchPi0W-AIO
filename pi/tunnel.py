#!/usr/bin/env python3
"""Multiplexed TCP tunnel over the USB gadget serial link, Pi side.

The one-shot RELAY command cannot carry FTP: that needs a control connection
that stays open and talks both ways, plus data connections on ports negotiated
at runtime. So this adds channels. The PC runs a SOCKS5 front-end over the
same link, which is what lets passive FTP work -- the client asks the proxy to
reach the address the console advertises, and the proxy is on the far side of
the link, where that address exists.

Frame on the wire:

    A5 | type(1) | channel(2 BE) | length(2 BE) | payload

Everything is length-prefixed so the stream stays parseable, and a lost byte
only costs one resync on the magic rather than the session.
"""

import socket
import struct
import threading

MAGIC = 0xA5
HEADER = 6
MAX_PAYLOAD = 4096

OPEN = 1        # payload: "host:port"
OPEN_OK = 2
OPEN_FAIL = 3   # payload: reason
DATA = 4
CLOSE = 5
QUIT = 6        # the PC is done; leave tunnel mode
PING = 7

IDLE_EXIT = 180  # seconds without a frame before giving the port back


def frame(kind, channel, payload=b""):
    return struct.pack(">BBHH", MAGIC, kind, channel, len(payload)) + payload


class FrameReader:
    """Pulls frames out of a byte stream, resyncing on the magic."""

    def __init__(self, read):
        self._read = read
        self._buf = b""
        # Bytes dropped while resyncing. If they spell a text line, a human or
        # the GUI is talking the line protocol at us -- which means the PC
        # side of the tunnel is gone and we should hand the port back rather
        # than sit here ignoring commands until the idle timeout.
        self.junk = b""

    def next_frame(self, deadline_check):
        while True:
            # Resync: drop anything before the magic.
            while self._buf and self._buf[0] != MAGIC:
                self.junk += self._buf[:1]
                self._buf = self._buf[1:]
            if bytes([10]) in self.junk:
                return "text"
            if len(self._buf) >= HEADER:
                _, kind, chan, length = struct.unpack(">BBHH", self._buf[:HEADER])
                if len(self._buf) >= HEADER + length:
                    payload = self._buf[HEADER:HEADER + length]
                    self._buf = self._buf[HEADER + length:]
                    return kind, chan, payload
                want = HEADER + length - len(self._buf)
            else:
                want = HEADER - len(self._buf)
            chunk = self._read(max(want, 1))
            if chunk:
                self._buf += chunk
            elif deadline_check():
                return None


class Tunnel:
    """Runs until the PC sends QUIT, the link goes idle, or we are stopped."""

    def __init__(self, ser, log):
        self.ser = ser
        self.log = log
        self.channels = {}          # id -> socket
        self.lock = threading.Lock()
        self.running = True

    # -- wire -----------------------------------------------------
    def send(self, kind, channel, payload=b""):
        with self.lock:
            try:
                self.ser.write(frame(kind, channel, payload))
            except Exception:
                # Never let a stalled host kill the tunnel; drop and go on.
                try:
                    self.ser.reset_output_buffer()
                except Exception:
                    pass

    # -- channels -------------------------------------------------
    def open_channel(self, channel, target):
        try:
            host, port = target.rsplit(":", 1)
            sock = socket.create_connection((host, int(port)), timeout=10)
            sock.settimeout(None)
        except Exception as e:
            self.log(f"tunnel ch{channel} open {target} failed: {e}")
            self.send(OPEN_FAIL, channel, str(e).encode()[:200])
            return
        self.channels[channel] = sock
        self.send(OPEN_OK, channel)
        self.log(f"tunnel ch{channel} -> {target}")
        threading.Thread(target=self._pump, args=(channel, sock),
                         daemon=True).start()

    def _pump(self, channel, sock):
        """Console -> PC for one channel."""
        try:
            while self.running:
                data = sock.recv(MAX_PAYLOAD)
                if not data:
                    break
                self.send(DATA, channel, data)
        except Exception:
            pass
        self.close_channel(channel, tell_peer=True)

    def close_channel(self, channel, tell_peer=False):
        sock = self.channels.pop(channel, None)
        if sock:
            try:
                sock.close()
            except Exception:
                pass
            if tell_peer:
                self.send(CLOSE, channel)

    def shutdown(self):
        self.running = False
        for ch in list(self.channels):
            self.close_channel(ch)

    # -- main loop ------------------------------------------------
    def run(self):
        import time
        last = time.monotonic()
        reader = FrameReader(lambda n: self.ser.read(n))

        def idle():
            return time.monotonic() - last > IDLE_EXIT

        self.log("tunnel mode on")
        self.send(PING, 0)          # tells the PC we are listening
        while self.running:
            got = reader.next_frame(idle)
            if got is None:
                self.log("tunnel idle, leaving tunnel mode")
                break
            if got == "text":
                self.log("tunnel saw a line command; handing the port back")
                break
            kind, chan, payload = got
            last = time.monotonic()

            if kind == OPEN:
                self.open_channel(chan, payload.decode("utf-8", "replace"))
            elif kind == DATA:
                sock = self.channels.get(chan)
                if sock:
                    try:
                        sock.sendall(payload)
                    except Exception:
                        self.close_channel(chan, tell_peer=True)
                else:
                    self.send(CLOSE, chan)
            elif kind == CLOSE:
                self.close_channel(chan)
            elif kind == PING:
                self.send(PING, 0)
            elif kind == QUIT:
                self.log("tunnel closed by PC")
                break

        self.shutdown()
        self.log("tunnel mode off")
