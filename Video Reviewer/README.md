# Video Reviewer

A local video quality-review app. The web interface runs on the same computer as the video files and listens only on `127.0.0.1`.

## macOS (Apple silicon)

1. Install Python 3.13 and Ollama for macOS.
2. Open Terminal and install the tools needed to build the local text model:

   ```sh
   xcode-select --install
   brew install cmake
   ```

   If Command Line Tools or CMake are already installed, skip the corresponding command. Homebrew is needed for the CMake command.
3. In Terminal, go to the `qc_app` folder and make the launcher executable:

   ```sh
   cd /path/to/Video-Reviewer/qc_app
   chmod +x "Start QC App.command"
   ```

4. Double-click `Start QC App.command`. On first launch it creates a Python virtual environment and installs the app dependencies. The `llama-cpp-python` package is built with Apple Metal support. Keep the Terminal window open while using the app.
5. Start Ollama and install a vision-capable model from Ollama for visual review and image questions. The local text assistant downloads its separate model the first time it is used.

The first dependency installation and first model downloads can take a while. If macOS blocks the launcher, use **Control-click > Open** once, or start it from Terminal with `./"Start QC App.command"`.

On macOS, projects are stored under `~/Library/Application Support/VideoQC/projects` and downloaded Python AI models under `~/Library/Caches/VideoQC`, separately from the downloaded source folder.

## Windows

Double-click `qc_app/Start QC App.bat`. Python must be installed and available as `python`. The launcher creates no virtual environment automatically.

## Data and backups

Projects, uploaded videos, generated thumbnails, and downloaded analysis models are local and are not published with this source. The repository `.gitignore` excludes the app's generated project/model folders and runtime logs. Before pushing to GitHub, review the files staged for upload and make sure no private media or reports are included.

To move a project to another computer, export or copy that project separately using a trusted method. GitHub is for the app source; it is not a project-data sync service.
