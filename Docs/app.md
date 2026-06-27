# ── Run in dev mode ──
python src/main.py



# ── Manual build (step by step) ──
Remove-Item -Recurse -Force build, dist
.\venv\Scripts\python.exe -m PyInstaller cortex.spec --clean --noconfirm
.\build_installer.bat

# ── Run compiled .exe ──
cd dist\Cortex
.\Cortex.exe

# ── LSP audit ──
python pyright_audit.py --json-out pyright_report.json

powershell -ExecutionPolicy Bypass -File build.ps1

Latest
===============

# 1. Main build — add --clean to flush stale asyncio cache
.\venv\Scripts\python.exe -m PyInstaller cortex.spec --clean --noconfirm

# 2. Rebuild installer
Run Inno Setup compiler on cortex_setup.iss
==============================

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
.\venv\Scripts\python.exe -m PyInstaller cortex.spec --clean --noconfirm



--------------------
New Command 
=====================
powershell -ExecutionPolicy Bypass -File build.ps1
===================
