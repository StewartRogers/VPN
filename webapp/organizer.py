import os
import re

_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v"}

# Matches quality/release tags that mark the end of the meaningful title,
# plus everything that follows. Mirrors organize.py's clean_filename.
_STRIP_RE = re.compile(
    r"[\s.]+"
    r"(\d{3,4}[pP]"                                         # 1080p, 720p, 4K…
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
    """Return a cleaned version of a video filename.

    Steps (mirrors stopvpn.sh clean_filename):
    1. Strip quality/release tags and everything after
    2. Strip bracket/parenthesis content
    3. Normalise dots/spaces → Title.Case.With.Dots.ext
    """
    name, ext = os.path.splitext(filename)
    ext = ext.lower().lstrip(".")

    # 1. Strip quality tags and everything after
    name = _STRIP_RE.sub("", name)

    # 2. Strip bracket and parenthesis blocks
    name = re.sub(r"\[.*?\]", "", name)
    name = re.sub(r"\(.*?\)", "", name)

    # 3. Normalise: collapse runs of dots/spaces, strip leading/trailing
    name = re.sub(r"[\s.]+", ".", name).strip(".")

    if not name:
        return filename  # nothing useful — return original unchanged

    return f"{name}.{ext}"


def scan_directory(source_dir: str, exclude_dirs: set = None,
                   skip_junk: bool = True, exclude_paths=None) -> list:
    """Walk source_dir and return metadata for every video file found.

    exclude_dirs: directory basenames to skip, matched case-insensitively
    at any depth (e.g. {"Samples", ".stfolder"}).

    exclude_paths: absolute directory paths to skip entirely, matched by path
    rather than by name. This is how a destination folder nested inside the
    source stays out of the results — excluding it by basename would also drop
    every unrelated folder that happens to share the name.

    skip_junk: leave out sample media and sample/proof/screenshot folders.
    These are the same things the delete step treats as junk, so including
    them here would move a 30-second sample into the output folder and then
    delete the folder it came from — the two steps have to agree on what
    counts as a real file.
    """
    source_dir = os.path.realpath(source_dir)
    exclude_lower = {d.lower() for d in (exclude_dirs or ())}
    exclude_abs = {os.path.realpath(p) for p in (exclude_paths or ()) if p}
    results = []

    for root, dirs, files in os.walk(source_dir):
        dirs[:] = sorted(d for d in dirs
                         if d.lower() not in exclude_lower
                         and not (skip_junk and d.lower() in _JUNK_DIRS)
                         and os.path.realpath(os.path.join(root, d)) not in exclude_abs)
        for fname in sorted(files):
            _, ext = os.path.splitext(fname)
            if ext.lower() not in _VIDEO_EXTS:
                continue
            if skip_junk and is_junk_file(os.path.join(root, fname)):
                continue
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, source_dir)
            in_subdir = os.path.dirname(rel_path) not in ("", ".")
            proposed = clean_filename(fname)
            results.append({
                "original": rel_path,
                "proposed": proposed,
                "in_subdir": in_subdir,
                "size": os.path.getsize(full_path),
            })

    return results


def organize_files(source_dir: str, operations: list) -> list:
    """Apply rename / flatten operations.

    Each operation dict:
        original  – relative path from source_dir
        rename_to – new filename (basename only); empty = keep original name
        flatten   – if True, move file to source_dir root

    Returns a list of result dicts with keys: original, status, message,
    and (on success) renamed_to.
    """
    source_dir = os.path.realpath(source_dir)
    results = []

    for op in operations:
        original_rel = op.get("original", "")
        rename_to = (op.get("rename_to") or "").strip()
        flatten = bool(op.get("flatten", False))

        src = os.path.realpath(os.path.join(source_dir, original_rel))

        # Security: reject path traversal
        if not src.startswith(source_dir + os.sep):
            results.append({"original": original_rel, "status": "error",
                             "message": "Path outside source directory"})
            continue

        if not os.path.isfile(src):
            results.append({"original": original_rel, "status": "error",
                             "message": "File not found"})
            continue

        dst_dir = source_dir if flatten else os.path.dirname(src)
        dst_name = os.path.basename(rename_to) if rename_to else os.path.basename(src)
        dst = os.path.realpath(os.path.join(dst_dir, dst_name))

        # Security: destination must stay inside source_dir
        if not dst.startswith(source_dir + os.sep):
            results.append({"original": original_rel, "status": "error",
                             "message": "Destination outside source directory"})
            continue

        if src == dst:
            results.append({"original": original_rel, "status": "skipped",
                             "message": "Already has the target name"})
            continue

        if os.path.exists(dst):
            if os.path.getsize(src) == os.path.getsize(dst):
                results.append({"original": original_rel, "status": "skipped",
                                 "message": f"Duplicate already exists: {dst_name}"})
            else:
                results.append({"original": original_rel, "status": "error",
                                 "message": f"Destination exists with different size: {dst_name}"})
            continue

        try:
            os.rename(src, dst)
            # Remove the subdirectory if it is now empty
            src_dir = os.path.dirname(src)
            if src_dir != source_dir:
                try:
                    os.rmdir(src_dir)
                except OSError:
                    pass
            results.append({"original": original_rel, "status": "ok",
                             "renamed_to": os.path.relpath(dst, source_dir)})
        except Exception as exc:
            results.append({"original": original_rel, "status": "error",
                             "message": str(exc)})

    return results


# ---------------------------------------------------------------- output moves

# Named leftovers, matched case-insensitively. cleanup_source() deletes the
# whole folder regardless of this list — it exists so scan_directory() can
# skip sample junk before it is ever scanned as a real video, and so cleanup's
# result messages can label a deleted file "junk" vs. just "file".
_JUNK_EXTS = {
    ".nfo", ".sfv", ".md5", ".txt", ".url", ".diz",   # metadata sidecars
    ".torrent", ".pad", ".exe",                        # torrent/scene admin
    ".jpg", ".jpeg", ".png", ".gif",                   # artwork/screenshots
}
_JUNK_DIRS = {"sample", "samples", "proof", "screens", "screenshots", "subs.sample"}
_JUNK_NAME_RE = re.compile(r"(^|[\s._-])sample([\s._-]|$)|^rarbg", re.IGNORECASE)


def is_junk_file(path: str) -> bool:
    """True if `path` is a leftover the delete step may remove."""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if ext in _JUNK_EXTS:
        return True
    # Sample *media* — real video files, so only by explicit name match.
    if ext in _VIDEO_EXTS and _JUNK_NAME_RE.search(os.path.splitext(name)[0]):
        return True
    return False


def is_junk_dir(path: str) -> bool:
    return os.path.basename(path).lower() in _JUNK_DIRS


def move_file(src: str, dst: str, chunk_cb=None) -> dict:
    """Move one file to `dst`, returning a result dict.

    os.rename cannot cross filesystems (EXDEV), and an output folder on a
    different mount is the normal case here — /mnt/hdddisk to /mnt/bluedrive.
    So this falls back to a copy-then-verify-then-unlink: the source is only
    unlinked once the destination exists at the full expected size. A partial
    copy therefore leaves the source intact rather than destroying the only
    good copy, which matters because the delete step runs after this.

    chunk_cb(bytes_done, total) is called during the slow path so callers can
    report progress and tell when the move is genuinely finished.
    """
    if os.path.exists(dst):
        if os.path.getsize(src) == os.path.getsize(dst):
            return {"status": "skipped", "message": "Duplicate already at destination"}
        return {"status": "error", "message": "Destination exists with different size"}

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    total = os.path.getsize(src)

    try:
        os.rename(src, dst)
        if chunk_cb:
            chunk_cb(total, total)
        return {"status": "moved", "message": "Moved (same filesystem)", "bytes": total}
    except OSError:
        pass  # cross-device — fall through to copy

    done = 0
    try:
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            while True:
                buf = fsrc.read(4 * 1024 * 1024)
                if not buf:
                    break
                fdst.write(buf)
                done += len(buf)
                if chunk_cb:
                    chunk_cb(done, total)
            fdst.flush()
            os.fsync(fdst.fileno())
    except Exception as exc:
        if os.path.exists(dst):
            try:
                os.unlink(dst)      # never leave a truncated file behind
            except OSError:
                pass
        return {"status": "error", "message": f"Copy failed: {exc}"}

    # Only now is the move complete. Verify before touching the source.
    if not os.path.exists(dst) or os.path.getsize(dst) != total:
        return {"status": "error", "message": "Copy incomplete — source kept"}
    try:
        os.unlink(src)
    except OSError as exc:
        return {"status": "error", "message": f"Copied but could not remove source: {exc}"}
    return {"status": "moved", "message": "Moved (copied across filesystems)", "bytes": total}


def _within(path: str, roots: list) -> bool:
    """True if `path` is one of `roots` or sits inside one."""
    return any(path == r or path.startswith(r + os.sep) for r in roots)


def cleanup_source(folder: str, source_root: str, exclude_roots=None) -> list:
    """Delete `folder` and everything in it.

    `folder` only gets here once at least one video was confirmed moved out of
    it, and at that point the operator wants the whole source folder gone —
    subtitles, `.nfo`, artwork, samples, and anything else still in it,
    including a video that did not move. That last part is a deliberate
    choice (2026-09-23): an unmoved video here is lost, so the web UI asks for
    confirmation before calling this.

    Content decides nothing. Three structural guards do, and they hold
    whatever the folder contains:

    - Only ever descends inside `source_root`, and never removes source_root
      itself.
    - Nothing at or below `exclude_roots` (every Movies/TV folder the operator
      has configured) is ever touched. A destination may legitimately sit
      inside the source tree, and the caller cannot enforce this on its own —
      it only sees the *source* folder of each moved file, which can be an
      ancestor of a destination rather than inside one. A folder holding a
      destination is emptied around it and kept.
    - Symlinks are removed, never followed: a linked folder's target survives.

    Returns a list of {path, status, message} describing what happened.
    """
    source_root = os.path.realpath(source_root)
    folder = os.path.realpath(folder)
    excluded = [os.path.realpath(p) for p in (exclude_roots or [])]
    results = []
    if folder == source_root or not folder.startswith(source_root + os.sep):
        return [{"path": folder, "status": "error",
                 "message": "Refusing to clean outside the source directory"}]
    if _within(folder, excluded):
        return [{"path": os.path.relpath(folder, source_root), "status": "kept",
                 "message": "inside a destination folder — not cleaned"}]
    if not os.path.isdir(folder):
        return results

    def _rel(p):
        return os.path.relpath(p, source_root)

    # followlinks=False (the default): a symlinked directory is listed in
    # `dirs` but never entered, and is unlinked below like a file.
    for root, dirs, files in os.walk(folder, topdown=False):
        if _within(os.path.realpath(root), excluded):
            continue
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                os.unlink(fpath)
                results.append({"path": _rel(fpath), "status": "deleted",
                                "message": "junk file" if is_junk_file(fpath) else "file"})
            except OSError as exc:
                results.append({"path": _rel(fpath), "status": "error",
                                "message": str(exc)})
        for dname in dirs:
            dpath = os.path.join(root, dname)
            try:
                if os.path.islink(dpath):
                    os.unlink(dpath)
                    results.append({"path": _rel(dpath), "status": "deleted",
                                    "message": "link"})
                    continue
                if _within(os.path.realpath(dpath), excluded) or os.listdir(dpath):
                    continue  # a destination, or holds one — reported below
                os.rmdir(dpath)
                results.append({"path": _rel(dpath), "status": "deleted",
                                "message": "folder"})
            except OSError as exc:
                results.append({"path": _rel(dpath), "status": "error",
                                "message": str(exc)})

    try:
        if not os.listdir(folder):
            os.rmdir(folder)
            results.append({"path": _rel(folder), "status": "deleted",
                            "message": "source folder"})
        else:
            results.append({"path": _rel(folder), "status": "kept",
                            "message": "holds a destination folder, or an item "
                                       "that could not be deleted"})
    except OSError as exc:
        results.append({"path": _rel(folder), "status": "error",
                        "message": str(exc)})
    return results


def browse(path: str) -> dict:
    """List subdirectories of `path` for the folder picker."""
    path = os.path.realpath(os.path.expanduser(path or "/"))
    if not os.path.isdir(path):
        raise NotADirectoryError(path)
    entries = []
    with os.scandir(path) as it:
        for e in it:
            try:
                if e.is_dir(follow_symlinks=False):
                    entries.append({"name": e.name,
                                    "path": os.path.join(path, e.name)})
            except OSError:
                continue
    entries.sort(key=lambda d: d["name"].lower())
    parent = os.path.dirname(path)
    return {"path": path,
            "parent": parent if parent != path else None,
            "dirs": entries}
