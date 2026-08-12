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
    "conversationEnded": false,
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
- `conversationEnded` 固定为 `false`；审批卡片只暂停 turn，不结束对话。

## 1.1 回复终态唯一性

- 每个入站 `replyToId` 最多一条成功发送的 `conversationEnded=true`。
- 终态成功或正在发送后，不同 `messageId` 的所有晚到帧均被 Hermes 抑制；相同
  终态 `messageId` 的网络幂等重试继续允许。
- 主动状态消息使用独立 `messageId`、`phase=end`、`kind=status`、`conversationEnded=false`。
- `phase=end` 结束状态气泡自身；最终 assistant 回复才结束对应用户 turn。
- `/steer` 注入确认、`/queue` 排队确认、`/background` 启动确认以及 gateway
  draining/busy 通知都属于状态消息，固定使用 `kind=status`、
  `conversationEnded=false`。
- AOPS interrupt 模式不再额外发送 `Interrupting current task` 确认气泡；旧
  turn 的结构化中断终态和新 turn 的正式回复已经完整表达状态。
- 转写回显、压缩/重试提示、长任务心跳、inactivity warning、footer、
  shutdown/restart/update 等生命周期通知均不得占用用户 turn 的终态。
- 旧 turn 被打断时，使用旧流原 `messageId` 发送一次带
  `interrupted=true`、`finishReason=interrupted` 的终态。
- Tec01 不会收到内部 `Operation interrupted: waiting for model response` 文本。
- Tec01 不会收到终态后完成的 `Self-improvement review` 后台摘要；该结果仅写入
  Hermes 本地日志。
- slash-confirm 审批卡片保持非终态；批准、始终批准或取消后，命令结果终结原始
  slash 指令，审批操作消息另行返回简短终态确认。两者使用各自的 `replyToId`。

### Steer 所有权交接

- `/steer` 或 busy steer 的确认绑定新入站 `replyToId`，但固定为非终态状态消息。
- 只有 steer 真正注入工具结果时才发生交接；交接前的工具帧仍归旧消息。
- 旧消息使用原活动流发送唯一终态，并带
  `finishReason=steered`、`superseded=true`、
  `continuedByReplyToId=<新消息ID>`。
- 新流带 `steeredFromReplyToId=<旧消息ID>`；交接后的工具、审批、澄清、过程文本、
  错误及最终回答均绑定新消息。
- 多条 steer 按接收顺序逐次交接，每个被替代 owner 都得到一个终态，最终回答归
  最后一个 owner。
- 无工具可供注入时不交接；新消息作为独立 FIFO turn 执行并获得自己的终态。

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
  "scope": "config",
  "persisted": true,
  "configUpdated": true,
  "configPath": "/home/hermes/.hermes/config.yaml",
  "preferenceKey": null,
  "effectiveImmediately": true,
  "restartRequired": false,
  "error": null
}
```

切换范围：

- 当前 profile 的全局模型配置会立即切换，无需重启网关。
- 不保存 AOPS channel 或 agentKey 级模型偏好；同一 profile 下所有会话从下一轮开始使用新模型。
- 唯一持久状态是 `~/.hermes/config.yaml` 的 `model.provider`、`model.default`、`model.base_url`、`model.api_mode` 和已有密钥字段。
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
  "source": "config.model",
  "scope": "config",
  "preferenceKey": null,
  "statePath": "",
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
- `source` 表示模型配置在 `config.yaml` 中的解析来源，例如 `config.model`、`config.custom_providers`。
- `scope` 固定为 `config`；AOPS 不支持 channel + agentKey 模型覆盖。
- `apiKeyPreview` 只用于排查是否读到密钥，前端不要展示完整密钥。

旧版本写入 `~/.hermes/aops/channel-state.json` 的 `modelPreferences` 不再生效，并会在首次执行模型命令时清理。

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
2026-06-05T15:12:01.234+08:00 io=recv event=message_posted phase=- seq=- messageType=silent silent=true channel=conv-1 msg=545201 replyTo=- title="CPU告警排查" text="/model list" status=ok elapsedMs=- raw={"event":"message_posted","data":{...}}
```

字段顺序固定：

- 本地时间：带时区，位于行首。
- `io`：`recv` 表示从 Tec01 上游收到，`send` 表示发给 Tec01 上游。
- `event`：如 `message_posted`、`message_reply`、`ping`、`pong`、`http.agent_report.request`、`http.agent_report.response`、`http.attachment.request`、`http.attachment.response`。
- `phase`、`seq`：流式回复阶段与序号；非回复事件为 `-`。
- `messageType`：优先使用 payload 中的 `messageType`；审批为 `approval`，工具为 `tool`，websocket 控制帧为 `ws`，HTTP 为 `http`。
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
- 成功的 `phase=delta` 默认不逐 chunk 落盘；`phase=end` 保留完整最终 raw，并增加 `streamDeltaCount`、`streamChars`、`streamDurationMs`。
- delta 发送失败立即记录 `lastSuccessfulSeq`、`lastSuccessfulDelta`、`failedDelta` 和错误详情。
- 审批卡片写 `messageType=approval text="approval kind=exec actions=/approve,/approve always,/deny command=<待执行命令摘要>"`。
- 工具进度写 `messageType=tool text="tool phase=start name=xxx text=..."`。
- 内部思考（`_thinking` / `reasoning.available`）既不会发送给 Tec01，也不会写入 AOPS 文件日志。
- 静默消息写 `messageType=silent silent=true`，便于解释“日志里有但前端普通消息通道没消息”的情况。
- auth/auth_ok、agent 注册上报、websocket ping/pong、redirect/重连、附件下载 HTTP 请求/响应等上游交互写对应的 `event`，并在 `raw=` 保留请求或响应数据。
- 普通 session 状态、busy handler、agent 内部状态、附件本地缓存 start/success 等非上游交互不写 AOPS 日志。

日志默认保留 7 天，可配置覆盖：

- `platforms.aops.extra.log_retention_days`
- `AOPS_LOG_RETENTION_DAYS`
- `platforms.aops.extra.log_stream_deltas` / `AOPS_LOG_STREAM_DELTAS`：显式设为 true 时恢复成功 delta 的逐帧 raw 日志，默认关闭，配置项优先于环境变量。

配置值小于 1 或无法解析时回退为 7。清理范围包括新日志 `aops-*.log`，以及旧日志 `aops-wire-*.log`、`aops-messages-*.log`。

## 9. Memory 管理协议

AOPS 本地命令支持对当前 profile 的 Hermes 内置记忆和外部 memory provider 做即时管理：

```text
/memory status
/memory reset memory|user|all
/memory memory_char_limit [chars]
/memory user_char_limit [chars]
/memory enable memory|user|all
/memory disable memory|user|all
/memory provider status
/memory provider enable [name]
/memory provider disable
/event-center
/event-center status
/event-center use <eventId>
/event-center clear
```

预算指令分别对应 `memory.memory_char_limit` 与 `memory.user_char_limit`，不会互相覆盖。文件路径为 `<profile-home>/memories/MEMORY.md` 和 `<profile-home>/memories/USER.md`。reset 只清空本地文件，不删除 Hindsight 远端数据；响应中的 `remoteDataRetained` 为 `true`。

所有变更均通过原子配置/文件写入完成，无需重启网关。网关会驱逐当前 profile 的缓存 agent，下一轮重新加载配置；正在执行的 turn 保持原有 system prompt。内置记忆开关与 `memory.provider` 开关独立，provider 关闭时保留最近一次 provider 供无参数 enable 恢复。

`/event-center use` 启用 `event-center.v2` 只读事件协助策略。状态响应包含 `contextVersion`、`templateSource`、`templatePath` 和渲染后模板的 `templateHash`（SHA-256），不返回模板正文。默认策略仅允许 `aops-cli event-center info` 查询，禁止事件写操作、消息发送、加载 `send-message` Skill，以及失败后修正并重试禁止命令。自定义模板路径为 `<profile-home>/aops/event-center-prompt.md`；修改后必须重新执行 `use`。

## 10. Profile 删除协议

Profile 删除采用两阶段 AOPS 指令：

```text
/profile delete
/profile delete confirm <token>
```

仅当前 named profile 可删除，`default` 永远受保护；删除通过一次性确认码授权，不需要额外的 AOPS slash admin 配置。完整响应、确认码绑定、错误码和本地/远端数据语义见：[AOPS Profile 删除接口](aops-profile-delete-interface.md)。

## 11. Skills 来源与完整性协议

`/skills list [source]` 的 `source` 可选值为 `all`、`user_created`、`agent_generated`、`skillhub`、`builtin`。不传或传 `all` 返回全部；其他值按包含来源过滤，非法值返回 `SKILLS_INVALID_SOURCE`。

每个 `items[]` 都返回 `source`、本地化 `sourceLabel`、`sourceMetadata`、`modified` 和 `integrity`。用户手工放入以及用户在一轮或多轮交互后明确要求 Agent/Subagent 总结生成的技能统一为 `user_created`；后台 review 或 Curator 自主生成的技能为 `agent_generated`。

`/learn [学习内容]` 是可执行的 AOPS common 指令：无参数时学习当前会话，也可传入文件、目录、URL 或说明；执行过程和最终结果遵循普通 Agent 流式回复协议。

SkillHub 技能使用安装时 `.hub/lock.json` 的 `content_hash/files/install_path` 做本地比较：`pristine` 对应 `modified=false`，偏离安装快照对应 `modified=true`，历史 lock 不完整或读取失败对应 `modified=null/status=unknown`。该检查不访问 SkillHub API，不表示市场存在新版本，也不能判断修改者身份。
