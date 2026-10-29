---
description: Turn spoken responses off or on
disable-model-invocation: true
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/speak.py" --toggle`

Reply with only the line printed above.
