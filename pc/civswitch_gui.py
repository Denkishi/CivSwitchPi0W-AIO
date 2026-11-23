#!/usr/bin/env python3
"""CivSwitch Pi Zero W -- control GUI.

One window for everything the Pi does: bridge control, sending JS payloads,
raw protocol commands, a tunnel for console tools, and a file browser for the
console's SD card.

Requires: pip install pyserial
"""

import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

import serial
import serial.tools.list_ports

import ftp_client

BAUD = 115200
DEFAULT_IP = "192.168.4.2"
DEFAULT_PORT = "9026"
FTP_PORT = "1337"
SOCKS_PORT = 1080

# Linux Foundation / Gadget Serial -- what g_serial presents on the Pi.
GADGET = (0x0525, 0xA4A7)

HERE = os.path.dirname(os.path.abspath(__file__))
TCP_PROXY = os.path.join(HERE, "tcp_proxy.py")
SOCKS_TUNNEL = os.path.join(HERE, "socks_tunnel.py")

# Both tunnels need the COM port to themselves, so only one runs at a time.
TUNNELS = {
    "TCP relay (for npm tools)": TCP_PROXY,
    "SOCKS5 (for FTP and browsers)": SOCKS_TUNNEL,
}

PAD = 8


class App:
    def __init__(self, root):
        self.root = root
        root.title("CivSwitch Bridge")
        root.geometry("680x760")
        root.minsize(600, 620)

        self.ser = None
        self.mode = "?"
        self.ports = []
        self.proxy = None          # Popen while a tunnel owns the COM port
        self.ftp = None
        # One control connection, so one operation at a time. Letting two
        # buttons overlap desynced it: the second command landed mid-transfer
        # and the server answered "503 Invalid command during transfer", then
        # reset the connection, which left Up and Refresh dead.
        self.ftp_lock = threading.Lock()
        self.cwd = "/"
        self.entries = []
        self._stop = threading.Event()

        ttk.Style().theme_use("clam")
        self._build()
        self._scan_ports()

    # -- layout ---------------------------------------------------
    def _build(self):
        root = self.root

        bar = ttk.Frame(root, padding=(PAD, PAD, PAD, 0))
        bar.pack(fill=tk.X)
        self.port = tk.StringVar()
        self.port_box = ttk.Combobox(bar, textvariable=self.port, width=22,
                                     state="readonly")
        self.port_box.pack(side=tk.LEFT)
        ttk.Button(bar, text="↻", width=3,
                   command=self._scan_ports).pack(side=tk.LEFT, padx=4)
        self.conn_btn = ttk.Button(bar, text="Connect", command=self._toggle_conn)
        self.conn_btn.pack(side=tk.LEFT, padx=4)
        self.state_lbl = ttk.Label(bar, text="● not connected",
                                   foreground="#b33")
        self.state_lbl.pack(side=tk.RIGHT)

        bridge = ttk.Frame(root, padding=(PAD, PAD, PAD, 0))
        bridge.pack(fill=tk.X)
        self.bridge_btn = ttk.Button(bridge, text="Start bridge",
                                     command=self._toggle_bridge,
                                     state=tk.DISABLED)
        self.bridge_btn.pack(side=tk.LEFT)
        self.info_lbl = ttk.Label(bridge, text="")
        self.info_lbl.pack(side=tk.LEFT, padx=PAD)

        box = ttk.LabelFrame(root, text="Payload", padding=PAD)
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
        tbox = ttk.LabelFrame(root, text="Tunnel", padding=PAD)
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

        # FTP browser. Needs the SOCKS tunnel, since it reaches the console
        # through it rather than over a network the PC does not have.
        fbox = ttk.LabelFrame(root, text="Console files (FTP over SOCKS)",
                              padding=PAD)
        fbox.pack(fill=tk.BOTH, expand=True, padx=PAD, pady=(PAD, 0))
        frow = ttk.Frame(fbox)
        frow.pack(fill=tk.X)
        self.ftp_port = tk.StringVar(value=FTP_PORT)
        ttk.Label(frow, text="port").pack(side=tk.LEFT)
        ttk.Entry(frow, textvariable=self.ftp_port, width=7).pack(side=tk.LEFT, padx=4)
        self.ftp_btn = ttk.Button(frow, text="Connect", command=self._toggle_ftp)
        self.ftp_btn.pack(side=tk.LEFT, padx=4)
        self.path_lbl = ttk.Label(frow, text="", foreground="#555")
        self.path_lbl.pack(side=tk.LEFT, padx=PAD)

        self.listbox = tk.Listbox(fbox, height=9, font=("Consolas", 9),
                                  activestyle="none")
        self.listbox.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.listbox.bind("<Double-Button-1>", lambda _e: self._ftp_open())

        brow = ttk.Frame(fbox)
        brow.pack(fill=tk.X, pady=(6, 0))
        self.ftp_buttons = []
        for text, cmd in (("Open", self._ftp_open), ("Up", self._ftp_up),
                          ("Refresh", self._ftp_refresh),
                          ("Download", self._ftp_download),
                          ("Upload", self._ftp_upload),
                          ("Delete", self._ftp_delete)):
            b = ttk.Button(brow, text=text, command=cmd, state=tk.DISABLED)
            b.pack(side=tk.LEFT, padx=(0, 4))
            self.ftp_buttons.append(b)

        prow = ttk.Frame(fbox)
        prow.pack(fill=tk.X, pady=(6, 0))
        self.prog = ttk.Progressbar(prow, mode="determinate", maximum=1000)
        self.prog.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.prog_lbl = ttk.Label(prow, text="", width=34, anchor=tk.W,
                                  foreground="#555")
        self.prog_lbl.pack(side=tk.LEFT, padx=(6, 0))

        cmd = ttk.Frame(root, padding=(PAD, PAD, PAD, 0))
        cmd.pack(fill=tk.X)
        ttk.Label(cmd, text="❯").pack(side=tk.LEFT)
        self.cmd = tk.StringVar()
        self.cmd_entry = ttk.Entry(cmd, textvariable=self.cmd, state=tk.DISABLED)
        self.cmd_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.cmd_entry.bind("<Return>", lambda _e: self._send_cmd())
        self.cmd_btn = ttk.Button(cmd, text="Run", command=self._send_cmd,
                                  state=tk.DISABLED)
        self.cmd_btn.pack(side=tk.LEFT)

        self.log = scrolledtext.ScrolledText(root, height=9, wrap=tk.WORD,
                                             font=("Consolas", 9),
                                             state=tk.DISABLED, relief=tk.FLAT)
        self.log.pack(fill=tk.BOTH, expand=True, padx=PAD, pady=PAD)
        self.log.tag_configure("err", foreground="#b33")
        self.log.tag_configure("dim", foreground="#888")
        self.log.tag_configure("me", foreground="#36c")

    # -- ports ----------------------------------------------------
    def _scan_ports(self):
        self.ports = sorted(serial.tools.list_ports.comports(),
                            key=lambda p: p.device)
        self.port_box["values"] = [
            p.device + ("  (Pi)" if (p.vid, p.pid) == GADGET else "")
            for p in self.ports
        ]
        if not self.ports:
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
        return self.ports[i].device if i >= 0 else None

    # -- connection -----------------------------------------------
    def _toggle_conn(self):
        self._disconnect() if self.ser else self._connect()

    def _connect(self):
        if self.proxy:
            self._say("A tunnel holds the port. Stop it first.", "err")
            return
        dev = self._selected_port()
        if not dev:
            self._say("Select a port.", "err")
            return
        try:
            self.ser = serial.Serial(dev, BAUD, timeout=0.2, write_timeout=15)
        except Exception as e:
            # A serial port takes a single reader, and "Access is denied" says
            # nothing about who has it. Name the usual suspects instead.
            if isinstance(e, PermissionError) or "ermission" in str(e)                     or "ccesso negato" in str(e):
                self._say(f"{dev} is held by another process. Close a running "
                          f"tunnel (socks_tunnel.py / tcp_proxy.py), another "
                          f"copy of this GUI, or a serial terminal.", "err")
            else:
                self._say(str(e), "err")
            return
        self._stop.clear()
        threading.Thread(target=self._reader, daemon=True).start()
        self.conn_btn.config(text="Disconnect")
        for w in (self.bridge_btn, self.cmd_entry, self.cmd_btn):
            w.config(state=tk.NORMAL)
        self._say(f"connected to {dev}", "dim")
        self.root.after(400, self._poll)

    def _disconnect(self):
        self._stop.set()
        try:
            self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.mode = "?"
        self.conn_btn.config(text="Connect")
        self.bridge_btn.config(state=tk.DISABLED, text="Start bridge")
        for w in (self.send_btn, self.cmd_entry, self.cmd_btn):
            w.config(state=tk.DISABLED)
        self.info_lbl.config(text="")
        self._status("not connected", "#b33")

    def _reader(self):
        while not self._stop.is_set() and self.ser:
            try:
                raw = self.ser.readline()
            except Exception:
                if not self._stop.is_set():
                    self.root.after(0, self._disconnect)
                return
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if line and line != "END":
                try:
                    self.root.after(0, self._on_line, line)
                except RuntimeError:
                    # Window already going away; after() needs the main loop.
                    return

    def _on_line(self, line):
        if line.startswith("{"):
            try:
                self._apply_info(json.loads(line))
                return
            except (json.JSONDecodeError, KeyError):
                pass
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

        self.bridge_btn.config(text="Stop bridge" if bridging else "Start bridge")
        self.send_btn.config(state=tk.NORMAL if bridging else tk.DISABLED)
        if bridging:
            self._status(f"AP {info.get('ap_ip', '')}", "#2a7")
        else:
            self._status("connected", "#555")

    def _poll(self):
        if self.ser and not self.proxy:
            self._write("INFO")
            self.root.after(5000, self._poll)

    # -- commands -------------------------------------------------
    def _write(self, cmd, payload=None):
        if not self.ser:
            self._say("Not connected.", "err")
            return
        try:
            self.ser.write((cmd + "\n").encode())
            if payload:
                time.sleep(0.1)
                self.ser.write(payload)
            self.ser.flush()
        except Exception as e:
            self._say(str(e), "err")

    def _send_cmd(self):
        line = self.cmd.get().strip()
        if not line or not self.ser:
            return
        self._say(f"❯ {line}", "me")
        self._write(line)
        self.cmd.set("")

    def _toggle_bridge(self):
        if self.mode == "bridge":
            self._say("stopping bridge...", "dim")
            self._write("STOP")
        else:
            self._say("starting bridge...", "dim")
            self._write("BRIDGE")

    def _pick(self):
        path = filedialog.askopenfilename(
            filetypes=[("JavaScript", "*.js"), ("All files", "*.*")])
        if path:
            self.file.set(path)

    def _send(self):
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
        self._write(f"SEND {self.ip.get()} {port} {len(data)}", data)

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
        self._stop_proxy() if self.proxy else self._start_proxy()

    def _start_proxy(self):
        dev = self._selected_port()
        if not dev:
            self._say("Select a port first.", "err")
            return
        script = TUNNELS[self.tunnel_kind.get()]
        if not os.path.isfile(script):
            self._say(f"{os.path.basename(script)} not found next to this file.",
                      "err")
            return
        if self.mode != "bridge":
            self._say("Start the bridge first: the relay needs it.", "err")
            return

        port_str = self.tunnel_port.get().strip()
        try:
            port_num = int(port_str)
            if not (1 <= port_num <= 65535):
                raise ValueError
        except ValueError:
            self._say("Invalid tunnel port (1-65535).", "err")
            return

        # One reader per COM port, so give ours up before the tunnel opens it.
        if self.ser:
            self._say("releasing the port for the tunnel", "dim")
            self._disconnect()
            time.sleep(0.8)

        socks = script == SOCKS_TUNNEL
        cmd = [sys.executable, "-u", script, "--port", dev, "--console",
               self.ip.get(), "--listen-port", str(port_num)]
        if not socks:
            cmd += ["--console-port", str(port_num)]
        try:
            self.proxy = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            self.proxy = None
            self._say(str(e), "err")
            return

        threading.Thread(target=self._proxy_reader, daemon=True).start()
        self.proxy_btn.config(text="Stop")
        self.conn_btn.config(state=tk.DISABLED)
        where = f"127.0.0.1:{port_num}"
        self.proxy_lbl.config(text=f"running · {where}", foreground="#2a7")
        self._status("tunnel running", "#2a7")
        if socks:
            self.ftp_btn.config(state=tk.NORMAL)

    def _proxy_reader(self):
        proc = self.proxy
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    try:
                        self.root.after(0, self._say, f"[tunnel] {line}", "dim")
                    except RuntimeError:
                        return
        except Exception:
            pass
        try:
            self.root.after(0, self._proxy_ended)
        except RuntimeError:
            pass

    def _proxy_ended(self):
        if self.proxy:
            self._say("[tunnel] stopped", "dim")
        self.proxy = None
        self.proxy_btn.config(text="Start")
        self.conn_btn.config(state=tk.NORMAL)
        self.proxy_lbl.config(text="stopped", foreground="#888")
        self._status("not connected", "#b33")
        self._ftp_disconnect()
        self.ftp_btn.config(state=tk.DISABLED)

    def _stop_proxy(self):
        self._ftp_disconnect()
        proc, self.proxy = self.proxy, None
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._proxy_ended()

    # -- FTP ------------------------------------------------------
    def _toggle_ftp(self):
        self._ftp_disconnect() if self.ftp else self._ftp_connect()

    def _ftp_connect(self):
        if not self.proxy or TUNNELS[self.tunnel_kind.get()] != SOCKS_TUNNEL:
            self._say("Start the SOCKS5 tunnel first.", "err")
            return
        try:
            port = int(self.ftp_port.get())
        except ValueError:
            self._say("Invalid FTP port.", "err")
            return
        try:
            s_port = int(self.tunnel_port.get().strip()) if self.tunnel_port.get().strip().isdigit() else SOCKS_PORT
            self.ftp = ftp_client.Ftp(self.ip.get(), port,
                                      ("127.0.0.1", s_port)).connect()
        except Exception as e:
            self.ftp = None
            self._say(f"FTP: {e}", "err")
            return
        self.ftp_btn.config(text="Disconnect")
        for b in self.ftp_buttons:
            b.config(state=tk.NORMAL)
        self._say(f"FTP connected to {self.ip.get()}:{port}", "dim")
        self.cwd = "/"
        self._ftp_refresh()

    def _ftp_disconnect(self):
        if self.ftp:
            try:
                self.ftp.close()
            except Exception:
                pass
        self.ftp = None
        self.ftp_btn.config(text="Connect")
        for b in self.ftp_buttons:
            b.config(state=tk.DISABLED)
        self.listbox.delete(0, tk.END)
        self.path_lbl.config(text="")

    def _ftp_task(self, fn, *args):
        """Run one FTP call off the UI thread; the link is slow."""
        if not self.ftp:
            return
        if not self.ftp_lock.acquire(blocking=False):
            self._say("FTP busy -- wait for the current operation.", "dim")
            return
        self._ftp_busy(True)

        def work():
            try:
                fn(*args)
            except Exception as e:
                self.root.after(0, self._ftp_error, e)
            finally:
                self.ftp_lock.release()
                try:
                    self.root.after(0, self._ftp_busy, False)
                except RuntimeError:
                    pass
        threading.Thread(target=work, daemon=True).start()

    def _progress_start(self, what, total):
        self._prog_total = total
        self._prog_last = 0.0
        self._prog_t0 = time.monotonic()
        self.prog.config(mode="determinate" if total else "indeterminate")
        self.prog["value"] = 0
        self.prog_lbl.config(text=f"{what}…")
        if not total:
            self.prog.start(80)

    def _progress_tick(self, done):
        """Called from the worker thread; throttled so the UI stays alive."""
        now = time.monotonic()
        if now - self._prog_last < 0.15 and done != self._prog_total:
            return
        self._prog_last = now
        rate = done / max(now - self._prog_t0, 0.001)
        try:
            self.root.after(0, self._progress_show, done, rate)
        except RuntimeError:
            pass

    def _progress_show(self, done, rate):
        total = self._prog_total
        speed = f"{rate / 1024:,.0f} KB/s"
        if total:
            self.prog["value"] = min(1000, int(done * 1000 / total))
            pct = done * 100 // total
            self.prog_lbl.config(
                text=f"{pct}%  {done:,} / {total:,} B  ·  {speed}")
        else:
            self.prog_lbl.config(text=f"{done:,} B  ·  {speed}")

    def _progress_end(self, note=""):
        try:
            self.prog.stop()
        except Exception:
            pass
        self.prog.config(mode="determinate")
        self.prog["value"] = 1000 if note else 0
        self.prog_lbl.config(text=note)

    def _ftp_busy(self, busy):
        state = tk.DISABLED if busy else tk.NORMAL
        for b in self.ftp_buttons:
            b.config(state=state)

    def _ftp_error(self, exc):
        self._say(f"FTP: {exc}", "err")
        text = str(exc)
        # A reset or a desynced control connection cannot be recovered by
        # retrying: drop the session so the next click reconnects cleanly
        # instead of failing in the same way forever.
        fatal = isinstance(exc, (OSError, ConnectionError)) or             text.startswith("503") or "closed" in text or "timed out" in text
        if fatal:
            self._say("FTP session dropped; click Connect again.", "dim")
            self._ftp_disconnect()

    def _ftp_refresh(self):
        if not self.ftp:
            return
        self._ftp_task(self._do_refresh)

    def _do_refresh(self):
        # Always CWD to where we think we are, then list with a bare LIST
        # (the server rejects LIST with a path). Passing None listed the
        # server's current directory instead, so Up changed the label and
        # showed the same contents.
        self.ftp.cwd(self.cwd)
        entries = self.ftp.listdir()
        entries.sort(key=lambda e: (not e[1], e[0].lower()))
        self.root.after(0, self._show_entries, entries)

    def _show_entries(self, entries):
        self.entries = entries
        self.listbox.delete(0, tk.END)
        for name, is_dir, size in entries:
            mark = "[dir] " if is_dir else "      "
            tail = "" if is_dir else f"   {size:,} B"
            self.listbox.insert(tk.END, f"{mark}{name}{tail}")
        self.path_lbl.config(text=self.cwd)

    def _selected_entry(self):
        sel = self.listbox.curselection()
        return self.entries[sel[0]] if sel else None

    def _join(self, name):
        return (self.cwd.rstrip("/") + "/" + name) if self.cwd != "/" else "/" + name

    def _ftp_open(self):
        entry = self._selected_entry()
        if not entry:
            return
        name, is_dir, _ = entry
        if not is_dir:
            self._ftp_download()
            return
        self.cwd = self._join(name)
        self._ftp_refresh()

    def _ftp_up(self):
        if self.cwd == "/":
            return
        self.cwd = self.cwd.rsplit("/", 1)[0] or "/"
        self._ftp_refresh()

    def _ftp_download(self):
        entry = self._selected_entry()
        if not entry or entry[1]:
            self._say("Pick a file.", "err")
            return
        name, _, size = entry
        local = filedialog.asksaveasfilename(initialfile=name)
        if not local:
            return
        remote = self._join(name)
        self._say(f"downloading {remote} ({size:,} B)", "dim")
        self._progress_start(f"Downloading {name}", size)
        self._ftp_task(self._do_download, remote, local)

    def _do_download(self, remote, local):
        try:
            with open(local, "wb") as fh:
                n = self.ftp.retrieve(remote, fh, progress=self._progress_tick)
        except Exception:
            self.root.after(0, self._progress_end, "failed")
            raise
        self.root.after(0, self._progress_end, f"done · {n:,} B")
        self.root.after(0, self._say, f"saved {n:,} B to {local}", "dim")

    def _ftp_upload(self):
        local = filedialog.askopenfilename()
        if not local:
            return
        remote = self._join(os.path.basename(local))
        try:
            total = os.path.getsize(local)
        except OSError:
            total = 0
        self._say(f"uploading {local} -> {remote} ({total:,} B)", "dim")
        self._progress_start(f"Uploading {os.path.basename(local)}", total)
        self._ftp_task(self._do_upload, local, remote)

    def _do_upload(self, local, remote):
        try:
            with open(local, "rb") as fh:
                n = self.ftp.store(fh, remote, progress=self._progress_tick)
        except Exception:
            self.root.after(0, self._progress_end, "failed")
            raise
        self.root.after(0, self._progress_end, f"done · {n:,} B")
        self.root.after(0, self._say, f"sent {n:,} B", "dim")
        self.root.after(0, self._ftp_refresh)

    def _ftp_delete(self):
        entry = self._selected_entry()
        if not entry:
            return
        name, is_dir, _ = entry
        if not messagebox.askyesno("Delete", f"Delete {name} from the console?"):
            return
        remote = self._join(name)
        self._ftp_task(self._do_delete, remote, is_dir)

    def _do_delete(self, remote, is_dir):
        if is_dir:
            self.ftp.rmdir(remote)
        else:
            self.ftp.delete(remote)
        self.root.after(0, self._say, f"deleted {remote}", "dim")
        self.root.after(0, self._ftp_refresh)

    # -- output ---------------------------------------------------
    def _say(self, text, tag=None):
        self.log.config(state=tk.NORMAL)
        stamp = time.strftime("%H:%M:%S")
        self.log.insert(tk.END, f"{stamp}  {text}\n", tag or ())
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _status(self, text, color):
        self.state_lbl.config(text=f"● {text}", foreground=color)

    def close(self):
        self._ftp_disconnect()
        if self.proxy:
            self._stop_proxy()
        if self.ser:
            self._disconnect()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()


if __name__ == "__main__":
    main()
