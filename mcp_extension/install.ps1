# Installs the renderdoc_mcp extension into the qrenderdoc extensions folder.
# Usage: powershell -ExecutionPolicy Bypass -File install.ps1
$src = Join-Path $PSScriptRoot "renderdoc_mcp"
$dst = Join-Path $env:APPDATA "qrenderdoc\extensions\renderdoc_mcp"

if (-not (Test-Path $src)) {
    Write-Error "Source folder not found: $src"
    exit 1
}

New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
Copy-Item -Recurse -Force $src $dst
Write-Host "Installed extension to: $dst"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. (Re)start qrenderdoc"
Write-Host "  2. Tools menu -> Manage Extensions -> tick 'Enabled' for 'RenderDoc MCP Server'"
Write-Host "  3. Tools menu -> RenderDoc MCP Server  (opens the server window and starts it)"
Write-Host "  4. Point your AI agent at:  http://127.0.0.1:30007/mcp"
