#!/usr/bin/env bash

unset ANTHROPIC_BASE_URL
unset ANTHROPIC_AUTH_TOKEN

# Load API key from separate file if it exists (not tracked in git)
if [[ -f ~/.claude-switch/anthropic-key ]]; then
    export ANTHROPIC_API_KEY=$(cat ~/.claude-switch/anthropic-key)
else
    echo "⚠️  Anthropic key not found. Create ~/.claude-switch/anthropic-key with your API key."
    return 1
fi

echo "✔ Switched to Anthropic (Claude)"
echo "  Base URL: default"
