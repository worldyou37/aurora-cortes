$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path .venv\Scripts\python.exe)) {
    Write-Host 'Execute Instalar-Windows.ps1 primeiro.' -ForegroundColor Yellow
    Read-Host 'Pressione Enter para sair'
    exit 1
}
function Find-Ollama {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $fallback = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path $fallback) { return $fallback }
    return $null
}
$ollamaExe = Find-Ollama
if (-not $ollamaExe) { throw 'Ollama não está instalado. Execute Instalar-Windows.ps1 primeiro.' }
try { $tags = Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 }
catch {
    Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden
    $tags = $null
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 2
        try { $tags = Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2; break } catch {}
    }
}
if (-not $tags) { throw 'O Ollama não iniciou. Execute novamente Instalar-Windows.ps1 ou reinicie o computador.' }
if (-not ($tags.models | Where-Object { $_.name -like 'qwen3:8b*' })) {
    Write-Host 'Preparando a IA pela primeira vez...' -ForegroundColor Yellow
    & $ollamaExe pull qwen3:8b
    if ($LASTEXITCODE -ne 0) { throw 'Não foi possível baixar a IA. Confira a conexão e o espaço livre e execute o instalador novamente.' }
}
Start-Process 'http://127.0.0.1:8774'
& .\.venv\Scripts\python.exe web.py
