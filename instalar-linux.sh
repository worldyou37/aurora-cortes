#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

fail() { echo "Instalação não concluída: $1" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || fail 'instale Python 3.10 ou superior e tente novamente.'
command -v curl >/dev/null 2>&1 || fail 'o curl é necessário para configurar a IA automaticamente.'
python3 - <<'PY' || fail 'é necessário Python 3.10 ou superior.'
import sys
if sys.version_info < (3, 10): raise SystemExit(1)
PY

printf '\nPreparando o ambiente da AURORA...\n'
python3 -m venv .venv || fail 'não foi possível criar o ambiente Python.'
.venv/bin/python -m pip install --upgrade pip || fail 'não foi possível preparar o instalador Python.'
.venv/bin/python -m pip install -r requirements.txt || fail 'não foi possível instalar os componentes. Confira a conexão e tente novamente.'
[[ -f config.json ]] || cp config.example.json config.json
mkdir -p incoming output state

if ! command -v ollama >/dev/null 2>&1; then
  command -v curl >/dev/null 2>&1 || fail 'o curl é necessário para instalar o Ollama automaticamente.'
  printf '\nInstalando Ollama pelo instalador oficial...\n'
  curl -fsSL https://ollama.com/install.sh | sh || fail 'a instalação do Ollama não terminou. Se o sistema pediu sua senha, execute novamente e autorize a instalação.'
fi
command -v ollama >/dev/null 2>&1 || fail 'não encontrei o Ollama após a instalação.'

if ! curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  nohup ollama serve > state/ollama.log 2>&1 < /dev/null &
  ready=0
  for _ in $(seq 1 30); do
    sleep 2
    if curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then ready=1; break; fi
  done
  [[ "$ready" == 1 ]] || fail 'O Ollama foi instalado, mas não iniciou. Reinicie o computador e execute este instalador novamente.'
fi

printf '\nBaixando o modelo local da IA. É um download grande; mantenha esta janela aberta.\n'
ollama pull qwen3:8b || fail 'não foi possível baixar o modelo. Confira o espaço livre e a conexão e execute o instalador novamente.'
printf '\nInstalação concluída. A IA e o modelo já estão prontos.\nExecute ./iniciar-linux.sh para abrir a AURORA.\n'
