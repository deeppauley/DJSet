$env:SLSKD_URL = "http://localhost:5030"

if (-not $env:SLSKD_API_KEY -and (Test-Path "$PSScriptRoot\.env")) {
    Get-Content "$PSScriptRoot\.env" | ForEach-Object {
        if ($_ -match '^\s*SLSKD_API_KEY\s*=\s*(.+)\s*$') {
            $env:SLSKD_API_KEY = $Matches[1].Trim('"')
        }
    }
}

py "$PSScriptRoot\web_app.py"
