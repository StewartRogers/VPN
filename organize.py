#!/usr/bin/env python3
"""
Interactive video file renamer / mover.

Usage:  python3 organize.py
"""

import os
import re
import shutil
import sys
from collections import defaultdict

_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v"}

_STRIP_RE = re.compile(
    r"[\s.]+"
    r"(\d{3,4}[pP]"
    r"|4[kK]"
    r"|HEVC|x26[45]|H\.?26[45]|AVC|XVID"
    r"|BluRay|BLU-?RAY|WEBRip|WEB-?DL|WEB|AMZN|FLUX|HULU|NF"
    r"|YIFY|RARBG|MeGusta|SPARKS|FGT|EZTV"
    r"|10bit|8bit|HDR|SDR|HDR10|DV|DoVi"
    r"|AAC[\d.]*|DDP[\d.]*|DD[\d.]*|FLAC|AC3|DTS|ATMOS|TrueHD"
    r"|REMUX|EXTENDED|THEATRICAL|DIRECTORS\.?CUT|DC|IMAX"
    r"|REPACK|PROPER|READNFO|INTERNAL"
    r").*$",
    re.IGNORECASE,
)


def clean_filename(filename: str) -> str:
    name, ext = os.path.splitext(filename)
    name = _STRIP_RE.sub("", name)
    name = re.sub(r"\[.*?\]", "", name)
    name = re.sub(r"\(.*?\)", "", name)
    name = re.sub(r"[\s.]+", ".", name).strip(".")
    if not name:
        return filename
    return f"{name}{ext.lower()}"


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"  {label}{suffix}: ").strip()
    return value if value else default


def _yn(label: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"  {label} [{hint}]: ").strip().lower()
    return default if not value else value.startswith("y")


def _dest_prompt(movies_dir: str, tv_dir: str) -> str:
    """Return chosen destination directory, or empty string to skip."""
    while True:
        raw = input("  Destination  [M]ovies / [T]V / [O]ther / [S]kip: ").strip().lower()
        if raw in ("m", "movies"):
            return movies_dir
        if raw in ("t", "tv"):
            return tv_dir
        if raw in ("o", "other"):
            path = _prompt("Path")
            if path:
                return os.path.realpath(os.path.expanduser(path))
        if raw in ("s", "skip"):
            return ""
        print("  Enter M, T, O, or S.")


def scan_grouped(source_dir: str, recursive: bool) -> tuple[list[dict], dict[str, list[dict]]]:
    """
    Returns root_files (files directly in source_dir) and subdir_map
    (ordered mapping of relative subdir path -> list of file dicts).
    """
    root_files: list[dict] = []
    subdir_map: dict[str, list[dict]] = defaultdict(list)

    def make_entry(full: str) -> dict:
        fname = os.path.basename(full)
        return {
            "path": full,
            "rel": os.path.relpath(full, source_dir),
            "name": fname,
            "proposed": clean_filename(fname),
            "size": os.path.getsize(full),
        }

    if recursive:
        for root, dirs, files in os.walk(source_dir):
            dirs.sort()
            at_root = os.path.realpath(root) == os.path.realpath(source_dir)
            for fname in sorted(files):
                if os.path.splitext(fname)[1].lower() in _VIDEO_EXTS:
                    full = os.path.join(root, fname)
                    if at_root:
                        root_files.append(make_entry(full))
                    else:
                        # Key is the top-level subdir, not the full nested path,
                        # so each immediate child of source_dir gets one prompt.
                        rel_full = os.path.relpath(root, source_dir)
                        top = rel_full.split(os.sep)[0]
                        subdir_map[top].append(make_entry(full))
    else:
        for fname in sorted(os.listdir(source_dir)):
            full = os.path.join(source_dir, fname)
            if os.path.isfile(full) and os.path.splitext(fname)[1].lower() in _VIDEO_EXTS:
                root_files.append(make_entry(full))

    return root_files, dict(subdir_map)


def _remove_empty_parents(start_dir: str, stop_at: str) -> None:
    current = start_dir
    while os.path.realpath(current) != os.path.realpath(stop_at):
        try:
            if os.listdir(current):
                break
            rel = os.path.relpath(current, stop_at)
            if not _yn(f"  Remove empty '{rel}'?", default=True):
                break
            os.rmdir(current)
            print(f"  ✓ Removed '{rel}'")
            current = os.path.dirname(current)
        except OSError as exc:
            print(f"  Warning: {exc}")
            break


def process_file(
    f: dict,
    source_dir: str,
    movies_dir: str,
    tv_dir: str,
    counter: str,
) -> str:
    """Process one file interactively. Returns 'moved', 'skipped', or 'error'."""
    print(f"  {counter}  {f['rel']}")

    # Rename
    proposed = f["proposed"]
    if proposed != f["name"]:
        print(f"  Proposed:  {proposed}")
        choice = input("  Rename?    [Y/n/e(dit)]: ").strip().lower()
        if choice == "e":
            manual = input("  New name:  ").strip()
            if manual:
                proposed = os.path.basename(manual)
        elif choice == "n":
            proposed = f["name"]
    else:
        print(f"  Name:      {proposed}  (unchanged)")

    # Destination
    dest_dir = _dest_prompt(movies_dir, tv_dir)
    if not dest_dir:
        print("  Skipped.")
        print()
        return "skipped"

    final = os.path.join(dest_dir, proposed)
    print(f"  → {final}")

    # Collision
    if os.path.exists(final):
        if os.path.getsize(final) == f["size"]:
            print("  Skipped: identical file already at destination.")
            print()
            return "skipped"
        print("  Warning: file already exists at destination with a different size.")
        if not _yn("  Overwrite?", default=False):
            print("  Skipped.")
            print()
            return "skipped"

    # No-op
    if os.path.realpath(f["path"]) == os.path.realpath(final):
        print("  No changes needed.")
        print()
        return "skipped"

    if not _yn("  Confirm?", default=True):
        print("  Skipped.")
        print()
        return "skipped"

    try:
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(f["path"], final)
        print("  ✓ Moved.")
        src_parent = os.path.dirname(f["path"])
        if os.path.realpath(src_parent) != os.path.realpath(source_dir):
            _remove_empty_parents(src_parent, source_dir)
        print()
        return "moved"
    except Exception as exc:
        print(f"  ✗ Error: {exc}")
        print()
        return "error"


def main() -> None:
    print()
    print("  Video File Organizer")
    print("  " + "─" * 40)
    print()

    raw = _prompt("Source directory")
    if not raw:
        print("  Aborted.")
        sys.exit(0)
    source_dir = os.path.realpath(os.path.expanduser(raw))
    if not os.path.isdir(source_dir):
        print(f"  Error: not a directory: {source_dir}")
        sys.exit(1)

    recursive = _yn("Scan subdirectories?", default=True)
    movies_dir = os.path.realpath(os.path.expanduser(_prompt("Movies directory")))
    tv_dir = os.path.realpath(os.path.expanduser(_prompt("TV directory")))

    print()
    print("  Scanning...", end=" ", flush=True)
    root_files, subdir_map = scan_grouped(source_dir, recursive)
    total = len(root_files) + sum(len(v) for v in subdir_map.values())
    print(f"{total} video file(s) found.")

    if not total:
        print()
        sys.exit(0)

    print()
    moved = skipped = errors = 0
    counter = 0

    # Subdirectories — one accept/deny prompt per top-level subdir
    for rel_dir, files in subdir_map.items():
        n = len(files)
        label = f"'{rel_dir}'" + (f"  ({n} file)" if n == 1 else f"  ({n} files)")
        print(f"  ┌─ Subdirectory: {label}")
        if not _yn("  Process?", default=True):
            skipped += n
            print(f"  └─ Skipped.\n")
            continue
        print()
        for f in files:
            counter += 1
            result = process_file(f, source_dir, movies_dir, tv_dir, f"[{counter}/{total}]")
            moved += result == "moved"
            skipped += result == "skipped"
            errors += result == "error"

    # Root-level files (no directory prompt — already in the download root)
    if root_files:
        if subdir_map:
            print("  ─── Root directory ───────────────────────\n")
        for f in root_files:
            counter += 1
            result = process_file(f, source_dir, movies_dir, tv_dir, f"[{counter}/{total}]")
            moved += result == "moved"
            skipped += result == "skipped"
            errors += result == "error"

    print("  " + "─" * 40)
    print(f"  Moved: {moved}   Skipped: {skipped}   Errors: {errors}")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        sys.exit(0)
