$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Refresh-Path {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $env:Path = "$machinePath;$userPath"
}

function Find-Ollama {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $fallback = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path $fallback) { return $fallback }
    return $null
}

try {
    $pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pythonLauncher) {
        throw 'Python 3.10 ou superior não foi encontrado. Instale-o pelo site python.org e execute este instalador novamente.'
    }

    $pythonArgs = @('-3.10')
    & $pythonLauncher.Source @pythonArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        $pythonArgs = @('-3')
        & $pythonLauncher.Source @pythonArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
    }
    if ($LASTEXITCODE -ne 0) {
        throw 'Instale Python 3.10 ou superior pelo site python.org e execute este instalador novamente.'
    }

    Write-Host 'Preparando o ambiente da AURORA...' -ForegroundColor Yellow
    & $pythonLauncher.Source @pythonArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Não foi possível preparar o ambiente Python.' }
    & .\.venv\Scripts\python.exe -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'Não foi possível atualizar os componentes da instalação.' }
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Não foi possível instalar os componentes da AURORA. Confira sua conexão e tente novamente.' }

    if (-not (Test-Path config.json)) { Copy-Item config.example.json config.json }
    New-Item -ItemType Directory -Force incoming, output, state | Out-Null

    $ollamaExe = Find-Ollama
    if (-not $ollamaExe) {
        Write-Host 'Baixando o instalador oficial do Ollama...' -ForegroundColor Yellow
        $installer = Join-Path $env:TEMP 'OllamaSetup.exe'
        Invoke-WebRequest 'https://ollama.com/download/OllamaSetup.exe' -OutFile $installer
        Write-Host 'Conclua a instalação do Ollama na janela que apareceu. A AURORA continuará automaticamente depois.' -ForegroundColor Yellow
        Start-Process -FilePath $installer -Wait
        Refresh-Path
        $ollamaExe = Find-Ollama
    }
    if (-not $ollamaExe) { throw 'Não encontrei o Ollama após a instalação. Termine a instalação e execute este instalador novamente.' }

    $serverReady = $false
    try { Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 | Out-Null; $serverReady = $true } catch {}
    if (-not $serverReady) {
        Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 2
            try { Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null; $serverReady = $true; break } catch {}
        }
    }
    if (-not $serverReady) { throw 'O Ollama foi instalado, mas não iniciou. Reinicie o computador e execute este instalador novamente.' }

    Write-Host 'Baixando o modelo local da IA. É um download grande; mantenha esta janela aberta.' -ForegroundColor Yellow
    & $ollamaExe pull qwen3:8b
    if ($LASTEXITCODE -ne 0) { throw 'Não foi possível baixar o modelo de IA. Confira o espaço livre e a conexão e execute o instalador novamente.' }

    Write-Host ''
    Write-Host 'Instalação concluída. A IA e o modelo já estão prontos.' -ForegroundColor Green
    Write-Host 'Para abrir a AURORA, execute Iniciar-Windows.ps1.' -ForegroundColor Green
} catch {
    Write-Host ''
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host 'Corrija o problema indicado e execute este instalador novamente.' -ForegroundColor Yellow
    exit 1
} finally {
    Read-Host 'Pressione Enter para fechar'
}
