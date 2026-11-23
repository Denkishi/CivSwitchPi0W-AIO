# PyInstaller spec for CivSwitch Bridge.
#   Build:  python -m PyInstaller pc/civswitch.spec --noconfirm
#   Output: dist/CivSwitch.exe  (one self-contained file, no Python needed)
#
# The GUI re-launches itself with `--run-tunnel socks|tcp` to be the tunnel
# child process, so socks_tunnel and tcp_proxy must be bundled even though the
# GUI only imports them inside a function.

a = Analysis(
    ['civswitch_gui.py'],
    pathex=['pc'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'socks_tunnel', 'tcp_proxy', 'pilink',
        'pi_tab', 'payload_tab', 'ftp_browser', 'ftp_client',
        'gui_extras', 'win_drop',
        'serial', 'serial.tools.list_ports',
        'PIL', 'PIL.Image', 'PIL.ImageTk',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['numpy', 'pytest', 'setuptools'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='CivSwitch',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # windowed app (no console window)
    disable_windowed_traceback=False,
)
# One-file build: all binaries and datas are passed into EXE above and there
# is no COLLECT step, so PyInstaller packs everything into a single .exe.
