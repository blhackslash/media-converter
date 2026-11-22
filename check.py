import os
import sys
import json
import re
import shutil
import concurrent.futures
from datetime import datetime

from common import (
    INPUT_DIR, OUTPUT_DIR, MODE, STOP, Plan, env_choice, env_float, env_int,
    is_partial, remove_quietly, scan_inputs, walk_files,
)

# --- Configuration ---
CHECKS = env_choice("CHECKS", "all", {"all", "integrity", "metadata", "none"})
DATE_TOLERANCE_SECONDS = env_int("DATE_TOLERANCE_SECONDS", 86400, minimum=0)  # Default 24h for timezone drift
DURATION_TOLERANCE_SECONDS = env_float("DURATION_TOLERANCE_SECONDS", 5.0)  # 5 second leniency

# Dynamic Threading: Defaults to (CPU cores + 4) up to 32 if not specified
DEFAULT_WORKERS = min(32, (os.cpu_count() or 1) + 4)
CHECK_THREADS = env_int("CHECK_THREADS", DEFAULT_WORKERS, minimum=1)

def check_structural_integrity(file_path):
    # Check 1: Is the file completely empty?
    if file_path.stat().st_size == 0:
        return False, "File is 0 bytes."

    # Check 2: FFprobe container scan
    res = STOP.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(file_path)
    ], capture_stdout=True)
    stderr = res.stderr.decode("utf-8", errors="replace").strip()
    if res.returncode == 0 and not stderr:
        return True, ""

    # Older ffmpeg builds cannot parse HEIC/AVIF containers at all. Before
    # declaring such a file corrupt (and deleting it), ask libheif.
    if file_path.suffix.lower() in (".heic", ".heif", ".avif") and shutil.which("heif-info"):
        if STOP.run(["heif-info", str(file_path)]).returncode == 0:
            return True, ""

    return False, stderr.replace('\n', ' ')[:200] or f"ffprobe exited with code {res.returncode}"


def extract_gps(meta):
    lat, lon = meta.get("GPSLatitude"), meta.get("GPSLongitude")

    if lat is not None and lon is not None:
        try:
            return float(lat), float(lon)
        except (TypeError, ValueError):
            pass

    coords = meta.get("GPSCoordinates")
    if coords:
        parts = str(coords).split()
        if len(parts) >= 2:
            try:
                return float(parts[0]), float(parts[1])
            except ValueError:
                pass

    return None, None

def get_metadata(file_path):
    # Added -Duration to track if the converted file was truncated
    cmd = ["exiftool", "-j", "-n", "-DateTimeOriginal", "-CreationDate", "-CreateDate",
           "-GPSLatitude", "-GPSLongitude", "-GPSCoordinates", "-Duration", "-ee", str(file_path)]
    try:
        res = STOP.run(cmd, capture_stdout=True)
        if res.returncode == 0:
            data = json.loads(res.stdout.decode("utf-8", errors="replace"))
            if data:
                return data[0]
    except (ValueError, OSError):
        pass
    return {}


def parse_base_date(date_str):
    if not date_str: return None
    match = re.search(r"(\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2})", str(date_str))
    if match:
        try:
            # Convert to actual datetime object for math
            return datetime.strptime(match.group(1), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            pass
    return None

def compare_metadata(meta_src, meta_dst):
    discrepancies = []

    # 1. Date Comparison with Tolerance
    dates_src = [parse_base_date(meta_src.get(k)) for k in ["DateTimeOriginal", "CreationDate", "CreateDate"]]
    dates_dst = [parse_base_date(meta_dst.get(k)) for k in ["DateTimeOriginal", "CreationDate", "CreateDate"]]

    # Filter out None values
    dates_src = [d for d in dates_src if d]
    dates_dst = [d for d in dates_dst if d]

    if dates_src:
        if not dates_dst:
            discrepancies.append("Missing Date")
        else:
            # Look for ANY valid match within the tolerance window
            match_found = False
            for d_src in dates_src:
                for d_dst in dates_dst:
                    if abs((d_src - d_dst).total_seconds()) <= DATE_TOLERANCE_SECONDS:
                        match_found = True
                        break
                if match_found: break
            
            if not match_found:
                discrepancies.append(f"Date Mismatch (exceeded {DATE_TOLERANCE_SECONDS}s tolerance)")

    # 2. GPS Comparison
    lat_src, lon_src = extract_gps(meta_src)
    lat_dst, lon_dst = extract_gps(meta_dst)

    if lat_src is not None and lon_src is not None:
        if lat_dst is None or lon_dst is None:
            discrepancies.append("Missing GPS Data")
        else:
            if abs(lat_src - lat_dst) > 0.0001:
                discrepancies.append("Latitude Mismatch")
            if abs(lon_src - lon_dst) > 0.0001:
                discrepancies.append("Longitude Mismatch")

    # 3. Duration Comparison (Ensures the file wasn't truncated)
    dur_src = meta_src.get("Duration")
    dur_dst = meta_dst.get("Duration")
    if dur_src is not None and dur_dst is not None:
        try:
            if abs(float(dur_src) - float(dur_dst)) > DURATION_TOLERANCE_SECONDS:
                discrepancies.append("Duration Mismatch (File may be truncated)")
        except (TypeError, ValueError):
            pass

    if not discrepancies: return True, ""
    return False, " | ".join(discrepancies)

def check_single_file(out_path, in_path):
    """Worker function for checking a single file."""
    name = str(out_path.relative_to(OUTPUT_DIR))
    if STOP.stopped:
        return {"status": "CANCELLED", "file": name, "msg": ""}
    if not in_path:
        return {"status": "MISSING", "file": name, "msg": "No matching input file found."}

    try:
        # 1. Structural Check
        if CHECKS in ("all", "integrity"):
            is_struct_ok, struct_err = check_structural_integrity(out_path)
            if STOP.stopped:
                return {"status": "CANCELLED", "file": name, "msg": ""}
            if not is_struct_ok:
                remove_quietly(out_path)
                return {"status": "FAIL", "file": name, "msg": f"CORRUPT FILE: {struct_err} -> Deleted corrupted file."}

        # 2. Metadata Check & Fix
        if CHECKS in ("all", "metadata"):
            is_meta_ok, meta_err = compare_metadata(get_metadata(in_path), get_metadata(out_path))
            if STOP.stopped:
                return {"status": "CANCELLED", "file": name, "msg": ""}
            if not is_meta_ok:
                msg_prefix = f"METADATA ERROR: {meta_err}. Attempting fix..."

                # Try to re-apply the EXIF fix directly over the file
                STOP.run([
                    "exiftool", "-q", "-q", "-tagsFromFile", str(in_path),
                    "-all:all",
                    "-Orientation<Rotation",
                    "-Orientation<Orientation",
                    "-overwrite_original", str(out_path)
                ])

                # Re-verify after fix
                is_meta_ok_retry, meta_err_retry = compare_metadata(get_metadata(in_path), get_metadata(out_path))
                if STOP.stopped:
                    return {"status": "CANCELLED", "file": name, "msg": ""}

                if not is_meta_ok_retry:
                    remove_quietly(out_path)
                    return {"status": "FAIL", "file": name, "msg": f"{msg_prefix} FIX FAILED: {meta_err_retry} -> Deleted file."}
                return {"status": "WARN", "file": name, "msg": f"{msg_prefix} Metadata restored successfully!"}

        return {"status": "OK", "file": name, "msg": "Verified"}
    except Exception as e:
        # Never delete on an unexpected error, just report it.
        return {"status": "ERROR", "file": name, "msg": str(e)}


def main():
    if MODE == "convert":
        print("--- Integrity Check Skipped (MODE=convert) ---")
        return 0

    print("--- Starting Integrity Check ---")

    if CHECKS == "none":
        print("Checks disabled by environment variable (CHECKS=none). Exiting gracefully.")
        return 0

    if not OUTPUT_DIR.is_dir() or not INPUT_DIR.is_dir():
        print(f"[ERROR] Input ({INPUT_DIR}) or output ({OUTPUT_DIR}) directory missing.")
        return 1

    STOP.install()

    # Use the same planning as the converter, so every output is compared with
    # the file it was actually made from (e.g. IMG_1.heic with IMG_1.jpg, not
    # with the IMG_1.mov of the same Live Photo).
    plan = Plan(scan_inputs())
    output_files = [p for p in walk_files(OUTPUT_DIR) if not is_partial(p.name)]
    total_files = len(output_files)

    print(f"Found {total_files} files in output directory to verify ({CHECK_THREADS} Concurrent Threads)...\n")
    passed = 0; failed = 0; errors = 0; count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=CHECK_THREADS) as executor:
        futures = [executor.submit(check_single_file, out_path, plan.by_output.get(out_path))
                   for out_path in output_files]

        for future in concurrent.futures.as_completed(futures):
            count += 1
            res = future.result()
            status, fname, msg = res["status"], res["file"], res["msg"]

            if status == "OK":
                print(f"[{count}/{total_files}] [OK] {fname} -> {msg}")
                passed += 1
            elif status == "WARN":
                print(f"[{count}/{total_files}] [WARN] {fname} -> {msg}")
                passed += 1  # It was fixed, so we consider it passed
            elif status == "MISSING":
                print(f"[{count}/{total_files}] [WARN] {fname} -> {msg}")
            elif status == "FAIL":
                print(f"[{count}/{total_files}] [FAIL] {fname} -> {msg}")
                failed += 1
            elif status == "ERROR":
                print(f"[{count}/{total_files}] [ERROR] {fname} -> {msg}")
                errors += 1

    if STOP.stopped:
        print("\n--- Integrity Check Stopped ---")
        return STOP.exit_code()

    print("\n--- Integrity Check Complete ---")
    print(f"Total Verified: {passed}")
    print(f"Total Deleted:  {failed}")
    if errors:
        print(f"Total Errors:   {errors} (files kept, see ERROR lines above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
