# Functional Demo Checklist

Use this checklist to verify the repository is in a runnable demo state for developers.

## 1) Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

## 2) Test Validation

```bash
python -m pytest -q
```

Expected: all tests pass.

## 3) Claude Middleware Demo Run

Run:

```bash
python examples/claude_agent_sdk_middleware.py
```

Expected output shape:

- line 1: `Model response to: ...`
- line 2: stats dictionary with keys such as:
  - `total_memories`
  - `avg_confidence`
  - `retrieval_count_total`

## 3.1) Slack-Style Runtime Integration Example

Run:

```bash
python examples/slack_assistant_agentdb_runtime.py
```

Expected output shape:

- one line: `Claude reply: ...`

This example demonstrates a reproducible event-handler pattern for Slack-like threads:

- initialize `AgentDB`
- build `AgentDBClaudeRuntime`
- call `handle_turn(...)` once per incoming message

## 4) What This Demo Proves

- pre-response governed retrieval is enforced
- post-response memory writes run through AgentDB permission routing
- outcome recording updates memory quality loop
- optional directory tracking policy configured at `AgentDB(...)` is active

## 5) Demo Caveats

- model call in this demo is a local stub (`fake_claude_call`), not a live provider invocation
- for real Claude SDK usage, replace the callable with your runtime client call while keeping middleware hooks
