@echo off
rem Double-clickable launcher; bypasses PowerShell script execution policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev.ps1"
