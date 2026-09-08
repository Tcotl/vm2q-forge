from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = Path(__file__).with_name("driver_assets.json")


def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Driver manifest must be an object: {path}")
    return data


def _verify(path: Path, metadata: dict[str, Any]) -> str:
    expected_size = metadata.get("size")
    if expected_size is not None and path.stat().st_size != int(expected_size):
        raise ValueError(
            f"Size mismatch for {path.name}: "
            f"expected {expected_size}, got {path.stat().st_size}"
        )
    expected = metadata.get("sha256")
    digest = _sha256(path)
    if expected and digest.lower() != str(expected).lower():
        raise ValueError(
            f"SHA-256 mismatch for {path.name}: "
            f"expected {expected}, got {digest}"
        )
    return digest


def fetch_assets(
    output_dir: Path,
    manifest_path: Path = MANIFEST_PATH,
    force: bool = False,
) -> tuple[Path, ...]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(manifest_path)
    downloaded: list[Path] = []

    for version in ("0.1.173-9", "0.1.285-1"):
        metadata = manifest.get(version)
        if not metadata:
            raise ValueError(f"Driver manifest has no entry for {version}")
        filename = str(metadata["filename"])
        url = str(metadata["url"])
        destination = output_dir / filename
        if destination.exists() and not force:
            digest = _verify(destination, metadata)
            print(f"existing={destination} sha256={digest}")
            downloaded.append(destination)
            continue

        with tempfile.NamedTemporaryFile(
            prefix=f".{filename}.",
            suffix=".download",
            dir=output_dir,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)

        try:
            print(f"download={url}")
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "VM2Q-Forge asset builder"},
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                with temporary_path.open("wb") as target:
                    shutil.copyfileobj(response, target)
            digest = _verify(temporary_path, metadata)
            temporary_path.replace(destination)
            print(f"saved={destination} sha256={digest}")
            downloaded.append(destination)
        finally:
            temporary_path.unlink(missing_ok=True)

    return tuple(downloaded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download and verify the VirtIO driver assets for offline bundles."
    )
    parser.add_argument("--output-dir", default="work/driver-assets")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        fetch_assets(
            Path(args.output_dir),
            manifest_path=args.manifest.expanduser().resolve(),
            force=args.force,
        )
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
