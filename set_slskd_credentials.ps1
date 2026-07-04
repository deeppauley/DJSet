param(
    [Parameter(Mandatory = $true)]
    [string]$Username
)

$configPath = Join-Path $PSScriptRoot "slskd-data\slskd.yml"
if (-not (Test-Path $configPath)) {
    throw "Config file not found: $configPath"
}

$securePassword = Read-Host "Soulseek password" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
try {
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

$escapedPassword = $plainPassword.Replace("'", "''")
$startMarker = "# BEGIN DJ Set Builder managed Soulseek credentials"
$endMarker = "# END DJ Set Builder managed Soulseek credentials"

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
$output.Add("soulseek:")
$output.Add("  address: vps.slsknet.org")
$output.Add("  port: 2271")
$output.Add("  username: $Username")
$output.Add("  password: '$escapedPassword'")
$output.Add("  listen_ip_address: 0.0.0.0")
$output.Add("  listen_port: 50300")
$output.Add($endMarker)

Copy-Item -LiteralPath $configPath -Destination "$configPath.bak" -Force
Set-Content -LiteralPath $configPath -Value $output -Encoding UTF8

docker compose -f "$PSScriptRoot\docker-compose.slskd.yml" restart slskd

Write-Host "Updated Soulseek credentials and restarted slskd."
Write-Host "Check status with:"
Write-Host '$env:SLSKD_URL="http://localhost:5030"; $env:SLSKD_API_KEY="<your-slskd-api-key>"; py soulseek_cli.py status'
