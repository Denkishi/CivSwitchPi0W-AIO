#!/usr/bin/env python3
"""Minimal FTP client that speaks through the SOCKS tunnel.

Enough FTP to browse the console's SD card and move files: login, navigate,
list, download, upload, delete, mkdir. Passive mode only -- active mode asks
the server to dial the client back, and nothing on the console side can reach
a port on the PC.

Python's ftplib has no SOCKS support, and the point here is to spare you the
proxy setup in a third-party client, so the handful of commands we need are
implemented directly.

Standalone:
    python ftp_client.py --list /
    python ftp_client.py --get /hbmenu.nro ./hbmenu.nro
"""

import argparse
import datetime
import re
import socket
import struct
import sys
from collections import namedtuple

DEFAULT_PROXY = ("127.0.0.1", 1080)
DEFAULT_HOST = "192.168.4.2"
DEFAULT_PORT = 1337

PASV_RE = re.compile(r"(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)")


MONTHS = {m: i for i, m in enumerate(
    ("jan feb mar apr may jun jul aug sep oct nov dec").split(), 1)}

# mtime is a datetime or None; link is the symlink target or "".
Entry = namedtuple("Entry", "name is_dir size mtime link")


class FtpError(RuntimeError):
    pass


class Cancelled(Exception):
    """Raised from a progress callback to abort a transfer."""


def socks_connect(host, port, proxy=DEFAULT_PROXY, timeout=30):
    """Open a TCP connection to host:port through a SOCKS5 proxy."""
    s = socket.create_connection(proxy, timeout=timeout)
    s.settimeout(timeout)
    s.sendall(b"\x05\x01\x00")
    greet = s.recv(2)
    if len(greet) != 2 or greet[0] != 5:
        s.close()
        raise FtpError("the proxy did not answer as SOCKS5 (is the tunnel up?)")
    try:
        packed = socket.inet_aton(host)
        request = b"\x05\x01\x00\x01" + packed
    except OSError:
        name = host.encode()
        request = b"\x05\x01\x00\x03" + bytes([len(name)]) + name
    s.sendall(request + struct.pack(">H", port))
    reply = s.recv(10)
    if len(reply) < 2 or reply[1] != 0:
        s.close()
        code = reply[1] if len(reply) > 1 else -1
        raise FtpError(f"the proxy refused {host}:{port} (SOCKS code {code})")
    return s


class Ftp:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 proxy=DEFAULT_PROXY, user="anonymous", password="anonymous"):
        self.host, self.port, self.proxy = host, port, proxy
        self.user, self.password = user, password
        self.ctl = None
        self._buf = b""
        self.confirmed = True

    # -- plumbing -------------------------------------------------
    def connect(self):
        self.ctl = socks_connect(self.host, self.port, self.proxy)
        self._expect(220)
        self._cmd(f"USER {self.user}", (230, 331))
        # 331 means it wants a password; 230 means it is already satisfied.
        if self._last_code == 331:
            self._cmd(f"PASS {self.password}", 230)
        self._cmd("TYPE I", 200)
        return self

    def close(self):
        if self.ctl:
            try:
                self._send("QUIT")
            except Exception:
                pass
            try:
                self.ctl.close()
            except Exception:
                pass
            self.ctl = None

    def _send(self, line):
        self.ctl.sendall(line.encode("utf-8") + b"\r\n")

    def _readline(self):
        while b"\r\n" not in self._buf:
            chunk = self.ctl.recv(4096)
            if not chunk:
                raise FtpError("the control connection closed")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line.decode("utf-8", "replace")

    def _response(self):
        line = self._readline()
        # Multi-line replies repeat the code with a dash: "220-..." then "220 ".
        if len(line) > 3 and line[3] == "-":
            code = line[:3]
            while True:
                more = self._readline()
                line += "\n" + more
                if more.startswith(code + " "):
                    break
        try:
            self._last_code = int(line[:3])
        except ValueError:
            raise FtpError(f"malformed reply: {line[:60]}")
        return self._last_code, line

    def _expect(self, wanted):
        code, line = self._response()
        allowed = wanted if isinstance(wanted, tuple) else (wanted,)
        if code not in allowed:
            raise FtpError(line.strip())
        return line

    def _cmd(self, line, wanted):
        self._send(line)
        return self._expect(wanted)

    # -- data channels --------------------------------------------
    def _pasv(self):
        line = self._cmd("PASV", 227)
        m = PASV_RE.search(line)
        if not m:
            raise FtpError(f"could not parse PASV: {line.strip()}")
        g = [int(x) for x in m.groups()]
        host = ".".join(str(x) for x in g[:4])
        port = g[4] * 256 + g[5]
        # Use the console address we already know rather than whatever PASV
        # reports: behind a proxy the reported host can be unroutable.
        return socks_connect(self.host, port, self.proxy)

    # -- operations -----------------------------------------------
    def pwd(self):
        line = self._cmd("PWD", 257)
        m = re.search(r'"(.*)"', line)
        return m.group(1) if m else "/"

    def cwd(self, path):
        # This server answers CWD with 200, not the standard 250.
        self._cmd(f"CWD {path}", (200, 250))
        return self.pwd()

    def noop(self):
        self._cmd("NOOP", (200, 250))

    def listdir(self, path=None):
        """Return [Entry] for path, or the current directory.

        Navigates with CWD and then lists with a bare LIST. This server
        answers "550 No such device" to LIST with a path argument, even for
        "/", so never pass one.
        """
        if path:
            self.cwd(path)
        data = self._pasv()
        try:
            self._send("LIST")
            self._expect((125, 150))
            raw = b""
            while True:
                chunk = data.recv(8192)
                if not chunk:
                    break
                raw += chunk
        finally:
            # Close even if the server rejects LIST (e.g. 550): otherwise the
            # PASV socket -- and its tunnel channel and Pi-side thread -- leak.
            data.close()
        self._expect((226, 250))
        return [e for e in (self._parse_entry(l)
                            for l in raw.decode("utf-8", "replace").splitlines())
                if e]

    @staticmethod
    def _parse_entry(line):
        # Unix-style listing: perms links owner group size month day time|year name
        parts = line.split(None, 8)
        if len(parts) < 9 or not parts[0] or parts[0][0] not in "-dl":
            return None
        name, link = parts[8], ""
        if parts[0].startswith("l") and " -> " in name:
            name, link = name.split(" -> ", 1)
        if name in (".", ".."):
            return None
        try:
            size = int(parts[4])
        except ValueError:
            size = 0
        return Entry(name, parts[0].startswith("d"), size,
                     Ftp._parse_date(parts[5], parts[6], parts[7]), link)

    @staticmethod
    def _parse_date(mon, day, tail):
        try:
            month = MONTHS[mon[:3].lower()]
            day = int(day)
            now = datetime.datetime.now()
            if ":" in tail:
                hh, mm = (int(x) for x in tail.split(":", 1))
                dt = datetime.datetime(now.year, month, day, hh, mm)
                # "Mon DD HH:MM" means within the last six months.
                if dt > now + datetime.timedelta(days=2):
                    dt = dt.replace(year=now.year - 1)
                return dt
            return datetime.datetime(int(tail), month, day)
        except (KeyError, ValueError):
            return None

    def _split(self, path):
        """CWD into the parent and return the bare name.

        This server answers RETR, STOR and DELE with "450 failed to open
        file" when given a path, so every file operation has to be done from
        inside the directory with a plain name.
        """
        if "/" not in path:
            return path
        parent, _, name = path.rpartition("/")
        self.cwd(parent or "/")
        return name

    # Seconds to wait for the end-of-transfer reply (see _finish_transfer).
    RETR_WAIT = 8
    STOR_WAIT = 15

    def _finish_transfer(self, wait):
        """Read the end-of-transfer reply without hanging on it.

        If the far end of the data channel never learns the transfer ended
        (an old Pi tunnel did not propagate the close), the server never says
        226 and we used to sit until the 30 s socket timeout with the whole
        UI blocked. All bytes are already through at this point, so wait a
        few seconds at most, then start a fresh control connection -- the old
        one would deliver that late 226 as the answer to the next command.

        Returns True if the server confirmed the transfer.
        """
        old = self.ctl.gettimeout()
        self.ctl.settimeout(wait)
        try:
            self._expect((226, 250))
            return True
        except socket.timeout:
            pass
        finally:
            if self.ctl:
                self.ctl.settimeout(old)
        self.reconnect()
        return False

    def reconnect(self):
        try:
            self.ctl.close()
        except Exception:
            pass
        self.ctl = None
        self._buf = b""
        self.connect()

    def size(self, remote):
        """Remote file size via SIZE, or None if the server will not say.

        Needs binary mode (set in connect()). Some servers answer 550 for a
        directory or when SIZE is unsupported; the caller treats None as
        "unknown" rather than failing.
        """
        name = self._split(remote)
        try:
            line = self._cmd(f"SIZE {name}", 213)
        except FtpError:
            return None
        try:
            return int(line[3:].strip())
        except ValueError:
            return None

    def _rest(self, offset):
        """Ask to resume at offset. Returns True if the server agreed."""
        if not offset:
            return False
        try:
            self._cmd(f"REST {offset}", (350, 350))
            return True
        except FtpError:
            return False

    def retrieve(self, remote, fileobj, progress=None, size=None, offset=0):
        """Download remote into fileobj.

        With size given, stop as soon as that many bytes arrived (so a stalled
        tail does not hang). With offset given and the server supporting REST,
        resume there -- fileobj must already hold the first offset bytes and be
        positioned at the end.
        """
        remote = self._split(remote)
        resumed = self._rest(offset) if offset else False
        base = offset if resumed else 0
        if offset and not resumed:
            # No resume: start over from the top.
            fileobj.seek(0)
            fileobj.truncate()
        data = self._pasv()
        self._send(f"RETR {remote}")
        self._expect((125, 150))
        total = base
        try:
            while not (size and total >= size):
                chunk = data.recv(65536)
                if not chunk:
                    break
                fileobj.write(chunk)
                total += len(chunk)
                if progress:
                    progress(total)
        finally:
            data.close()
        self.confirmed = self._finish_transfer(self.RETR_WAIT)
        return total

    def store(self, local_fileobj, remote, progress=None, offset=0):
        """Upload local_fileobj to remote.

        With offset given and the server supporting REST, resume there --
        local_fileobj is seeked to offset first.
        """
        remote = self._split(remote)
        resumed = self._rest(offset) if offset else False
        base = offset if resumed else 0
        if offset:
            local_fileobj.seek(base)
        data = self._pasv()
        self._send(f"STOR {remote}")
        self._expect((125, 150))
        total = base
        try:
            while True:
                chunk = local_fileobj.read(32768)
                if not chunk:
                    break
                data.sendall(chunk)
                total += len(chunk)
                if progress:
                    progress(total)
            # Say "that's all" explicitly before closing.
            try:
                data.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        finally:
            data.close()
        self.confirmed = self._finish_transfer(self.STOR_WAIT)
        return total

    def move(self, remote, dest_dir):
        """Move remote into dest_dir with RNFR/RNTO (server-side, instant).

        Raises FtpError if the server refuses a path in RNTO; the caller can
        then fall back to copying through the PC.
        """
        name = self._split(remote)
        self._cmd(f"RNFR {name}", 350)
        target = (dest_dir.rstrip("/") + "/" + name) if dest_dir != "/" else "/" + name
        self._cmd(f"RNTO {target}", (250, 200))

    def delete(self, remote):
        self._cmd(f"DELE {self._split(remote)}", (250, 200))

    def mkdir(self, remote):
        self._cmd(f"MKD {self._split(remote)}", (257, 250, 200))

    def rmdir(self, remote):
        self._cmd(f"RMD {self._split(remote)}", (250, 200))

    def rename(self, remote, new_name):
        """Rename within the same directory (new_name is a bare name)."""
        name = self._split(remote)
        self._cmd(f"RNFR {name}", 350)
        self._cmd(f"RNTO {new_name}", (250, 200))

    def walk(self, remote):
        """Yield (path, Entry) for everything under remote, parents first."""
        stack = [remote.rstrip("/") or "/"]
        while stack:
            top = stack.pop()
            self.cwd(top)
            for e in self.listdir():
                path = (top.rstrip("/") + "/" + e.name)
                yield path, e
                if e.is_dir and not e.link:
                    stack.append(path)

    def rmtree(self, remote, on_item=None):
        """Delete a directory and everything in it."""
        items = list(self.walk(remote))
        for path, e in reversed(items):     # children before parents
            if on_item:
                on_item(path)
            if e.is_dir and not e.link:
                self.rmdir(path)
            else:
                self.delete(path)
        self.rmdir(remote)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--proxy-host", default=DEFAULT_PROXY[0])
    ap.add_argument("--proxy-port", type=int, default=DEFAULT_PROXY[1])
    ap.add_argument("--list", metavar="PATH", nargs="?", const="")
    ap.add_argument("--get", nargs=2, metavar=("REMOTE", "LOCAL"))
    ap.add_argument("--put", nargs=2, metavar=("LOCAL", "REMOTE"))
    args = ap.parse_args()

    ftp = Ftp(args.host, args.port, (args.proxy_host, args.proxy_port))
    try:
        ftp.connect()
        if args.list is not None:
            for e in ftp.listdir(args.list or None):
                kind = "dir " if e.is_dir else "file"
                when = e.mtime.strftime("%Y-%m-%d %H:%M") if e.mtime else " " * 16
                print(f"{kind}  {e.size:>12}  {when}  {e.name}")
        elif args.get:
            with open(args.get[1], "wb") as fh:
                n = ftp.retrieve(args.get[0], fh)
            print(f"{n} bytes -> {args.get[1]}")
        elif args.put:
            with open(args.put[0], "rb") as fh:
                n = ftp.store(fh, args.put[1])
            print(f"{n} bytes -> {args.put[1]}")
        else:
            print(f"connected, cwd {ftp.pwd()}")
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        ftp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
