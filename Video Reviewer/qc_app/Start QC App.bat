@echo off
cd /d "%~dp0"
title Video QC Review - keep this window open while you work
set "VIDEO_QC_USE_GPU=0"

python -c "import flask, cv2, faster_whisper, rapidocr, docx, pypdf, ollama, spellchecker, imageio_ffmpeg, fastembed, llama_cpp" 2>nul
if errorlevel 1 (
    echo First-time setup: installing the add-ons the app needs. This takes a few minutes...
    python -m pip install --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Setup failed. Check your internet connection and try again.
        pause
        exit /b 1
    )
)

echo Starting Video QC Review. Your browser will open in a moment.
echo Keep this window open while you work. Close it to stop the app.
echo.
python app.py
echo.
echo The app has stopped. If you see an error above, send it to your helper.
pause
