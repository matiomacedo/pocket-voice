---
description: Skip the response being spoken; the next one in line starts
disable-model-invocation: true
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/speak.py" --skip`

Reply with only the word: Skipped.
