# Integration Manuals

This directory contains implementation-focused guides for wiring AgentDB into specific runtimes.

## Available Guides

- Model/runtime wiring (local LLM and Claude): `docs/integrations/model-and-runtime-wiring.md` — the only guide describing something you can run today (Claude SDK middleware, section A.1)
- Functional demo checklist: `docs/integrations/functional-demo.md`
- Google ADK: `docs/integrations/google-adk.md` — **planned, not yet implemented**; describes the target design for an adapter that doesn't exist yet
- OpenClaw: `docs/integrations/openclaw.md` — **planned, not yet implemented**; design brief for hooking any OpenClaw agent (data-helper first) into AgentDB, open questions flagged for whoever builds it
- OpenClaw & Hermes assessment: `docs/integrations/openclaw-hermes-assessment.md` — codebase comparison covering memory architecture, unique capabilities, overlapping user-configurable features, and recommended adapter build order (Hermes first)

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
