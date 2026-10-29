---
description: Change this session's voice
argument-hint: <voice>
disable-model-invocation: true
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/speak.py" --voice "$ARGUMENTS"`

Reply with only the line printed above.
