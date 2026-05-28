param(
    [string]$ListPath = "C:\Users\autom\Documents\artist in order downloads.txt",
    [int]$StartIndex = 1,
    [string]$StartAt,
    [switch]$Resume,
    [switch]$StopOnError,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONIOENCODING = "utf-8"
$env:SONGZIP_PROVIDER_SEARCH_BEFORE_DIRECT = "true"

function Normalize-QueueEntry {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Entry
    )

    $trimmed = $Entry.Trim()
    if ($trimmed -match "^https?://(?:www\.)?youtube\.com/results\?search_query=(.+)$") {
        $searchTerm = [Uri]::UnescapeDataString(($Matches[1] -replace "\+", " ")).Trim()
        if (-not [string]::IsNullOrWhiteSpace($searchTerm)) {
            return "ytartist: $searchTerm"
        }
    }

    return $trimmed
}

function Get-QueueEntryStatus {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Entry
    )

    $trimmed = $Entry.Trim()
    if ($trimmed -match "^(?i:done-)\s*") {
        return "Done"
    }

    if ($trimmed -match "^(?i:partial-)\s*") {
        return "Partial"
    }

    return "Pending"
}

function Get-QueueEntryQuery {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Entry
    )

    $trimmed = $Entry.Trim()
    if ($trimmed -match "^(?i:(done|partial)-)\s*(.+)$") {
        return $Matches[2].Trim()
    }

    return $trimmed
}

function Find-QueueMatchIndex {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Queue,
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $normalizedValue = Normalize-QueueEntry -Entry $Value
    for ($queueIndex = 0; $queueIndex -lt $Queue.Count; $queueIndex++) {
        $item = $Queue[$queueIndex]
        if (
            $item.Original.Equals($Value, [System.StringComparison]::OrdinalIgnoreCase) -or
            $item.Query.Equals($normalizedValue, [System.StringComparison]::OrdinalIgnoreCase)
        ) {
            return ($queueIndex + 1)
        }
    }

    return -1
}

function Test-BlockingSpotifyRateLimit {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Output
    )

    return $Output -match "Spotify rate limit reached" -or
        $Output -match "Retry-After was \d+ seconds" -or
        $Output -match "SpotifyException.*429" -or
        $Output -match "returned 429" -or
        $Output -match "status code 429"
}

$projectDir = $PSScriptRoot
$launcher = Join-Path $projectDir "run-spotdl.bat"

if (-not (Test-Path $launcher)) {
    throw "Could not find run-spotdl.bat in $projectDir"
}

if (-not (Test-Path $ListPath)) {
    throw "Could not find artist list file: $ListPath"
}

$progressPath = [System.IO.Path]::ChangeExtension($ListPath, ".progress.txt")
$listLines = Get-Content $ListPath
$libraryRoot = "C:\Users\autom\Documents\All Songs"
$queueLogRoot = Join-Path $projectDir ".spotdl-tools\artist-queue\logs"
New-Item -ItemType Directory -Path $queueLogRoot -Force | Out-Null
$queue = @()
for ($lineIndex = 0; $lineIndex -lt $listLines.Count; $lineIndex++) {
    $line = $listLines[$lineIndex]
    if ([string]::IsNullOrWhiteSpace($line)) {
        continue
    }

    $trimmed = $line.Trim()
    if ($trimmed.StartsWith("#")) {
        continue
    }

    $status = Get-QueueEntryStatus -Entry $trimmed
    if ($status -in @("Done", "Partial")) {
        continue
    }

    $queryText = Get-QueueEntryQuery -Entry $trimmed

    $queue += [PSCustomObject]@{
        Original = $queryText
        Query = Normalize-QueueEntry -Entry $queryText
        LineIndex = $lineIndex
        Status = $status
    }
}

if ($queue.Count -eq 0) {
    throw "No artist entries were found in $ListPath"
}

if ($Resume -and (Test-Path $progressPath)) {
    $savedIndexText = (Get-Content $progressPath -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($savedIndexText -match "^\d+$") {
        $StartIndex = [int]$savedIndexText
    } elseif (-not [string]::IsNullOrWhiteSpace($savedIndexText)) {
        $matchedSavedIndex = Find-QueueMatchIndex -Queue $queue -Value $savedIndexText.Trim()
        if ($matchedSavedIndex -gt 0) {
            $StartIndex = $matchedSavedIndex
        }
    }
}

if (-not [string]::IsNullOrWhiteSpace($StartAt)) {
    $matchedIndex = Find-QueueMatchIndex -Queue $queue -Value $StartAt

    if ($matchedIndex -lt 1) {
        throw "Could not find a queue item matching: $StartAt"
    }

    $StartIndex = $matchedIndex
}

if ($StartIndex -lt 1 -or $StartIndex -gt $queue.Count) {
    throw "StartIndex must be between 1 and $($queue.Count)."
}

Write-Host "Queued $($queue.Count) artist entries from $ListPath."
if ($StartIndex -gt 1) {
    Write-Host "Resuming from item $StartIndex."
}

$failures = @()
for ($index = $StartIndex - 1; $index -lt $queue.Count; $index++) {
    $item = $queue[$index]
    Write-Host ""
    Write-Host "[$($index + 1)/$($queue.Count)] Starting artist queue item:"
    Write-Host $item.Query

    if ($DryRun) {
        continue
    }

    $beforeCount = 0
    if (Test-Path $libraryRoot) {
        $beforeCount = (Get-ChildItem -Path $libraryRoot -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count
    }

    $safeLogName = ($item.Query -replace "[^\p{L}\p{Nd}\.-]+", "_").Trim("_")
    if ([string]::IsNullOrWhiteSpace($safeLogName)) {
        $safeLogName = "artist"
    }
    if ($safeLogName.Length -gt 80) {
        $safeLogName = $safeLogName.Substring(0, 80)
    }
    $commandLog = Join-Path $queueLogRoot ("{0:D4}-{1}.log" -f ($index + 1), $safeLogName)

    & $launcher $item.Query *>&1 | Tee-Object -FilePath $commandLog
    $exitCode = $LASTEXITCODE
    $commandOutput = ""
    if (Test-Path $commandLog) {
        $commandOutput = Get-Content -Path $commandLog -Raw -ErrorAction SilentlyContinue
    }

    $afterCount = 0
    if (Test-Path $libraryRoot) {
        $afterCount = (Get-ChildItem -Path $libraryRoot -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count
    }

    if (Test-BlockingSpotifyRateLimit -Output $commandOutput) {
        Set-Content -Path $progressPath -Value $item.Original -Encoding utf8
        $failures += [PSCustomObject]@{
            Index = $index + 1
            Query = "$($item.Query) (spotify rate limited)"
            ExitCode = 429
        }

        Write-Host "[$($index + 1)/$($queue.Count)] Spotify rate limit detected. Leaving this artist pending and stopping the queue."
        Write-Host "Retry later with: powershell -ExecutionPolicy Bypass -File .\run-artist-queue.ps1 -Resume"
        exit 75
    }

    if ($exitCode -eq 0) {
        $lineToMark = $queue[$index].LineIndex
        if (
            $lineToMark -ge 0 -and
            $lineToMark -lt $listLines.Count -and
            -not [string]::IsNullOrWhiteSpace($listLines[$lineToMark])
        ) {
            $listLines[$lineToMark] = "Done- $($queue[$index].Original)"
            Set-Content -Path $ListPath -Value $listLines -Encoding utf8
        }

        if ($index + 1 -lt $queue.Count) {
            Set-Content -Path $progressPath -Value $queue[$index + 1].Original -Encoding utf8
        } else {
            if (Test-Path $progressPath) {
                Remove-Item $progressPath -Force
            }
        }

        Write-Host "[$($index + 1)/$($queue.Count)] Finished successfully."
        continue
    }

    if ($afterCount -gt $beforeCount) {
        $lineToMark = $queue[$index].LineIndex
        if (
            $lineToMark -ge 0 -and
            $lineToMark -lt $listLines.Count -and
            -not [string]::IsNullOrWhiteSpace($listLines[$lineToMark])
        ) {
            $listLines[$lineToMark] = "Partial- $($queue[$index].Original)"
            Set-Content -Path $ListPath -Value $listLines -Encoding utf8
        }

        if ($index + 1 -lt $queue.Count) {
            Set-Content -Path $progressPath -Value $queue[$index + 1].Original -Encoding utf8
        } else {
            if (Test-Path $progressPath) {
                Remove-Item $progressPath -Force
            }
        }

        $failures += [PSCustomObject]@{
            Index = $index + 1
            Query = "$($item.Query) (partial)"
            ExitCode = $exitCode
        }

        Write-Host "[$($index + 1)/$($queue.Count)] Partial success detected. Moving to the next artist."
        continue
    }

    Set-Content -Path $progressPath -Value $item.Original -Encoding utf8
    $failures += [PSCustomObject]@{
        Index = $index + 1
        Query = $item.Query
        ExitCode = $exitCode
    }

    Write-Host "[$($index + 1)/$($queue.Count)] Failed with exit code $exitCode."
    if ($StopOnError) {
        exit $exitCode
    }
}

if ($DryRun) {
    Write-Host ""
    Write-Host "Dry run complete. No downloads were started."
    exit 0
}

Write-Host ""
if ($failures.Count -eq 0) {
    if (Test-Path $progressPath) {
        Remove-Item $progressPath -Force
    }
    Write-Host "All queued artists finished."
    exit 0
}

Write-Host "$($failures.Count) queue item(s) failed:"
foreach ($failure in $failures) {
    Write-Host " - [$($failure.Index)] $($failure.Query) (exit $($failure.ExitCode))"
}

exit 1
