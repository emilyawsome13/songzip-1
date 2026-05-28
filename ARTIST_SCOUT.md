# SongZip Artist Scout

Artist Scout builds a ranked, resumable artist research queue for genre discovery.
It does not download media by itself. Exporting a downloader-ready queue requires
an explicit rights confirmation so the workflow stays limited to artists and
tracks you are allowed to process.

## Quick Start

Discover up to 100 artists across bass-heavy genres:

```powershell
cd "C:\Users\autom\Documents\Songzip"
powershell -ExecutionPolicy Bypass -File .\run-artist-scout.ps1 discover
```

Show the next pending artist:

```powershell
powershell -ExecutionPolicy Bypass -File .\run-artist-scout.ps1 next
```

Show progress:

```powershell
powershell -ExecutionPolicy Bypass -File .\run-artist-scout.ps1 status
```

Mark an artist after review:

```powershell
powershell -ExecutionPolicy Bypass -File .\run-artist-scout.ps1 mark -Artist "1" -Status done
```

Export a reviewed queue only after confirming the sources are allowed:

```powershell
powershell -ExecutionPolicy Bypass -File .\run-artist-scout.ps1 export -IHaveRights
```

The exported queue path defaults to:

```text
C:\Users\autom\Documents\artist in order downloads.txt
```

That file can be used by the existing artist queue runner:

```powershell
powershell -ExecutionPolicy Bypass -File .\run-artist-queue.ps1 -Resume
```

## Useful Options

Use a custom genre set:

```powershell
powershell -ExecutionPolicy Bypass -File .\run-artist-scout.ps1 discover -Genres "dubstep,riddim,color bass"
```

Use curated seed metadata only:

```powershell
powershell -ExecutionPolicy Bypass -File .\run-artist-scout.ps1 discover -Offline
```

Export to a different file:

```powershell
powershell -ExecutionPolicy Bypass -File .\run-artist-scout.ps1 export -IHaveRights -ExportPath "C:\Users\autom\Documents\approved artists.txt"
```

## Stored Files

The scout queue is stored at:

```text
C:\Users\autom\Documents\Songzip\.spotdl-tools\artist-scout\artist_scout_queue.json
```

That folder is local runtime state and is ignored by git.
