"""The Payload tab: a small library of JS payloads with an editor and sender.

Instead of picking a .js file every time, keep the snippets you use here,
edit them in place, and send one -- or a chain of them in order -- to the
console's Remote JS Loader. The loader's reply streams into the response box.

Everything is the user's own payloads for their own loader; this is just a
nicer way to manage and fire them. Saved to civswitch_payloads.json next to
this file.
"""

import json
import os
import sys
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
# Next to the .exe when frozen (where __file__ is a temp dir), else pc/.
_APPDIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else HERE
STORE = os.path.join(_APPDIR, "civswitch_payloads.json")

SAMPLE = [
    {"name": "Hello (print)", "body": "// Minimal check the loader is alive\n"
     "print('hello from CivSwitch');\n"},
]


def _load():
    try:
        with open(STORE, encoding="utf-8") as fh:
            data = json.load(fh)
        out = [d for d in data if isinstance(d, dict) and "name" in d]
        return out or list(SAMPLE)
    except Exception:
        return list(SAMPLE)


def _save(items):
    try:
        with open(STORE, "w", encoding="utf-8") as fh:
            json.dump(items, fh, indent=2)
    except Exception:
        pass


class PayloadTab(ttk.Frame):
    """`host` is the App: provides _ui, _say, console_target, bridge_ready,
    send_payload and a colors dict."""

    def __init__(self, master, host):
        super().__init__(master, padding=6)
        self.host = host
        self.items = _load()
        self.dirty = False
        self._build()
        self._refresh_list()
        if self.items:
            self.listbox.selection_set(0)
            self._load_selected()
        self._tick()

    # -- layout ---------------------------------------------------
    def _build(self):
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="Console").pack(side=tk.LEFT)
        self.ip = tk.StringVar()
        ttk.Entry(bar, textvariable=self.ip, width=15).pack(side=tk.LEFT, padx=4)
        ttk.Label(bar, text="port").pack(side=tk.LEFT)
        self.port = tk.StringVar()
        ttk.Entry(bar, textvariable=self.port, width=7).pack(side=tk.LEFT, padx=4)
        self.status = ttk.Label(bar, text="", foreground="#888")
        self.status.pack(side=tk.RIGHT)

        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        pane = ttk.PanedWindow(body, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)

        # Left: the library
        left = ttk.Frame(pane)
        self.listbox = tk.Listbox(left, exportselection=False,
                                  selectmode=tk.EXTENDED, activestyle="none",
                                  font=("Segoe UI", 9))
        lsb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=lsb.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        lsb.grid(row=0, column=1, sticky="ns")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.listbox.bind("<<ListboxSelect>>", self._on_pick)
        lb = ttk.Frame(left)
        lb.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        for txt, cmd in (("New", self._new), ("Dup", self._dup),
                         ("Del", self._del), ("Import", self._import)):
            ttk.Button(lb, text=txt, width=6, command=cmd).pack(side=tk.LEFT,
                                                                padx=(0, 2))
        pane.add(left, weight=1)

        # Right: editor + actions + response
        right = ttk.Frame(pane)
        nrow = ttk.Frame(right)
        nrow.pack(fill=tk.X)
        ttk.Label(nrow, text="Name").pack(side=tk.LEFT)
        self.name = tk.StringVar()
        self.name_entry = ttk.Entry(nrow, textvariable=self.name)
        self.name_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.name.trace_add("write", lambda *_: self._mark_dirty())
        self.b_save = ttk.Button(nrow, text="Save", command=self._save_current)
        self.b_save.pack(side=tk.LEFT)

        self.editor = tk.Text(right, wrap=tk.NONE, font=("Consolas", 10),
                              undo=True, height=12, relief=tk.FLAT,
                              background="#fbfbfb")
        esb = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.editor.yview)
        self.editor.configure(yscrollcommand=esb.set)
        self.editor.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(4, 0))
        self.editor.bind("<<Modified>>", self._on_modified)

        arow = ttk.Frame(right)
        arow.pack(fill=tk.X, pady=(4, 0))
        self.b_send = ttk.Button(arow, text="Send ▶", command=self._send_current)
        self.b_send.pack(side=tk.LEFT)
        self.b_chain = ttk.Button(arow, text="Send selected in order",
                                  command=self._send_chain)
        self.b_chain.pack(side=tk.LEFT, padx=6)
        self.b_save_as = ttk.Button(arow, text="Export…", command=self._export)
        self.b_save_as.pack(side=tk.LEFT)
        ttk.Button(arow, text="Clear log", command=self._clear_log).pack(
            side=tk.RIGHT)

        ttk.Label(right, text="Console reply", foreground="#888").pack(
            anchor=tk.W, pady=(6, 0))
        self.resp = tk.Text(right, height=7, wrap=tk.WORD, font=("Consolas", 9),
                            state=tk.DISABLED, relief=tk.FLAT, background="#f4f4f4")
        self.resp.pack(fill=tk.BOTH, expand=False)
        pane.add(right, weight=3)

    # -- host glue ------------------------------------------------
    def _tick(self):
        try:
            ip, port = self.host.console_target()
            if not self.ip.get():
                self.ip.set(ip)
            if not self.port.get():
                self.port.set(str(port))
            ready = self.host.bridge_ready()
            st = tk.NORMAL if ready else tk.DISABLED
            self.b_send.config(state=st)
            self.b_chain.config(state=st)
            self.status.config(
                text="bridge up — ready to send" if ready
                else "start the bridge to send", foreground="#2a7" if ready
                else "#b33")
        finally:
            self.after(700, self._tick)

    def apply_colors(self, c):
        self.editor.config(background=c["log_bg"], foreground=c["text"],
                           insertbackground=c["text"])
        self.resp.config(background=c["field"], foreground=c["text"])
        self.listbox.config(background=c["tree_bg"], foreground=c["text"],
                            selectbackground=c["sel"], selectforeground=c["sel_fg"])
        self.status.config(foreground=c["muted"])

    def on_reply(self, line):
        """A line of the console's reply, routed here while a send is active."""
        self.resp.config(state=tk.NORMAL)
        self.resp.insert(tk.END, line + "\n")
        self.resp.see(tk.END)
        self.resp.config(state=tk.DISABLED)

    # -- library --------------------------------------------------
    def _refresh_list(self, keep=None):
        self.listbox.delete(0, tk.END)
        for it in self.items:
            self.listbox.insert(tk.END, it["name"])
        if keep is not None and 0 <= keep < len(self.items):
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(keep)
            self.listbox.see(keep)

    def _sel_index(self):
        s = self.listbox.curselection()
        return s[0] if s else None

    def _on_pick(self, _e=None):
        if len(self.listbox.curselection()) == 1:
            self._load_selected()

    def _load_selected(self):
        i = self._sel_index()
        if i is None:
            return
        it = self.items[i]
        self.name.set(it["name"])
        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", it.get("body", ""))
        self.editor.edit_reset()
        self.editor.edit_modified(False)
        self.dirty = False
        self._update_save()

    def _mark_dirty(self):
        self.dirty = True
        self._update_save()

    def _on_modified(self, _e=None):
        if self.editor.edit_modified():
            self.dirty = True
            self.editor.edit_modified(False)
            self._update_save()

    def _update_save(self):
        self.b_save.config(text="Save *" if self.dirty else "Save")

    def _current_body(self):
        return self.editor.get("1.0", "end-1c")

    def _save_current(self):
        i = self._sel_index()
        name = self.name.get().strip() or "payload"
        body = self._current_body()
        if i is None:
            self.items.append({"name": name, "body": body})
            i = len(self.items) - 1
        else:
            self.items[i] = {"name": name, "body": body}
        _save(self.items)
        self.dirty = False
        self._refresh_list(keep=i)
        self._update_save()
        self.host._say(f"payload '{name}' saved", "dim")

    def _new(self):
        self.items.append({"name": "new payload", "body": ""})
        _save(self.items)
        self._refresh_list(keep=len(self.items) - 1)
        self._load_selected()
        self.name_entry.focus_set()
        self.name_entry.select_range(0, tk.END)

    def _dup(self):
        i = self._sel_index()
        if i is None:
            return
        it = dict(self.items[i])
        it["name"] += " copy"
        self.items.insert(i + 1, it)
        _save(self.items)
        self._refresh_list(keep=i + 1)
        self._load_selected()

    def _del(self):
        idxs = sorted(self.listbox.curselection(), reverse=True)
        if not idxs:
            return
        what = self.items[idxs[0]]["name"] if len(idxs) == 1 \
            else f"{len(idxs)} payloads"
        if not messagebox.askyesno("Delete", f"Delete {what}?", parent=self):
            return
        for i in idxs:
            del self.items[i]
        _save(self.items)
        self._refresh_list(keep=min(idxs[-1], len(self.items) - 1))
        if self.items:
            self._load_selected()
        else:
            self.name.set("")
            self.editor.delete("1.0", tk.END)

    def _import(self):
        paths = filedialog.askopenfilenames(
            parent=self, title="Import payloads",
            filetypes=[("JavaScript", "*.js"), ("All files", "*.*")])
        added = 0
        for p in paths:
            try:
                with open(p, encoding="utf-8") as fh:
                    body = fh.read()
            except Exception as e:
                self.host._say(f"import {p}: {e}", "err")
                continue
            self.items.append({"name": os.path.basename(p), "body": body})
            added += 1
        if added:
            _save(self.items)
            self._refresh_list(keep=len(self.items) - 1)
            self._load_selected()
            self.host._say(f"imported {added} payload(s)", "dim")

    def _export(self):
        p = filedialog.asksaveasfilename(
            parent=self, initialfile=(self.name.get() or "payload") + ".js",
            defaultextension=".js",
            filetypes=[("JavaScript", "*.js"), ("All files", "*.*")])
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(self._current_body())
            self.host._say(f"exported to {p}", "dim")
        except Exception as e:
            self.host._say(f"export: {e}", "err")

    # -- sending --------------------------------------------------
    def _target(self):
        try:
            return self.ip.get().strip(), int(self.port.get())
        except ValueError:
            self.host._say("Invalid console port.", "err")
            return None

    def _send_one(self, name, body):
        data = body.encode("utf-8")
        tgt = self._target()
        if not tgt:
            return False
        ip, port = tgt
        self._log(f"▶ sending '{name}' ({len(data)} B) to {ip}:{port}")
        return self.host.send_payload(ip, port, data)

    def _send_current(self):
        if not self.host.bridge_ready():
            self.host._say("Start the bridge first.", "err")
            return
        self._send_one(self.name.get() or "payload", self._current_body())

    def _send_chain(self):
        if not self.host.bridge_ready():
            self.host._say("Start the bridge first.", "err")
            return
        idxs = list(self.listbox.curselection())
        if len(idxs) < 2:
            self.host._say("Select two or more payloads (Ctrl-click) to chain.",
                           "dim")
            return
        chain = [(self.items[i]["name"], self.items[i].get("body", ""))
                 for i in idxs]
        self._log(f"▶ chain of {len(chain)}: "
                  + " → ".join(n for n, _ in chain))
        # The Pi serialises commands, and the writer queue preserves order, so
        # the payloads land back-to-back in the order picked.
        for name, body in chain:
            self._send_one(name, body)

    def _log(self, text):
        self.resp.config(state=tk.NORMAL)
        self.resp.insert(tk.END, time.strftime("%H:%M:%S  ") + text + "\n")
        self.resp.see(tk.END)
        self.resp.config(state=tk.DISABLED)

    def _clear_log(self):
        self.resp.config(state=tk.NORMAL)
        self.resp.delete("1.0", tk.END)
        self.resp.config(state=tk.DISABLED)
