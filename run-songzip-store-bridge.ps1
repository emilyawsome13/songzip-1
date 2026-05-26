param(
  [string]$Host = "0.0.0.0",
  [int]$Port = 8820
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

$envFile = Join-Path $repoRoot ".spotdl.env"
if (Test-Path $envFile) {
  Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -match '^\s*$') {
      return
    }

    $parts = $_ -split '=', 2
    if ($parts.Length -eq 2) {
      [System.Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim())
    }
  }
}

if (-not $env:SONGZIP_REMOTE_STORE_SHARED_SECRET) {
  throw "Set SONGZIP_REMOTE_STORE_SHARED_SECRET in .spotdl.env before starting the SongZip store bridge."
}

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
  throw "The project virtual environment was not found at .\.venv. Create it first before running the SongZip store bridge."
}

$dataRoot = if ($env:SPOTDL_DATA_DIR) { $env:SPOTDL_DATA_DIR } else { "default local SongZip path" }
Write-Host "Starting SongZip store bridge on http://$Host`:$Port"
Write-Host "Database path root: $dataRoot"

& ".\.venv\Scripts\python.exe" -m uvicorn spotdl.songzip_store_app:app --host $Host --port $Port
