[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root 'runtime'
$launcher = Join-Path $PSScriptRoot 'devorch.cmd'

# The Control API is daemon-owned, so stopping it deliberately stops the unified daemon.
$result = & $launcher stop-daemon --runtime-root $runtime
if ($LASTEXITCODE -ne 0) { throw 'DevOrchestrator daemon failed to stop.' }
$result

