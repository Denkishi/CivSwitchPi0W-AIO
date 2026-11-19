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
import re
import socket
import struct
import sys

DEFAULT_PROXY = ("127.0.0.1", 1080)
DEFAULT_HOST = "192.168.4.2"
DEFAULT_PORT = 1337

PASV_RE = re.compile(r"(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)")


class FtpError(RuntimeError):
    pass


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

    def listdir(self, path=None):
        """Return [(name, is_dir, size)] for path, or the current directory.

        Navigates with CWD and then lists with a bare LIST. This server
        answers "550 No such device" to LIST with a path argument, even for
        "/", so never pass one.
        """
        if path:
            self.cwd(path)
        data = self._pasv()
        self._send("LIST")
        self._expect((125, 150))
        raw = b""
        try:
            while True:
                chunk = data.recv(8192)
                if not chunk:
                    break
                raw += chunk
        finally:
            data.close()
        self._expect((226, 250))
        return [e for e in (self._parse_entry(l)
                            for l in raw.decode("utf-8", "replace").splitlines())
                if e]

    @staticmethod
    def _parse_entry(line):
        # Unix-style listing: perms links owner group size date... name
        parts = line.split(None, 8)
        if len(parts) < 9 or not parts[0][0] in "-dl":
            return None
        name = parts[8]
        if name in (".", ".."):
            return None
        try:
            size = int(parts[4])
        except ValueError:
            size = 0
        return name, parts[0].startswith("d"), size

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

    def retrieve(self, remote, fileobj, progress=None):
        remote = self._split(remote)
        data = self._pasv()
        self._send(f"RETR {remote}")
        self._expect((125, 150))
        total = 0
        try:
            while True:
                chunk = data.recv(8192)
                if not chunk:
                    break
                fileobj.write(chunk)
                total += len(chunk)
                if progress:
                    progress(total)
        finally:
            data.close()
        self._expect((226, 250))
        return total

    def store(self, local_fileobj, remote, progress=None):
        remote = self._split(remote)
        data = self._pasv()
        self._send(f"STOR {remote}")
        self._expect((125, 150))
        total = 0
        try:
            while True:
                chunk = local_fileobj.read(8192)
                if not chunk:
                    break
                data.sendall(chunk)
                total += len(chunk)
                if progress:
                    progress(total)
        finally:
            data.close()
        self._expect((226, 250))
        return total

    def delete(self, remote):
        self._cmd(f"DELE {self._split(remote)}", (250, 200))

    def mkdir(self, remote):
        self._cmd(f"MKD {remote}", (257, 250))

    def rmdir(self, remote):
        self._cmd(f"RMD {remote}", (250, 200))


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
            for name, is_dir, size in ftp.listdir(args.list or None):
                kind = "dir " if is_dir else "file"
                print(f"{kind}  {size:>12}  {name}")
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
