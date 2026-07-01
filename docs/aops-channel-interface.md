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
- `silent`：入站顶层 `silent=true` 或既有 `messageType=silent`。不识别误拼 `slient`，也不把 `metadata.silent` 作为入站静默开关。
- `cron`：cron/后台投递。

cron 投递消息必须带 `data.channel` 数组，供 Tec01 后台路由到一个或多个前端渠道：

```json
{
  "event": "message_reply",
  "data": {
    "messageType": "cron",
    "channel": ["tec01", "anyi"],
    "job_id": "job_all001",
    "text": "⏰ 全渠道日报\\n\\n今日运行日报：无高优先级异常。",
    "botReplyExtra": {"messageType": "cron", "channel": ["tec01", "anyi"], "job_id": "job_all001"}
  }
}
```

合法渠道为 `tec01` 和 `anyi`。存量 cron 任务缺失 `channel` 时会迁移为 `["tec01"]`。cron 投递 `message_reply.data.job_id` 必填，值为触发本次投递的定时任务 ID；`botReplyExtra.job_id` 同步填充同一个值，方便 Tec01 后台按 extra 统一解析。注意：`channel` 是 Tec01 后台的前端路由字段，不是 Hermes 内部投递目标；`channelId` 是 AOPS 消息投递会话 ID，内部保存为 `origin.chat_id`。AOPS slash command 在会话内创建 cron 时默认保存创建会话为 `origin` 并写入 `deliver="origin"`，触发后回复到创建时的 `channelId`；旧会话归档后可通过 `/cron update <id> {"channelId":"new_channel_id"}` 迁移投递目标。仅在没有可用 origin 时才退回 `deliver="aops"`，并通过 `AOPS_HOME_CHANNEL` 或 gateway config 的 `home_channel` 投递到 AOPS home channel。

cron 每次执行都会保存本地输出文件并写入历史快照。快照包含执行当时的 `channelId`、`channel`、`deliver`、名称、提示词和 schedule；`outputPath` 是 gateway 机器上的本地路径，Tec01/Anyi 对端不能直接读取该文件。对端能看到的是 `message_reply.data.text` 中已推送的结果摘要/正文。若 UI 需要查看完整文件，需要新增受控的 history 文件读取或下载接口。

工具进度消息仍通过 `message_reply` 中间帧发送。`tool.completed` 会回传有界结果摘要，默认最多 4096 字符：

```json
{
  "event": "message_reply",
  "data": {
    "phase": "tool",
    "kind": "tool",
    "channelId": "conv_xxx",
    "messageType": "common",
    "conversationEnded": false,
    "tool": {
      "phase": "result",
      "name": "exec",
      "result": {
        "text": "{\"stdout\":\"hello\"}",
        "length": 18,
        "truncated": false
      },
      "durationMs": 1250,
      "isError": false
    }
  }
}
```

`platforms.aops.extra.push_tool_calls=false` 或 `AOPS_PUSH_TOOL_CALLS=false` 时，不发送 `tool.started/tool.completed` 中间帧；最终 assistant reply 不受影响。多模态/文件类工具结果只回传文本摘要，不上传本地文件内容。

静默回包保持 `messageType=silent`、`silent=true`、`replyToId=<原消息 id>`。同一 websocket 收到的 silent/common-silent 命令按接收顺序处理，避免 `/help`、`/model status` 回包乱序。

## 首条消息标题生成

AOPS 新会话标题在首条消息回复时同步返回，字段位于 `message_reply.data.title`：

- 首条消息是 `/cron create {json}`：不调用 LLM，按规则生成标题。payload 有 `name` 时为 `定时任务：{name}`；没有 `name` 时从 `prompt` 提取短摘要，例如 `定时任务：检查磁盘空间并汇报`。
- 首条普通 common 消息：在 final reply 返回前同步调用内部标题生成，只触发一次，并随同一条 final `message_reply` 返回 `title`。
- 其他 local command 不生成标题。
- 已有标题或用户通过 `/title` 设置过标题时不覆盖。

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
    "silent": true,
    "metadata": {
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
- `/toolsets list`：返回当前 profile 的工具集开关状态，与 dashboard 默认 Toolsets 页面使用同一状态源。
- `/toolsets enable <name>`、`/toolsets disable <name>`、`/toolsets set <name> <true|false>`：修改当前 profile 的 `config.yaml platform_toolsets.cli`，并同步镜像到历史兼容键 `platform_toolsets.aops`，立即影响后续新任务。
- `/skills`、`/skills list`：返回已安装技能，字段与 dashboard 技能状态保持一致。
- `/skills enable <name>`、`/skills disable <name>`、`/skills set <name> <true|false>`：修改当前 profile 的 `config.yaml skills.disabled`，立即影响后续新任务。
- `/skills uninstall <name>`、`/skills remove <name>`：卸载技能；先尝试 hub-installed 卸载，若确认不是 hub 技能，则安全删除当前 profile `~/.hermes/skills` 下的本地技能目录。
- `/cron`、`/cron list`：返回全部计划任务。
- `/cron list <tec01|anyi>`：按渠道包含式筛选计划任务；多渠道任务会同时出现在对应渠道列表中。
- `/cron create {json}`：创建计划任务，payload 支持 `name`、`prompt`、`schedule`、`channel`、`channelId`、`deliver`、`enabled`、`triggerNow`；`channel` 缺省为 `["tec01"]`，`channelId` 缺省为当前 AOPS 会话，AOPS 会话内缺省 `deliver` 为 `"origin"`；`triggerNow=true` 会在命令返回后立即后台触发一次，不等待下一轮 scheduler tick。
- `/cron update <id|name> {json}`：更新计划任务，支持修改名称、提示词、执行时间、Tec01/Anyi 路由渠道、AOPS 投递 `channelId`、投递目标、启用状态和是否立即触发；设置 `channelId` 会更新 `origin.chat_id` 并默认 `deliver="origin"`，除非显式传 `deliver="local"`；`triggerNow=true` 会立即后台触发一次。
- `/cron pause <id|name>`、`/cron resume <id|name>`、`/cron enable <id|name>`、`/cron disable <id|name>`、`/cron trigger <id|name>`：暂停、恢复/启用、禁用和立即触发计划任务；`/cron trigger` 会立即后台执行一次，不只是把 `next_run_at` 标记为下一轮 tick 到期。
- `/cron remove <id|name>`：按任务 ID 或唯一任务名删除计划任务。
- `/cron history <id> [tsMs]`、`/cron history before <id> [tsMs]`、`/cron history after <id> <tsMs>`：分页读取 cron 历史，最新记录在前；历史条目优先展示执行当时保存的名称、提示词、执行时间、`channelId`、`channel` 和 `deliver` 快照，不会随任务后续 update 改变。
- `/soul`、`/soul get`：读取当前 profile 的 `SOUL.md`，返回 `type="soul.status"`、`path`、`content`、`contentLength`、`updatedAtMs`。
- `/soul set {"content":"..."}`、`/soul append {"content":"..."}`：覆盖或追加 `SOUL.md`，返回 `type="soul.updated"`、`contentPreview`、`effectiveImmediately=true`；写入后会驱逐 idle agent cache，后续新 turn 立即加载新指令，正在运行的 turn 不热替换。
- `/user`、`/user get`：读取当前 profile 的 `memories/USER.md`，返回 `type="user.status"`。
- `/user set {"content":"..."}`、`/user append {"content":"..."}`：覆盖或追加 `memories/USER.md`，行为同 `/soul`。
- `/busy`、`/busy status`：返回当前 `display.busy_input_mode`。
- `/busy queue|steer|interrupt`：写入当前 profile 的 `config.yaml display.busy_input_mode`，并立即同步 gateway runner 内存态；返回 `type="busy.updated"`、`mode`、`saved`、`effectiveImmediately=true`。
- `/security`：返回当前审批策略。
- `/security set <off|manual|smart>`：修改审批策略；`off` 会关闭破坏性 slash 二次确认。
- `/reasoning`、`/reasoning status`、`/reasoning set ...`：读取或修改 reasoning 配置。
- `/bash clawhub explore --json`、`/bash clawhub install <slug>`、`/bash clawhub uninstall <slug>`：静默 SkillHub/ClawHub 对接命令；AOPS 安装路径只使用 `CLAWHUB_REGISTRY` 指向的内网 ClawHub/SkillHub 源，安装时优先使用 SkillHub `resolve/downloadUrl` 兼容接口并 fallback 到历史 download 端点；`install` 会忽略安全扫描阻断但保留扫描摘要，`uninstall` 同样兼容本地技能 fallback。

### SkillHub / ClawHub 静默命令响应

SkillHub 桥接响应统一使用：

```json
{
  "schemaVersion": "aops.skillhub.result.v1",
  "type": "commandResult",
  "ok": true,
  "command": "clawhub install aops-cli-explain",
  "context": {
    "parentMessageId": "1154867325",
    "botId": null,
    "agentId": "main",
    "model": null,
    "silent": true
  },
  "action": "install",
  "slug": "aops-cli-explain",
  "message": "Fetching: aops-cli-explain\nInstalled: aops-cli-explain",
  "installedPath": "/home/oma/.hermes/skills/aops-cli-explain",
  "scanIgnored": true,
  "scanVerdict": "DANGEROUS",
  "scanFindingsCount": 3
}
```

字段说明：

- `context.silent` 继承入站 `silent=true`；静默响应不回填会话标题。
- `installedPath` 为 gateway 本机路径，只用于后台诊断，Tec01/Anyi UI 不应假设可直接访问。
- `scanIgnored=true` 表示 AOPS 可信内网源安装时安全扫描告警不阻断安装；UI 可展示扫描结果，但不需要用户确认。
- `/bash clawhub explore --json` 返回 `items[]`，每项至少包含 `slug`、`displayName`、`summary`、`tags`、`stats`、`updatedAt`、`latestVersion`。
- `/bash clawhub uninstall <slug>` 返回 `action="uninstall"`；若目标不是 hub-installed 技能，会 fallback 到当前 profile 本地技能安全卸载，成功时 `source="local"`。

常见错误响应：

```json
{
  "schemaVersion": "aops.skillhub.result.v1",
  "type": "commandResult",
  "ok": false,
  "command": "clawhub install aops-cli-explain",
  "context": {
    "parentMessageId": "3836521159",
    "botId": null,
    "agentId": "main",
    "model": null,
    "silent": true
  },
  "action": "install",
  "slug": "aops-cli-explain",
  "message": "Fetching: aops-cli-explain\nError: Could not fetch 'aops-cli-explain' from any source.\nClawHub: ClawHub rate limited request: 429 Too Many Requests url=http://skillhub.internal/api/v1/resolve/aops-cli-explain",
  "installedPath": null,
  "error": {
    "code": "SKILLHUB_RATE_LIMITED",
    "message": "SkillHub rate limited the install request; please retry later.",
    "details": {
      "slug": "aops-cli-explain"
    }
  }
}
```

错误码：

- `SKILLHUB_RATE_LIMITED`：内网 SkillHub 返回 HTTP 429。UI 应提示稍后重试，批量安装时建议排队限速；若后台配置了认证 token，可提高服务端限额。
- `INSTALL_FAILED`：下载失败、包损坏、接口返回非 200、bundle 缺少 `SKILL.md` 或写入失败。`message` 会包含 ClawHub 底层状态码/URL/原因，供后台排查。
- `UNINSTALL_FAILED`：hub 卸载失败且不能 fallback 本地卸载，或本地安全删除失败。
- `EXPLORE_FAILED`：列表/浏览失败。

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

工具集配置按 Hermes profile 独立生效，AOPS 与 dashboard 默认 Toolsets 页面共享当前 profile 的 `config.yaml platform_toolsets.cli`：

```yaml
platform_toolsets:
  cli:
    - web
    - terminal
  # 兼容旧 AOPS 读取逻辑；/toolsets set 会同步镜像该键。
  aops:
    - web
    - terminal
```

兼容规则：

- 读取时优先使用 `platform_toolsets.cli`。
- 如果历史配置只有 `platform_toolsets.aops`，`/toolsets list` 和 AOPS 新任务会 fallback 读取该旧键。
- 写入时同时更新 `platform_toolsets.cli` 和 `platform_toolsets.aops`。

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
      "descriptionZh": "执行网页搜索和网页内容抓取。内网纯终端环境通常不可用，除非已配置可访问的搜索或抓取服务。",
      "enabled": true,
      "disabled": true,
      "configured": true,
      "configurable": false,
      "unsupportedReason": "内网 Linux 服务器通常无法访问公网搜索/抓取服务，默认关闭。",
      "tools": ["web_search", "web_extract"]
    }
  ],
  "error": null
}
```

字段说明：
- `enabled`：真实工具集开关状态，决定新用户 query/new turn 是否加载该工具集。
- `disabled`：前端控件是否不可操作，仅用于 UI 展示/交互控制，不代表真实工具集关闭状态。
- `configured=false`：工具集缺少 API key 或运行依赖，前端应展示“需要配置”。
- `configurable=false`：当前部署环境不建议前端开放操作，通常会同时返回 `unsupportedReason`。

开关修改立即写配置，AOPS gateway 每个新用户 query/new turn 都会重新读取 `config.yaml`，因此下一条用户消息立即生效；已在运行中的 agent turn 不强制中断或热替换。

`disabled` 字段可通过当前 profile 的 `config.yaml` 配置，推荐由一键安装模板下发：

```yaml
aops:
  toolsets:
    disabled:
      - browser
      - web
      - computer_use
    unsupportedReasons:
      browser: 需要可用浏览器或浏览器自动化运行环境，纯终端服务器默认关闭。
      web: 内网 Linux 服务器通常无法访问公网搜索/抓取服务，默认关闭。
      computer_use: 仅适用于 macOS 桌面控制，Linux 纯终端不可用。
```

如果显式配置了 `aops.toolsets.disabled`，则该列表就是前端置灰来源；没有配置时，AOPS 使用内置“内网 Linux 纯终端”默认置灰列表兜底。`unsupportedReasons` 只影响展示原因，不改变工具是否启用。

Hermes dashboard 的普通 Toolsets 页面默认读取同一份 `platform_toolsets.cli` 状态，因此 AOPS `/toolsets set web false` 后，刷新或重新进入 dashboard Toolsets 页面即可看到 `web.enabled=false`。

Dashboard API 仍支持显式读取指定 platform/profile，主要用于诊断或 profile 显式查看：

```http
GET /api/tools/toolsets?platform=aops&profile=<profile>
```

- `profile=default` 读取 `~/.hermes/config.yaml`。
- `profile=<name>` 读取 `~/.hermes/profiles/<name>/config.yaml`。
- 不传参数时 dashboard 保持原有行为，读取当前 dashboard profile 的 `platform_toolsets.cli`。
- AOPS `/toolsets` 与普通 dashboard 一致性不依赖 `platform=aops` 查询参数；它们共享 `platform_toolsets.cli`。

一键部署模板面向内网 Linux 纯终端服务器，默认只启用：

```yaml
platform_toolsets:
  cli:
    - terminal
    - file
    - code_execution
    - skills
    - todo
    - memory
    - session_search
    - clarify
    - delegation
    - cronjob
    - messaging
```

默认禁用浏览器、视觉/视频、图像/视频生成、TTS、X Search、MOA、Home Assistant、Spotify、Discord、元宝、Computer Use 和公网 web 搜索；如果历史配置缺少 `platform_toolsets.cli/aops`，AOPS 也会按这套内网 Linux 纯终端默认值兜底解析，避免 browser/web 等工具集被平台默认全集意外启用。

## Skills 接口

技能配置按 Hermes profile 独立生效，AOPS 与 dashboard 共享当前 profile 的 `config.yaml skills.disabled`：

```yaml
skills:
  disabled:
    - draft-notes
```

`/skills list` 返回示例：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "skills.list",
  "ok": true,
  "command": "/skills list",
  "itemType": "skill",
  "total": 1,
  "count": 1,
  "limit": null,
  "hasMore": false,
  "context": {
    "skillsRoot": "/home/user/.hermes/skills",
    "agentId": "main",
    "workspaceDir": "/home/user"
  },
  "summary": {
    "enabled": 1,
    "disabled": 0,
    "categories": 1
  },
  "items": [
    {
      "id": "ops/restart-service",
      "name": "Restart Service",
      "description": "Restart a service safely.",
      "descriptionZh": "安全重启服务。",
      "category": "ops",
      "enabled": true,
      "disabled": false,
      "homepage": "https://example.com/restart-service",
      "command": "/restart-service",
      "path": "/home/user/.hermes/skills/ops/restart-service/SKILL.md"
    }
  ],
  "error": null
}
```

更新响应仍使用同一外层结构，`type=skills.updated`，`items[0]` 为被更新的 skill，并包含：

```json
{
  "updated": {
    "name": "Restart Service",
    "enabled": false
  }
}
```

技能启停无需重启 gateway；后续新用户 query/new turn 会重新读取 `skills.disabled` 并生效，已运行中的 agent turn 不热替换。

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
- 任意 profile 触发 runtime 升级后，会重启同一系统用户下其他已运行或已有 systemd user service 的 default/named profile gateway；不主动启动从未运行过的 profile。

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
