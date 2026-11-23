"""Console file browser for the GUI: an FTP client over the SOCKS tunnel.

Works like a desktop file manager: sortable columns, icons, history, an
editable path bar, filter, multi-select, context menu, inline rename, folder
upload/download and a transfer queue with cancel.

Two FTP sessions, like FileZilla: one for browsing and one for transfers, so
you can keep navigating while a big file moves. If the server refuses a second
login, transfers fall back to the browsing session.

Threading follows the GUI's rule: FTP runs on worker threads, which never
touch Tk -- they post callbacks through host._ui().
"""

import os
import queue
import shutil
import socket
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import ftp_client
from ftp_client import Cancelled, FtpError

DEFAULT_FTP_PORT = "1337"

TYPES = {
    ".nro": "Homebrew app", ".nsp": "Switch package", ".nsz": "Switch package",
    ".xci": "Game card image", ".xcz": "Game card image", ".js": "JavaScript",
    ".json": "JSON", ".ini": "Settings", ".txt": "Text", ".log": "Log",
    ".bin": "Binary", ".zip": "ZIP archive", ".png": "PNG image",
    ".jpg": "JPEG image", ".jpeg": "JPEG image", ".nca": "Content archive",
    ".kip": "Kernel module", ".ovl": "Overlay",
}

# 16x16 icons. Tk 9 renders SVG natively; older Tk falls back to drawn ones.
SVG_FOLDER = """<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">
<path d="M1.5 3.5h4.2l1.4 1.5h7.4v8.5h-13z" fill="#f2c94c" stroke="#c9a227"/>
<path d="M1.5 6h13" stroke="#e0b53a"/></svg>"""
SVG_FILE = """<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">
<path d="M3.5 1.5h6l3 3v10h-9z" fill="#ffffff" stroke="#8a94a6"/>
<path d="M9.5 1.5v3h3" fill="none" stroke="#8a94a6"/></svg>"""
SVG_APP = """<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">
<rect x="1.5" y="1.5" width="13" height="13" rx="3" fill="#e4003a" stroke="#b0002c"/>
<path d="M5 5h2.5v6H5zM8.5 5H11v6H8.5z" fill="#fff"/></svg>"""
SVG_LINK = """<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">
<path d="M3.5 1.5h6l3 3v10h-9z" fill="#eef3ff" stroke="#5b7bd5"/>
<path d="M6 11l4-4M7 7h3v3" fill="none" stroke="#5b7bd5" stroke-width="1.4"/></svg>"""


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"


def kind_of(e):
    if e.link:
        return "Link"
    if e.is_dir:
        return "Folder"
    ext = os.path.splitext(e.name)[1].lower()
    return TYPES.get(ext, f"{ext[1:].upper()} file" if ext else "File")


def rjoin(base, name):
    return (base.rstrip("/") + "/" + name) if base != "/" else "/" + name


def rparent(path):
    return path.rsplit("/", 1)[0] or "/"


def is_fatal(exc):
    """True when the control connection is unusable and must be redone."""
    if isinstance(exc, (OSError, ConnectionError, socket.timeout)):
        return True
    text = str(exc)
    return text.startswith(("421", "503")) or "closed" in text \
        or "timed out" in text or "malformed" in text


def fmt_eta(sec):
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


# ---------------------------------------------------------------------------
class Session:
    """One FTP control connection with its own worker thread and job queue.

    Jobs run in order. A dropped connection is redone transparently and the
    job retried once, so an idle timeout on the console never surfaces.
    """

    def __init__(self, params, on_connect_fail=None):
        self.params = params            # (host, port, proxy)
        self.on_connect_fail = on_connect_fail
        self.q = queue.Queue()
        self.ftp = None
        self._lock = threading.Lock()
        self.pending = 0
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, fn, on_error, retry=True):
        with self._lock:
            self.pending += 1
        self.q.put((fn, on_error, retry))

    def clear(self):
        """Drop queued (not running) jobs; returns how many."""
        dropped = 0
        while True:
            try:
                job = self.q.get_nowait()
            except queue.Empty:
                break
            if job is None:
                self.q.put(None)
                break
            dropped += 1
        with self._lock:
            self.pending -= dropped
        return dropped

    def stop(self):
        self.clear()
        self.q.put(None)

    def _drop(self):
        ftp, self.ftp = self.ftp, None
        if ftp:
            try:
                ftp.close()
            except Exception:
                pass

    def _run(self):
        while True:
            job = self.q.get()
            if job is None:
                self._drop()
                return
            try:
                self._do(*job)
            finally:
                with self._lock:
                    self.pending -= 1

    def _do(self, fn, on_error, retry):
        for attempt in (0, 1):
            if self.ftp is None:
                host, port, proxy = self.params
                try:
                    self.ftp = ftp_client.Ftp(host, port, proxy).connect()
                except Exception as e:
                    if self.on_connect_fail:
                        self.on_connect_fail(fn, on_error, e)
                    else:
                        on_error(e)
                    return
            try:
                fn(self.ftp)
                return
            except Cancelled as e:
                # Mid-transfer abort leaves replies unread: start over.
                self._drop()
                on_error(e)
                return
            except Exception as e:
                if is_fatal(e):
                    self._drop()
                    if attempt == 0 and retry:
                        continue
                on_error(e)
                return


# ---------------------------------------------------------------------------
class FileBrowser(ttk.Frame):
    """The Files tab. `host` is the App (provides _ui, _say, browser_* hooks).

    Layout: a folder tree on the left (lazy, expands on demand) and the
    current folder's contents on the right. Files move between folders by
    dragging them onto a folder (in either pane), or with Cut/Paste.
    """

    COLS = ("size", "modified", "type")
    DUMMY = "//loading"     # placeholder child; real paths never contain //

    def __init__(self, master, host):
        super().__init__(master, padding=6)
        self.host = host
        self.s = max(1.0, self.winfo_fpixels("1i") / 96.0)   # DPI scale
        self.nav = None             # browsing session
        self.xfer = None            # transfer session
        self.xfer_fallback = False  # server allows one login only
        self.rnto_paths = None      # does RNTO accept a path? (None: unknown)
        self.connected = False
        self.autoconnect = True
        self.cwd = "/"
        self.entries = {}           # name -> Entry, current directory
        self.back, self.fwd = [], []
        self.sort_col, self.sort_rev = "#0", False
        self.loading = 0
        self.last_local = os.path.expanduser("~")
        self.cancel_evt = threading.Event()
        self.xfer_count = 0         # transfer jobs queued or running
        self.clip = None            # {"op": "cut"|"copy", "src": dir, "entries": [...]}
        self._overlay_state = None
        self._editor = None
        self._drag = None
        self._drop_mark = None

        self._icons()
        self._build()
        self._bind_keys()
        self._tick()

    def px(self, n):
        return int(round(n * self.s))

    # -- setup ----------------------------------------------------
    def _icons(self):
        scale = round(self.s, 2)

        def svg(data, fallback):
            for fmt in (f"svg -scale {scale}", "svg"):
                try:
                    return tk.PhotoImage(master=self, data=data, format=fmt)
                except tk.TclError:
                    continue
            return fallback()

        def drawn(fill, edge):
            n = self.px(16)
            img = tk.PhotoImage(master=self, width=n, height=n)
            img.put(fill, to=(self.px(2), self.px(3), self.px(14), self.px(14)))
            img.put(edge, to=(self.px(2), self.px(3), self.px(14), self.px(4)))
            return img

        self.ic_dir = svg(SVG_FOLDER, lambda: drawn("#f2c94c", "#c9a227"))
        self.ic_file = svg(SVG_FILE, lambda: drawn("#ffffff", "#8a94a6"))
        self.ic_app = svg(SVG_APP, lambda: drawn("#e4003a", "#b0002c"))
        self.ic_link = svg(SVG_LINK, lambda: drawn("#eef3ff", "#5b7bd5"))

    def _build(self):
        st = ttk.Style()
        for name in ("Files.Treeview", "Side.Treeview"):
            st.configure(name, rowheight=self.px(24), borderwidth=0)
            st.map(name, background=[("selected", "#cce4ff")],
                   foreground=[("selected", "#000000")])
        st.configure("Files.Treeview.Heading", padding=(self.px(6), self.px(3)))
        st.configure("Tool.TButton", padding=(self.px(4), self.px(1)))

        # Row 1: navigation + path + filter
        nav = ttk.Frame(self)
        nav.pack(fill=tk.X)
        self.b_back = ttk.Button(nav, text="←", width=3, style="Tool.TButton",
                                 command=self.go_back)
        self.b_fwd = ttk.Button(nav, text="→", width=3, style="Tool.TButton",
                                command=self.go_forward)
        self.b_up = ttk.Button(nav, text="↑", width=3, style="Tool.TButton",
                               command=self.go_up)
        self.b_ref = ttk.Button(nav, text="⟳", width=3, style="Tool.TButton",
                                command=self.refresh)
        for b in (self.b_back, self.b_fwd, self.b_up, self.b_ref):
            b.pack(side=tk.LEFT, padx=(0, 2))
        self.path_var = tk.StringVar(value="/")
        self.path_entry = ttk.Entry(nav, textvariable=self.path_var)
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 6))
        self.path_entry.bind("<Return>", lambda _e: self.go(self._typed_path()))
        self.path_entry.bind("<Escape>", lambda _e: (self.path_var.set(self.cwd),
                                                     self.tree.focus_set()))
        ttk.Label(nav, text="Filter").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._render())
        self.filter_entry = ttk.Entry(nav, textvariable=self.filter_var, width=16)
        self.filter_entry.pack(side=tk.LEFT, padx=(4, 0))
        self.filter_entry.bind("<Escape>", lambda _e: (self.filter_var.set(""),
                                                       self.tree.focus_set()))

        # Row 2: actions
        act = ttk.Frame(self)
        act.pack(fill=tk.X, pady=(6, 0))
        self.b_upload = ttk.Menubutton(act, text="Upload")
        up_menu = tk.Menu(self.b_upload, tearoff=False)
        up_menu.add_command(label="Files…", command=self.upload_files)
        up_menu.add_command(label="Folder…", command=self.upload_folder)
        self.b_upload["menu"] = up_menu
        self.b_upload.pack(side=tk.LEFT)
        self.b_download = ttk.Button(act, text="Download", command=self.download)
        self.b_newdir = ttk.Button(act, text="New folder", command=self.new_folder)
        self.b_cut = ttk.Button(act, text="Cut", command=lambda: self.to_clip("cut"))
        self.b_paste = ttk.Button(act, text="Paste", command=self.paste)
        self.b_rename = ttk.Button(act, text="Rename", command=self.rename)
        self.b_delete = ttk.Button(act, text="Delete", command=self.delete)
        for b in (self.b_download, self.b_newdir, self.b_cut, self.b_paste,
                  self.b_rename, self.b_delete):
            b.pack(side=tk.LEFT, padx=(4, 0))
        self.b_conn = ttk.Button(act, text="Disconnect", command=self._toggle_conn)
        self.b_conn.pack(side=tk.RIGHT)
        self.port_var = tk.StringVar(value=DEFAULT_FTP_PORT)
        ttk.Entry(act, textvariable=self.port_var, width=6).pack(side=tk.RIGHT,
                                                                 padx=(2, 6))
        ttk.Label(act, text="FTP port").pack(side=tk.RIGHT)

        # Folder tree | file list
        holder = ttk.Frame(self)
        holder.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        pane = ttk.PanedWindow(holder, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)

        sf = ttk.Frame(pane)
        self.side = ttk.Treeview(sf, show="tree", selectmode="browse",
                                 style="Side.Treeview")
        ssb = ttk.Scrollbar(sf, orient=tk.VERTICAL, command=self.side.yview)
        self.side.configure(yscrollcommand=ssb.set)
        self.side.grid(row=0, column=0, sticky="nsew")
        ssb.grid(row=0, column=1, sticky="ns")
        sf.rowconfigure(0, weight=1)
        sf.columnconfigure(0, weight=1)
        self.side.column("#0", width=self.px(190), minwidth=self.px(120))
        self.side.tag_configure("drop", background="#ffe9a8")
        pane.add(sf, weight=1)

        lf = ttk.Frame(pane)
        self.tree = ttk.Treeview(lf, columns=self.COLS, style="Files.Treeview",
                                 selectmode="extended", show="tree headings")
        ysb = ttk.Scrollbar(lf, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)
        pane.add(lf, weight=4)
        self.tree.column("#0", width=self.px(260), minwidth=self.px(150), stretch=True)
        self.tree.column("size", width=self.px(80), minwidth=self.px(60),
                         anchor=tk.E, stretch=False)
        self.tree.column("modified", width=self.px(125), minwidth=self.px(90),
                         stretch=False)
        self.tree.column("type", width=self.px(110), minwidth=self.px(70),
                         stretch=False)
        for col, text in (("#0", "Name"), ("size", "Size"),
                          ("modified", "Modified"), ("type", "Type")):
            self.tree.heading(col, text=text, anchor=tk.W if col != "size" else tk.E,
                              command=lambda c=col: self._sort_by(c))
        self.tree.tag_configure("odd", background="#f6f7f9")
        self.tree.tag_configure("cut", foreground="#9aa0a6")
        self.tree.tag_configure("drop", background="#ffe9a8")

        # Empty / disconnected state, laid over both panes.
        self.overlay = ttk.Frame(holder, padding=24)
        self.ov_title = ttk.Label(self.overlay, font=("Segoe UI", 12, "bold"))
        self.ov_title.pack(pady=(40, 6))
        self.ov_text = ttk.Label(self.overlay, foreground="#555", justify=tk.CENTER,
                                 wraplength=self.px(420))
        self.ov_text.pack()
        self.ov_btn = ttk.Button(self.overlay, command=self._overlay_action)
        self.ov_btn.pack(pady=14)

        # Status line
        stat = ttk.Frame(self)
        stat.pack(fill=tk.X, pady=(4, 0))
        self.status_l = ttk.Label(stat, foreground="#555")
        self.status_l.pack(side=tk.LEFT)
        self.status_r = ttk.Label(stat, foreground="#555")
        self.status_r.pack(side=tk.RIGHT)

        # Transfers
        xf = ttk.Frame(self)
        xf.pack(fill=tk.X, pady=(4, 0))
        self.prog = ttk.Progressbar(xf, mode="determinate", maximum=1000)
        self.prog.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.b_cancel = ttk.Button(xf, text="Cancel", command=self.cancel_transfers,
                                   state=tk.DISABLED)
        self.b_cancel.pack(side=tk.LEFT, padx=(6, 0))
        self.xfer_l = ttk.Label(self, foreground="#555", anchor=tk.W,
                                text="Tip: drag files from Explorer into this window "
                                     "to upload them here.")
        self.xfer_l.pack(fill=tk.X)

        self.menu = tk.Menu(self, tearoff=False)
        self._update_actions()

    def _bind_keys(self):
        t = self.tree
        t.bind("<Double-1>", self._on_double)
        t.bind("<Return>", lambda _e: self.open_selected())
        t.bind("<BackSpace>", lambda _e: self.go_up())
        t.bind("<Delete>", lambda _e: self.delete())
        t.bind("<F2>", lambda _e: self.rename())
        t.bind("<F5>", lambda _e: self.refresh())
        t.bind("<Control-a>", lambda _e: (t.selection_set(t.get_children()), "break")[1])
        t.bind("<Control-x>", lambda _e: self.to_clip("cut"))
        t.bind("<Control-c>", lambda _e: self.to_clip("copy"))
        t.bind("<Control-v>", lambda _e: self.paste())
        t.bind("<Alt-Left>", lambda _e: self.go_back())
        t.bind("<Alt-Right>", lambda _e: self.go_forward())
        t.bind("<Control-l>", lambda _e: self._focus_path())
        t.bind("<Control-f>", lambda _e: self.filter_entry.focus_set())
        t.bind("<Button-3>", self._on_right_click)
        t.bind("<<TreeviewSelect>>", lambda _e: (self._update_status(),
                                                 self._update_actions()))
        # Dragging rows onto folders (here or in the folder tree) moves them.
        t.bind("<ButtonPress-1>", self._drag_press)
        t.bind("<B1-Motion>", self._drag_motion)
        t.bind("<ButtonRelease-1>", self._drag_release)

        sd = self.side
        sd.bind("<<TreeviewOpen>>", self._side_open)
        sd.bind("<<TreeviewSelect>>", self._side_select)
        sd.bind("<Button-3>", self._side_menu)

    def _focus_path(self):
        self.path_entry.focus_set()
        self.path_entry.select_range(0, tk.END)
        return "break"

    # -- host integration -----------------------------------------
    def _ui(self, fn, *args):
        self.host._ui(fn, *args)

    def _say(self, text, tag=None):
        self.host._say(text, tag)

    def _params(self):
        try:
            port = int(self.port_var.get())
        except ValueError:
            raise ValueError("invalid FTP port")
        host, socks_port = self.host.browser_target()
        return host, port, ("127.0.0.1", socks_port)

    def _tick(self):
        """Keep the overlay in step with the bridge/tunnel state."""
        try:
            self._refresh_overlay()
            self._update_actions()
        finally:
            self.after(700, self._tick)

    def tunnel_down(self):
        self.disconnect(manual=False)
        self.autoconnect = True

    def shutdown(self):
        for s in (self.nav, self.xfer):
            if s:
                s.stop()
        self.nav = self.xfer = None

    def handle_drop(self, paths, point=None):
        """Files dropped from Windows Explorer: upload them.

        Into the folder under the pointer if it is one, else the current one.
        """
        if not (self.nav and self.connected):
            self._say("Connect to the console first, then drop the files again.",
                      "err")
            return
        dest = self._target_at(*point) if point else None
        self._upload(list(paths), dest=dest)

    # -- connection -----------------------------------------------
    def _toggle_conn(self):
        if self.nav:
            self.disconnect(manual=True)
        else:
            self.connect()

    def connect(self):
        if self.nav:
            return
        try:
            params = self._params()
        except ValueError as e:
            self._say(str(e), "err")
            return
        self.autoconnect = True
        self.xfer_fallback = False
        self.nav = Session(params)
        self.connected = False
        self._overlay_state = None
        self.back.clear()
        self.fwd.clear()
        self.side.delete(*self.side.get_children())
        self.side.insert("", tk.END, iid="/", text="  Console SD", image=self.ic_dir,
                         open=True)
        self._say(f"FTP: connecting to {params[0]}:{params[1]}", "dim")
        self.go("/", record=False, first=True)

    def disconnect(self, manual=True):
        if manual:
            self.autoconnect = False
        if self.nav or self.xfer:
            self.cancel_transfers(quiet=True)
        self.shutdown()
        self.connected = False
        self.loading = 0
        self.entries = {}
        self.clip = None
        self.tree.delete(*self.tree.get_children())
        self.side.delete(*self.side.get_children())
        self.path_var.set("/")
        self._overlay_state = None
        self._update_status()
        self._update_actions()

    def _lost(self, exc):
        """The browsing session failed and could not be redone."""
        self._say(f"FTP: {exc}", "err")
        self.shutdown()
        self.connected = False
        self.autoconnect = False
        self._overlay_state = None
        self._update_actions()

    # -- overlay --------------------------------------------------
    def _refresh_overlay(self):
        state = self.host.browser_state()
        if self.nav and self.connected:
            view = None
        elif self.nav:
            view = "connecting"
        elif state == "ready":
            if self.autoconnect:
                self.connect()
                view = "connecting"
            else:
                view = "ready"
        else:
            view = state
        if view == self._overlay_state:
            return
        self._overlay_state = view
        if view is None:
            self.overlay.place_forget()
            return
        title, text, btn = {
            "connecting": ("Connecting…", "Logging in to the console's FTP server.",
                           None),
            "ready": ("Not connected", "The SOCKS tunnel is up.", "Connect"),
            "no-tunnel": ("Browse the console's SD card",
                          "Files are reached through the SOCKS5 tunnel. Start it "
                          "and the browser connects on its own.",
                          "Start tunnel and connect"),
            "tunnel-starting": ("Starting the tunnel…",
                                "Waiting for the Pi to enter tunnel mode.", None),
            "other-tunnel": ("The TCP relay is running",
                             "Files need the SOCKS5 tunnel instead. Stop the "
                             "relay in the Bridge tab first.", None),
            "no-bridge": ("Bridge not running",
                          "Connect to the Pi and start the bridge from the top "
                          "bar, then come back here.", None),
        }.get(view, ("", "", None))
        self.ov_title.config(text=title)
        self.ov_text.config(text=text)
        if btn:
            self.ov_btn.config(text=btn)
            self.ov_btn.pack(pady=14)
        else:
            self.ov_btn.pack_forget()
        self.overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.overlay.lift()

    def _overlay_action(self):
        if self._overlay_state == "no-tunnel":
            self.autoconnect = True
            self.host.browser_start_socks()
        elif self._overlay_state == "ready":
            self.connect()

    # -- navigation -----------------------------------------------
    def _typed_path(self):
        p = self.path_var.get().strip().replace("\\", "/") or "/"
        if not p.startswith("/"):
            p = rjoin(self.cwd, p)
        parts = []
        for seg in p.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(seg)
        return "/" + "/".join(parts)

    def go(self, path, record=True, select=None, first=False):
        if not self.nav:
            return
        self._set_loading(+1)
        nav = self.nav

        def job(ftp):
            ftp.cwd(path)
            entries = ftp.listdir()
            self._ui(self._loaded, nav, path, entries, record, select)

        def fail(exc):
            self._ui(self._nav_failed, nav, path, exc, first)
        nav.submit(job, fail)

    def _loaded(self, nav, path, entries, record, select):
        self._set_loading(-1)
        if nav is not self.nav:
            return
        same = path == self.cwd
        keep = select or (list(self.tree.selection()) if same else [])
        if record and not same:
            self.back.append(self.cwd)
            self.fwd.clear()
        if not self.connected:
            self._say("FTP: connected", "dim")
        self.connected = True
        self.cwd = path
        self.path_var.set(path)
        self.entries = {e.name: e for e in entries}
        if not same:
            self.filter_var.set("")
        self._render(keep)
        if not same:
            self.tree.yview_moveto(0)
        self._side_sync(path, entries)
        self._refresh_overlay()

    def _nav_failed(self, nav, path, exc, first):
        self._set_loading(-1)
        if nav is not self.nav:
            return
        if first or (is_fatal(exc) and nav.ftp is None):
            self._lost(exc)
            return
        self.path_var.set(self.cwd)
        self._say(f"FTP: cannot open {path}: {exc}", "err")

    def refresh(self, select=None):
        self.go(self.cwd, record=False, select=select)

    def go_up(self):
        if self.cwd != "/":
            self.go(rparent(self.cwd), select=[self.cwd.rsplit("/", 1)[1]])

    def go_back(self):
        if self.back and self.nav:
            target = self.back.pop()
            self.fwd.append(self.cwd)
            self.go(target, record=False)

    def go_forward(self):
        if self.fwd and self.nav:
            target = self.fwd.pop()
            self.back.append(self.cwd)
            self.go(target, record=False)

    def _set_loading(self, delta):
        self.loading = max(0, self.loading + delta)
        self.tree.configure(cursor="watch" if self.loading else "")
        self._update_status()

    # -- folder tree (left pane) ----------------------------------
    def _side_fill(self, path, dirnames):
        """Make path's children in the tree exactly dirnames, keeping state."""
        sd = self.side
        if not sd.exists(path):
            return
        if sd.exists(path + self.DUMMY):
            sd.delete(path + self.DUMMY)
        want = sorted(dirnames, key=str.lower)
        have = {c for c in sd.get_children(path)}
        for c in have:
            if c.rsplit("/", 1)[-1] not in want:
                sd.delete(c)
        for i, name in enumerate(want):
            iid = rjoin(path, name)
            if not sd.exists(iid):
                sd.insert(path, i, iid=iid, text="  " + name, image=self.ic_dir)
                sd.insert(iid, tk.END, iid=iid + self.DUMMY, text="  …")
            else:
                sd.move(iid, path, i)

    def _side_sync(self, path, entries):
        sd = self.side
        if not sd.exists("/"):
            return
        # Make sure every ancestor has a node, so the current folder shows.
        node = "/"
        for seg in [p for p in path.split("/") if p]:
            child = rjoin(node, seg)
            if not sd.exists(child):
                if sd.exists(node + self.DUMMY):
                    sd.delete(node + self.DUMMY)
                sd.insert(node, tk.END, iid=child, text="  " + seg, image=self.ic_dir)
            sd.item(node, open=True)
            node = child
        self._side_fill(path, [e.name for e in entries if e.is_dir])
        if sd.selection() != (path,):
            sd.selection_set(path)
        sd.see(path)

    def _side_open(self, _event=None):
        node = self.side.focus()
        if node and self.side.exists(node + self.DUMMY):
            self._side_load(node)

    def _side_load(self, path):
        if not self.nav:
            return
        nav = self.nav

        def job(ftp):
            ftp.cwd(path)
            dirs = [e.name for e in ftp.listdir() if e.is_dir]
            self._ui(lambda: nav is self.nav and self._side_fill(path, dirs))
        nav.submit(job, lambda exc: self._ui(self._say, f"FTP: {exc}", "err"))

    def _side_select(self, _event=None):
        sel = self.side.selection()
        if sel and sel[0] != self.cwd and not sel[0].endswith(self.DUMMY):
            self.go(sel[0])

    def _side_menu(self, event):
        node = self.side.identify_row(event.y)
        if not node or node.endswith(self.DUMMY) or not self.connected:
            return
        m = self.menu
        m.delete(0, tk.END)
        m.add_command(label="Open", command=lambda: self.go(node))
        if self.clip:
            m.add_command(label=f"Paste into {node}",
                          command=lambda: self.paste(node))
        m.add_command(label="Upload files here…",
                      command=lambda: self.upload_files(node))
        m.add_command(label="Refresh", command=lambda: self._side_load(node))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _side_refresh(self, *paths):
        for p in paths:
            if self.side.exists(p) and p != self.cwd:
                self._side_load(p)

    # -- list -----------------------------------------------------
    def _sort_by(self, col):
        if self.sort_col == col:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_col, self.sort_rev = col, False
        self._render(list(self.tree.selection()))

    def _sorted(self):
        flt = self.filter_var.get().strip().lower()
        items = [e for e in self.entries.values()
                 if not flt or flt in e.name.lower()]
        key = {
            "#0": lambda e: e.name.lower(),
            "size": lambda e: e.size,
            "modified": lambda e: e.mtime.timestamp() if e.mtime else 0,
            "type": lambda e: (kind_of(e).lower(), e.name.lower()),
        }[self.sort_col]
        dirs = sorted((e for e in items if e.is_dir), key=key, reverse=self.sort_rev)
        files = sorted((e for e in items if not e.is_dir), key=key,
                       reverse=self.sort_rev)
        return dirs + files     # folders always first, like Explorer

    def _render(self, keep=None):
        t = self.tree
        keep = keep if keep is not None else list(t.selection())
        t.delete(*t.get_children())
        cut = set()
        if self.clip and self.clip["op"] == "cut" and self.clip["src"] == self.cwd:
            cut = {e.name for e in self.clip["entries"]}
        for i, e in enumerate(self._sorted()):
            if e.link:
                icon = self.ic_link
            elif e.is_dir:
                icon = self.ic_dir
            elif e.name.lower().endswith(".nro"):
                icon = self.ic_app
            else:
                icon = self.ic_file
            tags = ["odd"] if i % 2 else []
            if e.name in cut:
                tags.append("cut")
            t.insert("", tk.END, iid=e.name, text="  " + e.name, image=icon,
                     tags=tags, values=(
                         "" if e.is_dir else human(e.size),
                         e.mtime.strftime("%Y-%m-%d %H:%M") if e.mtime else "",
                         kind_of(e)))
        keep = [k for k in keep if t.exists(k)]
        if keep:
            t.selection_set(keep)
            t.focus(keep[0])
            t.see(keep[0])
        for col, text in (("#0", "Name"), ("size", "Size"),
                          ("modified", "Modified"), ("type", "Type")):
            arrow = (" ▼" if self.sort_rev else " ▲") if col == self.sort_col else ""
            t.heading(col, text=text + arrow)
        self._update_status()
        self._update_actions()

    def _selected(self):
        return [self.entries[i] for i in self.tree.selection() if i in self.entries]

    def _update_status(self):
        if not self.connected:
            self.status_l.config(text="")
        else:
            shown = self.tree.get_children()
            nd = sum(1 for i in shown if self.entries.get(i) and self.entries[i].is_dir)
            nf = len(shown) - nd
            text = f"{nd} folder{'s' * (nd != 1)}, {nf} file{'s' * (nf != 1)}"
            if self.filter_var.get().strip():
                text += f"  (filtered from {len(self.entries)})"
            sel = self._selected()
            if sel:
                size = sum(e.size for e in sel if not e.is_dir)
                text += f"   ·   {len(sel)} selected"
                if size:
                    text += f", {human(size)}"
            if self.clip:
                n = len(self.clip["entries"])
                text += f"   ·   {n} item{'s' * (n != 1)} to paste (Ctrl+V)"
            self.status_l.config(text=text)
        self.status_r.config(text="Loading…" if self.loading else "")

    def _update_actions(self):
        on = bool(self.nav and self.connected)
        sel = self._selected() if on else []
        st = lambda ok: tk.NORMAL if ok else tk.DISABLED    # noqa: E731
        self.b_back.config(state=st(on and self.back))
        self.b_fwd.config(state=st(on and self.fwd))
        self.b_up.config(state=st(on and self.cwd != "/"))
        self.b_ref.config(state=st(on))
        self.b_upload.config(state=st(on))
        self.b_newdir.config(state=st(on))
        self.b_download.config(state=st(sel))
        self.b_cut.config(state=st(sel))
        self.b_paste.config(state=st(on and self.clip))
        self.b_rename.config(state=st(len(sel) == 1))
        self.b_delete.config(state=st(sel))
        self.b_conn.config(text="Disconnect" if self.nav else "Connect",
                           state=st(self.nav or self.host.browser_state() == "ready"))

    # -- events ---------------------------------------------------
    def _on_double(self, event):
        if self.tree.identify_region(event.x, event.y) in ("tree", "cell"):
            self.open_selected()

    def open_selected(self):
        sel = self._selected()
        if len(sel) != 1:
            return
        e = sel[0]
        if e.is_dir:
            self.go(rjoin(self.cwd, e.name))
        else:
            self.download()

    def _on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
            self.tree.focus(row)
        sel = self._selected()
        on = bool(self.nav and self.connected)
        m = self.menu
        m.delete(0, tk.END)
        state = lambda ok: tk.NORMAL if ok and on else tk.DISABLED   # noqa: E731
        one = len(sel) == 1
        if one and sel[0].is_dir:
            m.add_command(label="Open", command=self.open_selected)
        if one and not sel[0].is_dir:
            m.add_command(label="Open on this PC", command=self.open_on_pc)
        m.add_command(label="Download…", command=self.download, state=state(sel))
        m.add_separator()
        m.add_command(label="Cut", accelerator="Ctrl+X",
                      command=lambda: self.to_clip("cut"), state=state(sel))
        m.add_command(label="Copy", accelerator="Ctrl+C",
                      command=lambda: self.to_clip("copy"), state=state(sel))
        if one and sel[0].is_dir and self.clip:
            target = rjoin(self.cwd, sel[0].name)
            m.add_command(label=f"Paste into {sel[0].name}",
                          command=lambda: self.paste(target))
        m.add_command(label="Paste here", accelerator="Ctrl+V", command=self.paste,
                      state=state(self.clip))
        m.add_separator()
        m.add_command(label="Rename", accelerator="F2", command=self.rename,
                      state=state(one))
        m.add_command(label="Delete", accelerator="Del", command=self.delete,
                      state=state(sel))
        m.add_command(label="Copy path", command=self.copy_path, state=state(sel))
        m.add_separator()
        m.add_command(label="New folder", command=self.new_folder, state=state(True))
        m.add_command(label="Upload files…", command=self.upload_files,
                      state=state(True))
        m.add_command(label="Upload folder…", command=self.upload_folder,
                      state=state(True))
        m.add_command(label="Refresh", accelerator="F5", command=self.refresh,
                      state=state(True))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def copy_path(self):
        paths = [rjoin(self.cwd, e.name) for e in self._selected()]
        if paths:
            self.clipboard_clear()
            self.clipboard_append("\n".join(paths))

    # -- drag rows onto folders -----------------------------------
    def _target_at(self, x_root, y_root, exclude=()):
        """Remote folder under a screen point (list row or tree node), or None."""
        w = self.winfo_containing(x_root, y_root)
        if w is self.tree:
            row = self.tree.identify_row(y_root - self.tree.winfo_rooty())
            e = self.entries.get(row)
            if e and e.is_dir and row not in exclude:
                return rjoin(self.cwd, row)
        elif w is self.side:
            node = self.side.identify_row(y_root - self.side.winfo_rooty())
            if node and not node.endswith(self.DUMMY):
                return node
        return None

    def _mark_drop(self, target):
        old = self._drop_mark
        if old == target:
            return
        for widget, iid in ((self.tree, old and old.rsplit("/", 1)[-1]),
                            (self.side, old)):
            if iid and widget.exists(iid):
                widget.item(iid, tags=[t for t in widget.item(iid, "tags")
                                       if t != "drop"])
        self._drop_mark = target
        if target:
            name = target.rsplit("/", 1)[-1]
            if rparent(target) == self.cwd and self.tree.exists(name):
                self.tree.item(name, tags=list(self.tree.item(name, "tags")) + ["drop"])
            if self.side.exists(target):
                self.side.item(target, tags=["drop"])

    def _drag_press(self, event):
        self._drag = None
        if self._editor or self.tree.identify_region(event.x, event.y) not in (
                "tree", "cell"):
            return
        row = self.tree.identify_row(event.y)
        if not row or row not in self.entries:
            return
        sel = self.tree.selection()
        mods = event.state & 0x0005          # Shift / Control
        self._drag = {"x": event.x_root, "y": event.y_root, "row": row,
                      "active": False, "deferred": False}
        if row in sel and len(sel) > 1 and not mods:
            # Keep the multi-selection so it can be dragged; a plain click
            # (no drag) narrows it on release, like Explorer.
            self._drag["deferred"] = True
            self.tree.focus_set()
            return "break"

    def _drag_motion(self, event):
        d = self._drag
        if not d:
            return
        if not d["active"]:
            if abs(event.x_root - d["x"]) + abs(event.y_root - d["y"]) < self.px(8):
                return "break" if d["deferred"] else None
            d["active"] = True
            d["items"] = list(self.tree.selection())
            self.tree.configure(cursor="fleur")
        target = self._target_at(event.x_root, event.y_root, exclude=d["items"])
        self._mark_drop(target)
        n = len(d["items"])
        self.status_r.config(text=(f"Move {n} item{'s' * (n != 1)} to {target}"
                                   if target else "Drop on a folder to move here"))
        return "break"

    def _drag_release(self, event):
        d, self._drag = self._drag, None
        if not d:
            return
        if d["active"]:
            target = self._drop_mark
            self._mark_drop(None)
            self.tree.configure(cursor="watch" if self.loading else "")
            self._update_status()
            if target:
                entries = [self.entries[i] for i in d["items"] if i in self.entries]
                self.move_to(entries, self.cwd, target)
            return "break"
        if d["deferred"]:
            self.tree.selection_set(d["row"])
            self.tree.focus(d["row"])

    # -- cut / copy / paste / move --------------------------------
    def to_clip(self, op):
        sel = self._selected()
        if not sel:
            return
        self.clip = {"op": op, "src": self.cwd, "entries": sel}
        verb = "cut" if op == "cut" else "copied"
        self._say(f"{len(sel)} item(s) {verb}: open the destination folder and "
                  f"paste (Ctrl+V)", "dim")
        self._render()

    def paste(self, dest=None):
        if not (self.clip and self.connected):
            return
        dest = dest or self.cwd
        clip = self.clip
        if clip["op"] == "cut":
            self.clip = None
            self.move_to(clip["entries"], clip["src"], dest)
        else:
            self.copy_to(clip["entries"], clip["src"], dest)
        self._render()

    def _check_dest(self, entries, src_dir, dest):
        if dest == src_dir:
            self._say("Those items are already in that folder.", "dim")
            return False
        for e in entries:
            p = rjoin(src_dir, e.name)
            if e.is_dir and (dest == p or dest.startswith(p + "/")):
                self._say(f"Cannot put '{e.name}' inside itself.", "err")
                return False
        return True

    def move_to(self, entries, src_dir, dest):
        if not entries or not self._check_dest(entries, src_dir, dest):
            return
        if self.rnto_paths is False:
            self._copy_job(entries, src_dir, dest, delete_source=True)
            return
        names = [e.name for e in entries]

        def job(ftp):
            done = []
            for i, e in enumerate(entries):
                try:
                    ftp.move(rjoin(src_dir, e.name), dest)
                except FtpError as exc:
                    if not done and self.rnto_paths is None:
                        # The server will not rename across folders: copy
                        # through the PC instead (slower, same result).
                        self.rnto_paths = False
                        self._ui(self._copy_job, entries[i:], src_dir, dest, True)
                        break
                    raise exc
                self.rnto_paths = True
                done.append(e.name)
            if done:
                self._ui(self._say, f"moved {', '.join(done)} → {dest}", "dim")
            self._ui(self._after_move, src_dir, dest)

        def fail(exc):
            self._ui(self._say, f"FTP: could not move {', '.join(names)}: {exc}",
                     "err")
            self._ui(self._after_move, src_dir, dest)
        self.nav.submit(job, fail, retry=False)

    def copy_to(self, entries, src_dir, dest):
        if entries and self._check_dest(entries, src_dir, dest):
            self._copy_job(entries, src_dir, dest, delete_source=False)

    def _after_move(self, src_dir, dest):
        if self.nav:
            if self.cwd in (src_dir, dest):
                self.refresh()
            self._side_refresh(src_dir, dest)

    def _copy_job(self, entries, src_dir, dest, delete_source):
        """Copy (or move) through the PC: FTP has no server-side copy."""
        verb = "move" if delete_source else "copy"
        label = f"{verb} of {entries[0].name}" if len(entries) == 1 \
            else f"{verb} of {len(entries)} items"

        def work(ftp):
            tmp = tempfile.mkdtemp(prefix="civswitch-copy-")
            try:
                for e in entries:
                    src = rjoin(src_dir, e.name)
                    dst = rjoin(dest, e.name)
                    dirs, files = [], []
                    if e.is_dir and not e.link:
                        dirs.append("")
                        for path, sub in ftp.walk(src):
                            rel = path[len(src) + 1:]
                            (dirs if sub.is_dir else files).append(
                                rel if sub.is_dir else (rel, sub.size))
                    else:
                        files.append(("", e.size))
                    count = len(files) * 2
                    for i, (rel, size) in enumerate(files, 1):
                        lp = os.path.join(tmp, e.name, *rel.split("/")) if rel \
                            else os.path.join(tmp, e.name)
                        os.makedirs(os.path.dirname(lp), exist_ok=True)
                        self._get_one(ftp, src + ("/" + rel if rel else ""), lp,
                                      size, i, count, verb="Copying ↓")
                    for d in dirs:
                        try:
                            ftp.mkdir(dst + ("/" + d if d else ""))
                        except FtpError:
                            pass
                    for i, (rel, size) in enumerate(files, len(files) + 1):
                        lp = os.path.join(tmp, e.name, *rel.split("/")) if rel \
                            else os.path.join(tmp, e.name)
                        tick = self._ticker("Copying ↑", os.path.basename(lp), size,
                                            i, count)
                        with open(lp, "rb") as fh:
                            ftp.store(fh, dst + ("/" + rel if rel else ""),
                                      progress=tick)
                    if delete_source:
                        if e.is_dir and not e.link:
                            ftp.rmtree(src)
                        else:
                            ftp.delete(src)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            done = "moved" if delete_source else "copied"
            self._ui(self.xfer_l.config, {"text": f"{done.capitalize()} "
                                                  f"{len(entries)} item(s) to {dest}"})
            self._ui(self._say, f"{done} {len(entries)} item(s) → {dest}", "dim")
            self._ui(self._after_move, src_dir, dest)
        self._queue_transfer(label, work)

    # -- inline editing -------------------------------------------
    def _inline_edit(self, iid, initial, on_done):
        if self._editor:
            return
        t = self.tree
        t.see(iid)
        t.update_idletasks()
        bbox = t.bbox(iid, "#0")
        if not bbox:
            on_done(None)
            return
        x, y, w, h = bbox
        ed = ttk.Entry(t)
        ed.insert(0, initial)
        stem = os.path.splitext(initial)[0] if "." in initial[1:] else initial
        ed.select_range(0, len(stem))
        ed.icursor(len(stem))
        off = self.px(26)
        ed.place(x=x + off, y=y + 1, width=max(w - off - 4, self.px(160)),
                 height=h - 2)
        ed.focus_set()
        self._editor = ed
        done = [False]

        def finish(commit):
            if done[0]:
                return
            done[0] = True
            value = ed.get().strip()
            ed.destroy()
            self._editor = None
            t.focus_set()
            on_done(value if commit else None)
        ed.bind("<Return>", lambda _e: finish(True))
        ed.bind("<KP_Enter>", lambda _e: finish(True))
        ed.bind("<Escape>", lambda _e: finish(False))
        ed.bind("<FocusOut>", lambda _e: finish(True))

    def _valid_name(self, name, current=None):
        if not name or name == current:
            return False
        if "/" in name or name in (".", ".."):
            self._say("A name cannot contain '/'.", "err")
            return False
        if name in self.entries:
            self._say(f"'{name}' already exists here.", "err")
            return False
        return True

    def rename(self):
        sel = self._selected()
        if len(sel) != 1 or not self.connected:
            return
        old = sel[0].name
        cwd = self.cwd

        def done(new):
            if not self._valid_name(new, old):
                return

            def job(ftp):
                ftp.rename(rjoin(cwd, old), new)
                self._ui(self._say, f"renamed {old} → {new}", "dim")
                self._ui(self._after_change, cwd, [new])
            self.nav.submit(job, self._op_error("rename"))
        self._inline_edit(old, old, done)

    def new_folder(self):
        if not self.connected:
            return
        base, n = "New folder", 2
        name = base
        while name in self.entries:
            name, n = f"{base} ({n})", n + 1
        tmp = "<new folder>"
        if self.tree.exists(tmp):
            return
        self.tree.insert("", 0, iid=tmp, text="  " + name, image=self.ic_dir)
        cwd = self.cwd

        def done(value):
            if self.tree.exists(tmp):
                self.tree.delete(tmp)
            if not value or not self._valid_name(value):
                return

            def job(ftp):
                ftp.mkdir(rjoin(cwd, value))
                self._ui(self._after_change, cwd, [value])
            self.nav.submit(job, self._op_error("create folder"))
        self._inline_edit(tmp, name, done)

    def delete(self):
        sel = self._selected()
        if not sel or not self.connected:
            return
        dirs = [e for e in sel if e.is_dir and not e.link]
        if len(sel) == 1:
            what = f"'{sel[0].name}'"
        else:
            what = f"these {len(sel)} items"
        extra = "\n\nFolders are deleted with everything inside them." if dirs else ""
        if not messagebox.askyesno("Delete", f"Delete {what} from the console?"
                                   f"{extra}\n\nThis cannot be undone.",
                                   icon=messagebox.WARNING, parent=self):
            return
        cwd = self.cwd
        names = [e.name for e in sel]

        def job(ftp):
            try:
                for e in sel:
                    path = rjoin(cwd, e.name)
                    if e.is_dir and not e.link:
                        ftp.rmtree(path, on_item=lambda p: self._ui(
                            self.status_r.config, {"text": f"Deleting {p}"}))
                    else:
                        ftp.delete(path)
                self._ui(self._say, f"deleted {', '.join(names)}", "dim")
            finally:
                self._ui(self._set_loading, -1)
                self._ui(self._after_change, cwd, [])
        self._set_loading(+1)
        self.nav.submit(job, self._op_error("delete"), retry=False)

    def _after_change(self, cwd, select):
        if self.nav and cwd == self.cwd:
            self.refresh(select=select or None)

    def _op_error(self, what):
        def handler(exc):
            self._ui(self._say, f"FTP: could not {what}: {exc}", "err")
        return handler

    # -- transfers ------------------------------------------------
    def _xfer_session(self):
        if self.xfer_fallback or not self.nav:
            return self.nav
        if self.xfer is None:
            self.xfer = Session(self.nav.params, on_connect_fail=self._xfer_cant_connect)
        return self.xfer

    def _xfer_cant_connect(self, fn, on_error, exc):
        # The server takes one login at a time: share the browsing session.
        def switch():
            first = not self.xfer_fallback
            self.xfer_fallback = True
            rest = []
            if self.xfer:
                while True:
                    try:
                        job = self.xfer.q.get_nowait()
                    except queue.Empty:
                        break
                    if job is not None:
                        rest.append(job)
                self.xfer.stop()
                self.xfer = None
            if first:
                self._say("FTP: the server allows one connection; transfers "
                          "will pause browsing while they run", "dim")
            if self.nav:
                for f, err, retry in [(fn, on_error, False)] + rest:
                    self.nav.submit(f, err, retry)
            else:
                on_error(exc)
        self._ui(switch)

    def _queue_transfer(self, label, work):
        """work(ftp) does the transfer; progress via self._ticker()."""
        sess = self._xfer_session()
        if not sess:
            return
        self.xfer_count += 1
        self._show_queue()

        def job(ftp):
            self.cancel_evt.clear()
            work(ftp)
            self._ui(self._transfer_done)

        def fail(exc):      # runs instead of _transfer_done above on any error
            self._ui(self._transfer_done)
            if isinstance(exc, Cancelled):
                self._ui(self._say, f"{label}: cancelled", "dim")
                self._ui(self.xfer_l.config, {"text": "Cancelled"})
            else:
                self._ui(self._say, f"{label} failed: {exc}", "err")
                self._ui(self.xfer_l.config, {"text": f"{label} failed"})
            self._ui(self._after_change, self.cwd, [])
        sess.submit(job, fail, retry=False)

    def _transfer_done(self):
        self.xfer_count = max(0, self.xfer_count - 1)
        self._show_queue()

    def _show_queue(self):
        busy = self.xfer_count > 0
        self.b_cancel.config(state=tk.NORMAL if busy else tk.DISABLED)
        if not busy:
            self.prog["value"] = 0

    def cancel_transfers(self, quiet=False):
        dropped = self.xfer.clear() if self.xfer else 0
        self.xfer_count = max(0, self.xfer_count - dropped)
        self.cancel_evt.set()
        self._show_queue()
        if not quiet and (dropped or self.xfer_count):
            self.xfer_l.config(text="Cancelling…")

    def _ticker(self, verb, name, size, idx, count):
        t0 = time.monotonic()
        last = [0.0]

        def tick(done):
            if self.cancel_evt.is_set():
                raise Cancelled()
            now = time.monotonic()
            if now - last[0] < 0.15 and done != size:
                return
            last[0] = now
            rate = done / max(now - t0, 0.001)
            self._ui(self._show_progress, verb, name, done, size, rate, idx, count)
        return tick

    def _show_progress(self, verb, name, done, size, rate, idx, count):
        parts = [f"{verb} {name}"]
        if count > 1:
            parts.insert(0, f"[{idx}/{count}]")
        if size:
            self.prog["value"] = min(1000, int(done * 1000 / size))
            parts.append(f"{done * 100 // size}%")
            parts.append(f"{human(done)} / {human(size)}")
            if rate > 0 and done < size:
                parts.append(f"{fmt_eta((size - done) / rate)} left")
        else:
            parts.append(human(done))
        parts.append(f"{human(rate)}/s")
        more = self.xfer_count - 1
        if more > 0:
            parts.append(f"(+{more} queued)")
        self.xfer_l.config(text="  ·  ".join(parts))

    def _get_one(self, ftp, remote, local, size, idx, count, verb="Downloading"):
        part = local + ".part"
        tick = self._ticker(verb, os.path.basename(local), size, idx, count)
        try:
            with open(part, "wb") as fh:
                ftp.retrieve(remote, fh, progress=tick, size=size or None)
            os.replace(part, local)
        except BaseException:
            try:
                os.remove(part)
            except OSError:
                pass
            raise

    # downloads
    def download(self):
        sel = self._selected()
        if not sel or not self.connected:
            return
        if len(sel) == 1 and not sel[0].is_dir:
            e = sel[0]
            local = filedialog.asksaveasfilename(parent=self, initialfile=e.name,
                                                 initialdir=self.last_local)
            if not local:
                return
            self.last_local = os.path.dirname(local)
            self._download_into(sel, None, single=local)
            return
        dest = filedialog.askdirectory(parent=self, initialdir=self.last_local,
                                       title="Download to folder")
        if not dest:
            return
        self.last_local = dest
        clash = [e.name for e in sel if os.path.exists(os.path.join(dest, e.name))]
        if clash and not messagebox.askyesno(
                "Overwrite?", f"{len(clash)} item(s) already exist in {dest}, "
                f"e.g. '{clash[0]}'. Overwrite them?", parent=self):
            return
        self._download_into(sel, dest)

    def _download_into(self, entries, dest, single=None, then=None):
        cwd = self.cwd
        label = f"download of {entries[0].name}" if len(entries) == 1 \
            else f"download of {len(entries)} items"

        def work(ftp):
            files = []
            for e in entries:
                rpath = rjoin(cwd, e.name)
                if single:
                    files.append((rpath, single, e.size))
                    continue
                lpath = os.path.join(dest, e.name)
                if e.is_dir:
                    os.makedirs(lpath, exist_ok=True)
                    self._ui(self.xfer_l.config, {"text": f"Scanning {e.name}…"})
                    for path, sub in ftp.walk(rpath):
                        if self.cancel_evt.is_set():
                            raise Cancelled()
                        rel = path[len(rpath) + 1:]
                        lp = os.path.join(lpath, *rel.split("/"))
                        if sub.is_dir:
                            os.makedirs(lp, exist_ok=True)
                        else:
                            files.append((path, lp, sub.size))
                else:
                    files.append((rpath, lpath, e.size))
            total = sum(f[2] for f in files)
            for i, (r, l, size) in enumerate(files, 1):
                self._get_one(ftp, r, l, size, i, len(files))
            where = single or dest
            self._ui(self.xfer_l.config,
                     {"text": f"Downloaded {len(files)} file(s), {human(total)}"})
            self._ui(self._say, f"downloaded {len(files)} file(s), {human(total)} "
                     f"→ {where}", "dim")
            if then:
                self._ui(then)
        self._queue_transfer(label, work)

    def open_on_pc(self):
        sel = self._selected()
        if len(sel) != 1 or sel[0].is_dir:
            return
        local = os.path.join(tempfile.mkdtemp(prefix="civswitch-"), sel[0].name)

        def opened():
            try:
                os.startfile(local)       # noqa: S606 -- the user asked to open it
            except Exception as e:
                self._say(f"cannot open {local}: {e}", "err")
        self._download_into(sel, None, single=local, then=opened)

    # uploads
    def upload_files(self, dest=None):
        if not self.connected:
            return
        paths = filedialog.askopenfilenames(parent=self, initialdir=self.last_local,
                                            title="Upload files")
        if paths:
            self.last_local = os.path.dirname(paths[0])
            self._upload(list(paths), dest=dest)

    def upload_folder(self):
        if not self.connected:
            return
        path = filedialog.askdirectory(parent=self, initialdir=self.last_local,
                                       title="Upload folder")
        if path:
            self.last_local = os.path.dirname(path)
            self._upload([path])

    def _upload(self, paths, dest=None):
        paths = [os.path.normpath(p) for p in paths if os.path.exists(p)]
        if not paths:
            return
        dest = dest or self.cwd
        names = [os.path.basename(p) for p in paths]
        if dest == self.cwd:
            clash = [n for n in names
                     if n in self.entries and not self.entries[n].is_dir]
            if clash and not messagebox.askyesno(
                    "Overwrite?", f"{len(clash)} file(s) already exist on the "
                    f"console, e.g. '{clash[0]}'. Overwrite them?", parent=self):
                return
        label = f"upload of {names[0]}" if len(names) == 1 \
            else f"upload of {len(names)} items"
        self._say(f"uploading {', '.join(names)} → {dest}", "dim")

        def work(ftp):
            plan = []
            for p in paths:
                r = rjoin(dest, os.path.basename(p))
                if os.path.isdir(p):
                    plan.append(("mkdir", r, None, 0))
                    for root, dirs, files in os.walk(p):
                        rel = os.path.relpath(root, p)
                        rbase = r if rel == "." else \
                            r + "/" + rel.replace(os.sep, "/")
                        for d in sorted(dirs):
                            plan.append(("mkdir", rbase + "/" + d, None, 0))
                        for f in sorted(files):
                            lp = os.path.join(root, f)
                            plan.append(("put", rbase + "/" + f, lp,
                                         os.path.getsize(lp)))
                else:
                    plan.append(("put", r, p, os.path.getsize(p)))
            puts = [x for x in plan if x[0] == "put"]
            idx = unconfirmed = 0
            for kind, remote, local, size in plan:
                if self.cancel_evt.is_set():
                    raise Cancelled()
                if kind == "mkdir":
                    try:
                        ftp.mkdir(remote)
                    except FtpError:
                        pass        # usually "already exists"
                    continue
                idx += 1
                tick = self._ticker("Uploading", os.path.basename(local), size,
                                    idx, len(puts))
                with open(local, "rb") as fh:
                    ftp.store(fh, remote, progress=tick)
                if not ftp.confirmed:
                    unconfirmed += 1
            total = sum(x[3] for x in puts)
            self._ui(self.xfer_l.config,
                     {"text": f"Uploaded {len(puts)} file(s), {human(total)}"})
            self._ui(self._say, f"uploaded {len(puts)} file(s), {human(total)}",
                     "dim")
            if unconfirmed:
                self._ui(self._say, f"{unconfirmed} upload(s) were not confirmed by "
                         "the console -- check the sizes. Copy the updated "
                         "pi/tunnel.py to the Pi to fix this.", "err")
            self._ui(self._side_refresh, dest)
            self._ui(self._after_change, dest, names)
        self._queue_transfer(label, work)
