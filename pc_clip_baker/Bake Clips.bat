@echo off
REM ===================================================================
REM  VJ clip baker - transcode clips to Pi-5 HEVC using your GPU (NVENC)
REM  1. Put source clips in the  input  folder next to this file.
REM  2. Double-click this file.
REM  3. Collect the HEVC .mp4s from the  output  folder, copy to the Pi.
REM ===================================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python was not found. Install Python 3 from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup, then run this again.
  echo.
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo.
  echo ffmpeg was not found. Get a full build from
  echo   https://www.gyan.dev/ffmpeg/builds/   ^(ffmpeg-release-full^)
  echo unzip it, and add its  bin  folder to your PATH, then run this again.
  echo.
  pause
  exit /b 1
)

python "%~dp0bake_clips.py" %*
echo.
pause
