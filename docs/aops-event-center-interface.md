# AOPS Channel 事件中心上下文接口

本文档面向 Tec01 下游 UI 和消息转发服务，描述通过 AOPS channel 设置、查询和清除事件中心只读协助上下文的协议。

## 1. 功能说明

事件中心上下文用于让 Hermes 在指定 Tec01 会话中持续遵循事件处理规则：

- 当前事件由 `eventId` 标识；
- Hermes 仅作为只读分析助手，不作为事件执行人；
- 在 `aops-cli event-center` 下仅允许查询当前事件详情；
- 禁止修改事件、工单状态和时间线；
- 禁止发送消息或主动加载 `send-message` Skill；
- 用户要求“处理、解决、更新、通知”时，只能调查、分析并起草供用户确认的内容。

Tec01 只需在进入或切换事件时执行一次 `/event-center use <eventId>`，不需要在每条普通消息中重复传递事件 ID 或完整提示词。

> 本功能通过系统提示词降低模型误操作概率，不是执行层权限控制。若业务要求绝对禁止写操作，仍需在工具执行层增加只读白名单。

## 2. 作用域和生命周期

上下文按以下组合隔离：

```text
profile + channelId + agentKey
```

规则：

- 同一 `channelId`、`agentKey` 的后续普通对话持续使用当前事件上下文；
- 不同 channel、agentKey 或 profile 互不影响；
- Gateway 重启后上下文仍然存在；
- `/new`、`/reset` 只切换 Hermes 对话 session，不清除事件上下文；
- 切换事件时再次执行 `use`，新事件覆盖旧事件；
- 离开事件处理场景时由 Tec01 显式执行 `clear`；
- `use` 和 `clear` 不中断正在运行的 turn，从下一条普通对话开始生效；
- 本地命令本身不触发 LLM，也不会查询事件详情。

状态保存在当前 profile：

```text
<profile-home>/aops/channel-state.json
```

## 3. AOPS 请求格式

Tec01 通过标准 `message_posted` 事件发送命令：

```json
{
  "event": "message_posted",
  "data": {
    "id": "msg-event-001",
    "userId": "S000639",
    "userName": "S000639",
    "agentKey": "main",
    "channelId": "conv_event_001",
    "channelType": "direct",
    "text": "/event-center use event_123456",
    "silent": true
  }
}
```

推荐 Tec01 使用静默方式执行事件上下文管理命令：

```json
{
  "messageType": "silent",
  "silent": true
}
```

命令执行逻辑不会因静默而改变。静默回复继续携带：

```json
{
  "messageType": "silent",
  "silent": true,
  "title": ""
}
```

## 4. 命令总览

```text
/event-center
/event-center status
/event-center use <eventId>
/event-center clear
```

`/event-center` 与 `/event-center status` 等价。

### 4.1 eventId 规则

`eventId` 必须满足：

- 长度为 `1..160`；
- 只允许字母、数字、`.`、`_`、`:`、`-`；
- 不允许空格、换行、Shell 控制字符、URL 或 JSON 片段。

合法示例：

```text
event_123456
INC-2026-001
tec01:event:345
```

## 5. 设置或切换事件

请求：

```text
/event-center use event_123456
```

成功响应的 `message_reply.data.text` 是 JSON 字符串，解析后为：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "event-center.context.updated",
  "ok": true,
  "command": "/event-center use event_123456",
  "active": true,
  "eventId": "event_123456",
  "contextVersion": "event-center.v2",
  "scope": "aops-channel",
  "updatedAtMs": 1784900000000,
  "templateSource": "built-in",
  "templatePath": "/home/oma/.hermes/aops/event-center-prompt.md",
  "templateHash": "64-character-sha256",
  "effectiveImmediately": true,
  "restartRequired": false,
  "error": null
}
```

行为：

- 使用当前 profile 的模板渲染 `eventId`；
- 将渲染结果保存到当前 channel 上下文；
- 驱逐当前 channel 的缓存 Agent；
- 下一条普通对话使用新策略；
- 不调用 LLM，不执行 `aops-cli event-center info`。

## 6. 查询状态

请求：

```text
/event-center status
```

启用状态示例：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "event-center.context.status",
  "ok": true,
  "command": "/event-center status",
  "active": true,
  "eventId": "event_123456",
  "contextVersion": "event-center.v2",
  "scope": "aops-channel",
  "updatedAtMs": 1784900000000,
  "templateSource": "built-in",
  "templatePath": "/home/oma/.hermes/aops/event-center-prompt.md",
  "templateHash": "64-character-sha256",
  "effectiveImmediately": true,
  "restartRequired": false,
  "error": null
}
```

未启用状态示例：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "event-center.context.status",
  "ok": true,
  "command": "/event-center status",
  "active": false,
  "eventId": null,
  "contextVersion": null,
  "scope": "aops-channel",
  "updatedAtMs": null,
  "templateSource": null,
  "templatePath": "/home/oma/.hermes/aops/event-center-prompt.md",
  "templateHash": null,
  "effectiveImmediately": true,
  "restartRequired": false,
  "error": null
}
```

状态接口不会返回完整系统提示词。Tec01 可用以下字段判断策略是否更新：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `active` | boolean | 当前 channel 是否启用事件上下文 |
| `eventId` | string/null | 当前事件 ID |
| `contextVersion` | string/null | 内置策略版本；强化只读版为 `event-center.v2` |
| `templateSource` | string/null | `built-in` 或 `profile-file` |
| `templatePath` | string | 当前 profile 自定义模板路径 |
| `templateHash` | string/null | 渲染后提示词的 SHA-256 |
| `updatedAtMs` | integer/null | 最近一次 `use` 成功时间 |

## 7. 清除事件上下文

请求：

```text
/event-center clear
```

成功响应：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "event-center.context.cleared",
  "ok": true,
  "command": "/event-center clear",
  "active": false,
  "eventId": null,
  "contextVersion": null,
  "scope": "aops-channel",
  "updatedAtMs": null,
  "templateSource": null,
  "templatePath": "/home/oma/.hermes/aops/event-center-prompt.md",
  "templateHash": null,
  "effectiveImmediately": true,
  "restartRequired": false,
  "error": null
}
```

重复执行 `clear` 仍返回成功。清除后，下一条普通消息恢复为无事件中心策略的常规对话。

## 8. AOPS 外层响应

静默 `use` 的完整外层示例：

```json
{
  "event": "message_reply",
  "data": {
    "messageId": "botmsg-event-001",
    "replyToId": "msg-event-001",
    "channelId": "conv_event_001",
    "phase": "end",
    "kind": "final",
    "messageType": "silent",
    "silent": true,
    "title": "",
    "text": "{\n  \"schemaVersion\": \"local-command-list.v1\",\n  \"type\": \"event-center.context.updated\",\n  \"ok\": true,\n  \"eventId\": \"event_123456\"\n}",
    "conversationEnded": true
  }
}
```

下游应解析 `data.text` 中的 JSON，并以 `type`、`ok`、`error.code` 为准，不要匹配自然语言。

每条本地命令会为其入站 `replyToId` 返回唯一终态：

```json
{
  "conversationEnded": true
}
```

## 9. 错误响应

统一错误结构：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "event-center.context.updated",
  "ok": false,
  "command": "/event-center use bad id",
  "active": false,
  "eventId": null,
  "error": {
    "code": "AOPS_EVENT_CENTER_INVALID_ID",
    "message": "eventId must be 1-160 characters using only letters, numbers, '.', '_', ':', or '-'"
  }
}
```

错误码：

| 错误码 | 说明 | UI 建议 |
| --- | --- | --- |
| `AOPS_EVENT_CENTER_INVALID_ID` | 事件 ID 为空、过长或包含非法字符 | 提示用户检查事件 ID，不重试 |
| `AOPS_EVENT_CENTER_INVALID_SUBCOMMAND` | 命令格式不支持 | 使用本文档中的固定命令重新发送 |
| `AOPS_EVENT_CENTER_STATE_WRITE_FAILED` | 本地状态写入失败 | 显示失败原因，允许用户重试或联系运维 |

## 10. Tec01 推荐对接流程

### 10.1 打开事件详情页或进入事件会话

Tec01 静默发送：

```text
/event-center use <eventId>
```

收到 `ok=true` 后再允许用户发起事件相关普通对话。

### 10.2 用户继续多轮对话

直接发送普通消息，不重复发送 `use`，也不需要传递完整提示词。

### 10.3 用户切换事件

静默发送新的：

```text
/event-center use <newEventId>
```

新上下文会覆盖旧上下文。

### 10.4 用户离开事件处理模式

如果同一个 Tec01 `channelId` 后续还会承载非事件对话，应静默发送：

```text
/event-center clear
```

如果产品希望该 channel 始终绑定同一个事件，则不需要在页面关闭时清除。

### 10.5 `/new` 和 `/reset`

Tec01 不需要重新发送 `use`。这两个命令仅重置 Hermes 会话历史，事件上下文继续绑定当前 Tec01 channel。

## 11. 自定义模板

当前 profile 可创建：

```text
~/.hermes/aops/event-center-prompt.md
```

支持占位符：

```text
{eventId}
{event_id}
{事件id}
```

模板文件修改后不会自动改变已保存的 channel 上下文。必须重新执行：

```text
/event-center use <eventId>
```

Tec01 可通过 `templateSource` 和 `templateHash` 确认当前 channel 是否已经启用新模板。模板正文不会通过 `status` 返回，也不要求 Tec01 保存或传递。

## 12. 内置策略边界

`event-center.v2` 默认策略要求模型：

- 仅在确有必要时调用：
  ```bash
  aops-cli event-center info --id '<eventId>'
  ```
- 不执行 `add`、`edit`、`status_edit`、`timeline_add` 等写命令；
- 不关闭、解决、受理或转派事件；
- 不调用消息发送能力；
- 不查看或加载 `send-message` Skill；
- 禁止命令失败后不得修正参数或重试；
- 只返回事件摘要、调查结果、建议步骤、时间线草稿和通知草稿；
- 明确提示用户确认后自行更新工单或发送消息。
