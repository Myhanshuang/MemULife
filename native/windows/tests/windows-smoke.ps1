param([string]$Configuration = "Release")
$ErrorActionPreference = "Stop"
$nativeRoot = Split-Path $PSScriptRoot -Parent
Push-Location $nativeRoot
try {
    dotnet restore CaptureHelper/CaptureHelper.csproj --locked-mode
    if ($LASTEXITCODE -ne 0) { throw "Restore failed" }
    dotnet build CaptureHelper/CaptureHelper.csproj --no-restore -c $Configuration
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
    dotnet run --project tests/CoreFixtures/CoreFixtures.csproj -c $Configuration
    if ($LASTEXITCODE -ne 0) { throw "Core fixtures failed" }
    $testSpool = Join-Path ([IO.Path]::GetTempPath()) ("memulife-codec-" + [guid]::NewGuid())
    try {
        # Synthetic WARP textures only: this mode never selects or records the desktop.
        dotnet run --project CaptureHelper/CaptureHelper.csproj --no-build -c $Configuration -- fixture `
            --spool $testSpool --device-id ([guid]::NewGuid().ToString()) --stream-id ([guid]::NewGuid().ToString())
        if ($LASTEXITCODE -ne 0) { throw "Windows Media Foundation encoder fixture failed" }
        $manifests = @(Get-ChildItem (Join-Path $testSpool "manifests") -Filter *.json -Recurse)
        if ($manifests.Count -ne 1) { throw "Expected exactly one sealed synthetic segment" }
        $manifest = Get-Content $manifests[0].FullName -Raw | ConvertFrom-Json
        $video = Join-Path $testSpool $manifest.relative_path
        if ($manifest.kind -ne "sealed_segment" -or $manifest.frame_count -ne 12) { throw "Invalid encoder receipt" }
        if ((Get-FileHash $video -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.sha256) { throw "Hash mismatch" }
        if ((Get-Item $video).Length -ne $manifest.size_bytes) { throw "Length mismatch" }
        Write-Output "Windows codec fixture passed. Interactive WGC/device qualification remains separate."
    } finally { Remove-Item $testSpool -Recurse -Force }
} finally { Pop-Location }
