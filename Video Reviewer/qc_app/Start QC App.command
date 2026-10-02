#!/bin/bash
set -e

cd "$(dirname "$0")"
trap 'status=$?; echo "Setup or app startup failed (exit $status)."; read -r -p "Press Return to close this window."; exit "$status"' ERR

export VIDEO_QC_DATA_DIR="${VIDEO_QC_DATA_DIR:-$HOME/Library/Application Support/VideoQC/projects}"
export VIDEO_QC_MODEL_CACHE_DIR="${VIDEO_QC_MODEL_CACHE_DIR:-$HOME/Library/Caches/VideoQC}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 was not found. Install Python 3 for macOS, then run this launcher again."
    read -r -p "Press Return to close this window."
    exit 1
fi

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi

PYTHON=".venv/bin/python"
if ! "$PYTHON" -c "import flask, cv2, faster_whisper, rapidocr, docx, pypdf, ollama, spellchecker, imageio_ffmpeg, fastembed, llama_cpp" >/dev/null 2>&1; then
    if ! xcode-select -p >/dev/null 2>&1; then
        echo "Xcode Command Line Tools are required to build the local text AI dependency."
        echo "Install them by running: xcode-select --install"
        read -r -p "Press Return to close this window."
        exit 1
    fi

    if ! command -v cmake >/dev/null 2>&1; then
        echo "CMake is required to build the local text AI dependency."
        echo "Install it with Homebrew: brew install cmake"
        read -r -p "Press Return to close this window."
        exit 1
    fi

    echo "Installing Video Reviewer dependencies. This may take several minutes..."
    CMAKE_ARGS="${CMAKE_ARGS:+$CMAKE_ARGS }-DGGML_METAL=on" \
        "$PYTHON" -m pip install --upgrade pip
    CMAKE_ARGS="${CMAKE_ARGS:+$CMAKE_ARGS }-DGGML_METAL=on" \
        "$PYTHON" -m pip install -r requirements.txt
fi

echo "Starting Video Reviewer. Keep this Terminal window open while you work."
"$PYTHON" app.py
