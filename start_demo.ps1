$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'

# Поиск доступного интерпретатора. Требуется Python 3.12 или новее.
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $bundledPython) {
    & $bundledPython -m giveaway demo
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m giveaway demo
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -m giveaway demo
} else {
    Write-Host 'Install Python 3.12 or newer and run: python -m giveaway demo'
    exit 1
}
exit $LASTEXITCODE
