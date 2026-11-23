"""The Pi tab: live diagnostics, safe power control, and one-click update.

The host (App) owns the serial link; this tab only renders what comes back and
asks the host to send DIAG / POWEROFF / REBOOT / RESTART / UPDATE. Diagnostics
auto-refresh while the tab is visible and the link is idle -- never while a
bridge start/stop is pending (that would stack commands behind a busy Pi) and
never while a tunnel owns the port.
"""

import tkinter as tk
from tkinter import ttk, messagebox


def _fmt_uptime(s):
    if s is None:
        return "—"
    d, s = divmod(int(s), 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _throttle_text(hex_str):
    """Decode vcgencmd get_throttled bits into plain words."""
    if not hex_str:
        return "—"
    try:
        v = int(hex_str, 16)
    except ValueError:
        return hex_str
    if v == 0:
        return "ok"
    now, past = [], []
    bits = {0: "undervoltage", 1: "freq capped", 2: "throttled",
            3: "temp limit"}
    for bit, name in bits.items():
        if v & (1 << bit):
            now.append(name)
        if v & (1 << (bit + 16)):
            past.append(name)
    out = []
    if now:
        out.append("now: " + ", ".join(now))
    if past:
        out.append("earlier: " + ", ".join(past))
    return "; ".join(out) or "ok"


class PiTab(ttk.Frame):
    def __init__(self, parent, host):
        super().__init__(parent, padding=10)
        self.host = host
        self._rows = {}
        self._last_diag = None
        self._build()

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill=tk.X)
        self.refresh_btn = ttk.Button(top, text="Refresh",
                                      command=self.host.pi_refresh)
        self.refresh_btn.pack(side=tk.LEFT)
        self.auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Auto every 5s",
                        variable=self.auto).pack(side=tk.LEFT, padx=8)
        self.status = ttk.Label(top, text="not connected", foreground="#888")
        self.status.pack(side=tk.RIGHT)

        health = ttk.LabelFrame(self, text="Health", padding=10)
        health.pack(fill=tk.X, pady=(10, 0))
        health.columnconfigure(1, weight=1)
        health.columnconfigure(3, weight=1)
        fields = [
            ("hostname", "Hostname"), ("mode", "Mode"),
            ("temp_c", "CPU temp"), ("load", "Load avg"),
            ("mem", "Memory"), ("disk", "Disk free"),
            ("uptime_s", "Uptime"), ("throttled", "Power"),
            ("lan_ip", "Wi-Fi IP"), ("ap_ip", "AP IP"),
        ]
        for i, (key, label) in enumerate(fields):
            r, col = divmod(i, 2)
            ttk.Label(health, text=label + ":").grid(
                row=r, column=col * 2, sticky="e", padx=(0, 6), pady=2)
            val = ttk.Label(health, text="—", anchor="w")
            val.grid(row=r, column=col * 2 + 1, sticky="we", padx=(0, 16))
            self._rows[key] = val

        cl = ttk.LabelFrame(self, text="Connected clients", padding=10)
        cl.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.clients = tk.Listbox(cl, height=4, relief=tk.FLAT,
                                  activestyle="none")
        self.clients.pack(fill=tk.BOTH, expand=True)

        upd = ttk.LabelFrame(self, text="Software", padding=10)
        upd.pack(fill=tk.X, pady=(10, 0))
        self.ver_lbl = ttk.Label(upd, text="Connect to compare versions.",
                                 anchor="w", justify=tk.LEFT)
        self.ver_lbl.pack(fill=tk.X)
        self.update_btn = ttk.Button(upd, text="Update Pi",
                                     command=self._do_update, state=tk.DISABLED)
        self.update_btn.pack(anchor="w", pady=(8, 0))

        power = ttk.LabelFrame(self, text="Power", padding=10)
        power.pack(fill=tk.X, pady=(10, 0))
        self.restart_btn = ttk.Button(power, text="Restart service",
                                      command=lambda: self._power("restart"))
        self.restart_btn.pack(side=tk.LEFT)
        self.reboot_btn = ttk.Button(power, text="Reboot",
                                     command=lambda: self._power("reboot"))
        self.reboot_btn.pack(side=tk.LEFT, padx=8)
        self.poweroff_btn = ttk.Button(power, text="Shut down",
                                       command=lambda: self._power("poweroff"))
        self.poweroff_btn.pack(side=tk.LEFT)
        self._set_enabled(False)

    # -- called by the host ---------------------------------------
    def on_diag(self, d):
        """Render a DIAG blob (already parsed from JSON)."""
        self._last_diag = d
        self.status.config(text="updated " + _now(), foreground="#2a7")
        g = self._rows
        g["hostname"].config(text=d.get("hostname") or "—")
        g["mode"].config(text=d.get("mode") or "—")
        t = d.get("temp_c")
        g["temp_c"].config(text=f"{t:.1f} °C" if t is not None else "—")
        load = d.get("load")
        g["load"].config(text="  ".join(str(x) for x in load) if load else "—")
        mt, mu = d.get("mem_total_mb"), d.get("mem_used_mb")
        g["mem"].config(text=f"{mu} / {mt} MB" if mt else "—")
        dt, df = d.get("disk_total_mb"), d.get("disk_free_mb")
        g["disk"].config(text=f"{df} MB free" if df is not None else "—")
        g["uptime_s"].config(text=_fmt_uptime(d.get("uptime_s")))
        g["throttled"].config(text=_throttle_text(d.get("throttled")))
        g["lan_ip"].config(text=d.get("lan_ip") or "—")
        g["ap_ip"].config(text=d.get("ap_ip") or "—")

        self.clients.delete(0, tk.END)
        for c in d.get("clients") or []:
            sig = c.get("signal_dbm")
            self.clients.insert(tk.END, f"{c.get('mac')}   "
                                f"{sig} dBm" if sig is not None else c.get("mac"))
        if not (d.get("clients") or []):
            self.clients.insert(tk.END, "(none)")

        self._render_versions(d.get("files") or {})
        self._set_enabled(True)

    def on_connect(self, up):
        self._set_enabled(up)
        if not up:
            self.status.config(text="not connected", foreground="#888")

    def _render_versions(self, remote):
        local = self.host.local_pi_hashes()
        lines, differ = [], []
        for name in sorted(set(local) | set(remote)):
            lh = local.get(name)
            rh = (remote.get(name) or {}).get("sha") if remote.get(name) else None
            if lh and rh and lh == rh:
                lines.append(f"✓ {name}  up to date ({lh})")
            elif lh and rh:
                lines.append(f"● {name}  Pi {rh} → PC {lh}")
                differ.append(name)
            elif lh and not rh:
                lines.append(f"+ {name}  not on Pi → will add ({lh})")
                differ.append(name)
            else:
                lines.append(f"? {name}  on Pi only")
        self.ver_lbl.config(text="\n".join(lines) or "No files to compare.")
        self._differ = differ
        self.update_btn.config(
            state=tk.NORMAL if differ and self.host.pi_connected() else tk.DISABLED,
            text=f"Update Pi ({len(differ)} file{'s' if len(differ) != 1 else ''})"
                 if differ else "Up to date")

    def _do_update(self):
        differ = getattr(self, "_differ", [])
        if not differ:
            return
        if not messagebox.askyesno(
                "Update Pi",
                "Push these files to the Pi and restart its service?\n\n"
                + "\n".join(differ)
                + "\n\nThe bridge drops for a few seconds while it restarts.",
                parent=self):
            return
        self.update_btn.config(state=tk.DISABLED, text="Updating…")
        self.host.pi_update(differ)

    def _power(self, verb):
        labels = {"restart": ("Restart service",
                              "Restart the CivSwitch service on the Pi?"),
                  "reboot": ("Reboot", "Reboot the Pi now?"),
                  "poweroff": ("Shut down",
                               "Shut the Pi down?\n\nYou will have to unplug "
                               "and replug it to turn it back on.")}
        title, msg = labels[verb]
        if messagebox.askyesno(title, msg, parent=self):
            self.host.pi_power(verb)

    def _set_enabled(self, up):
        state = tk.NORMAL if up else tk.DISABLED
        for b in (self.restart_btn, self.reboot_btn, self.poweroff_btn):
            b.config(state=state)
        if not up:
            self.update_btn.config(state=tk.DISABLED)

    def apply_colors(self, c):
        self.clients.config(background=c["log_bg"], foreground=c["text"],
                            selectbackground=c["sel"])


def _now():
    import time
    return time.strftime("%H:%M:%S")
