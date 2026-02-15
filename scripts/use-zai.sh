#!/usr/bin/env bash

unset ANTHROPIC_API_KEY

# Z.ai configuration
export ANTHROPIC_BASE_URL="https://api.z.ai/api/anthropic"

# Load API key from separate file if it exists (not tracked in git)
if [[ -f ~/.claude-switch/zai-key ]]; then
    export ANTHROPIC_AUTH_TOKEN=$(cat ~/.claude-switch/zai-key)
else
    echo "⚠️  Z.ai key not found. Create ~/.claude-switch/zai-key with your API key."
    return 1
fi

echo "✔ Switched to Z.ai (GLM)"
echo "  Base URL: https://api.z.ai/api/anthropic"
