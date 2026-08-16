# Stores the database password in a DPAPI-encrypted file for run_quarterly.ps1.
#
# The encryption is tied to the current Windows user account on this machine, so
# the file is useless if copied anywhere else. It is gitignored regardless.
#
# There is deliberately no -Password parameter. Passing a password as an argument
# puts it in PowerShell history and in any transcript logging that is enabled,
# which defeats the purpose of encrypting it afterwards.

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$configDir = Join-Path $projectRoot 'config'
$secretPath = Join-Path $configDir 'db_password.secure.txt'

if (-not (Test-Path $configDir)) {
    New-Item -ItemType Directory -Path $configDir | Out-Null
}

$secure = Read-Host -Prompt 'Enter the database password' -AsSecureString
if ($secure.Length -eq 0) {
    throw 'No password entered; nothing was written.'
}

$secure | ConvertFrom-SecureString | Set-Content -Path $secretPath -Encoding ASCII
Write-Host "Encrypted password written to $secretPath"
Write-Host "It can only be decrypted by $env:USERNAME on $env:COMPUTERNAME."
