# AOPS Channel Memory 管理接口

本文档面向 Tec01/Anyi 下游 UI 和消息转发服务，描述 AOPS channel 当前 profile 的本地记忆管理协议。协议通过 AOPS `message_posted` 发送本地命令，并通过一条 `message_reply` 返回结构化 JSON 文本。

## 1. 适用范围

本接口管理当前 profile 的以下内容：

- 查看内置长期记忆文件 `MEMORY.md` 的完整内容；
- 内置长期记忆文件 `MEMORY.md`；
- 用户画像文件 `USER.md`；
- 两个文件各自的字符预算；
- 内置记忆和用户画像开关；
- 外部 `memory.provider`（例如 `hindsight`）开关。

所有配置修改均即时写入当前 profile。无需重启 gateway；命令返回后，下一轮新 turn 使用新配置。正在执行中的 turn 不会被强制中断或热替换 system prompt。

## 2. 请求格式

下游发送标准 AOPS 入站事件，`text` 为以下命令之一：

```json
{
  "event": "message_posted",
  "data": {
    "id": "msg-001",
    "channelId": "tec01-session-001",
    "channelType": "direct",
    "userId": "S000639",
    "agentKey": "main",
    "text": "/memory status",
    "silent": false
  }
}
```

`silent=true` 时命令仍会执行，但回复必须按 AOPS 静默协议透传 `silent=true`，且 `title` 留空。下游不应因为静默而丢弃结构化 JSON；如需隐藏 UI 展示，可由下游自行处理。

## 3. 命令总览

### 3.1 查询状态

```text
/memory
/memory status
```

### 3.2 查看 MEMORY.md

```text
/memory get
/memory get memory
```

两种写法等价，均返回当前 profile 的 `memories/MEMORY.md` 完整 UTF-8 内容。该操作只读，不修改文件、不驱逐 agent cache，也不要求重启 gateway。`/memory show` 仍是 `/memory status` 的兼容别名，不用于读取文件。

### 3.3 重置本地文件

```text
/memory reset memory
/memory reset user
/memory reset all
```

`memory` 清空 `MEMORY.md`，`user` 清空 `USER.md`，`all` 同时清空两个文件。重置只影响本地文件，不删除 Hindsight 服务端历史。

### 3.4 分别查询或修改预算

```text
/memory memory_char_limit
/memory memory_char_limit <chars>

/memory user_char_limit
/memory user_char_limit <chars>
```

`chars` 必须为 `1..100000` 的正整数。两个预算相互独立：修改一个不会改变另一个。

### 3.5 内置记忆开关

```text
/memory enable memory
/memory disable memory

/memory enable user
/memory disable user

/memory enable all
/memory disable all
```

`user` 和 `profile` 均可作为用户画像目标；`all` 同时操作内置记忆和用户画像。关闭开关不会删除对应文件，也不会自动关闭外部 provider。

### 3.6 外部 provider 开关

```text
/memory provider status
/memory provider enable
/memory provider enable hindsight
/memory provider disable
```

- `enable <name>`：启用指定 provider；名称只允许字母、数字、`.`、`_`、`-`。
- 无参数 `enable`：恢复最近一次成功保存的 provider。
- `disable`：将 `memory.provider` 写为空字符串，并保存最近一次 provider 到 profile 的 `aops-memory-state.json`。
- provider 命令阶段不执行网络探测或依赖安装，避免下游请求阻塞；实际可用性在后续 agent 初始化时判断。

## 4. 统一响应封装

所有命令回复的 `data.text` 是格式化 JSON 字符串，解析后结构如下：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "memory.status",
  "ok": true,
  "command": "/memory status",
  "memory": {},
  "userProfile": {},
  "provider": {},
  "effectiveImmediately": true,
  "restartRequired": false,
  "error": null
}
```

AOPS 外层 `message_reply` 示例：

```json
{
  "event": "message_reply",
  "data": {
    "messageId": "botmsg-001",
    "replyToId": "msg-001",
    "channelId": "tec01-session-001",
    "messageType": "silent",
    "silent": true,
    "title": "",
    "text": "{\n  \"schemaVersion\": \"local-command-list.v1\",\n  \"type\": \"memory.status\",\n  ...\n}",
    "conversationEnded": true
  }
}
```

下游应以 `text` JSON 中的 `type` 和 `ok` 为准，不要依赖自然语言文本匹配。

## 5. 状态响应字段

`memory.status` 和成功的 `memory.updated` 都包含以下状态：

```json
{
  "memory": {
    "enabled": true,
    "charLimit": 2200,
    "path": "/home/oma/.hermes/memories/MEMORY.md"
  },
  "userProfile": {
    "enabled": true,
    "charLimit": 1375,
    "path": "/home/oma/.hermes/memories/USER.md"
  },
  "provider": {
    "enabled": true,
    "name": "hindsight",
    "lastProvider": "hindsight"
  },
  "effectiveImmediately": true,
  "restartRequired": false,
  "error": null
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `memory.enabled` | boolean | 是否启用 `MEMORY.md` 内置记忆 |
| `memory.charLimit` | integer | `memory.memory_char_limit` |
| `memory.path` | string | 当前 profile 的 `MEMORY.md` 绝对路径 |
| `userProfile.enabled` | boolean | 是否启用 `USER.md` 用户画像 |
| `userProfile.charLimit` | integer | `memory.user_char_limit` |
| `userProfile.path` | string | 当前 profile 的 `USER.md` 绝对路径 |
| `provider.enabled` | boolean | 当前是否配置了非空 `memory.provider` |
| `provider.name` | string | 当前 provider；关闭时为空字符串 |
| `provider.lastProvider` | string | 最近一次 provider，用于无参数 enable 恢复 |
| `effectiveImmediately` | boolean | 配置是否对后续 turn 即时生效 |
| `restartRequired` | boolean | 是否需要重启 gateway；本接口始终为 `false` |

### 5.1 `memory.content` 字段

`/memory get` 和 `/memory get memory` 成功时返回 `type="memory.content"`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `path` | string | 当前 profile 的 `MEMORY.md` 绝对路径 |
| `content` | string | 完整 UTF-8 文件内容，保留 Markdown 和换行 |
| `contentLength` | integer | `content` 的字符数 |
| `exists` | boolean | 文件是否存在 |
| `updatedAtMs` | integer/null | 文件最后修改时间（Unix 毫秒）；不存在时为 `null` |
| `enabled` | boolean | 当前 `memory.memory_enabled` 状态 |
| `charLimit` | integer | 当前 `memory.memory_char_limit` |
| `effectiveImmediately` | boolean | 读取成功时为 `true` |
| `restartRequired` | boolean | 始终为 `false` |

## 6. 响应示例

### 6.1 查询状态

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "memory.status",
  "ok": true,
  "command": "/memory status",
  "memory": {
    "enabled": true,
    "charLimit": 2200,
    "path": "/home/oma/.hermes/memories/MEMORY.md"
  },
  "userProfile": {
    "enabled": true,
    "charLimit": 1375,
    "path": "/home/oma/.hermes/memories/USER.md"
  },
  "provider": {
    "enabled": true,
    "name": "hindsight",
    "lastProvider": "hindsight"
  },
  "effectiveImmediately": true,
  "restartRequired": false,
  "error": null
}
```

### 6.2 查看 MEMORY.md

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "memory.content",
  "ok": true,
  "command": "/memory get",
  "path": "/home/oma/.hermes/memories/MEMORY.md",
  "content": "用户偏好使用中文回答。\n",
  "contentLength": 12,
  "exists": true,
  "updatedAtMs": 1784170000000,
  "enabled": true,
  "charLimit": 2200,
  "effectiveImmediately": true,
  "restartRequired": false,
  "error": null
}
```

文件不存在也属于成功读取：`ok=true`、`content=""`、`contentLength=0`、`exists=false`、`updatedAtMs=null`。

静默请求外层响应示例：

```json
{
  "event": "message_reply",
  "data": {
    "messageId": "botmsg-memory-get-001",
    "replyToId": "msg-memory-get-001",
    "channelId": "silent-msg-memory-get-001",
    "messageType": "silent",
    "silent": true,
    "title": "",
    "text": "{\n  \"schemaVersion\": \"local-command-list.v1\",\n  \"type\": \"memory.content\",\n  \"ok\": true,\n  ...\n}",
    "conversationEnded": true
  }
}
```

### 6.3 重置记忆

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "memory.reset",
  "ok": true,
  "command": "/memory reset memory",
  "target": "memory",
  "resetPaths": ["/home/oma/.hermes/memories/MEMORY.md"],
  "remoteDataRetained": true,
  "memory": {"enabled": true, "charLimit": 2200, "path": "/home/oma/.hermes/memories/MEMORY.md"},
  "userProfile": {"enabled": true, "charLimit": 1375, "path": "/home/oma/.hermes/memories/USER.md"},
  "provider": {"enabled": true, "name": "hindsight", "lastProvider": "hindsight"},
  "effectiveImmediately": true,
  "restartRequired": false,
  "error": null
}
```

### 6.4 修改记忆预算

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "memory.updated",
  "ok": true,
  "command": "/memory memory_char_limit 3000",
  "updatedField": "memory_char_limit",
  "updatedValue": 3000,
  "memory": {"enabled": true, "charLimit": 3000, "path": "/home/oma/.hermes/memories/MEMORY.md"},
  "userProfile": {"enabled": true, "charLimit": 1375, "path": "/home/oma/.hermes/memories/USER.md"},
  "provider": {"enabled": true, "name": "hindsight", "lastProvider": "hindsight"},
  "effectiveImmediately": true,
  "restartRequired": false,
  "error": null
}
```

### 6.5 关闭并恢复 provider

关闭：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "memory.updated",
  "ok": true,
  "command": "/memory provider disable",
  "updatedProvider": "",
  "provider": {"enabled": false, "name": "", "lastProvider": "hindsight"},
  "effectiveImmediately": true,
  "restartRequired": false,
  "error": null
}
```

恢复：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "memory.updated",
  "ok": true,
  "command": "/memory provider enable",
  "updatedProvider": "hindsight",
  "provider": {"enabled": true, "name": "hindsight", "lastProvider": "hindsight"},
  "effectiveImmediately": true,
  "restartRequired": false,
  "error": null
}
```

## 7. 错误响应

失败时仍返回结构化 JSON，`ok=false`，`error.code` 用于 UI 分支处理：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "memory.updated",
  "ok": false,
  "command": "/memory memory_char_limit 0",
  "effectiveImmediately": false,
  "restartRequired": false,
  "error": {
    "code": "AOPS_MEMORY_INVALID_LIMIT",
    "message": "记忆预算必须是 1..100000 的正整数。"
  }
}
```

错误码：

| 错误码 | 触发条件 |
| --- | --- |
| `AOPS_MEMORY_INVALID_SUBCOMMAND` | `/memory` 子命令或 provider 动作不支持 |
| `AOPS_MEMORY_INVALID_TARGET` | reset/enable/disable 的目标非法 |
| `AOPS_MEMORY_INVALID_LIMIT` | 预算不是 `1..100000` 正整数 |
| `AOPS_MEMORY_PROVIDER_REQUIRED` | 无参数 enable，但没有历史 provider |
| `AOPS_MEMORY_PROVIDER_INVALID` | provider 名称包含非法字符 |
| `AOPS_MEMORY_CONFIG_WRITE_FAILED` | `config.yaml` 无法原子写入 |
| `AOPS_MEMORY_RESET_FAILED` | 本地记忆文件无法清空 |
| `AOPS_MEMORY_READ_FAILED` | `MEMORY.md` 无法读取 |

失败响应中的状态字段是失败前读取到的当前状态；下游应以 `ok=false` 为准，不显示为已更新。

## 8. 配置映射

命令对应的 `config.yaml` 字段：

```yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
  memory_char_limit: 2200
  user_char_limit: 1375
  provider: hindsight
```

本地 provider 恢复状态文件：

```text
<profile-home>/aops-memory-state.json
```

reset 不会删除或修改 Hindsight 远端 bank、retain 数据或 recall 数据。

## 9. 下游实现建议

1. 命令菜单直接使用 `/help` 返回的结构化 completion；不要在 UI 中硬编码 provider 名称。
2. 根据 `type` 选择页面动作：`memory.content` 展示完整记忆内容，`memory.status` 刷新状态，`memory.updated` 刷新状态并提示成功，`memory.reset` 刷新文件状态并提示远端数据保留。
3. 根据 `ok` 判断成功；失败只展示 `error.message`，同时保留 `error.code` 便于埋点。
4. `path` 是服务端路径，不应作为可编辑输入；UI 只需显示文件名或“当前 profile”。
5. `effectiveImmediately=true` 表示下一轮生效，不代表当前已运行模型被中断。
6. 静默请求的回复仍可能包含完整结构化 JSON，但 `silent=true`、`title=""`；Tec01 可选择不展示。
