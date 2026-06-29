# Cortex AI Agent — Build & Run Reference

## Dev Mode
```
python src/main.py
```

## Automated Build (Recommended)
```
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
powershell -ExecutionPolicy Bypass -File build.ps1
```

## Manual Build (Step by Step)
```
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
.\venv\Scripts\python.exe -m PyInstaller cortex.spec --clean --noconfirm
.\build_installer.bat
```

## Run Compiled .exe
```
cd dist\Cortex
.\Cortex.exe
```

## LSP Audit
```
python pyright_audit.py --json-out pyright_report.json
```

## Build Notes
- `cortex.spec` — PyInstaller spec file (bundles bin/, node_modules/pyright, node_modules/typescript-language-server, src/ui/html/, src/assets/, plugins/, .env.example)
- `build.ps1` — Automated build script (uses `python -m PyInstaller` since `pyinstaller` CLI is not in PATH)
- Runtime hooks: noconsole, encodings, certifi, asyncio
- Console=False — no terminal window on launch
- Icon: src/assets/logo/logo.ico
- Inno Setup required for installer build (https://jrsoftware.org/isdl.php)

## Troubleshooting
| Issue | Fix |
|---|---|
| `pyinstaller` not recognized | Use `python -m PyInstaller` instead |
| `cortex.spec` missing | File created — run build again |
| Encodings import error | Handled by runtime_hook_encodings.py |
| asyncio TypeError on Python 3.14 | Handled by runtime_hook_asyncio.py |
| TLS cert errors in frozen build | Handled by runtime_hook_certifi.py |
| Console window popup | Handled by runtime_hook_noconsole.py |
