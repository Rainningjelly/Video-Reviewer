"""
Video analysis engine for the QC Review app. Everything runs locally on your PC.

To make a check stricter or looser, change the values in SETTINGS below and
re-run the analysis from the app.
"""
import difflib
import json
import logging
import os
import re
import subprocess
import tempfile
import urllib.request

import cv2
import imageio_ffmpeg
import numpy as np

# ============================== SETTINGS ==================================
SETTINGS = {
    # Audio
    "max_silence_seconds": 1.5,      # dead air longer than this is flagged
    "silence_threshold_db": -45.0,   # quieter than this = silence (try -35 if there's hiss)
    "loudness_jump_db": 12.0,        # sudden volume change between seconds
    # Pacing
    "max_shot_seconds": 6.0,         # longer shots are prioritized for AI pacing review
    "cut_sensitivity": 27.0,         # lower = detects more (softer) cuts
    "min_cut_gap_seconds": 0.5,
    # Picture
    "blank_min_seconds": 0.5,        # black / solid-colour screen longer than this
    "blur_threshold": 30.0,          # edge-sharpness below this = possibly blurry / upscaled
    # On-screen text
    "min_text_contrast": 40.0,
    # Narration vs on-screen highlights
    "highlight_tolerance_seconds": 2.0,  # on-screen text may appear this early/late
    "highlight_search_seconds": 10.0,    # further than this = "missing"
}
# ==========================================================================

CATEGORIES = [
    "Dead air / blank clip", "Scene pacing", "Audio issue", "Glitch",
    "Low quality image", "Text not visible", "Spelling", "Sync", "Delayed",
    "Years", "Name (picture with name)", "Percentage", "Measures", "Number formatting",
    "Narrator visual", "Missing image", "Wrong footage", "Footage doesn't match narration",
    "Map check", "Aesthetics", "Other", "Narration vs on-screen text", "Narration vs script",
]

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
APP_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(APP_DIR, "models")
FACE_MODELS = {
    "detector": ("face_detection_yunet_2023mar.onnx",
                 "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"),
    "recognizer": ("face_recognition_sface_2021dec.onnx",
                   "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"),
}
FACE_MATCH_THRESHOLD = 0.363  # SFace cosine similarity threshold
SAMPLE_RATE = 16000
ANALYSIS_CPU_THREADS = 2
USE_GPU_FOR_TRANSCRIPTION = os.environ.get("VIDEO_QC_USE_GPU", "0") == "1"
MAX_ANALYSIS_DURATION_SECONDS = 2 * 60 * 60
MAX_ANALYSIS_DIMENSION = 7680

cv2.setNumThreads(1)

logging.getLogger("RapidOCR").setLevel(logging.ERROR)
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


# ------------------------------ helpers ------------------------------------
def mmss(t):
    t = max(0.0, float(t))
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


class Issues:
    def __init__(self):
        self.items = []

    def add(self, category, time, title, detail="", end=None, severity="warning"):
        self.items.append({
            "id": f"a{len(self.items) + 1}",
            "category": category,
            "time": round(float(time), 3),
            "end": None if end is None else round(float(end), 3),
            "severity": severity,  # "error", "warning" or "check"
            "title": title,
            "detail": detail,
            "source": "auto",
        })


def runs_of(mask):
    """Return (start, end) index pairs for each run of True values."""
    edges = np.diff(np.concatenate(([0], np.asarray(mask, dtype=np.int8), [0])))
    return list(zip(np.where(edges == 1)[0], np.where(edges == -1)[0]))


def video_info(path):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("This file could not be opened as a video.")
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps != fps or fps <= 0:
        fps = 30.0
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    info = {
        "fps": round(float(fps), 3),
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "duration": round(float(frames / fps), 3),
    }
    cap.release()
    if info["duration"] <= 0 or info["duration"] > MAX_ANALYSIS_DURATION_SECONDS:
        raise RuntimeError("This video must be longer than zero and no longer than two hours.")
    if info["width"] <= 0 or info["height"] <= 0 or max(info["width"], info["height"]) > MAX_ANALYSIS_DIMENSION:
        raise RuntimeError("This video has unsupported dimensions.")
    return info


def write_analysis(path, data):
    directory = os.path.dirname(path)
    fd, temporary_path = tempfile.mkstemp(dir=directory, prefix="analysis.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as analysis_file:
            json.dump(data, analysis_file, ensure_ascii=False, indent=1)
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def subprocess_options():
    if os.name == "nt":
        return {"creationflags": 0x00004000}  # BELOW_NORMAL_PRIORITY_CLASS
    return {}


def read_frames(path, fps, width, height, checkpoint=lambda: None):
    """Yield BGR frames decoded by FFmpeg at a fixed rate and size."""
    cmd = [FFMPEG, "-v", "error", "-threads", str(ANALYSIS_CPU_THREADS), "-i", path, "-an",
           "-vf", f"fps={fps},scale={width}:{height}",
           "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **subprocess_options())
    size = width * height * 3
    try:
        while True:
            checkpoint()
            raw = proc.stdout.read(size)
            if len(raw) < size:
                break
            yield np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3).copy()
    finally:
        proc.stdout.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise RuntimeError("FFmpeg timed out while decoding video frames.")
    if proc.returncode:
        raise RuntimeError("FFmpeg could not decode video frames.")


# ------------------------------- audio -------------------------------------
def load_audio(path, progress=lambda fraction: None, total_duration=0, checkpoint=lambda: None):
    cmd = [FFMPEG, "-v", "error", "-threads", str(ANALYSIS_CPU_THREADS), "-i", path, "-vn", "-ac", "1",
           "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **subprocess_options())
    audio_data = bytearray()
    decoded_bytes = 0
    chunk_size = SAMPLE_RATE * 2 * 5
    try:
        while True:
            checkpoint()
            raw = proc.stdout.read(chunk_size)
            if not raw:
                break
            audio_data.extend(raw)
            decoded_bytes += len(raw)
            if total_duration > 0:
                progress(min(decoded_bytes / (SAMPLE_RATE * 2 * total_duration), 1.0))
    finally:
        proc.stdout.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise RuntimeError("FFmpeg timed out while decoding audio.")
    if proc.returncode:
        raise RuntimeError("FFmpeg could not decode audio.")
    if total_duration > 0:
        progress(1.0)
    if len(audio_data) < SAMPLE_RATE * 2 // 10:
        return None
    samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
    samples /= 32768.0
    return samples


def window_db(samples, seconds):
    win = int(SAMPLE_RATE * seconds)
    n = len(samples) // win
    chunks = samples[: n * win].reshape(n, win)
    rms = np.sqrt(np.mean(chunks ** 2, axis=1))
    return 20 * np.log10(rms + 1e-10), np.max(np.abs(chunks), axis=1)


def check_audio(samples, issues):
    s = SETTINGS
    # Dead air
    step = 0.02
    db, _ = window_db(samples, step)
    for a, b in runs_of(db < s["silence_threshold_db"]):
        length = (b - a) * step
        if length > s["max_silence_seconds"]:
            issues.add("Dead air / blank clip", a * step, f"Dead air for {length:.1f}s",
                       "No audible sound. Add narration, music or ambience, or tighten the edit.",
                       end=b * step, severity="error")

    # Digital drop-outs: short stretches of perfect silence in the middle of sound
    step = 0.01
    db, peak = window_db(samples, step)
    zero = peak < 2 / 32768
    loud = db > s["silence_threshold_db"] + 10
    for a, b in runs_of(zero):
        length = (b - a) * step
        if 0.03 <= length <= s["max_silence_seconds"] and a > 5 and b < len(loud) - 5:
            if loud[a - 5:a].any() and loud[b:b + 5].any():
                issues.add("Audio issue", a * step, f"Audio drop-out ({length * 1000:.0f} ms)",
                           "Sound cuts out completely for a moment. Often a bad edit point or render glitch.",
                           end=b * step, severity="warning")

    # Sudden volume jumps and overall level
    db1, _ = window_db(samples, 1.0)
    active = db1 > s["silence_threshold_db"] + 10
    for i in range(1, len(db1)):
        if active[i] and active[i - 1] and abs(db1[i] - db1[i - 1]) > s["loudness_jump_db"]:
            direction = "louder" if db1[i] > db1[i - 1] else "quieter"
            issues.add("Audio issue", i, f"Sudden volume change ({direction})",
                       f"Level jumps {abs(db1[i] - db1[i - 1]):.0f} dB between seconds. Check the audio mix.",
                       severity="check")
    if active.any():
        level = float(np.median(db1[active]))
        if level < -32:
            issues.add("Audio issue", 0, "Audio is quiet overall",
                       f"Typical level is {level:.0f} dBFS. Most online video sits around -20 to -14.",
                       severity="warning")
        elif level > -9:
            issues.add("Audio issue", 0, "Audio is very loud overall",
                       f"Typical level is {level:.0f} dBFS. Risk of distortion.", severity="warning")


def check_clipping(path, issues, progress=lambda fraction: None, total_duration=0, checkpoint=lambda: None):
    rate = 48000
    cmd = [FFMPEG, "-v", "error", "-threads", str(ANALYSIS_CPU_THREADS), "-i", path, "-vn", "-ac", "2", "-ar", str(rate), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **subprocess_options())
    clipped = []
    chunk = rate * 2 * 2
    decoded_seconds = 0
    while True:
        checkpoint()
        raw = proc.stdout.read(chunk)
        if not raw:
            break
        data = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16)
        clipped.append(int(np.count_nonzero(np.abs(data.astype(np.int32)) >= 32700)) >= 10)
        decoded_seconds += len(raw) / (rate * 2 * 2)
        if total_duration > 0:
            progress(min(decoded_seconds / total_duration, 1.0))
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RuntimeError("FFmpeg timed out while checking audio clipping.")
    if proc.returncode:
        raise RuntimeError("FFmpeg could not decode audio for clipping checks.")
    if total_duration > 0:
        progress(1.0)
    for a, b in runs_of(np.array(clipped, dtype=bool)):
        issues.add("Audio issue", a, "Audio clipping / distortion",
                   "The sound hits maximum level and distorts. Lower the gain of this section.",
                   end=b, severity="error")


# ------------------------------ transcript ---------------------------------
def transcribe(samples, model_name, language, progress, checkpoint=lambda: None):
    from faster_whisper import WhisperModel
    import ctranslate2

    progress(f"Loading speech-to-text model '{model_name}' (downloads once on first use)...")
    total = len(samples) / SAMPLE_RATE

    def collect_segments(device, compute_type):
        checkpoint()
        model_options = {"num_workers": 1}
        if device == "cpu":
            model_options["cpu_threads"] = ANALYSIS_CPU_THREADS
        model = WhisperModel(model_name, device=device, compute_type=compute_type, **model_options)
        segments, _ = model.transcribe(samples, language=None if language == "auto" else language,
                                       word_timestamps=True)
        collected = []
        for segment in segments:
            checkpoint()
            collected.append(segment)
            progress(f"Transcribing narration on {device.upper()}... {mmss(segment.end)} of {mmss(total)}",
                     segment.end / max(total, 1))
        return collected

    try:
        use_cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        use_cuda = False
    if use_cuda and USE_GPU_FOR_TRANSCRIPTION:
        try:
            segments = collect_segments("cuda", "float16")
        except Exception as error:
            progress(f"GPU transcription failed; retrying on CPU ({error})...")
            segments = collect_segments("cpu", "int8")
    else:
        segments = collect_segments("cpu", "int8")

    segs, words = [], []
    for seg in segments:
        segs.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()})
        for w in seg.words or []:
            words.append({"w": w.word, "start": round(w.start, 2), "end": round(w.end, 2)})
    return segs, words


# ------------------------------ video pass 1 -------------------------------
def scan_motion(path, info, progress, checkpoint=lambda: None):
    """Low-res pass over every frame: cuts, blank screens, flash frames, freezes."""
    s = SETTINGS
    fps = min(info["fps"], 30.0)
    expected = max(1, int(info["duration"] * fps))
    diffs, skips, stds = [0.0], [], []
    prev = prev2 = None
    for i, frame in enumerate(read_frames(path, fps, 160, 90, checkpoint)):
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV).astype(np.int16)
        stds.append(float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).std()))
        if prev is not None:
            diffs.append(float(np.mean(np.abs(hsv - prev))))
        if prev2 is not None:
            skips.append(float(np.mean(np.abs(hsv - prev2))))
        prev2, prev = prev, hsv
        if i % 30 == 0:
            progress(f"Checking cuts and glitches... {mmss(i / fps)}", i / expected)

    n = len(diffs)
    d = np.array(diffs)
    skip = np.full(n, np.inf)
    if n > 2:
        skip[1:n - 1] = skips
    std = np.array(stds)
    T = s["cut_sensitivity"]

    flash = set()
    for i in range(1, n - 1):
        if d[i] >= T and d[i + 1] >= T and skip[i] < T * 0.5:
            flash.add(i)

    cuts, last = [], 0.0
    for i in range(1, n):
        if d[i] >= T and i not in flash and (i - 1) not in flash:
            t = i / fps
            if t - last >= s["min_cut_gap_seconds"]:
                cuts.append(round(t, 3))
                last = t

    blank = std < 6
    blanks = [(a / fps, b / fps) for a, b in runs_of(blank) if (b - a) / fps >= s["blank_min_seconds"]]

    cut_frames = set(int(round(c * fps)) for c in cuts)
    frozen = []
    half = int(fps * 0.5)
    for a, b in runs_of((d < 0.3) & ~blank):
        if b - a < half or a < half:
            continue
        before = range(a - half, a)
        if any(j in cut_frames for j in range(a - half, a + 1)):
            continue
        if np.mean(d[list(before)]) > 2.0:
            frozen.append((a / fps, b / fps))

    return {
        "fps": fps,
        "duration": n / fps if n else info["duration"],
        "cuts": cuts,
        "flash": sorted(i / fps for i in flash),
        "blanks": blanks,
        "frozen": frozen,
        "blank_mask": blank,
    }


# ------------------------------ video pass 2 -------------------------------
class FaceTools:
    def __init__(self):
        os.makedirs(MODELS_DIR, exist_ok=True)
        paths = {}
        for key, (name, url) in FACE_MODELS.items():
            path = os.path.join(MODELS_DIR, name)
            if not os.path.exists(path):
                urllib.request.urlretrieve(url, path + ".part")
                os.replace(path + ".part", path)
            paths[key] = path
        self.detector = cv2.FaceDetectorYN.create(paths["detector"], "", (320, 320), 0.8, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(paths["recognizer"], "")

    def detect(self, frame):
        h, w = frame.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(frame)
        found = []
        if faces is None:
            return found
        for f in faces:
            if f[2] < w * 0.03:
                continue
            emb = self.recognizer.feature(self.recognizer.alignCrop(frame, f)).flatten()
            emb = emb / (np.linalg.norm(emb) + 1e-9)
            found.append(([f[0] / w, f[1] / h, f[2] / w, f[3] / h], emb))
        return found


def run_ocr(engine, frame, gray):
    result = engine(frame)
    if result.boxes is None or result.txts is None:
        return []
    H, W = gray.shape
    lines = []
    for box, txt, score in zip(result.boxes, result.txts, result.scores):
        pts = np.array(box)
        x0, y0 = np.maximum(pts.min(axis=0).astype(int), 0)
        x1, y1 = pts.max(axis=0).astype(int)
        x1, y1 = min(x1, W), min(y1, H)
        crop = gray[y0:y1, x0:x1]
        contrast = float(np.percentile(crop, 95) - np.percentile(crop, 5)) if crop.size else 0.0
        text = str(txt).strip()
        if text:
            lines.append({
                "text": text,
                "score": round(float(score), 3),
                "h": round((y1 - y0) / H, 4),
                "contrast": round(contrast, 1),
                "edge": bool(x0 <= 2 or y0 <= 2 or x1 >= W - 2 or y1 >= H - 2),
            })
    return lines


def scan_seconds(path, info, project_dir, progress, face_tools, ocr_engine, checkpoint=lambda: None):
    """One frame per second at HD size: thumbnails, sharpness, faces, on-screen text."""
    W = min(1280, info["width"]) // 2 * 2
    H = int(round(info["height"] * W / info["width"] / 2)) * 2
    thumbs = os.path.join(project_dir, "thumbs")
    os.makedirs(thumbs, exist_ok=True)
    total = max(1, int(info["duration"]))
    seconds, face_sec, face_box, face_emb = [], [], [], []
    last_small, last_lines = None, []

    for t, frame in enumerate(read_frames(path, 1, W, H, checkpoint)):
        thumb = cv2.resize(frame, (320, int(320 * H / W)), interpolation=cv2.INTER_AREA)
        cv2.imwrite(os.path.join(thumbs, f"{t}.jpg"), thumb, [cv2.IMWRITE_JPEG_QUALITY, 80])
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        faces = face_tools.detect(frame) if face_tools else []
        for box, emb in faces:
            face_sec.append(t)
            face_box.append(box)
            face_emb.append(emb)

        lines = []
        if ocr_engine is not None:
            small = cv2.resize(gray, (160, 90)).astype(np.int16)
            if last_small is None or np.mean(np.abs(small - last_small)) > 3:
                last_lines = run_ocr(ocr_engine, frame, gray)
                last_small = small
            lines = last_lines

        seconds.append({
            "t": t,
            "sharp": round(float(np.percentile(np.abs(cv2.Laplacian(gray, cv2.CV_32F)), 99.9)), 1),
            "std": round(float(gray.std()), 1),
            "faces": [[round(float(v), 3) for v in box] for box, _ in faces],
            "ocr": lines,
        })
        progress(f"Reading on-screen text and checking picture quality... {mmss(t)}", t / total)

    if face_emb:
        np.savez(os.path.join(project_dir, "faces.npz"), sec=np.array(face_sec),
                 box=np.array(face_box, dtype=np.float32), emb=np.array(face_emb, dtype=np.float32))
    return seconds


# ------------------------- on-screen text events ---------------------------
def norm(text):
    return re.sub(r"\s+", " ", text.lower()).strip()


def build_text_events(seconds):
    """Merge the same on-screen text seen in consecutive seconds into one event."""
    events, active = [], []
    for sec in seconds:
        t = sec["t"]
        for line in sec["ocr"]:
            key = norm(line["text"])
            if len(re.sub(r"\W", "", key)) < 2:
                continue
            match = None
            for ev in active:
                if ev["_key"] == key or difflib.SequenceMatcher(None, ev["_key"], key).ratio() >= 0.8:
                    match = ev
                    break
            if match is None:
                match = dict(line, start=t, end=t + 1, _key=key, _last=t)
                active.append(match)
                events.append(match)
            else:
                match["end"], match["_last"] = t + 1, t
                if line["score"] > match["score"]:
                    match["text"], match["score"] = line["text"], line["score"]
                match["h"] = min(match["h"], line["h"])
                match["contrast"] = min(match["contrast"], line["contrast"])
                match["edge"] = match["edge"] or line["edge"]
        active = [ev for ev in active if ev["_last"] >= t - 1]
    for ev in events:
        ev.pop("_key")
        ev.pop("_last")
    return events


def check_text(events, spoken_words, language, issues):
    s = SETTINGS
    spell = None
    if language in ("en", "auto"):
        from spellchecker import SpellChecker
        spell = SpellChecker()
    flagged = set()
    for ev in events:
        if ev["score"] < 0.6 or len(ev["text"]) < 3:
            continue
        where = f'"{ev["text"]}"'
        if ev["contrast"] < s["min_text_contrast"]:
            issues.add("Text not visible", ev["start"], "On-screen text has low contrast",
                       f"{where} blends into the background. Add a shadow, outline or backing box.",
                       end=ev["end"], severity="warning")
        if ev["edge"] and len(re.sub(r"\W", "", ev["text"])) >= 5:
            issues.add("Text not visible", ev["start"], "On-screen text touches the frame edge",
                       f"{where} may be cut off. Keep text inside the title-safe area.",
                       end=ev["end"], severity="check")
        if ev["end"] - ev["start"] < 2 and len(ev["text"].split()) >= 5:
            issues.add("Text not visible", ev["start"], "Text is on screen too briefly",
                       f"{where} shows for about {ev['end'] - ev['start']:.0f}s. Too short to read.",
                       end=ev["end"], severity="check")

        if spell is None or ev["score"] < 0.8:
            continue
        for token in re.findall(r"[A-Za-z][A-Za-z']+", ev["text"]):
            word = token.strip("'").lower()
            if len(word) < 4 or word in spoken_words or spell.known([word]):
                continue
            if (word, ev["start"]) in flagged:
                continue
            flagged.add((word, ev["start"]))
            spoken_close = difflib.get_close_matches(word, spoken_words, n=1, cutoff=0.8)
            if spoken_close:
                issues.add("Spelling", ev["start"], f'Spelling error: "{token}"',
                           f'In {where}. The narrator says "{spoken_close[0]}".',
                           end=ev["end"], severity="error")
                continue
            if token.isupper():
                continue
            guess = spell.correction(word)
            if not guess:
                continue
            correction_similarity = difflib.SequenceMatcher(None, word, guess.lower()).ratio()
            if correction_similarity < 0.82:
                continue
            hint = f' Did you mean "{guess}"?' if guess and guess != word else ""
            if token[0].isupper():
                issues.add("Spelling", ev["start"], f'Check spelling: "{token}"',
                           f"Unknown term in {where}.{hint}", end=ev["end"], severity="check")
            else:
                severity = "error" if correction_similarity >= 0.9 else "check"
                issues.add("Spelling", ev["start"], f'Possible spelling error: "{token}"',
                           f"In {where}.{hint}", end=ev["end"], severity=severity)


# --------------------- years / percentages / measures ----------------------
def check_number_formatting(events, issues):
    quantity_before = re.compile(
        r"\b(population|count|total|number|estimated|estimate|approximately|about|roughly|nearly|over|at least|"
        r"reached|recorded|weighed|weighs|cost|costs|worth|contained|included|produced|sold|reported)\b"
        r"(?:\W+\w+){0,4}\W*$",
        re.I,
    )
    quantity_after = re.compile(
        r"^\W*(people|animals|birds|pigs|cows|deer|cases|homes|acres|miles|kilometers|kilometres|"
        r"meters|metres|feet|pounds|dollars|eggs|items|species|percent|%)\b",
        re.I,
    )
    for event in events:
        text = str(event.get("text", ""))
        for match in re.finditer(r"(?<![\w.,])(\d{4,})(?![\w.,])", text):
            digits = match.group(1)
            value = int(digits)
            if 1000 <= value <= 2099:
                before = text[max(0, match.start() - 64):match.start()]
                after = text[match.end():match.end() + 32]
                before = re.split(r"[;.!?\n]", before)[-1]
                after = re.split(r"[;.!?\n]", after)[0]
                explicit_quantity = quantity_before.search(before) or quantity_after.search(after)
                if not explicit_quantity:
                    continue
            formatted = f"{value:,}"
            issues.add(
                "Number formatting",
                event["start"],
                f"Number needs thousands separators: {digits}",
                f'For consistent formal formatting, write "{formatted}" instead of "{digits}".',
                end=event.get("end"),
                severity="check",
            )


UNIT_TABLE = {
    "km": ("metric", ["km", "kilometer", "kilometers", "kilometre", "kilometres"]),
    "m": ("metric", ["m", "meter", "meters", "metre", "metres"]),
    "cm": ("metric", ["cm", "centimeter", "centimeters", "centimetre", "centimetres"]),
    "mm": ("metric", ["mm", "millimeter", "millimeters", "millimetre", "millimetres"]),
    "kg": ("metric", ["kg", "kgs", "kilo", "kilos", "kilogram", "kilograms"]),
    "g": ("metric", ["gram", "grams"]),
    "tonne": ("metric", ["tonne", "tonnes", "metric tons"]),
    "l": ("metric", ["liter", "liters", "litre", "litres"]),
    "km/h": ("metric", ["km/h", "kph", "kilometers per hour", "kilometres per hour"]),
    "°C": ("metric", ["°c", "ºc", "degrees celsius", "celsius"]),
    "ha": ("metric", ["ha", "hectare", "hectares"]),
    "km²": ("metric", ["km²", "km2", "sq km", "square kilometers", "square kilometres"]),
    "mi": ("imperial", ["mi", "mile", "miles"]),
    "ft": ("imperial", ["ft", "foot", "feet"]),
    "in": ("imperial", ["inch", "inches"]),
    "yd": ("imperial", ["yd", "yard", "yards"]),
    "lb": ("imperial", ["lb", "lbs", "pound", "pounds"]),
    "oz": ("imperial", ["oz", "ounce", "ounces"]),
    "ton": ("imperial", ["ton", "tons"]),
    "gal": ("imperial", ["gal", "gallon", "gallons"]),
    "mph": ("imperial", ["mph", "miles per hour"]),
    "°F": ("imperial", ["°f", "ºf", "degrees fahrenheit", "fahrenheit"]),
    "acre": ("imperial", ["acre", "acres"]),
    "sq mi": ("imperial", ["sq mi", "square miles"]),
}
FORM_TO_UNIT = {form: unit for unit, (_, forms) in UNIT_TABLE.items() for form in forms}
UNIT_ALT = "|".join(re.escape(f) for f in sorted(FORM_TO_UNIT, key=len, reverse=True))

ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
WORD_VALUES = {w: i for i, w in enumerate(ONES)} | {w: (i + 2) * 10 for i, w in enumerate(TENS)}
W_ALT = "|".join(sorted(list(WORD_VALUES) + ["hundred"], key=len, reverse=True))
NUM = rf"\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|\d+(?:\.\d+)?|(?:{W_ALT})(?:[\s-]+(?:and[\s-]+)?(?:{W_ALT}))*"
SCALES = {"thousand": 1e3, "million": 1e6, "billion": 1e9}

MEASURE_RE = re.compile(rf"(?<![\w.,])({NUM})(?:\s*(thousand|million|billion))?\s*({UNIT_ALT})(?![\w/²])", re.I)
PERCENT_RE = re.compile(rf"(?<![\w.,])({NUM})\s*(%|percent|per cent)(?!\w)", re.I)
YEAR_RE = re.compile(r"(?<![\w.,$£€-])(1\d{3}|20\d{2})(?:'?s)?(?![\w%]|,\d)")

KIND_CATEGORY = {"year": "Years", "percent": "Percentage", "measure": "Measures", "name": "Name (picture with name)"}


def to_number(text):
    text = text.strip().lower()
    if re.fullmatch(r"[\d,.]+", text):
        return float(text.replace(",", ""))
    value = 0
    for tok in re.split(r"[\s-]+", text):
        if tok in WORD_VALUES:
            value += WORD_VALUES[tok]
        elif tok == "hundred":
            value = max(value, 1) * 100
        elif tok != "and":
            return None
    return float(value)


def extract_facts(text):
    facts, taken = [], []
    for m in MEASURE_RE.finditer(text):
        value = to_number(m.group(1))
        if value is None:
            continue
        value *= SCALES.get((m.group(2) or "").lower(), 1)
        form = m.group(3).lower()
        unit = FORM_TO_UNIT[form]
        facts.append({"kind": "measure", "value": value, "unit": unit, "system": UNIT_TABLE[unit][0],
                      "form": m.group(3), "raw": m.group(0), "pos": m.start()})
        taken.append(m.span())
    for m in PERCENT_RE.finditer(text):
        value = to_number(m.group(1))
        if value is not None:
            facts.append({"kind": "percent", "value": value, "unit": "%", "raw": m.group(0), "pos": m.start()})
            taken.append(m.span())
    for m in YEAR_RE.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        facts.append({"kind": "year", "value": float(m.group(1)), "unit": "", "raw": m.group(0), "pos": m.start()})
    return facts


def same_fact(a, b):
    return (a["kind"] == b["kind"] and a["unit"] == b["unit"]
            and abs(a["value"] - b["value"]) <= max(1e-6, 0.005 * abs(a["value"])))


def spoken_text_with_times(words):
    text, starts, times = "", [], []
    for w in words:
        if text:
            text += " "
        starts.append(len(text))
        times.append(w["start"])
        text += w["w"]
    return text, starts, times


def time_at(pos, starts, times):
    idx = int(np.searchsorted(starts, pos, side="right")) - 1
    return times[max(idx, 0)] if times else 0.0


NAME_STOP = set("""the a an in on at of this that these those it its he she his her they their we i you
but and or so when then there here after before during today yesterday meanwhile however although while if
as by for from with without mr mrs ms dr sir lady lord saint st""".split())
NAME_EXCLUDE = set("""january february march april may june july august september october november december
monday tuesday wednesday thursday friday saturday sunday""".split())


def spoken_names(words):
    """Guess people's names: runs of 2+ capitalised words in the transcript."""
    names, run = [], []

    def flush():
        clean = [w for w in run]
        while clean and clean[0][0].lower() in NAME_STOP:
            clean.pop(0)
        if len(clean) >= 2 and not any(w[0].lower() in NAME_EXCLUDE for w in clean):
            names.append((" ".join(w[0] for w in clean), clean[0][1]))

    for w in words:
        raw = w["w"].strip()
        word = raw.strip(".,!?;:\"()")
        if re.fullmatch(r"[A-Z][\w'’-]*", word):
            run.append((word, w["start"]))
            if raw and raw[-1] in ".,!?;:":
                flush()
                run = []
        else:
            flush()
            run = []
    flush()
    return names


def check_highlights(words, events, seconds, issues):
    """Compare facts the narrator says with the text shown on screen."""
    s = SETTINGS
    tol, search = s["highlight_tolerance_seconds"], s["highlight_search_seconds"]
    text, starts, times = spoken_text_with_times(words)
    spoken = []
    for f in extract_facts(text):
        f["time"] = time_at(f["pos"], starts, times)
        spoken.append(f)
    screen = []
    for ev in events:
        for f in extract_facts(ev["text"]):
            f.update(start=ev["start"], end=ev["end"], screen_text=ev["text"])
            screen.append(f)

    highlights, seen_missing = [], set()
    for f in spoken:
        t, label, cat = f["time"], f["raw"].strip(), KIND_CATEGORY[f["kind"]]
        near = [g for g in screen if same_fact(f, g) and g["start"] - tol <= t <= g["end"] + tol]
        if near:
            g = min(near, key=lambda item: abs(item["start"] - t))
            delay = g["start"] - t
            if delay >= 1.0:
                issues.add("Delayed", t, f'Highlight delayed: "{label}"',
                           f'Narrator says "{label}" at {mmss(t)}, and matching text appears {delay:.1f}s later.',
                           end=g["end"], severity="check")
                highlights.append({"kind": f["kind"], "time": t, "said": label, "shown": g["screen_text"], "status": "delayed"})
            else:
                highlights.append({"kind": f["kind"], "time": t, "said": label, "shown": g["screen_text"], "status": "ok"})
            continue
        close = [g for g in screen if same_fact(f, g) and g["start"] - search <= t <= g["end"] + search]
        if close:
            g = min(close, key=lambda g: abs(g["start"] - t))
            gap = g["start"] - t
            when = f"{abs(gap):.0f}s {'after' if gap > 0 else 'before'}"
            category = "Delayed" if gap > 0 else "Sync"
            title = f'Highlight delayed: "{label}"' if gap > 0 else f'Highlight out of sync: "{label}"'
            issues.add(category, t, title,
                       f'Narrator says "{label}" at {mmss(t)}, but "{g["screen_text"]}" appears {when}.',
                       severity="warning")
            highlights.append({"kind": f["kind"], "time": t, "said": label, "shown": g["screen_text"],
                               "status": "delayed" if gap > 0 else "sync"})
            continue
        other = [g for g in screen if g["kind"] == f["kind"] and g["start"] - tol <= t <= g["end"] + tol]
        if other:
            g = other[0]
            issues.add(cat, t, f'Narration and screen don\'t match: "{label}"',
                       f'Narrator says "{label}" but the screen shows "{g["raw"].strip()}" ({g["screen_text"]}).',
                       severity="error")
            highlights.append({"kind": f["kind"], "time": t, "said": label, "shown": g["screen_text"], "status": "mismatch"})
            continue
        key = (f["kind"], round(f["value"], 3), f["unit"])
        highlights.append({"kind": f["kind"], "time": t, "said": label, "shown": "", "status": "missing"})
        if key not in seen_missing:
            seen_missing.add(key)
            issues.add(cat, t, f'Missing on-screen highlight: "{label}"',
                       f'Narrator says "{label}" but no matching on-screen text was found nearby.',
                       severity="warning")

    for g in screen:
        if not any(same_fact(f, g) and g["start"] - search <= f["time"] <= g["end"] + search for f in spoken):
            issues.add(KIND_CATEGORY[g["kind"]], g["start"], f'On-screen "{g["raw"].strip()}" isn\'t in the narration',
                       f'Shown in "{g["screen_text"]}". Check it is correct and relevant.', end=g["end"], severity="check")

    check_units(spoken, screen, issues)
    check_names(words, events, seconds, issues, highlights)
    highlights.sort(key=lambda h: h["time"])
    return highlights


def check_units(spoken, screen, issues):
    measures = [dict(f, t=f.get("time", f.get("start")), where="narration" if "time" in f else "screen")
                for f in spoken + screen if f["kind"] == "measure"]
    systems = {}
    for f in measures:
        systems[f["system"]] = systems.get(f["system"], 0) + 1
    if len(systems) > 1:
        main = max(systems, key=systems.get)
        for f in measures:
            if f["system"] != main:
                issues.add("Measures", f["t"], f'Unit inconsistency: "{f["raw"].strip()}"',
                           f"The video mostly uses {main} units, but here the {f['where']} uses {f['system']}. "
                           "Pick one system (or show both) throughout.", severity="warning")
    forms = {}
    for f in measures:
        if f["where"] == "screen":
            forms.setdefault(f["unit"], {}).setdefault(f["form"], []).append(f)
    for variants in forms.values():
        if len(variants) > 1:
            main = max(variants, key=lambda k: len(variants[k]))
            for form, items in variants.items():
                if form != main:
                    for f in items:
                        issues.add("Measures", f["t"], f'Unit written differently: "{form}"',
                                   f'On screen this unit is usually written "{main}", but here it is "{form}".',
                                   severity="check")


def check_names(words, events, seconds, issues, highlights):
    faces_at = {sec["t"]: sec["faces"] for sec in seconds}
    seen = set()
    for name, t in spoken_names(words):
        if not probable_person_name(name):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        last = key.split()[-1]
        label = None
        for ev in events:
            if ev["start"] - 3 <= t <= ev["end"] + 8:
                tokens = re.findall(r"[a-z'’-]+", ev["text"].lower())
                if any(difflib.SequenceMatcher(None, last, tok).ratio() >= 0.8 for tok in tokens):
                    label = ev
                    break
        if label is None:
            issues.add("Name (picture with name)", t, f'Name without on-screen label: "{name}"',
                       f'Narrator mentions "{name}". If this is a person, show their picture with their name.',
                       severity="check")
            highlights.append({"kind": "name", "time": t, "said": name, "shown": "", "status": "missing"})
            continue
        has_face = any(faces_at.get(sec) for sec in range(int(label["start"]), int(label["end"])))
        highlights.append({"kind": "name", "time": t, "said": name, "shown": label["text"],
                           "status": "ok" if has_face or not faces_at else "no picture"})
        if not has_face and any(faces_at.values()):
            issues.add("Name (picture with name)", label["start"], f'Name shown without a picture: "{label["text"]}"',
                       "No face detected while the name is on screen. Check the person's photo is shown.",
                       end=label["end"], severity="warning")


NON_PERSON_NAME_TERMS = {
    "county", "parish", "city", "town", "state", "states", "country", "countries", "island", "islands",
    "mountain", "mountains", "bald", "river", "lake", "ocean", "sea", "indies", "worth", "el", "fort",
    "north", "south", "east", "west", "carolina", "mexico", "texas", "missouri", "alabama", "florida",
    "department", "service", "center", "centre", "university", "college", "extension", "research", "wildlife",
    "national", "agency", "administration", "committee", "senate", "house", "office", "system", "association",
}


def probable_person_name(name):
    tokens = re.findall(r"[A-Za-z][A-Za-z'’-]*", name)
    if len(tokens) not in (2, 3) or any(token.isupper() for token in tokens):
        return False
    lowered = {token.lower().strip("'’") for token in tokens}
    if lowered & NON_PERSON_NAME_TERMS:
        return False
    return all(token[0].isupper() for token in tokens)


def consolidate_repeat_issues(items):
    grouped = []
    recent = {}
    nearby_categories = {"Text not visible", "Low quality image", "Glitch", "Measures", "Years", "Percentage"}
    for issue in sorted(items, key=lambda item: item["time"]):
        key = (issue.get("category"), issue.get("title"))
        previous_index = recent.get(key)
        previous = grouped[previous_index] if previous_index is not None else None
        same_spelling = issue.get("category") in {"Spelling", "Number formatting"}
        nearby_repeat = (
            issue.get("category") in nearby_categories and previous is not None
            and float(issue["time"]) - float(previous.get("occurrences", [previous])[ -1 ]["time"]) <= 2.0
        )
        if previous is not None and (same_spelling or nearby_repeat):
            occurrences = previous.setdefault("occurrences", [{"time": previous["time"], "detail": previous.get("detail", "")}])
            occurrences.append({"time": issue["time"], "detail": issue.get("detail", "")})
            if issue.get("end") is not None:
                previous["end"] = max(float(previous.get("end") or issue["end"]), float(issue["end"]))
            previous["detail"] = f"{previous.get('detail', '')} Repeated {len(occurrences)} times; see occurrence timestamps."
        else:
            grouped.append(issue)
            recent[key] = len(grouped) - 1
    return grouped


# ------------------------------- narrator ----------------------------------
def find_narrator(project_dir, time):
    """Find every second where the face at `time` appears. Returns narrator issues."""
    path = os.path.join(project_dir, "faces.npz")
    if not os.path.exists(path):
        raise ValueError("No faces were detected in this video.")
    data = np.load(path)
    sec, box, emb = data["sec"], data["box"], data["emb"]
    candidates = np.where(np.abs(sec - round(time)) <= 1)[0]
    if len(candidates) == 0:
        raise ValueError("No face found at this moment. Pause on a clear shot of the narrator's face.")
    ref = emb[candidates[np.argmax(box[candidates, 2] * box[candidates, 3])]]
    match = (emb @ ref) >= FACE_MATCH_THRESHOLD

    with open(os.path.join(project_dir, "analysis.json"), encoding="utf-8") as f:
        shots = json.load(f)["shots"]
    items = []
    for shot in shots:
        in_shot = match & (sec >= int(shot["start"])) & (sec < shot["end"])
        if not in_shot.any():
            continue
        width = float(np.median(box[in_shot, 2]))
        centre = float(np.median(box[in_shot, 0] + box[in_shot, 2] / 2))
        full_frame = width > 0.12 and 0.3 < centre < 0.7
        items.append({
            "id": f"n{len(items) + 1}",
            "category": "Narrator visual",
            "time": float(shot["start"]), "end": float(shot["end"]),
            "severity": "warning" if full_frame else "check",
            "title": "Narrator on screen, no supporting visual?" if full_frame else "Narrator on screen",
            "detail": ("The narrator fills the middle of the frame. Add an image or visual alongside him."
                       if full_frame else "Narrator appears smaller or to the side. Check a supporting image/visual is shown."),
            "source": "narrator",
        })
    return items


# -------------------------------- main -------------------------------------
def analyze(video_path, project_dir, model_name="base", language="en", report=lambda p, m: None,
            checkpoint=lambda: None):
    """Run every automatic check and write analysis.json into project_dir."""
    stages = {"audio": (0, 5), "motion": (5, 25), "speech": (25, 55), "frames": (55, 95), "checks": (95, 100)}

    def stage(name):
        lo, hi = stages[name]
        return lambda msg, frac=0.0: report(lo + (hi - lo) * min(max(frac, 0.0), 1.0), msg)

    warnings = []
    info = video_info(video_path)
    issues = Issues()
    if info["duration"] > 20 * 60:
        warnings.append(f"This video is {info['duration'] / 60:.0f} minutes long. Analysis will be slow.")
    if info["height"] < 1080:
        issues.add("Low quality image", 0, f"Video resolution is {info['width']}x{info['height']}",
                   "Lower than Full HD (1920x1080). Check the export settings.", severity="check")

    p = stage("audio")
    audio_duration = max(float(info["duration"]), 0.1)
    p("Preparing audio checks...", 0)
    samples = load_audio(
        video_path,
        lambda fraction: p(
            f"Decoding audio for checks... {mmss(fraction * audio_duration)} of {mmss(audio_duration)}",
            fraction * 0.45,
        ),
        audio_duration,
        checkpoint,
    )
    if samples is None:
        warnings.append("No audio track found, so audio and narration checks were skipped.")
    else:
        p("Checking audio levels and gaps...", 0.5)
        check_audio(samples, issues)
        check_clipping(
            video_path,
            issues,
            lambda fraction: p(
                f"Checking audio clipping... {mmss(fraction * audio_duration)} of {mmss(audio_duration)}",
                0.5 + fraction * 0.5,
            ),
            audio_duration,
            checkpoint,
        )
        audio_len = len(samples) / SAMPLE_RATE
        if abs(audio_len - info["duration"]) > 0.5:
            issues.add("Sync", min(audio_len, info["duration"]), "Audio and video lengths differ",
                       f"Audio is {audio_len:.1f}s and video is {info['duration']:.1f}s. Possible sync drift.",
                       severity="warning")
    p("Audio checks complete.", 1)

    motion = scan_motion(video_path, info, stage("motion"), checkpoint)
    info["duration"] = round(motion["duration"], 3)
    for a, b in motion["blanks"]:
        issues.add("Dead air / blank clip", a, f"Blank screen for {b - a:.1f}s",
                   "Black or solid-colour frame. Missing footage or a gap in the timeline?", end=b, severity="error")
    for t in motion["flash"]:
        issues.add("Glitch", t, "Flash frame / single-frame glitch",
                   "One frame is completely different from the frames around it. Usually a stray clip on the timeline.",
                   severity="error")
    for a, b in motion["frozen"]:
        issues.add("Glitch", a, f"Picture freezes for {b - a:.1f}s",
                   "Moving footage suddenly stops. Clip too short, or a render problem?", end=b, severity="check")

    bounds = [0.0] + motion["cuts"] + [info["duration"]]
    shots = [{"start": round(a, 3), "end": round(b, 3), "duration": round(b - a, 3)}
             for a, b in zip(bounds[:-1], bounds[1:]) if b > a]
    segments, words = [], []
    if samples is not None:
        p = stage("speech")
        try:
            segments, words = transcribe(samples, model_name, language, p, checkpoint)
        except Exception as e:
            warnings.append(f"Speech-to-text failed ({e}). Narration checks were skipped.")

    p = stage("frames")
    p("Preparing face and text readers (first run downloads them)...")
    face_tools = ocr_engine = None
    try:
        face_tools = FaceTools()
    except Exception as e:
        warnings.append(f"Face detection unavailable ({e}). Name-picture and narrator checks are limited.")
    try:
        from rapidocr import RapidOCR
        ocr_engine = RapidOCR()
    except Exception as e:
        warnings.append(f"On-screen text reader unavailable ({e}). Spelling and highlight checks were skipped.")
    seconds = scan_seconds(video_path, info, project_dir, p, face_tools, ocr_engine, checkpoint)

    p = stage("checks")
    p("Comparing narration with on-screen text...")
    for shot in shots:
        vals = [s["sharp"] for s in seconds if shot["start"] - 0.5 <= s["t"] < shot["end"] and s["std"] >= 8]
        if vals and np.median(vals) < SETTINGS["blur_threshold"]:
            issues.add("Low quality image", shot["start"], "Blurry or low-quality picture",
                       f"Sharpness score {np.median(vals):.0f} (sharp HD footage is usually above 45). "
                       "Check for an upscaled or soft image.", end=shot["end"], severity="check")

    events = build_text_events(seconds)
    spoken_words = {re.sub(r"[^\w']", "", w["w"]).lower() for w in words}
    check_text(events, spoken_words, language, issues)
    check_number_formatting(events, issues)
    highlights = check_highlights(words, events, seconds, issues) if words or events else []

    for shot in shots:
        mid = (shot["start"] + shot["end"]) / 2
        shot["thumb"] = min(int(mid), max(len(seconds) - 1, 0))
        shot["narration"] = "".join(w["w"] for w in words if shot["start"] <= w["start"] < shot["end"]).strip()
        shot["on_screen"] = [ev["text"] for ev in events if ev["start"] < shot["end"] and ev["end"] > shot["start"]]

    for item in issues.items:
        if item["end"] is not None:
            item["end"] = round(min(item["end"], info["duration"]), 3)
    issues.items = consolidate_repeat_issues(issues.items)
    issues.items.sort(key=lambda i: i["time"])
    result = {
        "info": info,
        "settings": SETTINGS,
        "warnings": warnings,
        "thumb_count": len(seconds),
        "shots": shots,
        "transcript": segments,
        "words": words,
        "text_events": events,
        "highlights": highlights,
        "issues": issues.items,
    }
    write_analysis(os.path.join(project_dir, "analysis.json"), result)
    report(100, "Done")
    return result
