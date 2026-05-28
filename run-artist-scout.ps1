param(
    [ValidateSet("discover", "status", "next", "mark", "export")]
    [string]$Action = "discover",
    [string]$Genres = "dubstep,riddim,tearout dubstep,brostep,color bass,future riddim,experimental bass,deathstep,neurofunk,bass music",
    [int]$Limit = 100,
    [string]$QueuePath,
    [string]$Artist,
    [ValidateSet("pending", "scanning", "done", "partial", "blocked", "skipped")]
    [string]$Status = "done",
    [string]$Notes = "",
    [string]$ExportPath = "C:\Users\autom\Documents\artist in order downloads.txt",
    [switch]$Offline,
    [switch]$IHaveRights
)

$ErrorActionPreference = "Stop"
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONIOENCODING = "utf-8"

$projectDir = $PSScriptRoot
$scriptPath = Join-Path $projectDir "tools\songzip_artist_scout.py"

if (-not $QueuePath) {
    $QueuePath = Join-Path $projectDir ".spotdl-tools\artist-scout\artist_scout_queue.json"
}

if (-not (Test-Path $scriptPath)) {
    throw "Could not find artist scout script: $scriptPath"
}

$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    $pythonExe = "py"
}

$scoutArgs = @($scriptPath, $Action, "--queue", $QueuePath)

if ($Action -eq "discover") {
    $scoutArgs += @("--genres", $Genres, "--limit", [string]$Limit)
    if ($Offline) {
        $scoutArgs += "--offline"
    }
}
elseif ($Action -eq "mark") {
    if ([string]::IsNullOrWhiteSpace($Artist)) {
        throw "Use -Artist with the mark action."
    }
    $scoutArgs += @("--artist", $Artist, "--status", $Status)
    if (-not [string]::IsNullOrWhiteSpace($Notes)) {
        $scoutArgs += @("--notes", $Notes)
    }
}
elseif ($Action -eq "export") {
    $scoutArgs += @("--output", $ExportPath)
    if ($IHaveRights) {
        $scoutArgs += "--i-have-rights"
    }
}

Push-Location $projectDir
try {
    & $pythonExe $scoutArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
