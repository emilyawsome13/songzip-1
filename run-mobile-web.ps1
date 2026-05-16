param(
    [int]$Port = 8801,
    [string]$BindHost = "0.0.0.0"
)

$ErrorActionPreference = "Stop"
$projectDir = $PSScriptRoot
$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"

function Load-EnvFile {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return
    }

    foreach ($line in Get-Content -Path $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }

        $parts = $trimmed -split "=", 2
        if ($parts.Count -ne 2) {
            continue
        }

        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        Set-Item -Path ("Env:{0}" -f $key) -Value $value
    }
}

if (-not (Test-Path $pythonExe)) {
    throw "Virtual environment not found at .venv. Run .\run-spotdl.ps1 web once first."
}

Load-EnvFile -Path (Join-Path $projectDir ".spotdl.env")

$env:SPOTDL_PORT = [string]$Port
$env:SPOTDL_HOST = $BindHost
$env:SPOTDL_FORMAT = "mp3"
$env:SPOTDL_OUTPUT_TEMPLATE = "{artist} - {title}.{output-ext}"
$env:SPOTDL_BUNDLE_FLATTEN = "true"
$env:SPOTDL_BUNDLE_COMPRESSION = "store"
$env:SPOTDL_THREADS = "3"
$env:SPOTDL_DOWNLOAD_GAP_SECONDS = "2"
$env:SPOTDL_YT_DLP_ARGS = "--concurrent-fragments 2 --extractor-args youtube:player_client=web"

Push-Location $projectDir
try {
    & $pythonExe -m uvicorn spotdl.render_mobile_app:app --host $BindHost --port $Port
}
finally {
    Pop-Location
}
