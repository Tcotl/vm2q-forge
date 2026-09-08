# AGENTS.md

VM2Q Forge converts VMware work directories/ZIP archives to qcow2, with VirtIO
driver injection, QEMU Guest Agent installation (WinPE/DISM bake), portable
bundle builds, and a bilingual (zh_CN/en_US) Tkinter GUI. README and `docs/`
are written in Chinese — keep new docs consistent with that.

## Layout

- `vmware2qcow2/` — core package. Stdlib only (`dependencies = []` in
  pyproject); do not add third-party runtime imports.
  - `core.py` — conversion orchestrator (~2400 lines); tests import its
    private helpers (`_parse_vmx`, `_driver_plan`, `_network_plan`, ...), so
    renaming them breaks tests.
  - `model.py` — frozen dataclasses + `Literal` types shared by all layers;
    add new state here, not ad-hoc dicts.
  - `cli.py`, `gui.py` (`ConverterApp`), `i18n.py`, `runtime.py`, `winpe.py`,
    `drivers.py`, `resources.py`.
- `tests/` — `unittest.TestCase` suites; local only, never pushed to GitHub
  (whole directory gitignored); scratch files go under `tests/.tmp/`.
  `test_gui.py` is an AST structural test asserting
  `ConverterApp` defines `_conversion_options` and not `_options`.
- `packaging/` — `build_portable.py` (ZIP bundles), `build_gui_release.py`
  (native DMG/EXE), `build_winpe_iso.py`, `fetch_driver_assets.py`.
- `docs/design.md` — layering and runtime-resolution design; read before
  touching `core.py`/`runtime.py`. Also `docs/winpe.md`,
  `docs/linux-compatibility.md`.
- `assets/winpe/startnet.cmd` — WinPE DISM entry injected into boot.wim.
- `.github/workflows/build.yml` (CI + wheels), `release.yml` (release matrix).

## Commands

```bash
python -m unittest discover -s tests -v   # local tests (tests/ is not published)
python -m compileall -q vmware2qcow2 tests packaging
python -m pip install ".[build]"          # build + pyinstaller extras
python -m build                           # wheel + sdist
python3 -m vmware2qcow2 <vm-dir|zip> --output out.qcow2   # run CLI
python3 -m vmware2qcow2.gui               # run GUI
python packaging/build_portable.py --edition online --flavor cli ...   # portable ZIP
python packaging/build_gui_release.py --edition online ...             # native DMG/EXE
```

## Rules and gotchas

- Python 3.10+ (`from __future__ import annotations`, `X | Y` unions). CI
  matrix: Ubuntu 3.10/3.13, macOS 3.12, Windows 3.12.
- Driver ISO binaries (virtio-win 0.1.173-9 / 0.1.285-1, WinPE) are never
  committed; they live in `vmware2qcow2/drivers/`, `~/.cache/vmware2qcow2/`,
  or build-time `assets/drivers/` (all gitignored). Tests must not require
  them, and must not require `qemu-img` on PATH.
- Never commit generated artifacts: `outputs/`, `work/`, `dist/`, `build/`,
  `runtime/` are gitignored; the whole `tests/` directory is local-only too.
- GUI strings live in `vmware2qcow2/i18n.py` (`TRANSLATIONS`, keys per
  language, default `zh_CN`). Adding/changing UI labels means updating both
  languages; language switching must preserve form state.
- Runtime resolution order (`runtime.py`, `resources.py`) is load-bearing for
  frozen/portable apps: `VM2Q_RUNTIME_DIR` → `sys._MEIPASS/runtime/<os>-<arch>/`
  → macOS `.app` Frameworks/Resources → portable `runtime/` → program-dir
  `runtime/` → system PATH. Host identity is normalized to
  `macos|linux|windows|other` × `amd64|arm64|x86|other`.
- Windows conversions default to `require_qga=True` with the WinPE/DISM
  backend; `--no-require-qga` is the explicit legacy escape hatch. QGA bake
  credentials come from `VM2Q_WINDOWS_BAKE_USERNAME/PASSWORD` env vars.
- Releases: 6 OS/arch targets × `online|offline` × `cli|gui`. Offline builds
  need the `VM2Q_WINPE_AMD64_URL`/`VM2Q_WINPE_AMD64_SHA256` secrets. Native
  GUI artifacts (DMG = macOS arm64, EXE = Windows amd64) must be built on the
  target host platform; macOS uses ad-hoc signing only.
- Conversions never overwrite existing user outputs unless `--overwrite` is
  set; failed stages must keep logs and command records.
