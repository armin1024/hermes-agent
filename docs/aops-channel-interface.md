# AOPS Channel Interface

本文档是 AOPS channel 的接口与运维功能总入口。后续只要 AOPS channel、Tec01 一键安装、profile、多 agent、静默命令、模型/工具集配置或 runtime 上报发生变化，必须同步更新本文档。

## 功能总览

- AOPS gateway 使用 `AOPS_BOT_TOKEN` 鉴权，`AOPS_BOT_URL` 作为上游地址；`tec-client-ip` 请求头为同一系统用户共享的持久 UUID。
- Gateway 连接后会上报 agent 列表和 runtime 信息，包括宿主机 IPv4 集合、系统用户、Hermes profile、模型、模型网关和模型授权码。
- 支持普通对话、native streaming reply、审批卡片、附件下载、多模态消息、cron 投递和本地 slash 命令。
- 支持 silent 消息链路，AOPS 上游可通过静默 slash 命令读取或修改模型、技能、工具集、cron、安全策略等状态。
- 支持 Tec01 curl 执行式一键安装：新用户首个 agent 使用 default agent，后续同一系统用户多 agent 使用 named profile。

## Runtime Agent Report

Hermes 在连接 AOPS 后调用：

```http
POST /api/v1/bot/agents/report
Authorization: Bearer <AOPS_BOT_TOKEN>
tec-client-ip: <per-system-user-uuid>
Content-Type: application/json
```

请求体示例：

```json
{
  "botId": "bot_xxx",
  "reportedAt": "2026-06-12T10:00:00Z",
  "source": "hermes",
  "defaultAgentId": "main",
  "agents": [
    {
      "id": "main",
      "enabled": true,
      "default": true,
      "workspace": "~/.hermes"
    }
  ],
  "runtime": {
    "schema": "aops-runtime-report.v1",
    "host": {
      "hostname": "T1379",
      "ips": ["10.10.0.18", "192.168.10.23"]
    },
    "user": {
      "systemUser": "hermes"
    },
    "hermes": {
      "home": "/home/hermes/.hermes",
      "profile": "default"
    },
    "model": {
      "provider": "custom",
      "model": "qwen3-32b",
      "default": "qwen3-32b",
      "baseUrl": "http://model-gateway.internal/v1",
      "apiMode": "openai",
      "apiKeyEnv": "MODEL_GATEWAY_API_KEY",
      "apiKey": "sk-plain-model-gateway-key"
    }
  }
}
```

字段规则：

- `runtime.host.ips` 只包含非 localhost 的 IPv4 地址；过滤 `127.0.0.1`、loopback、IPv6 和重复 IP。
- `runtime.model.apiKey` 是 HTTP payload 明文，用于 AOPS 上游后续创建多 agent；Hermes 本地日志会显示为 `[REDACTED]`，不要用日志判断上游收到的值。
- `runtime.model.apiKey` 解析优先级为 `config.yaml model.api_key/apiKey`、`model.api_key_env/apiKeyEnv` 指向的环境变量、provider runtime credentials。
- agent report 日志只用于排障，日志 payload 会脱敏 `runtime.model.apiKey`。

## WebSocket 消息协议

AOPS WebSocket 地址由 `AOPS_BOT_URL` 转换为 `/api/v1/ws`，例如 `https://aops.example.com` -> `wss://aops.example.com/api/v1/ws`。

入站支持：

```json
{
  "event": "message_posted",
  "data": {
    "id": "msg-001",
    "text": "hello",
    "channelId": "conv_xxx",
    "agentKey": "main",
    "messageType": "common",
    "metadata": {}
  }
}
```

出站统一使用：

```json
{
  "event": "message_reply",
  "data": {
    "messageId": "botmsg-001",
    "seq": 1,
    "phase": "start",
    "kind": "final",
    "channelId": "conv_xxx",
    "replyToId": "msg-001",
    "messageType": "common",
    "conversationEnded": false,
    "ts": 1760000000000
  }
}
```

`messageType` 规则：

- `common`：普通对话和普通 slash 命令。
- `silent`：入站 `silent=true`、`metadata.silent=true` 或 `messageType=silent`；包含 `messageType=common` 但 `metadata.silent=true` 的消息。
- `cron`：cron/后台投递。

静默回包保持 `messageType=silent`、`silent=true`、`replyToId=<原消息 id>`。同一 websocket 收到的 silent/common-silent 命令按接收顺序处理，避免 `/help`、`/model status` 回包乱序。

## Silent Slash Commands

AOPS 上游统一通过 `send_message` 静默消息调用本地命令：

```json
{
  "action": "send_message",
  "requestId": "cmd-xxx",
  "data": {
    "role": "user",
    "content": "/model status",
    "contentType": "text",
    "model": "hermes",
    "metadata": {
      "silent": true,
      "id": "123456",
      "botId": "bot_xxx",
      "agentId": "main"
    }
  }
}
```

通用响应形态为 JSON 字符串，放在 `message_reply.data.text`：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "model.status",
  "ok": true,
  "command": "/model status",
  "itemType": "model",
  "items": [],
  "summary": {},
  "error": null
}
```

支持命令：

- `/help`：返回 `local-command-tree.v2` 命令树，`usage` 用于展示，`completions[].required` 表示必填参数，`completions[].choices` 表示枚举候选。
- `/model list`：从当前模型网关 `/models` 读取可用模型。
- `/model status`、`/model current`：返回当前生效模型，不请求模型网关。
- `/model use <provider> <model>`：切换当前 AOPS channel + agent 的模型偏好，并同步更新当前 profile 的 `config.yaml`。
- `/toolsets list`：返回当前 profile 的 AOPS 工具集开关状态。
- `/toolsets enable <name>`、`/toolsets disable <name>`、`/toolsets set <name> <true|false>`：修改当前 profile 的 `config.yaml platform_toolsets.aops`，立即影响后续任务。
- `/skills`、`/skills list`：返回已安装技能和命令缓存。
- `/cron`、`/cron list`：返回计划任务。
- `/cron remove <id|name>`：按任务 ID 或唯一任务名删除计划任务。
- `/cron history <id> [tsMs]`、`/cron history before <id> [tsMs]`、`/cron history after <id> <tsMs>`：分页读取 cron 历史，最新记录在前。
- `/security`：返回当前审批策略。
- `/security set <off|manual|smart>`：修改审批策略；`off` 会关闭破坏性 slash 二次确认。
- `/reasoning`、`/reasoning status`、`/reasoning set ...`：读取或修改 reasoning 配置。
- `/bash clawhub explore --json`、`/bash clawhub install <slug>`、`/bash clawhub uninstall <slug>`：静默 SkillHub/ClawHub 对接命令。

## 模型接口

`/model list` 返回示例：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "model.list",
  "ok": true,
  "command": "/model list",
  "itemType": "model",
  "items": [
    {
      "id": "qwen-coder",
      "model": "qwen-coder",
      "displayName": "qwen-coder",
      "current": true,
      "command": "/model use tec01-gateway qwen-coder"
    }
  ],
  "context": {
    "currentModel": "qwen-coder",
    "currentProvider": "tec01-gateway",
    "baseUrl": "http://model-gateway.internal/v1",
    "apiKeyConfigured": true,
    "apiMode": "chat_completions",
    "probedUrl": "http://model-gateway.internal/v1/models",
    "elapsedMs": 120
  },
  "error": null
}
```

`/model status` 返回示例：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "model.status",
  "ok": true,
  "command": "/model status",
  "model": "qwen35-122b",
  "modelId": "qwen35-122b",
  "provider": "custom",
  "providerLabel": "custom",
  "baseUrl": "http://model-gateway.internal/v1",
  "apiMode": "chat_completions",
  "apiKeyConfigured": true,
  "apiKeyPreview": "sk-1...abcd",
  "source": "config.model",
  "scope": "config",
  "commands": {
    "list": "/model list",
    "status": "/model status",
    "switch": "/model use custom <model>"
  },
  "error": null
}
```

模型密钥不会通过 `/model status` 或 `/model list` 返回明文，只返回 `apiKeyConfigured` 和 `apiKeyPreview`。

## Toolsets 接口

工具集配置按 Hermes profile 独立生效，写入当前 profile 的 `config.yaml`：

```yaml
platform_toolsets:
  aops:
    web: true
    terminal: false
```

`/toolsets list` 返回示例：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "toolsets.list",
  "ok": true,
  "command": "/toolsets list",
  "itemType": "toolset",
  "total": 3,
  "count": 3,
  "limit": null,
  "hasMore": false,
  "context": {
    "platform": "aops",
    "agentId": "main",
    "profileName": "main",
    "profileHome": "/home/user/.hermes/profiles/main",
    "configPath": "/home/user/.hermes/profiles/main/config.yaml"
  },
  "summary": {
    "enabled": 2,
    "disabled": 1,
    "configured": 2
  },
  "items": [
    {
      "name": "web",
      "label": "Web Search & Scraping",
      "description": "web_search, web_extract",
      "enabled": true,
      "configured": true,
      "tools": ["web_search", "web_extract"]
    }
  ],
  "error": null
}
```

`configured=false` 表示工具集缺少 API key 或运行依赖，前端应展示“需要配置”，但不阻止开关操作。开关修改立即写配置，后续新任务生效；已在运行中的 agent turn 不强制中断。

## 审批接口

当 Hermes 需要用户授权时，发送 `message_reply`：

```json
{
  "event": "message_reply",
  "data": {
    "messageId": "botmsg-xxx",
    "seq": 1,
    "phase": "actions",
    "kind": "approval",
    "channelId": "conv_xxx",
    "replyToId": "545200",
    "messageType": "common",
    "conversationEnded": true,
    "content": [
      {
        "type": "approval",
        "id": "exec-approval-001",
        "approvalKind": "exec",
        "allowedActions": [
          {"command": "/approve", "display": "仅本次允许"},
          {"command": "/approve always", "display": "始终允许"},
          {"command": "/deny", "display": "拒绝"}
        ],
        "expiresAtMs": 1760000300000,
        "message": "检测到需要审批的高风险操作。",
        "command": "python -c '...'",
        "description": "script execution via -e/-c flag"
      }
    ]
  }
}
```

前端可直接把 `allowedActions[].command` 作为用户消息回发。Hermes 兼容文本 `/approve`、`/approve session`、`/approve always`、`/deny`、`/always`、`/cancel`，也兼容 metadata/content 中的结构化 `approvalId/action`。

## 附件和日志

- 入站 `attachments` 由 Hermes 使用 AOPS 鉴权头下载，图片、音频、视频、文档进入统一缓存目录 `~/.hermes/cache/{images,audio,videos,documents}`。
- 静默 SkillHub 命令跳过附件处理，避免附件干扰命令执行。
- AOPS wire 日志位于 `~/.hermes/logs/aops/aops-YYYY-MM-DD.log`，默认保留 7 天，可通过 `platforms.aops.extra.log_retention_days` 或 `AOPS_LOG_RETENTION_DAYS` 覆盖。
- 日志会记录 Tec01/AOPS 上游交互摘要和 raw payload；敏感字段如 `runtime.model.apiKey` 在日志中脱敏。

## Curl One-Click Profile 安装

推荐入口：

```bash
curl -fsSL "http://tec01.internal/hermes/install-oneclick.sh" | sudo bash -s -- \
  --template-url "http://tec01.internal/hermes/aops-profile-template.yaml" \
  --set targetUser=hermes \
  --set env.AOPS_BOT_TOKEN=claw_xxx \
  --set env.AOPS_BOT_URL=http://aops-bot.internal \
  --set configYaml.model.model=qwen3-32b \
  --set configYaml.model.default=qwen3-32b \
  --set configYaml.model.base_url=http://model-gateway.internal/v1 \
  --set env.MODEL_GATEWAY_API_KEY=sk-model-key
```

参数规则：

- 必填：`--set targetUser=<user>`、`--set env.AOPS_BOT_TOKEN=<token>`。
- 可选：任意 `--set key=value` 覆盖模板；模板未出现的合法 `env.*` 和 `config.configYaml.*` 也支持。
- Markdown 字段 `soul`、`userMemory`、`config.soul`、`config.userMemory`、`config.userInstructions` 支持字面量 `\n` 转换为真实换行，例如 `--set soul="# Rules \n- 使用中文回复"`。
- `--skills-zip <path_or_url>` 支持本地或 URL zip，可重复；只接受 zip，安全解压到目标 profile `skills/`。
- 模板来源优先级为 `--template-file`、`--template-url`、脚本内置模板。

Profile 策略：

- 同一系统用户下可以有多个 AOPS agent，`AOPS_BOT_TOKEN` 是唯一身份键。
- 新用户首次安装且没有任何已有 AOPS token 时，首个 agent 写入 default profile，即 `~/.hermes`，gateway 使用裸 `hermes gateway ...`。
- 后续不同 token 创建 named profile，默认命名 `<targetUser>-N`，如 `hermes-1`；存量旧命名 profile 参与序号统计但不重命名。
- 相同 token 重新执行时更新原 default/profile；多个 profile 命中相同 token 时失败。
- 已安装 runtime 会比较 `~/hermes-agent/.aops_bundle_sha256` 与模板 `bundle.sha256`，不一致则下载新 bundle 并升级。
- default agent 发生 runtime 升级后，会重启其他已运行或已有 systemd user service 的 named profile gateway；不主动启动从未运行过的 profile。

模板默认能力：

- `.env`：写入 AOPS、ClawHub、模型网关等环境变量。
- `config.yaml`：支持完整 `config.configYaml` 深合并。
- Hindsight：写入 `hindsight/config.json`，默认 `bank_id_template=users-{user}`。
- Memory/Soul：写入 `memories/USER.md` 和 `SOUL.md`。
- 技能包：写入当前 profile `skills/`。

## 维护要求

- AOPS channel 行为、字段、命令、模板、profile 策略、打包策略发生变化时，必须同步更新本文档。
- 上游 UI 依赖的 JSON 字段不得静默改名；如需调整，先新增字段并保留旧字段兼容。
- 新增 silent command 时，应同时更新 `/help` 命令树、相关测试和本文档。
- 打内网包时，`install-oneclick.sh` 和对应离线包统一存放在 `dist/tec01-hermes-oneclick/` 目录，便于留档和清理。
