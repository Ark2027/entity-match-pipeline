# Runs the quarterly match against config/settings.json.
#
# The password is supplied through an environment variable whose name must match
# the "password_env_var" value in settings.json. Pass -PasswordEnvVar if you
# changed it there; the default below matches the example config.
#
# If that variable is not already set, the script falls back to a DPAPI-encrypted
# file written by save_db_password.ps1. That file is bound to the current Windows
# user account and is useless if copied elsewhere.

param(
    [string]$PasswordEnvVar = 'MATCH_PIPELINE_DB_PASSWORD'
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = 'python'
$secretPath = Join-Path $projectRoot 'config\db_password.secure.txt'

if (-not (Get-Item -Path "Env:$PasswordEnvVar" -ErrorAction SilentlyContinue)) {
    if (-not (Test-Path $secretPath)) {
        throw "Set `$env:$PasswordEnvVar, or run scripts\save_db_password.ps1 to create $secretPath."
    }
    $securePassword = Get-Content $secretPath | ConvertTo-SecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    try {
        Set-Item -Path "Env:$PasswordEnvVar" -Value ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr))
    }
    finally {
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }
}

$pythonBootstrap = @"
import sys
from pathlib import Path

project_root = Path(r"$projectRoot")
sys.path.insert(0, str(project_root / "src"))

from entity_match_pipeline.pipeline import main

raise SystemExit(
    main(
        [
            "--config",
            str(project_root / "config" / "settings.json"),
            *sys.argv[1:],
        ]
    )
)
"@

$pythonBootstrap | & $python - @args
