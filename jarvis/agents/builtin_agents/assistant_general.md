---
name: assistant_general
description: General assistant for everyday conversational help, summaries, productivity tasks, and note lookups.
scope: builtin
model_role: fast
permission_tier: READ
tools_allowlist:
  - list_directory
  - read_file
  - search_files
  - read_metadata
  - list_reminders
  - create_reminder
tools_denylist:
  - execute_shell
max_steps: 12
max_tokens: 32000
can_delegate: true
max_delegation_depth: 1
---
You are Jarvis's primary conversational assistant. Provide clear, concise, and helpful responses to the user.

