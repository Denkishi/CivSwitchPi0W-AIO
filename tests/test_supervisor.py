"""Hardware-free checks for the Pi supervisor's DIAG and UPDATE commands.

Runs anywhere (no serial port, no Tk, no Raspberry Pi): the supervisor module
is imported from a temp copy so UPDATE writes land in the temp dir, and the
diagnostics probes are best-effort so they return None off a Pi rather than
raising. Run with `pytest` or directly with `python tests/test_supervisor.py`.
"""

import hashlib
import importlib.util
import os
import shutil
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_supervisor(pidir):
    for n in ("supervisor.py", "tunnel.py", "bridge_mode.py"):
        src = os.path.join(REPO, "pi", n)
        if os.path.exists(src):
            shutil.copy(src, pidir)
    spec = importlib.util.spec_from_file_location(
        "sup_under_test", os.path.join(pidir, "supervisor.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeSerial:
    def __init__(self):
        self.inp = b""
        self.out = bytearray()
        self.is_open = True

    def feed(self, b):
        self.inp += b

    def read(self, n=1):
        d = self.inp[:n]
        self.inp = self.inp[n:]
        return d

    def write(self, d):
        self.out += bytes(d)
        return len(d)

    def reset_output_buffer(self):
        pass


def _make(sup, mode="bridge"):
    sv = sup.Supervisor.__new__(sup.Supervisor)
    sv.mode = mode
    sv._ser = _FakeSerial()
    sv._host_seen = True
    sv._running = True
    sv.bridge_proc = None
    return sv


def _out(sv):
    return bytes(sv._ser.out).decode("utf-8", "replace")


def run():
    tmp = tempfile.mkdtemp()
    pidir = os.path.join(tmp, "pi")
    os.makedirs(pidir)
    sup = _load_supervisor(pidir)
    try:
        # DIAG never raises and carries the keys the GUI reads.
        d = _make(sup).get_diag()
        assert isinstance(d, dict)
        for k in ("mode", "ts", "temp_c", "load", "uptime_s", "mem_total_mb",
                  "disk_free_mb", "throttled", "clients", "lan_ip", "ap_ip",
                  "hostname", "files"):
            assert k in d, f"DIAG missing {k}"
        assert "tunnel.py" in d["files"]

        def do_update(name, data, sha=None):
            sv = _make(sup)
            sv._ser.feed(data)
            sv.apply_update(name, len(data),
                            sha if sha is not None
                            else hashlib.sha256(data).hexdigest())
            return sv

        tunnel = os.path.join(pidir, "tunnel.py")
        orig = open(tunnel, "rb").read()

        # A valid .py update replaces the file, keeps a .bak, and acks OK.
        new = b"# patched\nX = 1\n"
        sv = do_update("tunnel.py", new)
        assert "OK updated tunnel.py" in _out(sv)
        assert open(tunnel, "rb").read() == new
        assert open(tunnel + ".bak", "rb").read() == orig
        shutil.copy(tunnel + ".bak", tunnel)  # restore

        # A sha mismatch is rejected and the file is left alone.
        sv = do_update("tunnel.py", b"Y = 2\n", sha="0" * 64)
        assert "ERR sha mismatch" in _out(sv)
        assert open(tunnel, "rb").read() == orig

        # A payload that will not compile is rejected, file untouched, no temp.
        sv = do_update("tunnel.py", b"def (:\n  bad\n")
        assert "ERR syntax check failed" in _out(sv)
        assert open(tunnel, "rb").read() == orig
        assert not os.path.exists(os.path.join(pidir, ".tunnel.py.incoming"))

        # A name outside the whitelist is refused and the stream is drained.
        sv = _make(sup)
        junk = b"ABCDEFG"
        sv._ser.feed(junk)
        sv.apply_update("evil.py", len(junk),
                        hashlib.sha256(junk).hexdigest())
        assert "ERR name not updatable" in _out(sv)
        assert sv._ser.inp == b""

        # Power actions reply at once and run systemctl off-thread.
        calls = []
        sup.subprocess.run = lambda *a, **k: calls.append(a[0])
        sv = _make(sup)
        t0 = time.monotonic()
        sv.deferred_system("restart")
        sv.deferred_system("reboot")
        assert time.monotonic() - t0 < 0.5
        time.sleep(2.0)
        assert any("restart" in c for c in calls)
        assert any("reboot" in c for c in calls)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_supervisor():
    run()


if __name__ == "__main__":
    run()
    print("all supervisor checks passed")
