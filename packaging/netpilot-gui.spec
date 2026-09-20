# PyInstaller spec: the netpilot desktop application as a single .exe.
#
# Build:  pyinstaller packaging/netpilot-gui.spec
# Output: dist/netpilot-gui.exe (Windows) / dist/netpilot-gui (Linux, macOS)

from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("netpilot", includes=["web/static/*"])

analysis = Analysis(
    ["netpilot_gui_entry.py"],
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
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="netpilot-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # no console window behind the app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
