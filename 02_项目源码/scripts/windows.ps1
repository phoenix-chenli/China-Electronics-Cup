param(
    [ValidateSet('prepare', 'start', 'stop', 'check')][string]$Action = 'start',
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$ExtraArgs
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

# Only this process receives PATH additions; system policy/PATH remain unchanged.
foreach ($ocr in @("$env:ProgramFiles\Tesseract-OCR", "$env:LOCALAPPDATA\Programs\Tesseract-OCR")) {
    if (Test-Path -LiteralPath (Join-Path $ocr 'tesseract.exe')) { $env:PATH = "$ocr;$env:PATH"; break }
}
$python = Join-Path $root '.venv\Scripts\python.exe'
$prefix = @()
if (!(Test-Path -LiteralPath $python)) {
    $python = $null
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($version in @('-3.13', '-3.12')) {
            $probeCode = 1
            try { & py $version -c 'import sys;sys.exit(not (sys.version_info >= (3,12) and sys.maxsize > 2**32))' 2>$null; $probeCode = $LASTEXITCODE } catch { $probeCode = 1 }
            if ($probeCode -eq 0) { $python = 'py'; $prefix = @($version); break }
        }
    }
    if (!$python -and (Get-Command python -ErrorAction SilentlyContinue)) {
        $probeCode = 1
        try { & python -c 'import sys;sys.exit(not (sys.version_info >= (3,12) and sys.maxsize > 2**32))' 2>$null; $probeCode = $LASTEXITCODE } catch { $probeCode = 1 }
        if ($probeCode -eq 0) { $python = 'python' }
    }
    if (!$python) { throw 'Install Python 3.13 x64 (or 3.12 x64) from python.org, then run this entry again.' }
}
$launchArgs = @((Join-Path $root 'start.py'))
if ($Action -eq 'prepare') { $launchArgs += '--prepare-only' }
if ($Action -eq 'stop') { $launchArgs += '--stop' }
if ($Action -eq 'check') { $launchArgs += '--check' }
& $python @prefix @launchArgs @ExtraArgs
$code = $LASTEXITCODE
if ($code -eq 0 -and $Action -eq 'prepare') {
    $python = Join-Path $root '.venv\Scripts\python.exe'
    & $python (Join-Path $root 'start.py') --check
    $code = $LASTEXITCODE
}
exit $code
