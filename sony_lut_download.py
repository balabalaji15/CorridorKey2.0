#!/usr/bin/env python3
"""Download Sony's official S-Log LUT archives and install .cube files for CorridorKey."""

from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ARCHIVES = (
    (
        "Sony S-Log2 / S-Gamut",
        "https://di.update.sony.net/NEX/ciOkozszIz/Look_profile_for_resolve_S-Gamut_Slog2.zip",
        "S-Log2",
    ),
    (
        "Sony S-Log3 / S-Gamut3.Cine",
        "https://di.update.sony.net/NEX/ciOkozszIz/Look_profile_for_resolve_S-Gamut3.cine_Slog3.zip",
        "S-Log3-S-Gamut3.Cine",
    ),
)
MAX_ARCHIVE_BYTES = 40 * 1024 * 1024
MAX_ONE_CUBE_BYTES = 50 * 1024 * 1024
MAX_EXTRACT_TOTAL_BYTES = 200 * 1024 * 1024


def download_zip(label: str, url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "CorridorKey/2.0 Sony LUT installer",
            "Accept": "application/zip, application/octet-stream;q=0.9, */*;q=0.8",
            "Referer": "https://www.sony.co.in/electronics/support/software/00263050",
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = response.read(MAX_ARCHIVE_BYTES + 1)
    if not payload:
        raise RuntimeError(f"{label}: Sony returned an empty download.")
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise RuntimeError(f"{label}: archive exceeds the 40 MB safety limit.")
    if not payload.startswith(b"PK"):
        raise RuntimeError(f"{label}: downloaded response is not a ZIP archive.")
    return payload


def extract_cubes(label: str, payload: bytes, destination: Path, total_written: int) -> tuple[int, int]:
    written = 0
    count = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload), "r")
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"{label}: Sony download is not a valid ZIP file.") from exc

    with archive:
        entries = [
            entry for entry in archive.infolist()
            if not entry.is_dir()
            and PurePosixPath(entry.filename.replace("\\", "/")).suffix.casefold() == ".cube"
        ]
        if not entries:
            raise RuntimeError(f"{label}: no .cube files were found in the Sony ZIP.")

        for entry in entries:
            if entry.file_size <= 0 or entry.file_size > MAX_ONE_CUBE_BYTES:
                print(f"  Skipping empty/oversized LUT: {entry.filename}")
                continue
            if total_written + written + entry.file_size > MAX_EXTRACT_TOTAL_BYTES:
                raise RuntimeError("Extracted LUT data exceeds the 200 MB safety limit.")

            # Save only the basename in its family folder to prevent ZIP path traversal.
            basename = Path(PurePosixPath(entry.filename.replace("\\", "/")).name).name
            if not basename:
                continue
            target = destination / basename

            with archive.open(entry, "r") as source:
                header = source.read(256 * 1024)
                if b"LUT_3D_SIZE" not in header.upper():
                    print(f"  Skipping invalid CUBE file: {entry.filename}")
                    continue

                fd, temp_name = tempfile.mkstemp(
                    prefix=f".{target.stem}.", suffix=".tmp", dir=str(destination)
                )
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(header)
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            written += len(chunk)
                    os.replace(temp_name, target)
                finally:
                    try:
                        os.unlink(temp_name)
                    except FileNotFoundError:
                        pass

            count += 1
            print(f"  Installed: {target.name}")

    if count == 0:
        raise RuntimeError(f"{label}: the archive contained no valid 3D .cube LUT files.")
    return count, written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()

    # Windows cmd.exe can leave literal wrapping quotes in an argument when
    # a path ends with a backslash before the closing quote. Strip them here
    # as a second line of defence; the BAT launcher also removes the trailing
    # separator before passing --root.
    root_text = str(args.root).strip().strip('"').strip()
    project_root = Path(root_text).expanduser().resolve()
    destination_root = project_root / "resources" / "luts" / "sony"
    log_dir = project_root / "logs"
    destination_root.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "sony_lut_install.log"

    imported = 0
    total_written = 0
    errors: list[str] = []
    log_lines: list[str] = []

    def report(message: str) -> None:
        print(message, flush=True)
        log_lines.append(message)

    report("CorridorKey — automatic Sony LUT installer")
    report(f"Project folder: {project_root}")
    report(f"LUT destination: {destination_root}")
    report("No browser or file picker will be opened.")

    for label, url, folder in ARCHIVES:
        family_dir = destination_root / folder
        family_dir.mkdir(parents=True, exist_ok=True)
        report(f"\nDownloading {label}...")
        try:
            payload = download_zip(label, url)
            count, written = extract_cubes(label, payload, family_dir, total_written)
            imported += count
            total_written += written
            report(f"Completed {label}: {count} valid LUT(s).")
        except Exception as exc:
            message = f"{label}: {type(exc).__name__}: {exc}"
            errors.append(message)
            report(f"ERROR: {message}")

    report(f"\nFinished. Installed {imported} Sony .cube LUT file(s).")
    report(f"Log: {log_file}")
    if errors:
        report("Some downloads failed:")
        for error in errors:
            report(f" - {error}")

    log_file.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return 0 if imported > 0 and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
