param([int]$Port = 5051)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root
$skills = Join-Path $root "bundles/codex"
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.12 -B -m apps.workbench --workspace $root --host codex --skills $skills serve --port $Port
} else {
    & python -B -m apps.workbench --workspace $root --host codex --skills $skills serve --port $Port
}
