#!/usr/bin/env bash

unset ANTHROPIC_API_KEY

# Replace with your real Z.ai key
export ANTHROPIC_BASE_URL="https://api.z.ai/api/anthropic"
export ANTHROPIC_AUTH_TOKEN="sk-zai-REPLACE_ME"

echo "✔ Switched to Z.ai (GLM)"
echo "  Base URL: https://api.z.ai/api/anthropic"
