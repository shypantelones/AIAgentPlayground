# One-off model download. Uses a throwaway container with internet access (the sandbox itself stays offline).
# Usage: .\scripts\pull-model.ps1 [model]     default: qwen3:14b
param([string]$Model = 'qwen3:14b')
Set-Location (Split-Path $PSScriptRoot)
docker volume create openclaw-sandbox_ollama-models | Out-Null
docker run --rm -v openclaw-sandbox_ollama-models:/root/.ollama --entrypoint sh ollama/ollama:latest -c "ollama serve >/dev/null 2>&1 & sleep 6; ollama pull $Model; ollama list"
