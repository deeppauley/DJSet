param(
    [string]$DownloadsPath = (Join-Path $PSScriptRoot "slskd-data\downloads"),
    [string]$OutputPath = (Join-Path $PSScriptRoot "Downloaded Soulseek Tracks.m3u8")
)

$extensions = @(".mp3", ".flac", ".wav", ".m4a", ".aiff", ".aif")
$files = Get-ChildItem -LiteralPath $DownloadsPath -Recurse -File |
    Where-Object { $extensions -contains $_.Extension.ToLowerInvariant() } |
    Sort-Object FullName

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("#EXTM3U")

foreach ($file in $files) {
    $title = [System.IO.Path]::GetFileNameWithoutExtension($file.Name)
    $lines.Add("#EXTINF:-1,$title")
    $lines.Add($file.FullName)
}

Set-Content -LiteralPath $OutputPath -Value $lines -Encoding UTF8
Write-Host "Created playlist: $OutputPath"
Write-Host "Tracks: $($files.Count)"
