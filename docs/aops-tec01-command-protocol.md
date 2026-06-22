# AOPS Tec01 命令与审批对接说明

> 当前 AOPS channel 的完整接口文档以 `docs/aops-channel-interface.md` 为准。本文保留为 Tec01 命令与审批专题说明；后续功能更新必须同步更新主接口文档。

本文说明 Tec01 前后端通过 AOPS channel 对接 Hermes 后端时，模型切换、安全策略切换和审批卡片的协议约定。

## 1. 审批消息

当 Hermes 需要用户确认或授权时，AOPS 出站事件为 `message_reply`，数据位于 `data`。

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
    "text": "",
    "conversationEnded": true,
    "content": [
      {
        "type": "approval",
        "id": "exec-approval-001",
        "approvalKind": "exec",
        "allowedActions": [
          { "command": "/approve", "display": "仅本次允许" },
          { "command": "/approve always", "display": "始终允许" },
          { "command": "/deny", "display": "拒绝" }
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

字段规则：

- `phase` 固定为 `actions`。
- `kind` 固定为 `approval`，Tec01 可用它快速识别审批卡片。
- `content[].type` 固定为 `approval`。
- `content[].approvalKind` 当前可能为 `exec` 或 `slash`。
- `content[].allowedActions` 是后端允许的按钮集合，数组项为对象。
- `content[].allowedActions[].command` 是可直接回发执行的文本指令。
- `content[].allowedActions[].display` 是按钮展示文案，UI 直接渲染该字段，不需要再转换。
- `replyToId` 对应用户触发审批的消息 ID。

动作含义：

- `/approve`：仅本次允许。
- `/approve session`：本会话允许，常用于不允许永久授权的安全审查类审批。
- `/approve always`：始终允许同类操作。
- `/deny`：拒绝。

回传方式：

- Tec01 可直接把用户点击项的 `allowedActions[].command` 作为消息文本回发。
- 如使用结构化回传，Tec01 应带回 `approvalId` 和 `action`；`action` 可直接使用 `/approve`、`/approve session`、`/approve always`、`/deny` 等指令文本。
- Hermes AOPS 入站兼容 `data.content`、`data.metadata` 中的 `approvalId/action`，也继续兼容旧的 `allow-once`、`allow-session`、`allow-always`、`deny` 值。
- 文本兜底仍兼容 `/approve`、`/approve session`、`/approve always`、`/deny`、`/always`、`/cancel`。

## 2. slash 确认类审批

`/new`、`/reset`、`/undo`、`/reload-mcp` 等需要用户确认的 slash 指令也使用审批卡片。

```json
{
  "type": "approval",
  "id": "1",
  "approvalKind": "slash",
  "allowedActions": [
    { "command": "/approve", "display": "执行本次" },
    { "command": "/always", "display": "始终执行" },
    { "command": "/cancel", "display": "取消" }
  ],
  "message": "Confirm /new ...",
  "command": "/new",
  "description": "slash command confirmation"
}
```

slash 审批动作映射：

- `/approve` -> 执行本次确认。
- `/always` -> 执行并关闭后续同类确认。
- `/cancel` -> 取消本次指令。

## 3. 模型列表

Tec01 发送：

```json
{
  "event": "message_posted",
  "data": {
    "id": "545201",
    "text": "/model list",
    "channelId": "conv_xxx",
    "agentKey": "main"
  }
}
```

Hermes 行为：

- 只读取当前用户配置的模型网关，不再枚举所有供应商。
- 配置来源优先级：AOPS channel 已选模型偏好、`config.yaml model.base_url/api_key/apiKey/api_key_env/apiKeyEnv`、匹配当前 provider 的 `custom_providers/providers`。
- `api_key` 支持 `${ENV}`、`$ENV`、`env:ENV` 和直接填写环境变量名；Hermes 会解析后作为 `/models` 请求的 `Authorization: Bearer <key>`。
- 请求当前模型网关的 OpenAI-compatible `/models` endpoint。
- 默认超时为 4 秒，可用 `AOPS_MODEL_LIST_TIMEOUT` 调整。

返回结构：

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
  "summary": {
    "model": "qwen-coder",
    "provider": "tec01-gateway"
  },
  "error": null
}
```

失败时：

```json
{
  "type": "model.list",
  "ok": false,
  "itemType": "model",
  "items": [],
  "error": {
    "code": "MODEL_GATEWAY_FETCH_FAILED",
    "message": "无法从当前模型网关获取模型列表。",
    "details": {
      "probedUrl": "http://model-gateway.internal/v1/models"
    }
  }
}
```

## 4. 模型切换

Tec01 可直接发送列表项里的 `command`：

```text
/model use tec01-gateway qwen-coder
```

返回结构：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "model.switch",
  "ok": true,
  "command": "/model use tec01-gateway qwen-coder",
  "model": "qwen-coder",
  "provider": "tec01-gateway",
  "providerLabel": "tec01-gateway",
  "baseUrl": "http://model-gateway.internal/v1",
  "apiMode": "chat_completions",
  "apiKeyConfigured": true,
  "scope": "aops-channel",
  "persisted": true,
  "configUpdated": true,
  "configPath": "/home/hermes/.hermes/config.yaml",
  "preferenceKey": "aops:conv_xxx:main",
  "error": null
}
```

切换范围：

- 当前 AOPS channel + agentKey 会立即切换，无需重启网关。
- 会话偏好持久保存到 `~/.hermes/aops/channel-state.json`，网关重启后仍生效。
- 同时同步更新 `~/.hermes/config.yaml` 的 `model.provider`、`model.default`、`model.base_url`、`model.api_mode` 和已有的密钥字段。
- 如果原配置使用 `${ENV}` 或 `api_key_env`，保存时优先保留原模板，不把解析后的密钥明文写回配置。
- `/model status`、`/model current` 是只读状态接口，不会把 `status/current/list` 误写成模型名。

## 5. 当前模型状态

Tec01 可发送：

```text
/model status
```

或：

```text
/model current
```

该接口只返回当前生效模型信息，不请求模型网关 `/models`，适合页面初始化时快速展示当前模型。

返回结构：

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
  "baseUrl": "http://model.tfbai/v1",
  "apiMode": "chat_completions",
  "apiKeyConfigured": true,
  "apiKeyPreview": "sk-1...abcd",
  "source": "aops.channelPreference",
  "scope": "aops-channel",
  "preferenceKey": "aops:conv_xxx:main",
  "statePath": "/home/hermes/.hermes/aops/channel-state.json",
  "configPath": "/home/hermes/.hermes/config.yaml",
  "commands": {
    "list": "/model list",
    "status": "/model status",
    "switch": "/model use custom <model>"
  },
  "error": null
}
```

字段说明：

- `modelId` 与 `model` 均为当前生效模型 ID，前端优先使用 `modelId`。
- `source=config.model` 表示来自 `config.yaml`；`source=aops.channelPreference` 表示当前 AOPS 会话已有模型偏好。
- `scope=config` 表示当前使用用户默认配置；`scope=aops-channel` 表示当前 channel + agentKey 有覆盖偏好。
- `apiKeyPreview` 只用于排查是否读到密钥，前端不要展示完整密钥。

## 6. 安全策略

查询当前安全策略：

```text
/security
```

或：

```text
/security status
```

设置安全策略：

```text
/security set off
/security set manual
/security set smart
```

模式映射：

- `off`：完全访问权限，普通危险命令审批关闭；hardline 禁止项仍不可绕过。
- `manual`：默认权限，危险命令需要用户人工审批。
- `smart`：自动审查，低风险自动通过，高风险或不确定场景继续发审批卡片。

返回结构：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "security.updated",
  "ok": true,
  "command": "/security set smart",
  "current": {
    "mode": "smart",
    "label": "自动审查",
    "description": "低风险命令可由辅助模型自动通过，高风险或不确定场景继续请求审批。"
  },
  "persisted": true,
  "configPath": "~/.hermes/config.yaml",
  "error": null
}
```

安全策略写入 `~/.hermes/config.yaml approvals.mode`，下一次审批判断立即生效。

## 7. Bot 回复 title 字段

Hermes 所有 AOPS `message_reply.data` 都会带 `title` 字段：

```json
{
  "event": "message_reply",
  "data": {
    "messageId": "botmsg-xxx",
    "channelId": "conv_xxx",
    "replyToId": "547602",
    "phase": "end",
    "title": "CPU 告警排查结果",
    "text": "根据分析...",
    "messageType": "common"
  }
}
```

title 来源规则：

- 优先使用 Tec01 入站 `message_posted.data.title`、`conversationTitle`、`metadata.title` 或 `metadata.conversationTitle`。
- Hermes 会按 `channelId` 记住最近一次入站标题，后续同会话回复自动回填。
- 如果 Tec01 入站没有提供 dashboard 标题，则返回 Hermes 当前 session title；仍没有时返回空字符串。
- 后端不会凭空生成 Tec01 dashboard 标题；如果前端需要完全一致，请在入站消息或 metadata 中携带该标题。

## 8. AOPS 上游交互日志

Hermes 将所有 AOPS channel 上游交互写入统一日志：

```text
~/.hermes/logs/aops/aops-YYYY-MM-DD.log
```

旧版 `aops-wire-YYYY-MM-DD.log` 和 `aops-messages-YYYY-MM-DD.log` 不再继续写入。

单行格式示例：

```text
2026-06-05T15:12:01.234+08:00 io=recv event=message_posted messageType=silent silent=true channel=conv-1 msg=545201 replyTo=- title="CPU告警排查" text="/model list" status=ok elapsedMs=- raw={"event":"message_posted","data":{...}}
```

字段顺序固定：

- 本地时间：带时区，位于行首。
- `io`：`recv` 表示从 Tec01 上游收到，`send` 表示发给 Tec01 上游。
- `event`：如 `message_posted`、`message_reply`、`ping`、`pong`、`http.agent_report.request`、`http.agent_report.response`、`http.attachment.request`、`http.attachment.response`。
- `messageType`：优先使用 payload 中的 `messageType`；审批为 `approval`，工具为 `tool`，websocket 控制帧为 `ws`，HTTP 为 `http`，被过滤的内部思考为 `filtered`。
- `silent`：是否为静默消息。
- `channel`：Tec01 会话或频道 ID。
- `msg`：Tec01 消息 ID。
- `replyTo`：回复目标消息 ID。
- `title`：当前 Tec01 会话标题。
- `text`：用户文本、回复摘要、审批/工具/http/ws 摘要。
- `status`：`ok`、`failed` 或 `skipped`。
- `elapsedMs`：本地处理耗时；不适用时为 `-`。
- `raw=`：紧凑 JSON，保留本次发送或接收的完整原始数据。

关键摘要规则：

- 用户普通消息写 `io=recv event=message_posted text="<用户文本>"`。
- 用户指令消息写 `io=recv event=message_posted text="/xxx ..."`。
- Hermes 普通回复写 `io=send event=message_reply text="<回复摘要>"`，长文本截断到 500 字符。
- 审批卡片写 `messageType=approval text="approval kind=exec actions=/approve,/approve always,/deny command=<待执行命令摘要>"`。
- 工具进度写 `messageType=tool text="tool phase=start name=xxx text=..."`。
- 被过滤的内部思考写 `io=send event=message_reply messageType=filtered status=skipped text="filtered reason=internal_thinking tool=_thinking"`，不会发送给 Tec01。
- 静默消息写 `messageType=silent silent=true`，便于解释“日志里有但前端普通消息通道没消息”的情况。
- bot/me、agent 注册上报、websocket ping/pong、附件下载 HTTP 请求/响应等上游交互写对应的 `event`，并在 `raw=` 保留请求或响应数据。
- 普通 session 状态、busy handler、agent 内部状态、附件本地缓存 start/success 等非上游交互不写 AOPS 日志。

日志默认保留 7 天，可配置覆盖：

- `platforms.aops.extra.log_retention_days`
- `AOPS_LOG_RETENTION_DAYS`

配置值小于 1 或无法解析时回退为 7。清理范围包括新日志 `aops-*.log`，以及旧日志 `aops-wire-*.log`、`aops-messages-*.log`。
