# Integration Manuals

This directory contains runtime integration guides. Check the current source and README before treating a design example as an available adapter.

## Available Guides

- Model/runtime wiring (local LLM and Claude): `docs/integrations/model-and-runtime-wiring.md` — generic backend, middleware, and Claude runtime examples
- Claude Agent SDK gateway & budgeted retrieval guide: `docs/integrations/claude-agent-sdk-gateway-guide.md` — lessons from a real production integration of `memory_budget_tokens` and `db.gateway` (conflict queue, proposal workflow): permission gotchas, authorization-tier recommendations, and open friction points
- Functional demo checklist: `docs/integrations/functional-demo.md`
- Google ADK: `docs/integrations/google-adk.md` — **planned, not yet implemented**; describes the target design for an adapter that doesn't exist yet
- OpenClaw: `docs/integrations/openclaw.md` — **planned, not yet implemented**; design brief for hooking any OpenClaw agent (data-helper first) into AgentDB, open questions flagged for whoever builds it
- OpenClaw & Hermes assessment: `docs/integrations/openclaw-hermes-assessment.md` — codebase comparison covering memory architecture, unique capabilities, overlapping user-configurable features, and recommended adapter build order (Hermes first)
- Hermes: `docs/integrations/hermes.md` — `AgentDBHermesProvider` is implemented; the guide includes historical design material and should be checked against `src/agentdb/adapters/hermes.py`

Current runtime-facing code includes:

- Claude Agent SDK middleware adapter pattern
- simplified `AgentDBClaudeRuntime` integration for app handlers
- downstream Claude SDK hook-registration example in `examples/claude_agent_sdk_hook_registration.py`
- built-in profiles: `budget_optimized`, `full_storage`, and `research`
- optional custom directory tracking tied to session/message IDs
- skills drift management via `db.skills_api`
- reproducible Slack-style runtime handler example in `examples/slack_assistant_agentdb_runtime.py`

## Recommended Reading Order

1. Start with `README.md` quickstart for environment setup.
2. Pick one runtime integration guide from this folder.
3. Apply your permissions configuration before first write.
4. Run `python -m pytest -q` in `.venv` to validate local setup.
