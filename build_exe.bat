@echo off
REM Builds dist\FretFlow\FretFlow.exe  (add --onefile for a single exe; slower start-up)
python -m pip install -r requirements.txt || exit /b 1
pyinstaller --noconfirm --clean --windowed --name FretFlow ^
  --collect-all sounddevice --collect-all _sounddevice_data ^
  --exclude-module tkinter --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.Qt3DCore ^
  main.py
echo.
echo Done: dist\FretFlow\FretFlow.exe
