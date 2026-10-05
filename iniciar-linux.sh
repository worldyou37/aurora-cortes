#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo 'Execute ./instalar-linux.sh primeiro.' >&2
  exit 1
fi
command -v ollama >/dev/null 2>&1 || { echo 'Ollama não está instalado. Execute o instalador da AURORA primeiro.' >&2; exit 1; }
if ! curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  nohup ollama serve > state/ollama.log 2>&1 < /dev/null &
  ready=0
  for _ in $(seq 1 30); do
    sleep 1
    if curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then ready=1; break; fi
  done
  [[ "$ready" == 1 ]] || { echo 'O Ollama não iniciou. Execute novamente o instalador da AURORA.' >&2; exit 1; }
fi
if ! ollama list | awk 'NR>1 && $1 ~ /^qwen3:8b/ { found=1 } END { exit !found }'; then
  echo 'Preparando a IA pela primeira vez...'
  ollama pull qwen3:8b || { echo 'Não foi possível baixar a IA. Confira a conexão e o espaço livre e execute o instalador novamente.' >&2; exit 1; }
fi
if command -v xdg-open >/dev/null 2>&1; then xdg-open http://127.0.0.1:8774 >/dev/null 2>&1 & fi
exec .venv/bin/python web.py
