REM SPDX-License-Identifier: GPL-3.0-only
REM Copyright (C) 2026 HWQ277

@echo off
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py mcdown.py
) else (
    python mcdown.py
)

if errorlevel 1 pause
