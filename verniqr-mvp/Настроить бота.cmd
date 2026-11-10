@echo off
setlocal DisableDelayedExpansion
set "VERNIQR_SETUP_ROOT=%~dp0"
set "VERNIQR_SETUP_NO_EDITOR="
if /I "%~1"=="--no-editor" set "VERNIQR_SETUP_NO_EDITOR=1"
if not exist "%~dp0.env" if not exist "%~dp0.env.example" goto missing_files
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $target=Join-Path $env:VERNIQR_SETUP_ROOT '.env'; if (-not [IO.File]::Exists($target)) { $template=[IO.File]::ReadAllText((Join-Path $env:VERNIQR_SETUP_ROOT '.env.example'),[Text.Encoding]::UTF8); $bytes=New-Object byte[] 32; $rng=[Security.Cryptography.RandomNumberGenerator]::Create(); try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }; $secret=[BitConverter]::ToString($bytes).Replace('-','').ToLowerInvariant(); [IO.File]::WriteAllText($target,$template.Replace('development-only-change-before-publishing',$secret),[Text.UTF8Encoding]::new($false)) }; Write-Host 'Settings file is ready: .env'; if ($env:VERNIQR_SETUP_NO_EDITOR -ne '1') { Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\notepad.exe') -ArgumentList ('{0}{1}{0}' -f [char]34,$target) -WindowStyle Normal } } catch { Write-Host 'Could not prepare settings:' $_.Exception.Message; exit 1 }"
if errorlevel 1 goto failed
exit /b 0
:missing_files
echo Extract the entire ZIP to a folder before running this file.
echo This file must be next to .env.example and the verniqr folder.
:failed
if not defined VERNIQR_SETUP_NO_EDITOR pause
exit /b 1
