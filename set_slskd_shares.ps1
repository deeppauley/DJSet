$configPath = Join-Path $PSScriptRoot "slskd-data\slskd.yml"
if (-not (Test-Path $configPath)) {
    throw "Config file not found: $configPath"
}

$startMarker = "# BEGIN DJ Set Builder managed shares"
$endMarker = "# END DJ Set Builder managed shares"

$lines = Get-Content -LiteralPath $configPath
$output = New-Object System.Collections.Generic.List[string]
$skip = $false

foreach ($line in $lines) {
    if ($line -eq $startMarker) {
        $skip = $true
        continue
    }

    if ($line -eq $endMarker) {
        $skip = $false
        continue
    }

    if (-not $skip) {
        $output.Add($line)
    }
}

if ($output.Count -gt 0 -and $output[$output.Count - 1].Trim() -ne "") {
    $output.Add("")
}

$output.Add($startMarker)
$output.Add("shares:")
$output.Add("  directories:")
$output.Add("    - '[app-downloads]/app/downloads'")
$output.Add("    - '[slskd-downloads]/downloads'")
$output.Add("  filters:")
$output.Add("    - \.ini$")
$output.Add("    - Thumbs.db$")
$output.Add("    - \.DS_Store$")
$output.Add("  cache:")
$output.Add("    storage_mode: memory")
$output.Add("    workers: 16")
$output.Add("    retention: ~")
$output.Add($endMarker)

Copy-Item -LiteralPath $configPath -Destination "$configPath.bak" -Force
Set-Content -LiteralPath $configPath -Value $output -Encoding UTF8

docker compose -f "$PSScriptRoot\docker-compose.slskd.yml" restart slskd
Write-Host "Updated slskd shares to include /app/downloads and /downloads, then restarted slskd."
