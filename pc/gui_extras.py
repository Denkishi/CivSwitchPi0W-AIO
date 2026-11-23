"""Notifications and light/dark theming for the GUI.

Notifications are deliberately dependency-free: an in-app toast that slides
into the corner and fades, plus a taskbar flash when the window is not
focused (the real "tell me when it is done" signal on Windows). No tray icon,
no PowerShell, nothing to install.
"""

import sys
import tkinter as tk
from tkinter import ttk

# -- palettes ---------------------------------------------------------------
LIGHT = {
    "bg": "#f0f0f0", "panel": "#f0f0f0", "field": "#ffffff", "text": "#1a1a1a",
    "muted": "#555555", "border": "#c8c8c8", "accent": "#2a7", "sel": "#cce4ff",
    "sel_fg": "#000000", "odd": "#f6f7f9", "tree_bg": "#ffffff",
    "log_bg": "#ffffff", "ok": "#2a7", "err": "#b33", "dim": "#888",
    "me": "#36c", "cut": "#9aa0a6", "drop": "#ffe9a8", "toast": "#323232",
    "toast_fg": "#ffffff",
}
DARK = {
    "bg": "#232629", "panel": "#2b2f33", "field": "#1b1e21", "text": "#e6e6e6",
    "muted": "#a0a6ad", "border": "#3a3f44", "accent": "#4fd18b", "sel": "#2d4b6b",
    "sel_fg": "#ffffff", "odd": "#2a2e32", "tree_bg": "#1f2225",
    "log_bg": "#1b1e21", "ok": "#4fd18b", "err": "#ff6b6b", "dim": "#8b9199",
    "me": "#6ab0ff", "cut": "#6b7076", "drop": "#5a4a1e", "toast": "#e8e8e8",
    "toast_fg": "#1a1a1a",
}


def palette(dark):
    return DARK if dark else LIGHT


def apply_theme(root, dark):
    """Recolour ttk styles and the plain tk widgets to match the palette."""
    c = palette(dark)
    st = ttk.Style(root)
    st.theme_use("clam")
    root.configure(background=c["bg"])
    st.configure(".", background=c["bg"], foreground=c["text"],
                 fieldbackground=c["field"], bordercolor=c["border"],
                 lightcolor=c["bg"], darkcolor=c["bg"])
    st.configure("TFrame", background=c["bg"])
    st.configure("TLabel", background=c["bg"], foreground=c["text"])
    st.configure("TLabelframe", background=c["bg"], foreground=c["text"])
    st.configure("TLabelframe.Label", background=c["bg"], foreground=c["text"])
    st.configure("TButton", background=c["panel"], foreground=c["text"])
    st.map("TButton", background=[("active", c["sel"]), ("disabled", c["bg"])],
           foreground=[("disabled", c["muted"])])
    st.configure("Tool.TButton", background=c["panel"], foreground=c["text"])
    st.configure("TMenubutton", background=c["panel"], foreground=c["text"])
    st.configure("TCheckbutton", background=c["bg"], foreground=c["text"])
    st.map("TCheckbutton", background=[("active", c["bg"])])
    st.configure("TEntry", fieldbackground=c["field"], foreground=c["text"],
                 insertcolor=c["text"])
    st.configure("TCombobox", fieldbackground=c["field"], foreground=c["text"])
    st.map("TCombobox", fieldbackground=[("readonly", c["field"])],
           foreground=[("readonly", c["text"])])
    st.configure("TNotebook", background=c["bg"])
    st.configure("TNotebook.Tab", background=c["panel"], foreground=c["text"],
                 padding=(10, 4))
    st.map("TNotebook.Tab", background=[("selected", c["field"])],
           foreground=[("selected", c["text"])])
    for tv in ("Treeview", "Files.Treeview", "Side.Treeview"):
        st.configure(tv, background=c["tree_bg"], fieldbackground=c["tree_bg"],
                     foreground=c["text"])
        st.map(tv, background=[("selected", c["sel"])],
               foreground=[("selected", c["sel_fg"])])
    st.configure("Treeview.Heading", background=c["panel"], foreground=c["text"])
    st.map("Treeview.Heading", background=[("active", c["sel"])])
    st.configure("TProgressbar", background=c["accent"], troughcolor=c["field"])
    st.configure("TPanedwindow", background=c["bg"])
    st.configure("Vertical.TScrollbar", background=c["panel"],
                 troughcolor=c["bg"], arrowcolor=c["text"])
    return c


class Toast(tk.Toplevel):
    """A small borderless message that slides in bottom-right and fades out."""

    def __init__(self, root, title, body, colors, ms=4200):
        super().__init__(root)
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        bg, fg = colors["toast"], colors["toast_fg"]
        self.configure(background=bg)
        frame = tk.Frame(self, background=bg, padx=14, pady=10,
                         highlightthickness=1, highlightbackground=colors["accent"])
        frame.pack(fill=tk.BOTH, expand=True)
        tk.Label(frame, text=title, background=bg, foreground=colors["accent"],
                 font=("Segoe UI", 10, "bold"), anchor="w",
                 justify="left").pack(anchor="w")
        tk.Label(frame, text=body, background=bg, foreground=fg,
                 font=("Segoe UI", 9), anchor="w", justify="left",
                 wraplength=300).pack(anchor="w", pady=(2, 0))
        self.bind("<Button-1>", lambda _e: self._close())
        for w in (frame, *frame.winfo_children()):
            w.bind("<Button-1>", lambda _e: self._close())
        self.update_idletasks()
        self._place()
        self._alpha = 0.0
        self._fade_in()
        self._timer = self.after(ms, self._fade_out)

    def _place(self):
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = self.winfo_reqwidth()
        h = self.winfo_reqheight()
        self.geometry(f"+{sw - w - 24}+{sh - h - 60}")

    def _set(self, a):
        try:
            self.attributes("-alpha", a)
        except tk.TclError:
            pass

    def _fade_in(self):
        self._alpha = min(0.95, self._alpha + 0.15)
        self._set(self._alpha)
        if self._alpha < 0.95:
            self.after(20, self._fade_in)

    def _fade_out(self):
        self._alpha = max(0.0, self._alpha - 0.1)
        self._set(self._alpha)
        if self._alpha > 0:
            self.after(25, self._fade_out)
        else:
            self._close()

    def _close(self):
        try:
            self.after_cancel(self._timer)
        except Exception:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass


def is_foreground(root):
    """True if this window is the active one. Assume yes off Windows."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        return ctypes.windll.user32.GetForegroundWindow() == hwnd
    except Exception:
        return True


def flash_taskbar(root):
    """Flash the taskbar button if the window is not in the foreground."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        if ctypes.windll.user32.GetForegroundWindow() == hwnd:
            return

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                        ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                        ("dwTimeout", wintypes.DWORD)]
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 5, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass
