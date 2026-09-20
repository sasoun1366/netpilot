# PyInstaller spec: the `netpilot` command-line tool as a single .exe.
#
# Build:  pyinstaller packaging/netpilot-cli.spec
# Output: dist/netpilot.exe (Windows) / dist/netpilot (Linux, macOS)

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

# The entry script lives next to this spec, but the analysis must also see the
# repository root so `import netpilot` resolves even from a non-editable checkout.
SPEC_DIR = Path(SPECPATH)  # noqa: F821 - injected by PyInstaller
REPO_ROOT = SPEC_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

datas = collect_data_files("netpilot", includes=["web/static/*"])

analysis = Analysis(
    [str(SPEC_DIR / "netpilot_cli_entry.py")],
    pathex=[str(REPO_ROOT)],
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
