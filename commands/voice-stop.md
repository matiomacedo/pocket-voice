---
description: Stop speaking the current response
disable-model-invocation: true
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/speak.py" --stop`

Speech was stopped. Reply with only the word: Stopped.
