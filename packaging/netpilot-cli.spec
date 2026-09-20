# PyInstaller spec: the `netpilot` command-line tool as a single .exe.
#
# Build:  pyinstaller packaging/netpilot-cli.spec
# Output: dist/netpilot.exe (Windows) / dist/netpilot (Linux, macOS)

from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("netpilot", includes=["web/static/*"])

analysis = Analysis(
    ["netpilot_cli_entry.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "netpilot.web.app",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # the CLI never draws a window
        "PyQt6",
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        "tkinter",
    ],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="netpilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
