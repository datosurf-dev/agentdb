# Integration Manuals

This directory contains implementation-focused guides for wiring AgentDB into specific runtimes.

## Available Guides

- Google ADK: `docs/integrations/google-adk.md`
- Model/runtime wiring (local LLM and Claude): `docs/integrations/model-and-runtime-wiring.md`
- Functional demo checklist: `docs/integrations/functional-demo.md`

The model/runtime wiring guide includes:

- Claude Agent SDK middleware adapter pattern
- simplified `AgentDBClaudeRuntime` integration for app handlers
- downstream Claude SDK hook-registration example in `examples/claude_agent_sdk_hook_registration.py`
- two operating profiles: `full_storage` and `budget_optimized`
- optional custom directory tracking tied to session/message IDs
- skills drift management via `db.skills_api`
- reproducible Slack-style runtime handler example in `examples/slack_assistant_agentdb_runtime.py`

## Recommended Reading Order

1. Start with `README.md` quickstart for environment setup.
2. Pick one runtime integration guide from this folder.
3. Apply your permissions configuration before first write.
4. Run `python -m pytest -q` in `.venv` to validate local setup.
