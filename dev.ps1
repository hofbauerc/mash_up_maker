# Starts the Mash-Up Maker backend and frontend, each in its own window,
# then opens the app in the browser. Stop by closing the two windows.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

# Pick up tools installed after the current shell was opened (uv, ffmpeg).
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'User')

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv not found. Install it with: winget install astral-sh.uv'
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw 'npm not found. Install Node.js 20+ first.'
}

if (-not (Test-Path "$root\frontend\node_modules")) {
    Write-Host 'First run: installing frontend dependencies...'
    Push-Location "$root\frontend"
    npm install
    Pop-Location
}

Write-Host 'Starting backend  -> http://127.0.0.1:8000'
Start-Process powershell -ArgumentList '-NoExit', '-Command',
    "`$host.UI.RawUI.WindowTitle = 'mashup backend'; Set-Location '$root\backend'; uv run uvicorn app.main:app --reload --port 8000"

Write-Host 'Starting frontend -> http://localhost:5173'
Start-Process powershell -ArgumentList '-NoExit', '-Command',
    "`$host.UI.RawUI.WindowTitle = 'mashup frontend'; Set-Location '$root\frontend'; npm run dev"

Write-Host 'Waiting for the backend to come up...'
$up = $false
foreach ($i in 1..60) {
    try {
        Invoke-RestMethod http://127.0.0.1:8000/api/health -TimeoutSec 2 | Out-Null
        $up = $true
        break
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
if ($up) {
    Write-Host 'Backend is up. Opening http://localhost:5173'
    Start-Process http://localhost:5173
} else {
    Write-Warning 'Backend did not respond within 30s - check the "mashup backend" window for errors.'
}
