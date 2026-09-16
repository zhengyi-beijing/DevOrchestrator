$ErrorActionPreference = 'Stop'
Set-Location 'C:\work\github\DevOrchestrator-dev'
$prompt = 'Return exactly OPUS_COPILOT_OK and nothing else.'
& npx --yes '@github/copilot@1.0.85' -p $prompt --model claude-opus-5 -s
exit $LASTEXITCODE
