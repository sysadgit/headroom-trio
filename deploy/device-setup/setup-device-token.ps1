<#
.SYNOPSIS
  Headroom device-token setup for Windows (PowerShell 5.1+).

.EXAMPLE
  .\setup-device-token.ps1 -Url https://headroom.example.com            # prompts for the token
  .\setup-device-token.ps1 -Url https://... -Token hrt_xxx
  .\setup-device-token.ps1 -Uninstall

.NOTES
  1. Verifies the token against <Url>/headroom/whoami. If that fails NOTHING is changed.
  2. APPENDS the token header to the user-level ANTHROPIC_CUSTOM_HEADERS variable.
     Existing headers (e.g. X-Headroom-Project) are preserved, never replaced.
  Never touches ANTHROPIC_BASE_URL, Claude settings files, or any project.
#>
param(
  [string]$Url = "",
  [string]$Token = $env:HEADROOM_DEVICE_TOKEN,
  [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$HeaderName = "x-headroom-device-token"
$VarName = "ANTHROPIC_CUSTOM_HEADERS"

function Get-UserVar { [Environment]::GetEnvironmentVariable($VarName, "User") }
function Set-UserVar($value) {
  [Environment]::SetEnvironmentVariable($VarName, $value, "User")
  Set-Item -Path "Env:$VarName" -Value $value -ErrorAction SilentlyContinue
}
function Remove-OurHeader([string]$current) {
  if (-not $current) { return "" }
  $kept = $current -split "`r?`n" | Where-Object { $_ -and ($_ -notmatch "^\s*$HeaderName\s*:") }
  return ($kept -join "`n")
}

if ($Uninstall) {
  $current = Get-UserVar
  if (-not $current -or $current -notmatch $HeaderName) { Write-Host "No Headroom token header found."; exit 0 }
  Write-Host "Previous value saved to: $env:USERPROFILE\.headroom-custom-headers.backup"
  Set-Content -Path "$env:USERPROFILE\.headroom-custom-headers.backup" -Value $current
  Set-UserVar (Remove-OurHeader $current)
  Write-Host "Removed. Open a NEW terminal for it to take effect."
  exit 0
}

if (-not $Url) { Write-Error "Missing -Url (the Headroom server, e.g. https://headroom.example.com)"; exit 2 }
$Url = $Url.TrimEnd("/")
if ($Url -notmatch "^https://" -and $Url -notmatch "^http://(localhost|127\.0\.0\.1)") {
  if ($Url -match "^http://") { Write-Warning "$Url is plain HTTP. Your token travels unencrypted. Ask your admin for the https:// address." }
  else { Write-Error "-Url must start with https://"; exit 2 }
}

if (-not $Token) {
  $secure = Read-Host "Paste your device token (input hidden)" -AsSecureString
  $Token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}
$Token = ($Token -replace "\s", "")
if ($Token -notmatch "^hrt_") { Write-Error "That doesn't look like a Headroom device token (should start with hrt_)."; exit 2 }

Write-Host "Checking token with $Url/headroom/whoami ..."
try {
  $resp = Invoke-RestMethod -Uri "$Url/headroom/whoami" -Headers @{ $HeaderName = $Token } -TimeoutSec 15
} catch {
  Write-Host "FAIL: could not reach $Url/headroom/whoami : $($_.Exception.Message)" -ForegroundColor Red
  Write-Host "Nothing was changed."; exit 1
}
if (-not $resp.valid) {
  Write-Host "FAIL: the server does not recognise this token (revoked, expired or mistyped)." -ForegroundColor Red
  Write-Host "Nothing was changed."; exit 1
}
Write-Host "OK: token accepted for device '$($resp.device)'."

$current = Get-UserVar
if ($current) { Set-Content -Path "$env:USERPROFILE\.headroom-custom-headers.backup" -Value $current }
$base = Remove-OurHeader $current            # idempotent: replace any previous token line
$line = "${HeaderName}: $Token"
$new = if ($base) { "$base`n$line" } else { $line }
Set-UserVar $new
Write-Host "Done. Open a NEW terminal (or restart VS Code) and start Claude Code."
if ($current) { Write-Host "Your previous value was backed up to $env:USERPROFILE\.headroom-custom-headers.backup" }
