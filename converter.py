import os
import shutil
import subprocess
import sys
import time
import uuid
import concurrent.futures

from common import (
    INPUT_DIR, OUTPUT_DIR, MODE, IMAGE_FORMAT, MODERN_IMAGE_SUFFIXES,
    PARTIAL_MARKER, STOP, Plan, env_bool, env_choice, env_int, env_str, fatal,
    file_type, is_partial, remove_quietly, scan_inputs, walk_files,
)

# --- Configuration ---
HW_ACCEL = env_bool("HW_ACCEL", True)
RENDER_DEVICE = env_str("RENDER_DEVICE", "renderD128")
DEVICE_PATH = f"/dev/dri/{RENDER_DEVICE}"

# Explicit Multi-threading Configuration
JOBS = env_int("JOBS", 4, minimum=1)        # Number of files processed simultaneously
THREADS = str(env_int("THREADS", 2, minimum=0))  # CPU threads allocated per encoder instance (0 = auto)

VIDEO_QUALITY = str(env_int("VIDEO_QUALITY", 28, minimum=0))
VIDEO_CODEC = env_choice("VIDEO_CODEC", "hevc", {"hevc", "av1"})
VIDEO_PRESET = os.environ.get("VIDEO_PRESET")

IMAGE_QUALITY = env_int("IMAGE_QUALITY", 60, minimum=0, maximum=100)
IMAGE_SPEED = str(env_int("IMAGE_SPEED", 4, minimum=0, maximum=10))

FORCE_OVERWRITE = env_bool("FORCE_OVERWRITE", False)
LIMIT_SIZE = env_choice("LIMIT_SIZE", "always", {"always", "videos", "images", "never"})

MB = 1024 * 1024


def get_default_preset(codec):
    if VIDEO_PRESET:
        return VIDEO_PRESET
    return "6" if codec == "av1" else "medium"


def heif_decoder():
    # libheif renamed heif-convert to heif-dec in 1.17; support both.
    return shutil.which("heif-dec") or shutil.which("heif-convert")


def check_dependencies():
    tools = ['ffmpeg', 'exiftool']
    tools.append('avifenc' if IMAGE_FORMAT == "avif" else 'heif-enc')
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        fatal(f"Missing tool(s): {', '.join(missing)}")


def temp_path(final_path, suffix=None):
    """Hidden, uniquely named file next to the final output (same filesystem,
    so the final os.replace() is atomic). The extension is kept so encoders
    still pick the right format."""
    suffix = suffix or final_path.suffix
    return final_path.with_name(f".{final_path.stem}{PARTIAL_MARKER}{uuid.uuid4().hex[:8]}{suffix}")


def decode_error(res, fallback):
    err = res.stderr.decode('utf-8', errors='replace').strip().replace('\n', ' ')
    return err[-250:] or fallback


# --- Processing Functions ---
def video_cmd(input_path, output_file, use_gpu):
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-y"]
    if use_gpu:
        cmd += ["-vaapi_device", DEVICE_PATH]
    cmd += [
        "-threads", THREADS, "-i", str(input_path),
        "-map", "0:v:0", "-map", "0:a?",
        "-map_metadata", "0",
        "-map_metadata:s:v", "0:s:v",
        "-map_chapters", "0",
    ]
    if use_gpu:
        cmd += ["-vf", "format=p010,hwupload"]
        if VIDEO_CODEC == "av1":
            cmd += ["-c:v", "av1_vaapi", "-qp", VIDEO_QUALITY]
        else:
            cmd += ["-c:v", "hevc_vaapi", "-profile:v", "main10", "-qp", VIDEO_QUALITY]
    else:
        preset = get_default_preset(VIDEO_CODEC)
        if VIDEO_CODEC == "av1":
            cmd += ["-c:v", "libsvtav1", "-crf", VIDEO_QUALITY, "-preset", preset]
        else:
            cmd += ["-c:v", "libx265", "-crf", VIDEO_QUALITY, "-preset", preset]
            if THREADS != "0":
                # -threads alone does not cap x265's own thread pool.
                cmd += ["-x265-params", f"pools={THREADS}:log-level=error"]
        cmd += ["-pix_fmt", "yuv420p10le"]
    cmd += [
        "-threads", THREADS,
        "-metadata:s:v:0", "rotate=",
        "-c:a", "aac", "-b:a", "192k",
    ]
    if output_file.suffix.lower() in (".mp4", ".m4v", ".mov"):
        # Must be one flag: a second -movflags overrides the first, which
        # silently dropped +faststart before.
        cmd += ["-movflags", "+faststart+use_metadata_tags"]
        if VIDEO_CODEC == "hevc":
            # Tag as hvc1 so Apple devices (QuickTime, Photos, iOS) play the file.
            cmd += ["-tag:v", "hvc1"]
    cmd.append(str(output_file))
    return cmd


def process_video(input_path, output_file, use_gpu):
    return STOP.run(video_cmd(input_path, output_file, use_gpu))


def decode_image_to_png(input_path, final_output):
    """Encoders only read JPEG/PNG directly; everything else is decoded to a
    temporary PNG first. Returns (path_to_use, temp_or_None, error_or_None)."""
    if input_path.suffix.lower() in {'.jpg', '.jpeg', '.png'}:
        return input_path, None, None

    tmp_png = temp_path(final_output, ".png")
    decoder = heif_decoder() if input_path.suffix.lower() == ".heic" else None
    if decoder:
        res = STOP.run([decoder, str(input_path), str(tmp_png)])
    else:
        res = STOP.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(input_path), "-frames:v", "1", "-update", "1", str(tmp_png)])
    if res.returncode != 0 or not tmp_png.exists():
        remove_quietly(tmp_png)
        return None, None, decode_error(res, "Could not decode image.")
    return tmp_png, tmp_png, None


def process_image(input_path, output_file):
    safe_input, tmp_png, err = decode_image_to_png(input_path, output_file)
    if err:
        return _failed(err)
    try:
        if IMAGE_FORMAT == "avif":
            qp_max = 63 - int((IMAGE_QUALITY * 63) / 100)
            cmd = ["avifenc", "--min", "0", "--max", str(qp_max), "--speed", IMAGE_SPEED,
                   "--jobs", THREADS if THREADS != "0" else "all", str(safe_input), str(output_file)]
        else:
            cmd = ["heif-enc", "-q", str(IMAGE_QUALITY), str(safe_input), "-o", str(output_file)]
        return STOP.run(cmd)
    finally:
        if tmp_png:
            remove_quietly(tmp_png)


def _failed(msg):
    return subprocess.CompletedProcess([], 1, b"", msg.encode())


def copy_file(src, dst):
    """Copy with metadata, atomically (temp file + rename)."""
    tmp = temp_path(dst)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        remove_quietly(tmp)


def result(status, input_path, orig_size=0, new_size=0, msg=""):
    return {"status": status, "file": str(input_path.relative_to(INPUT_DIR)),
            "orig_size": orig_size, "new_size": new_size, "msg": msg}


def process_single_file(input_path, output_file, revert_file, use_gpu):
    """Worker function for processing a single file"""
    if STOP.stopped:
        return result("CANCELLED", input_path)
    tmp = None
    try:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        ftype = file_type(input_path)
        suffix = input_path.suffix.lower()

        if not FORCE_OVERWRITE:
            for existing in {output_file, revert_file}:
                if existing.exists() and existing.stat().st_size > 0:
                    return result("SKIP", input_path)

        start = time.time()
        orig_size = input_path.stat().st_size
        tmp = temp_path(output_file)

        if ftype == "IMAGE" and suffix == f".{IMAGE_FORMAT}":
            # Already in the target format: copy as-is.
            copy_file(input_path, output_file)
            _drop_counterpart(output_file, revert_file)
            return result("OK", input_path, orig_size, orig_size, f"(already {IMAGE_FORMAT}, copied)")

        res = process_video(input_path, tmp, use_gpu) if ftype == "VIDEO" else process_image(input_path, tmp)

        if STOP.stopped:
            return result("CANCELLED", input_path)
        if res.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            return result("FAIL", input_path, msg=decode_error(res, f"Encoder exited with code {res.returncode}."))

        if suffix not in MODERN_IMAGE_SUFFIXES:
            # Copy metadata (dates, GPS, ...) from the source and fix the orientation.
            STOP.run([
                "exiftool", "-q", "-q", "-tagsFromFile", str(input_path),
                "-all:all",
                "-Orientation<Rotation",
                "-Orientation<Orientation",
                "-overwrite_original", str(tmp)
            ])

        elapsed = time.time() - start
        new_size = tmp.stat().st_size

        should_limit = (LIMIT_SIZE == "always"
                        or (LIMIT_SIZE == "videos" and ftype == "VIDEO")
                        or (LIMIT_SIZE == "images" and ftype == "IMAGE"))

        if should_limit and new_size > orig_size and suffix not in MODERN_IMAGE_SUFFIXES:
            remove_quietly(tmp)
            # Keep the original under its own extension so it is not mislabeled
            # (before, e.g. a PNG was copied to "photo.heic").
            copy_file(input_path, revert_file)
            _drop_counterpart(revert_file, output_file)
            msg = f"(Grew from {orig_size/MB:.1f}MB to {new_size/MB:.1f}MB). Keeping original as {revert_file.name}."
            return result("REVERT", input_path, orig_size, orig_size, msg)

        os.replace(tmp, output_file)
        _drop_counterpart(output_file, revert_file)
        msg = f"({orig_size/MB:.1f}MB -> {new_size/MB:.1f}MB) in {elapsed:.1f}s"
        return result("OK", input_path, orig_size, new_size, msg)

    except Exception as e:
        return result("ERROR", input_path, msg=str(e))
    finally:
        if tmp is not None:
            remove_quietly(tmp)


def _drop_counterpart(kept, other):
    """With FORCE_OVERWRITE, a file can flip between converted and reverted;
    remove the stale one so the output never holds both."""
    if other != kept:
        remove_quietly(other)


def clean_partials():
    """Remove leftovers from a run that was killed hard (e.g. SIGKILL / power loss)."""
    if not OUTPUT_DIR.exists():
        return
    removed = 0
    for path in walk_files(OUTPUT_DIR):
        if is_partial(path.name):
            remove_quietly(path)
            removed += 1
    if removed:
        print(f"Removed {removed} unfinished file(s) from an interrupted run.")


def main():
    if MODE == "check":
        print("--- Media Converter Skipped (MODE=check) ---")
        return 0

    STOP.install()
    check_dependencies()
    if not INPUT_DIR.is_dir():
        fatal(f"Input directory {INPUT_DIR} does not exist or is not a directory.")

    use_gpu = HW_ACCEL
    if HW_ACCEL and not os.path.exists(DEVICE_PATH):
        print(f"[WARN] HW_ACCEL=true but {DEVICE_PATH} does not exist (is /dev/dri passed to the container?). "
              f"Falling back to CPU encoding.")
        use_gpu = False

    print(f"--- Media Converter Started ({JOBS} Concurrent Jobs | {THREADS} Threads per Job | "
          f"{'GPU' if use_gpu else 'CPU'} video encoding) ---")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    clean_partials()

    plan = Plan(scan_inputs())
    for src, out in plan.renamed:
        print(f"[WARN] Name clash: {src.relative_to(INPUT_DIR)} -> {out.relative_to(OUTPUT_DIR)}")
    total_files = len(plan.items)
    print(f"Found {total_files} media files. Processing...")

    total_orig_size = 0
    total_new_size = 0
    count = 0
    failures = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=JOBS) as executor:
        futures = [executor.submit(process_single_file, src, out, rev, use_gpu)
                   for src, (out, rev) in plan.items.items()]

        for future in concurrent.futures.as_completed(futures):
            count += 1
            res = future.result()
            status, fname, msg = res["status"], res["file"], res["msg"]

            if status in ("OK", "REVERT"):
                total_orig_size += res["orig_size"]
                total_new_size += res["new_size"]
                print(f"[{count}/{total_files}] {status}: {fname} {msg}")
            elif status in ("SKIP", "CANCELLED"):
                if status == "SKIP":
                    print(f"[{count}/{total_files}] SKIP: {fname}")
            else:
                failures += 1
                print(f"[{count}/{total_files}] {status}: {fname} - {msg}")

    if STOP.stopped:
        print("\n--- Stopped before all files were processed ---")
        return STOP.exit_code()

    print("\n--- Pipeline Summary ---")
    saved = total_orig_size - total_new_size
    print(f"Original Size: {total_orig_size/(1024*MB):.2f} GB")
    print(f"Converted Size: {total_new_size/(1024*MB):.2f} GB")
    print(f"Total Storage Saved: {saved/MB:.1f} MB ({(saved/total_orig_size*100) if total_orig_size > 0 else 0:.1f}%)")
    if failures:
        print(f"Failed: {failures} file(s), see FAIL/ERROR lines above.")
    print("--- Done ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
