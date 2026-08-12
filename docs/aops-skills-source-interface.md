# AOPS Skills 来源与 SkillHub 修改状态接口

本文档面向 Tec01 UI，说明 AOPS `/skills` 返回的技能来源、来源过滤和 SkillHub 本地修改状态。

## 1. 命令

```text
/skills
/skills list
/skills list all
/skills list user_created
/skills list agent_generated
/skills list skillhub
/skills list builtin
```

- `/skills`、`/skills list` 和 `/skills list all` 等价，返回当前 profile 的全部技能。
- 其余命令只返回指定来源的技能。
- 过滤在 Hermes 本地完成，不会请求 SkillHub 市场接口。
- 命令支持 AOPS `silent=true`；静默响应仍在 `message_reply.data.text` 中返回相同 JSON，外层保持 `silent=true`、`title=""`。

## 2. 来源枚举

```ts
type SkillSource =
  | 'user_created'
  | 'agent_generated'
  | 'skillhub'
  | 'builtin'
```

| source | 中文含义 | 判定规则 |
| --- | --- | --- |
| `user_created` | 用户创建 | 用户手工放入、本地编写，或用户在一轮/多轮对话后明确要求主 Agent、Subagent、cron 总结生成 |
| `agent_generated` | Agent 自动总结生成 | 没有用户创建要求，由后台 self-improvement、background review 或 Curator 自主生成 |
| `skillhub` | SkillHub 技能市场 | 当前 profile 的 `.hub/lock.json` 中存在安装记录 |
| `builtin` | Hermes 内置 | 当前 profile 的 bundled manifest 中存在记录 |

来源判断优先级：

```text
skillhub > builtin > agent_generated > user_created
```

存量没有来源标记的本地技能统一返回 `user_created`。

## 3. 成功响应

`message_reply.data.text` 是以下 JSON 的字符串形式：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "skills.list",
  "ok": true,
  "command": "/skills list skillhub",
  "itemType": "skill",
  "total": 1,
  "count": 1,
  "limit": null,
  "hasMore": false,
  "context": {
    "skillsRoot": "/home/oma/.hermes/skills",
    "agentId": "main",
    "workspaceDir": "/home/oma"
  },
  "summary": {
    "enabled": 1,
    "disabled": 0,
    "categories": 1,
    "sources": {
      "user_created": 0,
      "agent_generated": 0,
      "skillhub": 1,
      "builtin": 0
    },
    "modifiedSkillHub": 1
  },
  "filter": {
    "source": "skillhub"
  },
  "items": [
    {
      "id": "aops-cli-explain",
      "name": "aops-cli-explain",
      "description": "Explain AOPS CLI output.",
      "descriptionZh": "解释 AOPS CLI 输出。",
      "category": null,
      "enabled": true,
      "disabled": false,
      "homepage": null,
      "command": "/aops-cli-explain",
      "path": "/home/oma/.hermes/skills/aops-cli-explain/SKILL.md",
      "source": "skillhub",
      "sourceLabel": "SkillHub 技能市场",
      "modified": true,
      "sourceMetadata": {
        "market": "clawhub",
        "identifier": "aops-cli-explain",
        "installedAtMs": 1785800000000,
        "updatedAtMs": 1785800000000
      },
      "integrity": {
        "status": "modified",
        "installedHash": "sha256:abc123",
        "currentHash": "sha256:def456",
        "reason": "content_changed",
        "checkedAtMs": 1785801000000
      }
    }
  ],
  "error": null
}
```

`summary` 统计的是过滤后的 `items`，不是过滤前的全部技能。

## 4. 通用技能字段

```ts
interface SkillItem {
  id: string
  name: string
  description: string | null
  descriptionZh: string
  category: string | null
  enabled: boolean
  disabled: boolean
  homepage: string | null
  command: string | null
  path: string
  source: SkillSource
  sourceLabel: string
  modified: boolean | null
  sourceMetadata: SkillSourceMetadata
  integrity: SkillIntegrity
}
```

- `source` 是 UI 判断和过滤使用的稳定枚举。
- `sourceLabel` 已根据 AOPS 当前语言本地化，仅用于展示，不应用于逻辑判断。
- `modified` 只对 `skillhub` 有意义；其他来源固定为 `null`。
- `/skills enable/disable/set/uninstall` 返回的 `items[]` 也包含相同来源字段。

## 5. 不同来源的 sourceMetadata

用户要求创建的新技能：

```json
{
  "source": "user_created",
  "sourceMetadata": {
    "creationTrigger": "user_request"
  }
}
```

存量或手工本地技能：

```json
{
  "source": "user_created",
  "sourceMetadata": {
    "creationTrigger": "user_or_local"
  }
}
```

后台自动生成技能：

```json
{
  "source": "agent_generated",
  "sourceMetadata": {
    "creationTrigger": "background_review"
  }
}
```

内置技能：

```json
{
  "source": "builtin",
  "sourceMetadata": {
    "creationTrigger": "bundled"
  }
}
```

SkillHub 技能：

```ts
interface SkillHubSourceMetadata {
  market: string | null
  identifier: string | null
  installedAtMs: number | null
  updatedAtMs: number | null
}
```

## 6. SkillHub 修改状态

```ts
type SkillIntegrityStatus =
  | 'pristine'
  | 'modified'
  | 'unknown'
  | 'not_applicable'

type SkillIntegrityReason =
  | 'content_changed'
  | 'files_added'
  | 'files_deleted'
  | 'symlink_detected'
  | 'lock_incomplete'
  | 'read_failed'
  | null
```

| integrity.status | modified | UI 含义 |
| --- | --- | --- |
| `pristine` | `false` | 当前内容与安装快照一致 |
| `modified` | `true` | 安装后的文件内容或文件集合发生变化 |
| `unknown` | `null` | 历史 lock 不完整、路径异常或文件不可读，无法可靠判断 |
| `not_applicable` | `null` | 非 SkillHub 技能，不进行安装快照比较 |

检测说明：

- 比较 `.hub/lock.json` 中的安装哈希、安装文件列表和当前技能目录。
- `__pycache__/`、非安装清单中的 `*.pyc`、`.DS_Store` 不视为修改。
- 发现符号链接时不跟随链接，直接返回 `symlink_detected`。
- `modified=true` 只表示本地偏离安装快照，不代表市场存在新版本。
- 接口不能判断修改者是用户、Agent 还是外部程序。

## 7. 非法来源错误

请求：

```text
/skills list invalid
```

响应：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "skills.list",
  "ok": false,
  "command": "/skills list invalid",
  "itemType": "skill",
  "total": 0,
  "count": 0,
  "items": [],
  "filter": {
    "source": "invalid"
  },
  "error": {
    "code": "SKILLS_INVALID_SOURCE",
    "message": "不支持的技能来源 `invalid`；可用值为 all、user_created、agent_generated、skillhub、builtin。",
    "details": {
      "allowed": [
        "agent_generated",
        "all",
        "builtin",
        "skillhub",
        "user_created"
      ]
    }
  }
}
```

## 8. Tec01 UI 建议

- 使用 `source` 作为筛选值和徽标类型，不要使用可能随语言变化的 `sourceLabel`。
- SkillHub 技能仅在 `modified === true` 时展示“本地已修改”警告。
- `modified === null` 且 `integrity.status === 'unknown'` 时展示“无法校验”，不要展示为“未修改”。
- 用户创建和 Agent 自动生成应使用不同徽标，避免用户误删自己明确要求生成的技能。
- UI 不需要为了列表或完整性状态额外调用 SkillHub API。
