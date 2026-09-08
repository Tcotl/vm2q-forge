from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable


def _unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        try:
            normalized = str(path.expanduser().resolve())
        except OSError:
            normalized = str(path.expanduser())
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(Path(normalized))
    return tuple(result)


def application_resource_roots() -> tuple[Path, ...]:
    """Return data roots for source, PyInstaller one-file, and macOS app builds."""

    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))

    executable = Path(sys.executable).expanduser()
    try:
        executable = executable.resolve()
    except OSError:
        pass
    for parent in (executable.parent, *executable.parents):
        if parent.name == "Contents":
            roots.extend(
                (
                    parent / "Resources",
                    parent / "Frameworks",
                )
            )
            break

    package_dir = Path(__file__).resolve().parent
    roots.extend((package_dir, package_dir.parent))
    return _unique_paths(roots)
