"""Accept files dragged from Windows Explorer onto a Tk window.

Tk has no native drag and drop on Windows. The classic WM_DROPFILES message
is enough for "drop files from Explorer": register the toplevel window with
DragAcceptFiles and subclass its window procedure to catch the message.
No third-party package needed.

The window procedure runs inside Tk's own message dispatch, so it only hands
the paths to `callback`, which must be thread/reentrancy-safe (the GUI passes
a function that just queues work for its main loop).
"""

import ctypes
import sys

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4


def enable(widget, callback):
    """Call callback(paths, (x_root, y_root)) when files are dropped.

    Returns True if drag and drop is active.
    """
    if sys.platform != "win32":
        return False
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)
        user32.GetParent.restype = wintypes.HWND
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                             ctypes.c_void_p]
        user32.CallWindowProcW.restype = LRESULT
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                           wintypes.UINT, wintypes.WPARAM,
                                           wintypes.LPARAM]
        user32.ClientToScreen.argtypes = [wintypes.HWND,
                                          ctypes.POINTER(wintypes.POINT)]
        shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        shell32.DragQueryFileW.restype = wintypes.UINT
        shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                           wintypes.LPWSTR, wintypes.UINT]
        shell32.DragQueryPoint.argtypes = [ctypes.c_void_p,
                                           ctypes.POINTER(wintypes.POINT)]
        shell32.DragFinish.argtypes = [ctypes.c_void_p]

        widget.update_idletasks()
        hwnd = user32.GetParent(widget.winfo_id()) or widget.winfo_id()
        state = {}

        def proc(h, msg, wparam, lparam):
            if msg == WM_DROPFILES:
                try:
                    hdrop = ctypes.c_void_p(wparam)
                    n = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
                    paths = []
                    for i in range(n):
                        length = shell32.DragQueryFileW(hdrop, i, None, 0)
                        buf = ctypes.create_unicode_buffer(length + 1)
                        shell32.DragQueryFileW(hdrop, i, buf, length + 1)
                        paths.append(buf.value)
                    pt = wintypes.POINT()
                    shell32.DragQueryPoint(hdrop, ctypes.byref(pt))
                    user32.ClientToScreen(h, ctypes.byref(pt))
                    shell32.DragFinish(hdrop)
                    callback(paths, (pt.x, pt.y))
                except Exception:
                    pass
                return 0
            return user32.CallWindowProcW(state["old"], h, msg, wparam, lparam)

        state["proc"] = WNDPROC(proc)          # keep a reference alive
        state["old"] = user32.SetWindowLongPtrW(
            hwnd, GWLP_WNDPROC, ctypes.cast(state["proc"], ctypes.c_void_p))
        if not state["old"]:
            return False
        shell32.DragAcceptFiles(hwnd, True)
        widget._win_drop_state = state
        return True
    except Exception:
        return False


def make_dpi_aware():
    """Render crisp on high-DPI screens instead of bitmap-stretched and blurry.

    Must run before the first Tk window is created.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
