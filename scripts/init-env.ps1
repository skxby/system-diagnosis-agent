$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $projectDir '.env'
if (-not (Test-Path -LiteralPath $envPath)) {
    Copy-Item -LiteralPath (Join-Path $projectDir '.env.example') -Destination $envPath
}
$content = [IO.File]::ReadAllText($envPath)
if ($content -notmatch '(?m)^SECRET_KEY=.+$' -or $content -match '(?m)^SECRET_KEY=(change-me|changethis|)\s*$') {
    $randomBytes = New-Object byte[] 48
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($randomBytes) } finally { $generator.Dispose() }
    $secret = [Convert]::ToBase64String($randomBytes)
    if ($content -match '(?m)^SECRET_KEY=.*$') {
        $content = [regex]::Replace($content, '(?m)^SECRET_KEY=.*$', "SECRET_KEY=$secret")
    } else { $content += "`nSECRET_KEY=$secret`n" }
    [IO.File]::WriteAllText($envPath, $content, (New-Object Text.UTF8Encoding $false))
    Write-Output 'Generated a private session key. Existing login sessions must sign in again.'
}
