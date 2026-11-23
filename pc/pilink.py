"""Shared helpers for talking to the Pi supervisor over the serial link.

Used by the GUI and by the tunnel scripts so that every way out -- closing the
window, stopping a tunnel, or the GUI dying -- leaves the Pi in a known state.
"""

import json
import os
import sys
import threading
import time


def _write(ser, data):
    try:
        ser.write(data)
        return True
    except Exception:
        return False


def _drain(ser, timeout):
    """Wait for queued bytes to leave, with a bound.

    pyserial's flush() on Windows loops on out_waiting with no timeout, so a
    Pi that is not reading would hang us there forever.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if not ser.out_waiting:
                return
        except Exception:
            return
        time.sleep(0.05)


def stop_bridge(ser, timeout=8.0, log=None):
    """Ask the supervisor to stop the bridge, whatever state the link is in.

    The Pi may still be in tunnel mode, where the first text line is eaten as
    junk (it makes the tunnel hand the port back). So keep sending INFO until a
    JSON reply proves we are in line mode, and only then decide. If nothing
    answers in time -- e.g. the Pi is busy starting the bridge and is not
    reading -- STOP is sent anyway: it sits in the Pi's input buffer and runs
    as soon as the current command finishes.

    Returns the mode the Pi reported ("idle", "bridge") or None if unknown.
    """
    def say(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass            # stdout may be a dead pipe; never block STOP
    try:
        ser.timeout = 0.2
        ser.write_timeout = 3
    except Exception:
        pass
    try:
        ser.reset_input_buffer()
    except Exception:
        pass

    deadline = time.monotonic() + timeout
    mode = None
    buf = b""
    while mode is None and time.monotonic() < deadline:
        if not _write(ser, b"INFO\n"):
            break
        wait_until = min(time.monotonic() + 1.5, deadline)
        while mode is None and time.monotonic() < wait_until:
            try:
                chunk = ser.read(ser.in_waiting or 1)
            except Exception:
                chunk = b""
                break
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line.startswith(b"{"):
                    try:
                        info = json.loads(line.decode("utf-8", "replace"))
                        mode = info.get("mode") or "?"
                    except (ValueError, AttributeError):
                        pass
            if len(buf) > 65536:
                buf = b""

    if mode == "idle":
        say("bridge already stopped")
        return mode
    _write(ser, b"STOP\n")
    _drain(ser, 2.0)
    time.sleep(0.3)
    say("bridge stop sent" if mode else "bridge stop sent (Pi did not answer, queued)")
    return mode


def watch_parent(on_exit, hard_timeout=15.0):
    """Exit cleanly when the parent says so or goes away.

    The GUI starts the tunnels with stdin as a pipe. It writes "quit" (stop
    the tunnel, keep the bridge) or "stop" (also stop the bridge). If the GUI
    dies, the pipe closes and readline() returns "" -- treated as "stop", so a
    crashed GUI never leaves an invisible process holding the COM port.

    on_exit(stop_bridge: bool) runs once; the process always exits afterwards,
    and a timer kills it if cleanup itself hangs.
    """
    def run():
        cmd = ""
        try:
            cmd = (sys.stdin.readline() or "").strip().lower()
        except Exception:
            pass
        killer = threading.Timer(hard_timeout, os._exit, (1,))
        killer.daemon = True
        killer.start()
        try:
            on_exit(cmd != "quit")
        except Exception as e:
            try:
                print(f"cleanup failed: {e}", flush=True)
            except Exception:
                pass
        finally:
            try:
                sys.stdout.flush()
            except Exception:
                pass
            os._exit(0)

    threading.Thread(target=run, daemon=True).start()
