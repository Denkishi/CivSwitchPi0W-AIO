#!/usr/bin/env python3
"""CivSwitch Pi Zero W -- control GUI.

One window for everything the Pi does: bridge control, sending JS payloads,
raw protocol commands, a tunnel for console tools, and a file browser for the
console's SD card.

Threading rules (these are what keep the window from freezing or crashing):
  * The Tk main thread never does serial, socket or process I/O that can
    block. All of it runs on worker threads.
  * Worker threads never touch Tk. They post callbacks to self._q, which the
    main thread drains every 30 ms (self._ui()).

Requires: pip install pyserial
"""

import faulthandler
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import ttk, filedialog, messagebox, scrolledtext

import serial
import serial.tools.list_ports

import ftp_browser
import pi_tab
import gui_extras
import payload_tab
import pilink
import win_drop

BAUD = 115200
DEFAULT_IP = "192.168.4.2"
DEFAULT_PORT = "9026"
FTP_PORT = "1337"
SOCKS_PORT = 1080
INSTANCE_PORT = 47826       # localhost port held while the GUI runs

# Linux Foundation / Gadget Serial -- what g_serial presents on the Pi.
GADGET = (0x0525, 0xA4A7)

HERE = os.path.dirname(os.path.abspath(__file__))
TCP_PROXY = os.path.join(HERE, "tcp_proxy.py")
SOCKS_TUNNEL = os.path.join(HERE, "socks_tunnel.py")

# When bundled by PyInstaller, __file__ lives in a throwaway temp dir, so
# settings and logs must go next to the .exe instead -- otherwise they vanish
# on exit. Running from source, that is just the pc/ folder.
FROZEN = getattr(sys, "frozen", False)
APPDIR = os.path.dirname(sys.executable) if FROZEN else HERE
LOG_FILE = os.path.join(APPDIR, "civswitch_gui.log")
PREFS_FILE = os.path.join(APPDIR, "civswitch_prefs.json")


def _load_pref(key, default):
    try:
        with open(PREFS_FILE, encoding="utf-8") as fh:
            return json.load(fh).get(key, default)
    except Exception:
        return default


def _save_pref(key, value):
    try:
        data = {}
        try:
            with open(PREFS_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            pass
        data[key] = value
        with open(PREFS_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        pass

# Both tunnels need the COM port to themselves, so only one runs at a time.
TUNNELS = {
    "TCP relay (for npm tools)": TCP_PROXY,
    "SOCKS5 (for FTP and browsers)": SOCKS_TUNNEL,
}

# Supervisor lines that mean a BRIDGE/STOP command has finished. While one is
# running the Pi does not read the serial port at all (it can take minutes),
# so the GUI must not pile up more commands behind it.
DONE_MARKERS = ("Bridge mode active", "Bridge failed", "All stopped")
BUSY_MARKERS = {"Autostarting bridge": "start"}
PENDING_TIMEOUT = 150
LOG_LINES = 3000

PAD = 8


def _diag(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass


class SerialLink:
    """Owns one open COM port: a reader thread and a writer thread.

    Writes are queued, so a Pi that is busy (and not reading) can never stall
    the UI. shutdown() blocks and must be called from a worker thread.
    """

    def __init__(self, dev):
        self.dev = dev
        self.ser = serial.Serial(dev, BAUD, timeout=0.2, write_timeout=5)
        self.stop = threading.Event()
        self.wq = queue.Queue()
        self._rt = self._wt = None

    def start(self, on_line, on_lost, on_note):
        self.on_line, self.on_lost, self.on_note = on_line, on_lost, on_note
        self._rt = threading.Thread(target=self._reader, daemon=True)
        self._wt = threading.Thread(target=self._writer, daemon=True)
        self._rt.start()
        self._wt.start()

    def send(self, *parts):
        """Queue bytes to write; a float in parts is a pause in seconds."""
        if not self.stop.is_set():
            self.wq.put(parts)

    def _writer(self):
        while True:
            job = self.wq.get()
            if job is None or self.stop.is_set():
                return
            try:
                for p in job:
                    if isinstance(p, float):
                        time.sleep(p)
                    else:
                        self.ser.write(p)
            except serial.SerialTimeoutException:
                self.on_note("write timed out: the Pi is not reading "
                             "(still busy with the previous command?)")
            except Exception as e:
                if not self.stop.is_set():
                    self.on_lost(f"serial write failed: {e}")
                return

    def _reader(self):
        buf = b""
        while not self.stop.is_set():
            try:
                chunk = self.ser.read(self.ser.in_waiting or 1)
            except Exception as e:
                if not self.stop.is_set():
                    self.on_lost(f"serial lost: {e}")
                return
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").strip()
                if line and line != "END":
                    self.on_line(line)
            if len(buf) > 65536:
                buf = b""

    def shutdown(self, stop_bridge=False, log=None):
        self.stop.set()
        self.wq.put(None)
        for name in ("cancel_write", "cancel_read"):
            try:
                getattr(self.ser, name)()
            except Exception:
                pass
        for t in (self._rt, self._wt):
            if t and t is not threading.current_thread():
                t.join(1.5)
        mode = None
        if stop_bridge:
            try:
                mode = pilink.stop_bridge(self.ser, log=log)
            except Exception as e:
                if log:
                    log(f"stop bridge failed: {e}")
        try:
            self.ser.close()
        except Exception:
            pass
        return mode


def end_child(proc, cmd, wait):
    """Ask a tunnel process to exit; escalate if it does not. Blocking.

    Returns True if it exited on its own (so it ran its own cleanup).
    """
    try:
        proc.stdin.write(cmd + "\n")
        proc.stdin.flush()
    except Exception:
        pass
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=wait)
        return True
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass
    return False


class App:
    def __init__(self, root):
        self.root = root
        root.title("CivSwitch Bridge")
        # Sizes are in pixels; scale them for high-DPI screens.
        k = max(1.0, root.winfo_fpixels("1i") / 96.0)
        root.geometry(f"{int(820 * k)}x{int(820 * k)}")
        root.minsize(int(680 * k), int(640 * k))

        self._q = queue.Queue()
        self.link = None            # SerialLink while we own the COM port
        self._link_busy = False     # opening or closing a port
        self.mode = "?"
        self.pending = None         # "start" | "stop" while the Pi is busy
        self.pending_deadline = 0.0
        self.infos_out = 0          # INFOs sent and not yet answered
        self.info_sent = 0.0        # when the last INFO went out
        self._poll_gen = 0
        self.ports = []
        self.proxy = None           # Popen while a tunnel owns the COM port
        self._proxy_busy = False
        self.proxy_socks = False    # the running tunnel is the SOCKS one
        self.socks_ready = False    # ...and it is accepting connections
        self._closing = False
        self._last_devices = set()  # COM devices seen, for auto-reconnect
        self.auto_serial = True     # reconnect when the Pi is replugged
        self._payload_until = 0.0   # mirror replies to the Payload tab until

        self.dark = tk.BooleanVar(value=_load_pref("dark", False))
        self.colors = gui_extras.apply_theme(root, self.dark.get())
        self._build()
        root.report_callback_exception = self._tk_error
        self._apply_theme()
        self._drain()
        self._scan_ports()
        self._last_devices = {p.device for p in self.ports}
        # Files dragged in from Explorer. The window procedure only queues
        # them; the main loop handles them.
        win_drop.enable(root, lambda paths, pt: self._ui(self._on_os_drop,
                                                        paths, pt))
        self.root.after(1500, self._watch_ports)
        self.root.after(5000, self._pi_auto)

    # -- thread hand-off ------------------------------------------
    def _ui(self, fn, *args):
        """Run fn(*args) on the Tk thread. Safe from any thread."""
        self._q.put((fn, args))

    def _drain(self):
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            try:
                fn, args = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:
                self._tk_error(*sys.exc_info())
        try:
            self.root.after(30, self._drain)
        except tk.TclError:
            pass

    def _tk_error(self, exc, val, tb):
        text = "".join(traceback.format_exception(exc, val, tb))
        _diag(text)
        try:
            self._say(f"internal error: {val} (details in {os.path.basename(LOG_FILE)})",
                      "err")
        except Exception:
            pass

    def _bg(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    # -- layout ---------------------------------------------------
    def _build(self):
        root = self.root

        bar = ttk.Frame(root, padding=(PAD, PAD, PAD, 0))
        bar.pack(fill=tk.X)
        self.port = tk.StringVar()
        self.port_box = ttk.Combobox(bar, textvariable=self.port, width=22,
                                     state="readonly")
        self.port_box.pack(side=tk.LEFT)
        self.scan_btn = ttk.Button(bar, text="↻", width=3,
                                   command=self._scan_ports)
        self.scan_btn.pack(side=tk.LEFT, padx=4)
        self.conn_btn = ttk.Button(bar, text="Connect", command=self._toggle_conn)
        self.conn_btn.pack(side=tk.LEFT, padx=4)
        self.state_lbl = ttk.Label(bar, text="● not connected",
                                   foreground="#b33")
        self.state_lbl.pack(side=tk.RIGHT)
        self.theme_btn = ttk.Button(bar, text="🌙" if not self.dark.get() else "☀",
                                    width=3, command=self._toggle_theme)
        self.theme_btn.pack(side=tk.RIGHT, padx=(0, 8))

        bridge = ttk.Frame(root, padding=(PAD, PAD, PAD, 0))
        bridge.pack(fill=tk.X)
        self.bridge_btn = ttk.Button(bridge, text="Start bridge",
                                     command=self._toggle_bridge,
                                     state=tk.DISABLED)
        self.bridge_btn.pack(side=tk.LEFT)
        self.info_lbl = ttk.Label(bridge, text="")
        self.info_lbl.pack(side=tk.LEFT, padx=PAD)
        self.stop_on_exit = tk.BooleanVar(value=True)
        ttk.Checkbutton(bridge, text="Stop bridge on exit",
                        variable=self.stop_on_exit).pack(side=tk.RIGHT)

        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill=tk.BOTH, expand=True, padx=PAD, pady=(PAD, 0))
        tab = ttk.Frame(self.tabs, padding=(0, 0, 0, PAD))
        self.tabs.add(tab, text="  Bridge  ")

        box = ttk.LabelFrame(tab, text="Payload", padding=PAD)
        box.pack(fill=tk.X, padx=PAD, pady=(PAD, 0))
        row = ttk.Frame(box)
        row.pack(fill=tk.X)
        ttk.Label(row, text="Console").pack(side=tk.LEFT)
        self.ip = tk.StringVar(value=DEFAULT_IP)
        ttk.Entry(row, textvariable=self.ip, width=16).pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="port").pack(side=tk.LEFT, padx=(4, 2))
        self.tcp = tk.StringVar(value=DEFAULT_PORT)
        ttk.Entry(row, textvariable=self.tcp, width=7).pack(side=tk.LEFT)
        row2 = ttk.Frame(box)
        row2.pack(fill=tk.X, pady=(6, 0))
        self.file = tk.StringVar()
        ttk.Entry(row2, textvariable=self.file).pack(
            side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row2, text="…", width=3,
                   command=self._pick).pack(side=tk.LEFT, padx=4)
        self.send_btn = ttk.Button(row2, text="Send", command=self._send,
                                   state=tk.DISABLED)
        self.send_btn.pack(side=tk.LEFT)

        # A tunnel takes the COM port off us while it runs.
        tbox = ttk.LabelFrame(tab, text="Tunnel", padding=PAD)
        tbox.pack(fill=tk.X, padx=PAD, pady=(PAD, 0))
        trow = ttk.Frame(tbox)
        trow.pack(fill=tk.X)
        self.tunnel_kind = tk.StringVar(value=list(TUNNELS)[0])
        self.tunnel_combo = ttk.Combobox(trow, textvariable=self.tunnel_kind, width=27,
                                         state="readonly",
                                         values=list(TUNNELS))
        self.tunnel_combo.pack(side=tk.LEFT)
        self.tunnel_combo.bind("<<ComboboxSelected>>", self._on_tunnel_kind_changed)

        ttk.Label(trow, text="port").pack(side=tk.LEFT, padx=(6, 2))
        self.tunnel_port = tk.StringVar(value=DEFAULT_PORT)
        self.tunnel_port_combo = ttk.Combobox(trow, textvariable=self.tunnel_port,
                                              values=["9026", "8080"], width=6)
        self.tunnel_port_combo.pack(side=tk.LEFT, padx=2)

        self.proxy_btn = ttk.Button(trow, text="Start", command=self._toggle_proxy)
        self.proxy_btn.pack(side=tk.LEFT, padx=6)
        self.proxy_lbl = ttk.Label(trow, text="stopped", foreground="#888")
        self.proxy_lbl.pack(side=tk.LEFT, padx=PAD)

        cmd = ttk.Frame(tab, padding=(PAD, PAD, PAD, 0))
        cmd.pack(fill=tk.X)
        ttk.Label(cmd, text="❯").pack(side=tk.LEFT)
        self.cmd = tk.StringVar()
        self.cmd_entry = ttk.Entry(cmd, textvariable=self.cmd, state=tk.DISABLED)
        self.cmd_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.cmd_entry.bind("<Return>", lambda _e: self._send_cmd())
        self.cmd_btn = ttk.Button(cmd, text="Run", command=self._send_cmd,
                                  state=tk.DISABLED)
        self.cmd_btn.pack(side=tk.LEFT)

        self.browser = ftp_browser.FileBrowser(self.tabs, self)
        self.tabs.add(self.browser, text="  Files  ")

        self.payload = payload_tab.PayloadTab(self.tabs, self)
        self.tabs.add(self.payload, text="  Payload  ")

        self.pi = pi_tab.PiTab(self.tabs, self)
        self.tabs.add(self.pi, text="  Pi  ")

        self.log = scrolledtext.ScrolledText(root, height=7, wrap=tk.WORD,
                                             font=("Consolas", 9),
                                             state=tk.DISABLED, relief=tk.FLAT)
        self.log.pack(fill=tk.X, padx=PAD, pady=PAD)
        self.log.tag_configure("err", foreground="#b33")
        self.log.tag_configure("dim", foreground="#888")
        self.log.tag_configure("me", foreground="#36c")

    # -- ports ----------------------------------------------------
    def _scan_ports(self):
        self.ports = sorted(serial.tools.list_ports.comports(),
                            key=lambda p: p.device)
        self._fill_ports()

    def _fill_ports(self, keep=None):
        """Refresh the combobox from self.ports, keeping `keep` selected."""
        self.port_box["values"] = [
            p.device + ("  (Pi)" if (p.vid, p.pid) == GADGET else "")
            for p in self.ports
        ]
        if not self.ports:
            return
        if keep:
            for i, p in enumerate(self.ports):
                if p.device == keep:
                    self.port_box.current(i)
                    return
        # Pick the Pi by numeric VID/PID; on Windows its description is just
        # a generic "USB Serial Device".
        for i, p in enumerate(self.ports):
            if (p.vid, p.pid) == GADGET:
                self.port_box.current(i)
                return
        self.port_box.current(0)

    def _selected_port(self):
        i = self.port_box.current()
        return self.ports[i].device if 0 <= i < len(self.ports) else None

    # -- connection -----------------------------------------------
    def _toggle_conn(self):
        if self._link_busy:
            return
        self._disconnect() if self.link else self._connect()

    def _connect(self):
        if self.proxy or self._proxy_busy:
            self._say("A tunnel holds the port. Stop it first.", "err")
            return
        if self.link or self._link_busy or self._closing:
            return
        dev = self._selected_port()
        if not dev:
            self._say("Select a port.", "err")
            return
        self._link_busy = True
        self.conn_btn.config(state=tk.DISABLED, text="Connecting…")
        self._status("connecting…", "#555")

        def work():
            try:
                link = SerialLink(dev)
            except Exception as e:
                self._ui(self._connect_failed, dev, e)
                return
            self._ui(self._connected, link)
        self._bg(work)

    def _connect_failed(self, dev, e):
        self._link_busy = False
        self.conn_btn.config(state=tk.NORMAL, text="Connect")
        self._status("not connected", "#b33")
        # A serial port takes a single reader, and "Access is denied" says
        # nothing about who has it. Name the usual suspects instead.
        if isinstance(e, PermissionError) or "ermission" in str(e) \
                or "ccesso negato" in str(e) or "denied" in str(e):
            self._say(f"{dev} is held by another process: a tunnel left running "
                      f"(python socks_tunnel.py / tcp_proxy.py), another copy of "
                      f"this GUI, or a serial terminal. Unplugging and replugging "
                      f"the Pi frees it.", "err")
        else:
            self._say(str(e), "err")

    def _connected(self, link):
        self._link_busy = False
        if self._closing:
            self._bg(link.shutdown)
            return
        self.link = link
        self.mode = "?"
        self.pending = None
        self.infos_out = 0
        link.start(
            on_line=lambda line: self._ui(self._on_line, link, line),
            on_lost=lambda msg: self._ui(self._link_lost, link, msg),
            on_note=lambda msg: self._ui(self._say, msg, "dim"),
        )
        self.conn_btn.config(state=tk.NORMAL, text="Disconnect")
        for w in (self.cmd_entry, self.cmd_btn):
            w.config(state=tk.NORMAL)
        self._status("connected", "#555")
        self.info_lbl.config(text="waiting for status…")
        self._refresh_controls()
        self._say(f"connected to {link.dev}", "dim")
        self._poll_gen += 1
        self.root.after(300, self._poll, self._poll_gen)
        if hasattr(self, "pi"):
            self.pi.on_connect(True)

    def _link_lost(self, link, msg):
        if link is not self.link:
            return
        self._say(msg, "err")
        self._disconnect()

    def _disconnect(self, then=None, stop_bridge=False):
        """Release the port. `then` runs on the UI thread once it is free."""
        link, self.link = self.link, None
        self._poll_gen += 1
        self.mode = "?"
        self.pending = None
        self.infos_out = 0
        for w in (self.send_btn, self.cmd_entry, self.cmd_btn):
            w.config(state=tk.DISABLED)
        self.info_lbl.config(text="")
        self._refresh_controls()
        if hasattr(self, "pi"):
            self.pi.on_connect(False)
        if not link:
            self.conn_btn.config(state=tk.NORMAL, text="Connect")
            self._status("not connected", "#b33")
            if then:
                then()
            return
        self._link_busy = True
        self.conn_btn.config(state=tk.DISABLED, text="Closing…")

        def work():
            log = (lambda m: self._ui(self._say, m, "dim")) if stop_bridge else None
            link.shutdown(stop_bridge=stop_bridge, log=log)
            time.sleep(0.5)     # let Windows really release the handle
            self._ui(self._disconnected, then)
        self._bg(work)

    def _disconnected(self, then):
        self._link_busy = False
        self.conn_btn.config(state=tk.NORMAL if not self.proxy else tk.DISABLED,
                             text="Connect")
        if not self.proxy:
            self._status("not connected", "#b33")
        if then:
            then()

    def _on_line(self, link, line):
        if link is not self.link:
            return
        if line.startswith("{"):
            try:
                info = json.loads(line)
                if isinstance(info, dict) and "ts" in info and "files" in info:
                    # A DIAG blob, not an INFO: it carries a timestamp and the
                    # file-version map. Route it to the Pi tab and stop.
                    if hasattr(self, "pi"):
                        self.pi.on_diag(info)
                    return
                if isinstance(info, dict) and "mode" in info:
                    self.infos_out = max(0, self.infos_out - 1)
                    # A BRIDGE/STOP is always followed by an INFO, and the Pi
                    # answers in order: once every INFO is answered, the
                    # command is done, even if its text marker was missed.
                    if self.pending and self.infos_out == 0:
                        self.pending = None
                    self._apply_info(info)
                    return
            except ValueError:
                pass
        for marker, kind in BUSY_MARKERS.items():
            if marker in line:
                self._set_pending(kind)
        if self.pending and any(m in line for m in DONE_MARKERS):
            self.pending = None
            self._refresh_controls()
        # Mirror a payload's reply into the Payload tab for a short window
        # after it was sent (the loader's output comes back as log lines).
        if (self._payload_until and time.monotonic() < self._payload_until
                and hasattr(self, "payload")):
            self.payload.on_reply(line)
        self._say(line, "err" if "[ERROR]" in line
                  or line.startswith("ERR") else None)

    def _apply_info(self, info):
        self.mode = info.get("mode", "?")
        bridging = self.mode == "bridge"
        clients = info.get("clients", 0)
        lan_ip = info.get("lan_ip")

        parts = [self.mode]
        if bridging:
            parts.append(f"{clients} client(s)")
        if lan_ip:
            # The Pi reports its own address, so there is nothing to look up
            # and no need for a static lease.
            parts.append(f"ssh pi@{lan_ip}")
        self.info_lbl.config(text="  ·  ".join(parts))
        self._refresh_controls()
        if self.pending:
            return
        if bridging:
            self._status(f"AP {info.get('ap_ip', '')}", "#2a7")
        else:
            self._status("connected", "#555")

    def _set_pending(self, kind):
        self.pending = kind
        self.pending_deadline = time.monotonic() + PENDING_TIMEOUT
        self._status("Pi busy…", "#c80")
        self._refresh_controls()

    def _refresh_controls(self):
        """Bridge/Send buttons follow the link, the mode and any pending command."""
        if not self.link:
            self.bridge_btn.config(state=tk.DISABLED, text="Start bridge")
            self.send_btn.config(state=tk.DISABLED)
            return
        if self.pending:
            text = "Starting bridge…" if self.pending == "start" else "Stopping bridge…"
            self.bridge_btn.config(state=tk.DISABLED, text=text)
            self.send_btn.config(state=tk.DISABLED)
            return
        bridging = self.mode == "bridge"
        # Unknown mode: wait for the first INFO. Sending BRIDGE while the Pi
        # is already bridging makes it tear everything down first (minutes).
        self.bridge_btn.config(
            state=tk.NORMAL if self.mode != "?" else tk.DISABLED,
            text="Stop bridge" if bridging else "Start bridge")
        self.send_btn.config(state=tk.NORMAL if bridging else tk.DISABLED)

    def _command(self, line, pending=None):
        """Send a line command; BRIDGE/STOP get an INFO queued right behind."""
        if not self.link:
            return
        data = (line + "\n").encode()
        if pending:
            self._set_pending(pending)
            data += b"INFO\n"
            self.infos_out += 1
            self.info_sent = time.monotonic()
        self.link.send(data)

    def _ask_info(self):
        if self.link and not self.pending:
            self.infos_out += 1
            self.info_sent = time.monotonic()
            self.link.send(b"INFO\n")

    def _poll(self, gen):
        if gen != self._poll_gen or not self.link or self._closing:
            return
        now = time.monotonic()
        if self.pending and now > self.pending_deadline:
            self._say("the Pi did not report back; asking for status again", "err")
            self.pending = None
            self.infos_out = 0
            self._refresh_controls()
        if not self.pending:
            # Never stack INFOs behind unanswered ones -- except while the
            # mode is still unknown: a Pi left in tunnel mode eats the first
            # line, so keep asking every couple of seconds until it answers.
            stale = 2.5 if self.mode == "?" else 30
            if not self.infos_out or now - self.info_sent > stale:
                self.infos_out = 0
                self._ask_info()
        self.root.after(2500 if self.mode == "?" else 5000, self._poll, gen)

    # -- commands -------------------------------------------------
    def _send_cmd(self):
        line = self.cmd.get().strip()
        if not line or not self.link:
            return
        self._say(f"❯ {line}", "me")
        # The Pi matches commands exactly (case-sensitive).
        self._command(line, {"BRIDGE": "start", "STOP": "stop"}.get(line))
        self.cmd.set("")

    def _toggle_bridge(self):
        if not self.link or self.pending or self.mode == "?":
            return
        if self.mode == "bridge":
            self._say("stopping bridge (this can take a minute)…", "dim")
            self._command("STOP", "stop")
        else:
            self._say("starting bridge (up to ~30 s)…", "dim")
            self._command("BRIDGE", "start")

    def _pick(self):
        path = filedialog.askopenfilename(
            filetypes=[("JavaScript", "*.js"), ("All files", "*.*")])
        if path:
            self.file.set(path)

    def _send(self):
        if not self.link:
            return
        path = self.file.get()
        if not os.path.isfile(path):
            self._say("Pick a payload file.", "err")
            return
        try:
            port = int(self.tcp.get())
        except ValueError:
            self._say("Invalid TCP port.", "err")
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = fh.read().encode("utf-8")
        except Exception as e:
            self._say(str(e), "err")
            return
        self._say(f"sending {len(data)} bytes to {self.ip.get()}:{port}", "dim")
        self.link.send(f"SEND {self.ip.get()} {port} {len(data)}\n".encode(),
                       0.1, data)

    # -- tunnel ---------------------------------------------------
    def _on_tunnel_kind_changed(self, _event=None):
        kind = self.tunnel_kind.get()
        if "SOCKS" in kind:
            self.tunnel_port_combo["values"] = ["1080"]
            self.tunnel_port.set(str(SOCKS_PORT))
        else:
            self.tunnel_port_combo["values"] = ["9026", "8080"]
            if self.tunnel_port.get() == str(SOCKS_PORT):
                self.tunnel_port.set(self.tcp.get() or DEFAULT_PORT)

    def _toggle_proxy(self):
        if self._proxy_busy:
            return
        self._stop_proxy() if self.proxy else self._start_proxy()

    def _start_proxy(self):
        dev = self._selected_port()
        if not dev:
            self._say("Select a port first.", "err")
            return
        script = TUNNELS[self.tunnel_kind.get()]
        # Frozen: the tunnel is bundled as a module, not a .py on disk.
        if not FROZEN and not os.path.isfile(script):
            self._say(f"{os.path.basename(script)} not found next to this file.",
                      "err")
            return
        if self.mode != "bridge" or self.pending:
            self._say("Start the bridge first (and wait for it): the tunnel "
                      "needs it.", "err")
            return

        port_str = self.tunnel_port.get().strip()
        try:
            port_num = int(port_str)
            if not (1 <= port_num <= 65535):
                raise ValueError
        except ValueError:
            self._say("Invalid tunnel port (1-65535).", "err")
            return

        socks = script == SOCKS_TUNNEL
        if FROZEN:
            # No python and no .py on disk: re-launch our own .exe in tunnel
            # mode (handled at the top of main()).
            cmd = [sys.executable, "--run-tunnel", "socks" if socks else "tcp"]
        else:
            cmd = [sys.executable, "-u", script]
        cmd += ["--port", dev, "--console", self.ip.get(),
                "--listen-port", str(port_num), "--parent-watch"]
        if not socks:
            cmd += ["--console-port", str(port_num)]

        self._proxy_busy = True
        self.proxy_btn.config(state=tk.DISABLED, text="Starting…")
        self.tunnel_combo.config(state=tk.DISABLED)
        # One reader per COM port, so give ours up before the tunnel opens it.
        self._say("releasing the port for the tunnel", "dim")
        self._disconnect(then=lambda: self._launch_proxy(cmd, socks, port_num))

    def _launch_proxy(self, cmd, socks, port_num):
        if self._closing:
            self._proxy_busy = False
            return
        try:
            # stdin is the lifeline: the tunnel exits cleanly when we write
            # "quit"/"stop" or when this process dies and the pipe closes.
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            self._proxy_busy = False
            self.proxy_btn.config(state=tk.NORMAL, text="Start")
            self.tunnel_combo.config(state="readonly")
            self._say(str(e), "err")
            return
        self.proxy = proc
        self.proxy_socks = socks
        self.socks_ready = False
        self._proxy_busy = False
        self._bg(self._proxy_reader, proc)
        self.proxy_btn.config(state=tk.NORMAL, text="Stop")
        self.conn_btn.config(state=tk.DISABLED)
        self.proxy_lbl.config(text=f"running · 127.0.0.1:{port_num}",
                              foreground="#2a7")
        self._status("tunnel running", "#2a7")

    def _proxy_reader(self, proc):
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self._ui(self._tunnel_line, proc, line)
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        self._ui(self._proxy_ended, proc)

    def _proxy_ended(self, proc):
        if proc is not self.proxy:
            return          # an older process; the current one is unaffected
        self.proxy = None
        self._proxy_busy = False
        self._say(f"[tunnel] stopped (exit code {proc.returncode})", "dim")
        self.proxy_btn.config(state=tk.NORMAL, text="Start")
        self.tunnel_combo.config(state="readonly")
        self.proxy_lbl.config(text="stopped", foreground="#888")
        self.socks_ready = False
        self.browser.tunnel_down()
        self.conn_btn.config(state=tk.NORMAL, text="Connect")
        self._status("not connected", "#b33")
        if not self._closing:
            # Take the port back so the status and bridge controls work again.
            self.root.after(800, self._connect)

    def _on_os_drop(self, paths, point):
        """Files dropped on the window from Explorer."""
        if not paths or self._closing:
            return
        on_bridge_tab = self.tabs.select() != str(self.browser)
        if on_bridge_tab and len(paths) == 1 and paths[0].lower().endswith(".js") \
                and os.path.isfile(paths[0]):
            self.file.set(paths[0])
            self._say(f"payload set to {paths[0]}", "dim")
            return
        if on_bridge_tab:
            self.tabs.select(self.browser)
            point = None            # the pointer was over the Bridge tab
        self.browser.handle_drop(paths, point)

    def _tunnel_line(self, proc, line):
        if proc is self.proxy and self.proxy_socks and "SOCKS5 on" in line:
            self.socks_ready = True
        self._say(f"[tunnel] {line}", "dim")

    # -- hooks for the Files tab ----------------------------------
    def browser_target(self):
        """(console IP, local SOCKS port) for the FTP sessions."""
        s = self.tunnel_port.get().strip()
        return self.ip.get().strip(), int(s) if s.isdigit() else SOCKS_PORT

    def browser_state(self):
        if self.proxy:
            if not self.proxy_socks:
                return "other-tunnel"
            return "ready" if self.socks_ready else "tunnel-starting"
        if self._proxy_busy:
            return "tunnel-starting"
        if self.mode == "bridge" and not self.pending:
            return "no-tunnel"
        return "no-bridge"

    def browser_start_socks(self):
        if self.proxy or self._proxy_busy:
            return
        socks = next(k for k, v in TUNNELS.items() if v == SOCKS_TUNNEL)
        self.tunnel_kind.set(socks)
        self._on_tunnel_kind_changed()
        self._start_proxy()

    # -- hooks for the Payload tab --------------------------------
    def console_target(self):
        s = self.tcp.get().strip()
        return self.ip.get().strip() or DEFAULT_IP, int(s) if s.isdigit() \
            else int(DEFAULT_PORT)

    def bridge_ready(self):
        return bool(self.link and self.mode == "bridge" and not self.pending
                    and not self.proxy)

    def send_payload(self, ip, port, data):
        """Fire one payload over the bridge. Returns True if it went out."""
        if not self.bridge_ready():
            return False
        # Keep mirroring the console's reply into the Payload tab for a while.
        self._payload_until = time.monotonic() + 30
        self.link.send(f"SEND {ip} {port} {len(data)}\n".encode(), 0.1, data)
        return True

    # -- hooks for the Pi tab -------------------------------------
    def pi_connected(self):
        return bool(self.link)

    def local_pi_hashes(self):
        """{name: sha256[:12]} for the pi/*.py files this GUI ships, so the Pi
        tab can show which of the Pi's files are out of date."""
        import hashlib
        here = os.path.dirname(os.path.abspath(__file__))
        pidir = os.path.join(os.path.dirname(here), "pi")
        out = {}
        for name in ("supervisor.py", "tunnel.py", "bridge_mode.py"):
            try:
                data = open(os.path.join(pidir, name), "rb").read()
                out[name] = hashlib.sha256(data).hexdigest()[:12]
            except Exception:
                pass
        return out

    def pi_refresh(self):
        """Ask the Pi for a diagnostics blob, if the link is free."""
        if self.link and not self.pending and not self.proxy:
            self.link.send(b"DIAG\n")

    def _pi_auto(self):
        """Poll DIAG every 5 s while the Pi tab is the visible one."""
        self.root.after(5000, self._pi_auto)
        if self._closing or not self.link:
            return
        try:
            on_pi = self.tabs.tabs()[self.tabs.index("current")] \
                == str(self.pi)
        except Exception:
            on_pi = False
        if on_pi and self.pi.auto.get():
            self.pi_refresh()

    def pi_power(self, verb):
        if not self.link:
            return
        self._say(f"Pi {verb} requested", "dim")
        self.link.send((verb.upper() + "\n").encode())
        if verb in ("reboot", "poweroff"):
            # The link will drop; show it and stop driving the port.
            self.root.after(2500, lambda: self._say(
                f"Pi is {'rebooting' if verb == 'reboot' else 'shutting down'}; "
                "the link will drop.", "dim"))

    def pi_update(self, names):
        """Push each named pi/<file> to the Pi with UPDATE, then RESTART and
        re-query DIAG to confirm. Runs on the Tk thread: link.send is async."""
        if not self.link:
            return
        import hashlib
        here = os.path.dirname(os.path.abspath(__file__))
        pidir = os.path.join(os.path.dirname(here), "pi")
        sent = 0
        for name in names:
            try:
                data = open(os.path.join(pidir, name), "rb").read()
            except Exception as e:
                self._say(f"skip {name}: {e}", "err")
                continue
            sha = hashlib.sha256(data).hexdigest()
            self._say(f"pushing {name} ({len(data)} B)…", "dim")
            self.link.send(f"UPDATE {name} {len(data)} {sha}\n".encode(),
                           0.1, data)
            sent += 1
        if not sent:
            return
        # Restart so a new supervisor.py takes effect, then verify.
        self.link.send(b"RESTART\n")
        self._say("restarting Pi service; re-checking in ~8 s…", "dim")
        self.root.after(8000, self.pi_refresh)

    def _stop_proxy(self):
        proc = self.proxy
        if not proc:
            return
        self._proxy_busy = True
        self.proxy_btn.config(state=tk.DISABLED, text="Stopping…")
        self.browser.tunnel_down()
        # "quit" ends the tunnel but leaves the bridge running.
        self._bg(end_child, proc, "quit", 8)

    # -- output ---------------------------------------------------
    def _say(self, text, tag=None):
        self.log.config(state=tk.NORMAL)
        stamp = time.strftime("%H:%M:%S")
        self.log.insert(tk.END, f"{stamp}  {text}\n", tag or ())
        # Keep the widget bounded; a long session used to slow everything down.
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > LOG_LINES:
            self.log.delete("1.0", f"{lines - LOG_LINES}.0")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _status(self, text, color):
        self.state_lbl.config(text=f"● {text}", foreground=color)

    # -- theme ----------------------------------------------------
    def _toggle_theme(self):
        self.dark.set(not self.dark.get())
        _save_pref("dark", self.dark.get())
        self.colors = gui_extras.apply_theme(self.root, self.dark.get())
        self.theme_btn.config(text="☀" if self.dark.get() else "🌙")
        self._apply_theme()

    def _apply_theme(self):
        c = self.colors
        self.log.config(background=c["log_bg"], foreground=c["text"],
                        insertbackground=c["text"])
        self.log.tag_configure("err", foreground=c["err"])
        self.log.tag_configure("dim", foreground=c["dim"])
        self.log.tag_configure("me", foreground=c["me"])
        if hasattr(self, "browser"):
            self.browser.apply_colors(c)
        if hasattr(self, "payload"):
            self.payload.apply_colors(c)
        if hasattr(self, "pi"):
            self.pi.apply_colors(c)

    # -- notifications --------------------------------------------
    def notify(self, title, body):
        # Only when the user is looking elsewhere -- otherwise the progress bar
        # already shows it. That is exactly "tell me when I am away".
        if self._closing or gui_extras.is_foreground(self.root):
            return
        try:
            gui_extras.Toast(self.root, title, body, self.colors)
            gui_extras.flash_taskbar(self.root)
        except Exception:
            pass

    # -- auto-reconnect -------------------------------------------
    def _watch_ports(self):
        """Notice the Pi being plugged in or pulled out, and react.

        Reconnect on its own when the Pi's gadget serial appears and nothing
        else holds the port. Harmless while a tunnel runs or we are busy.
        """
        if self._closing:
            return
        try:
            ports = sorted(serial.tools.list_ports.comports(),
                           key=lambda p: p.device)
            devices = {p.device for p in ports}
            if devices != self._last_devices:
                sel = self._selected_port()
                self.ports = ports
                self._fill_ports(keep=sel)
                new = devices - self._last_devices
                self._last_devices = devices
                pi_dev = next((p.device for p in ports
                               if (p.vid, p.pid) == GADGET), None)
                if (self.auto_serial and pi_dev and pi_dev in new
                        and not self.link and not self._link_busy
                        and not self.proxy and not self._proxy_busy
                        and not self._closing):
                    self._say("Pi detected; connecting…", "dim")
                    self._select_device(pi_dev)
                    self._connect()
        except Exception:
            pass
        self.root.after(1500, self._watch_ports)

    def _select_device(self, dev):
        for i, p in enumerate(self.ports):
            if p.device == dev:
                self.port_box.current(i)
                return

    # -- shutdown -------------------------------------------------
    def close(self):
        """Hide at once, clean up off the UI thread, then go.

        Order matters: the tunnel (if any) must let go of the COM port, and
        the Pi is told to stop the bridge, before the process exits.
        """
        if self._closing:
            return
        self._closing = True
        try:
            self.root.withdraw()
        except Exception:
            pass
        stop = bool(self.stop_on_exit.get())
        proc, link = self.proxy, self.link
        self.browser.shutdown()
        self.link = None
        self._poll_gen += 1
        dev = self._selected_port()
        i = self.port_box.current()
        is_pi = 0 <= i < len(self.ports) and \
            (self.ports[i].vid, self.ports[i].pid) == GADGET
        idle = self.mode == "idle" and link is not None

        def work():
            try:
                if proc:
                    # "stop" makes the tunnel stop the bridge itself after
                    # leaving tunnel mode, while it still owns the port.
                    clean = end_child(proc, "stop" if stop else "quit", 15)
                    # If the tunnel was already being stopped with "quit" (the
                    # user hit Stop, then closed before it finished), our
                    # "stop" lands on a dying process and the bridge would be
                    # left up. So once the child is gone, stop the bridge
                    # ourselves over the reopened port -- idempotent: it checks
                    # the mode first and does nothing if the Pi is already idle.
                    if stop and dev and is_pi:
                        time.sleep(0.8)
                        self._stop_bridge_on(dev)
                    elif not clean and stop and dev:
                        time.sleep(0.8)
                        self._stop_bridge_on(dev)
                elif link:
                    link.shutdown(stop_bridge=stop and not idle, log=_diag)
                elif stop and is_pi:
                    # Not connected (or mid-connect): open the port just to
                    # stop the bridge -- only if it really is the Pi, never
                    # some other serial device.
                    time.sleep(1.0)
                    self._stop_bridge_on(dev)
            except Exception:
                _diag("close: " + traceback.format_exc())
            finally:
                self._ui(self._destroy)
        self._bg(work)
        # Never let a stuck driver keep the process alive.
        self.root.after(30000, self._destroy)

    @staticmethod
    def _stop_bridge_on(dev):
        try:
            ser = serial.Serial(dev, BAUD, timeout=0.2, write_timeout=3)
        except Exception as e:
            _diag(f"close: could not reopen {dev} to stop the bridge: {e}")
            return
        try:
            pilink.stop_bridge(ser, log=_diag)
        finally:
            try:
                ser.close()
            except Exception:
                pass

    def _destroy(self):
        try:
            self.root.destroy()
        except Exception:
            pass


def _single_instance():
    """Hold a localhost port so a second copy cannot fight over the COM port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", INSTANCE_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return None
    return s


def _run_tunnel_child():
    """Frozen-exe only: when started as `civswitch.exe --run-tunnel <kind> …`,
    act as the tunnel process instead of the GUI. The remaining args are the
    tunnel's own, so hand them over untouched."""
    kind = sys.argv[2]
    del sys.argv[1:3]   # drop "--run-tunnel kind"; leave the tunnel's own args
    if kind == "socks":
        import socks_tunnel
        socks_tunnel.main()
    else:
        import tcp_proxy
        tcp_proxy.main()


def main():
    if FROZEN and len(sys.argv) > 2 and sys.argv[1] == "--run-tunnel":
        _run_tunnel_child()
        return

    try:
        diag = open(LOG_FILE, "a", encoding="utf-8")
        faulthandler.enable(file=diag)
        if sys.stderr is None:          # pythonw has no console
            sys.stderr = diag
    except Exception:
        pass

    win_drop.make_dpi_aware()
    lock = _single_instance()
    if lock is None:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("CivSwitch Bridge",
                             "CivSwitch Bridge is already running.\n"
                             "Only one copy can use the Pi's COM port.")
        r.destroy()
        return

    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.close)
    try:
        root.mainloop()
    finally:
        lock.close()
        # Daemon threads stuck inside a serial driver call must not keep the
        # process (and with it the COM port) alive after the window is gone.
        try:
            sys.stdout and sys.stdout.flush()
        except Exception:
            pass
        os._exit(0)


if __name__ == "__main__":
    main()
