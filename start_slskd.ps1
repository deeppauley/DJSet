if (-not $env:SLSKD_URL) {
    $env:SLSKD_URL = "http://localhost:5030"
}

if (-not $env:SLSKD_API_KEY) {
    $env:SLSKD_API_KEY = "change-me-to-a-long-local-api-key"
}

docker compose -f "$PSScriptRoot\docker-compose.slskd.yml" up -d

Write-Host "slskd web UI: http://localhost:5030"
Write-Host "Default web login: slskd / slskd"
Write-Host "CLI env set for this PowerShell session."
