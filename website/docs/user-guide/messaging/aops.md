---
sidebar_position: 19
title: "AOPS"
description: "Connect Hermes to AOPS via the native WebSocket gateway"
---

# AOPS

AOPS uses a native WebSocket gateway instead of Hermes' usual "send then edit" streaming model. Hermes connects to AOPS, receives `message_posted`, and sends back `message_reply` events for streamed text, tool activity, approvals, and terminal errors.

## What Works

- Native streamed replies (`start` / `delta` / `end`)
- Tool progress events
- Dangerous-command approval payloads
- `agentKey` routing into per-route Hermes model/provider overrides
- DM auth policies: `open`, `allowlist`, `pairing`, `disabled`

## Current Limits

- No native media/file/image return path in AOPS v1
- `send_message` to AOPS requires a live `hermes gateway` process
- AOPS is text-first; don't assume rich markdown rendering

## Required Environment Variables

```bash
AOPS_BOT_TOKEN=your-bot-token
AOPS_BASE_URL=https://aops.example.com
AOPS_HOME_CHANNEL=user-001
```

## Optional Environment Variables

```bash
AOPS_PUSH_TOOL_CALLS=true
AOPS_DM_POLICY=open
AOPS_ALLOW_FROM=user-001,user-002
AOPS_TRUSTED_AGENT_KEY_FROM=*
AOPS_ALLOWED_USERS=user-001
AOPS_ALLOW_ALL_USERS=false
AOPS_PROXY=http://127.0.0.1:7890
```

## Recommended `config.yaml`

```yaml
platforms:
  aops:
    enabled: true
    token: ${AOPS_BOT_TOKEN}
    home_channel:
      platform: aops
      chat_id: user-001
      name: Home
    extra:
      base_url: https://aops.example.com
      push_tool_calls: true
      dm_policy: open
      allow_from: ["user-001"]
      trusted_agent_key_from: ["*"]
      agent_routes:
        main:
          default: true
          workspace: "~/.hermes"
        devops:
          model: "openrouter/anthropic/claude-sonnet-4"
          provider: "openrouter"
          prompt: "You are the DevOps-focused Hermes route."
          workspace: "~/.hermes/devops"
```

## `agentKey` Routing

If AOPS sends `message_posted.data.agentKey`, Hermes can map that key into a route in `platforms.aops.extra.agent_routes`.

- `model`
- `provider`
- `api_mode`
- `command`
- `args`
- `credential_pool`
- `prompt` (merged into the channel prompt for that turn)

`trusted_agent_key_from` controls who is allowed to activate those overrides.

## DM Authorization

- `open`: allow all direct messages
- `allowlist`: allow only users in `allow_from`
- `pairing`: require Hermes DM pairing unless already approved
- `disabled`: reject DMs without pairing fallback

Group chats are allowed by default.

## Setup

```bash
hermes gateway setup
hermes gateway
```

The setup wizard only asks for the required AOPS fields. Advanced routing and auth settings stay in `config.yaml`.
