"""
VIDEO QUALITY-CONTROL CHECKER
=============================
Scans a local MP4 video (up to about 20 minutes) and reports:
  1. DEAD AIR     - silence in the audio lasting longer than 1.5 seconds
  2. STATIC SHOTS - a scene that runs longer than 6 seconds without a cut

--------------------------------------------------------------------------
ONE-TIME SETUP (run this in Command Prompt / PowerShell / Terminal):

    pip install opencv-python numpy imageio-ffmpeg

  You don't need to install FFmpeg yourself. The "imageio-ffmpeg" package
  downloads its own copy automatically.
--------------------------------------------------------------------------
HOW TO RUN:

    python video_qc.py "C:\\Videos\\my video.mp4"

  Or run   python video_qc.py   on its own and drag the video file into
  the window when it asks for one.

  The results are printed on screen and also saved to a text file next to
  your video (for example "my video_QC_report.txt").
--------------------------------------------------------------------------
TUNING: to make the checks stricter or looser, change the values in the
SETTINGS section below.
"""

import os
import subprocess
import sys

import cv2
import imageio_ffmpeg
import numpy as np

# ============================== SETTINGS ==================================
MAX_SILENCE_SECONDS = 1.5    # Flag silences longer than this
SILENCE_THRESHOLD_DB = -45.0 # Audio quieter than this counts as silence.
                             # If background hiss hides real gaps, try -35.
MAX_STATIC_SECONDS = 6.0     # Flag shots longer than this without a cut
CUT_SENSITIVITY = 27.0       # How much the picture must change to count as a
                             # cut. Lower = more sensitive (finds more cuts).
MIN_SHOT_SECONDS = 0.5       # Ignore "cuts" closer together than this
                             # (stops camera flashes counting as cuts)
MAX_VIDEO_MINUTES = 20       # Warn if the video is longer than this
# ==========================================================================

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
AUDIO_SAMPLE_RATE = 16000    # Low sample rate is fine for loudness checks
AUDIO_WINDOW_SECONDS = 0.02  # Loudness is measured in 20 ms slices
FRAME_W, FRAME_H = 160, 90   # Frames are shrunk to this size for speed


def format_time(seconds):
    """Real clock time, e.g. 00:01:23.450"""
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def format_timecode(seconds, fps):
    """Editor-style timecode HH:MM:SS:FF (frames), non-drop-frame."""
    fps_whole = max(1, round(fps))
    total_frames = int(round(seconds * fps))
    ff = total_frames % fps_whole
    total_secs = total_frames // fps_whole
    h, rem = divmod(total_secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}:{ff:02d}"


def stamp(seconds, fps):
    return f"{format_time(seconds)}  (TC {format_timecode(seconds, fps)})"


def get_video_info(path):
    """Return (frames per second, duration in seconds)."""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None, None
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.release()
    if not fps or fps != fps or fps <= 0:  # fps != fps catches "NaN"
        fps = 30.0
    return fps, frame_count / fps


# ---------------------------- CHECK 1: AUDIO -------------------------------
def find_dead_air(path):
    """Return a list of (start, end) silent gaps, or None if there's no audio."""
    print("Checking audio for dead air...")
    cmd = [FFMPEG, "-v", "error", "-i", path, "-vn",
           "-ac", "1", "-ar", str(AUDIO_SAMPLE_RATE), "-f", "s16le", "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("FFmpeg timed out while reading audio.") from error
    if not result.stdout:
        return None

    samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    window = int(AUDIO_SAMPLE_RATE * AUDIO_WINDOW_SECONDS)
    n_windows = len(samples) // window
    if n_windows == 0:
        return None
    chunks = samples[: n_windows * window].reshape(n_windows, window)

    # Loudness of each 20 ms slice, in decibels
    rms = np.sqrt(np.mean(chunks ** 2, axis=1))
    loudness_db = 20 * np.log10(rms + 1e-10)
    is_silent = loudness_db < SILENCE_THRESHOLD_DB

    # Find where runs of silence start and stop
    edges = np.diff(np.concatenate(([0], is_silent.astype(np.int8), [0])))
    run_starts = np.where(edges == 1)[0]
    run_ends = np.where(edges == -1)[0]

    gaps = []
    for s, e in zip(run_starts, run_ends):
        start_t = s * AUDIO_WINDOW_SECONDS
        end_t = e * AUDIO_WINDOW_SECONDS
        if end_t - start_t > MAX_SILENCE_SECONDS:
            gaps.append((start_t, end_t))
    return gaps


# ---------------------------- CHECK 2: VIDEO -------------------------------
def find_cuts(path, fps, duration):
    """Return (list of cut times in seconds, measured video length)."""
    print("Checking video for scene cuts (this can take a few minutes)...")
    analysis_fps = min(fps, 30.0)
    cmd = [FFMPEG, "-v", "error", "-i", path, "-an",
           "-vf", f"fps={analysis_fps},scale={FRAME_W}:{FRAME_H}",
           "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    frame_size = FRAME_W * FRAME_H * 3
    expected_frames = max(1, int(duration * analysis_fps))
    cuts = []
    last_cut = 0.0
    previous = None
    index = 0
    last_percent = -1

    while True:
        raw = proc.stdout.read(frame_size)
        if len(raw) < frame_size:
            break
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(FRAME_H, FRAME_W, 3)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV).astype(np.int16)

        if previous is not None:
            # Average change in colour + brightness between this frame and the last
            change = np.mean(np.abs(hsv - previous))
            t = index / analysis_fps
            if change >= CUT_SENSITIVITY and t - last_cut >= MIN_SHOT_SECONDS:
                cuts.append(t)
                last_cut = t
        previous = hsv
        index += 1

        percent = min(100, index * 100 // expected_frames)
        if percent != last_percent and percent % 5 == 0:
            print(f"  ...{percent}% done", end="\r")
            last_percent = percent

    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired as error:
        proc.kill()
        proc.wait()
        raise RuntimeError("FFmpeg timed out while scanning video frames.") from error
    if proc.returncode:
        raise RuntimeError("FFmpeg could not decode video frames.")
    print()
    return cuts, index / analysis_fps


def find_static_shots(cuts, video_end):
    """Return (start, end) of every shot longer than MAX_STATIC_SECONDS."""
    boundaries = [0.0] + cuts + [video_end]
    long_shots = []
    for start, end in zip(boundaries[:-1], boundaries[1:]):
        if end - start > MAX_STATIC_SECONDS:
            long_shots.append((start, end))
    return long_shots


# ------------------------------- REPORT ------------------------------------
def build_report(path, fps, duration, gaps, cuts, long_shots):
    lines = []
    lines.append("=" * 72)
    lines.append(f"QC REPORT: {os.path.basename(path)}")
    lines.append(f"Length: {format_time(duration)}   Frame rate: {fps:.3f} fps")
    lines.append("Times shown as real time, plus editor timecode (TC HH:MM:SS:FF).")
    lines.append("=" * 72)

    lines.append("")
    lines.append(f"[1] DEAD AIR (silence longer than {MAX_SILENCE_SECONDS}s)")
    lines.append("-" * 72)
    if gaps is None:
        lines.append("  No audio track found in this video.")
    elif not gaps:
        lines.append("  PASS - no dead air found.")
    else:
        for i, (s, e) in enumerate(gaps, 1):
            lines.append(f"  #{i:<3} {stamp(s, fps)}  ->  {format_time(e)}   "
                         f"silent for {e - s:.2f}s")

    lines.append("")
    lines.append(f"[2] STATIC SHOTS (no cut for more than {MAX_STATIC_SECONDS}s)")
    lines.append("-" * 72)
    lines.append(f"  Cuts detected in the whole video: {len(cuts)}")
    if not long_shots:
        lines.append("  PASS - every shot is short enough.")
    else:
        for i, (s, e) in enumerate(long_shots, 1):
            lines.append(f"  #{i:<3} {stamp(s, fps)}  ->  {format_time(e)}   "
                         f"no cut for {e - s:.2f}s")

    total = len(gaps or []) + len(long_shots)
    lines.append("")
    lines.append("=" * 72)
    lines.append(f"TOTAL ISSUES FLAGGED: {total}")
    lines.append("=" * 72)
    return "\n".join(lines)


def main():
    launched_without_args = len(sys.argv) < 2
    if launched_without_args:
        path = input("Drag your MP4 file into this window (or paste its path) and press Enter:\n> ")
    else:
        path = sys.argv[1]
    path = path.strip().strip('"').strip("'")

    if not os.path.isfile(path):
        print(f"ERROR: File not found: {path}")
        sys.exit(1)

    fps, duration = get_video_info(path)
    if fps is None:
        print("ERROR: Could not open this file as a video.")
        sys.exit(1)
    if duration > MAX_VIDEO_MINUTES * 60:
        print(f"WARNING: This video is {duration / 60:.1f} minutes long "
              f"(over {MAX_VIDEO_MINUTES}). Checking anyway, but it may be slow.")

    gaps = find_dead_air(path)
    cuts, measured_end = find_cuts(path, fps, duration)
    video_end = measured_end if measured_end > 0 else duration
    long_shots = find_static_shots(cuts, video_end)

    report = build_report(path, fps, video_end, gaps, cuts, long_shots)
    print()
    print(report)

    report_path = os.path.splitext(path)[0] + "_QC_report.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(f"\nReport saved to: {report_path}")

    if launched_without_args:
        input("\nPress Enter to close...")


if __name__ == "__main__":
    main()
