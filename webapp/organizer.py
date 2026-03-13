import os
import re

_VIDEO_EXTS = {".mp4", ".mkv"}

# Matches quality/release tags that mark the end of the meaningful title,
# plus everything that follows. Mirrors the sed pipeline in stopvpn.sh.
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


def scan_directory(source_dir: str) -> list:
    """Walk source_dir and return metadata for every video file found."""
    source_dir = os.path.realpath(source_dir)
    results = []

    for root, dirs, files in os.walk(source_dir):
        dirs.sort()
        for fname in sorted(files):
            _, ext = os.path.splitext(fname)
            if ext.lower() not in _VIDEO_EXTS:
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
        dst_name = rename_to if rename_to else os.path.basename(src)
        dst = os.path.realpath(os.path.join(dst_dir, dst_name))

        # Security: destination must stay inside source_dir
        if not dst.startswith(source_dir):
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
