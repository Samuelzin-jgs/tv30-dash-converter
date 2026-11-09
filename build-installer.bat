@echo off
setlocal
cd /d "%~dp0"
set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" (
  echo Inno Setup 6 nao encontrado. Instale com: winget install JRSoftware.InnoSetup
  exit /b 1
)
if not exist "C:\ffmpeg-mpegh\bin\ffmpeg.exe" (
  echo FFmpeg nao encontrado em C:\ffmpeg-mpegh. Rode antes: bash tools/build-ffmpeg-mpegh.sh no MSYS2 UCRT64
  exit /b 1
)
python tools\make_payload.py || exit /b 1
python -m pip install --quiet pyinstaller || exit /b 1
python -m PyInstaller --clean --noconfirm --onedir --windowed --name TV30Converter --icon installer\icon.ico --add-data "installer\icon.ico;." --distpath dist --workpath build app.py || exit /b 1
"%ISCC%" installer\setup.iss || exit /b 1
echo.
echo Instalador em release\
