# AOPS Channel Profile 删除接口

本文档面向 Tec01/Anyi 下游 UI，描述通过 AOPS channel 删除当前 Hermes named profile 的协议。

## 1. 约束

- 只允许删除当前 AOPS 消息所属的 profile，不支持通过消息指定其他 profile。
- `default` profile 永远不可删除。
- 删除为本地永久删除：配置、环境变量、记忆、会话、技能、cron、Hindsight 本地配置和 gateway 状态都会删除。
- Hindsight 服务端数据不会删除，响应中的 `remoteHindsightRetained` 始终为 `true`。
- 删除命令不需要额外的 AOPS slash admin 配置；用户通过一次性确认码确认后即可执行。
- 命令支持 `silent=true`，静默回复保持 `silent=true`、`title=""`。

## 2. 权限与确认

删除命令不依赖 `allow_admin_from` 或 `group_allow_admin_from`，也不要求下游额外维护管理员名单。

安全边界由以下规则提供：

- 只能删除当前 AOPS 消息所属的 named profile；
- `default` profile 永远不可删除；
- 必须先执行 `/profile delete` 获取一次性确认码；
- 确认码有效期为 5 分钟，并绑定当前 profile、`userId`、`channelId` 和 thread；
- `/profile delete confirm <token>` 必须由同一来源执行，确认码只能使用一次。

即使系统配置了通用 AOPS slash admin，`/profile delete` 仍按上述确认流程处理，不要求额外管理员配置。

## 3. 命令流程

### 3.1 生成删除预览

```text
/profile delete
```

该命令只生成预览和一次性确认码，不执行删除。

成功响应：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "profile.delete.preview",
  "ok": true,
  "command": "/profile delete",
  "profile": {
    "name": "hermes-2",
    "path": "/home/oma/.hermes/profiles/hermes-2",
    "isDefault": false,
    "gatewayRunning": true,
    "serviceName": "hermes-gateway-hermes-2",
    "skillCount": 12
  },
  "deletion": {
    "confirmationToken": "<one-time-token>",
    "expiresAtMs": 1784000000000,
    "irreversible": true,
    "localDataDeleted": [
      "config",
      "env",
      "memories",
      "sessions",
      "skills",
      "cron",
      "hindsight-config",
      "gateway-state"
    ],
    "remoteHindsightRetained": true
  },
  "effectiveImmediately": false,
  "restartRequired": false,
  "error": null
}
```

确认码有效期为 5 分钟，并绑定当前 profile、`userId`、`channelId` 和 thread。相同来源再次执行 `/profile delete` 会使之前的确认码失效。

### 3.2 确认删除

```text
/profile delete confirm <confirmationToken>
```

成功响应：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "profile.delete.accepted",
  "ok": true,
  "command": "/profile delete confirm <confirmationToken>",
  "profile": {
    "name": "hermes-2"
  },
  "deletion": {
    "status": "scheduled",
    "operationId": "8e5b4b0c6f3c4a2d9a7d8f5e3c1b0a9f",
    "irreversible": true,
    "remoteHindsightRetained": true
  },
  "effectiveImmediately": true,
  "restartRequired": false,
  "error": null
}
```

`scheduled` 是下游可依赖的最终协议状态。gateway 会先发送该回复，然后由独立 worker 停止服务并清理 profile；不要等待被删除的 profile 再发送完成消息。

## 4. 删除执行顺序

确认后执行：

1. 确认码立即失效，防止重放；
2. 等待当前确认响应发送；
3. 停止目标 gateway；
4. 禁用并删除 systemd/launchd service；
5. 删除 profile wrapper；
6. 永久删除 profile 目录；
7. 清理 active profile 指向，必要时恢复为 `default`。

删除 worker 独立于当前 gateway 进程，避免删除当前 profile 时因 service stop 导致清理中断。

## 5. default profile

删除 default 会返回：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "profile.delete.preview",
  "ok": false,
  "command": "/profile delete",
  "error": {
    "code": "PROFILE_DEFAULT_PROTECTED",
    "message": "The default profile cannot be deleted."
  }
}
```

如果需要清空整个 Hermes 用户环境，应使用完整卸载流程，不使用 profile 删除命令。

## 6. 错误码

| 错误码 | 含义 |
| --- | --- |
| `PROFILE_DEFAULT_PROTECTED` | 尝试删除 default profile |
| `PROFILE_NOT_FOUND` | 当前 profile 目录不存在 |
| `PROFILE_DELETE_CONFIRMATION_REQUIRED` | 缺少确认码或命令格式错误 |
| `PROFILE_DELETE_CONFIRMATION_INVALID` | 确认码错误、来源不匹配或已被使用 |
| `PROFILE_DELETE_CONFIRMATION_EXPIRED` | 确认码超过 5 分钟有效期 |
| `PROFILE_DELETE_IN_PROGRESS` | 当前 profile 已存在删除任务 |
| `PROFILE_DELETE_SERVICE_STOP_FAILED` | gateway/service 停止失败 |
| `PROFILE_DELETE_MULTIPLEX_UNAVAILABLE` | multiplex gateway 无法安全摘除该 profile |
| `PROFILE_DELETE_FAILED` | 本地清理失败 |

所有错误均使用 `ok=false`，下游应根据 `error.code` 分支，不要匹配自然语言 `message`。

## 7. 静默回复

请求：

```json
{
  "event": "message_posted",
  "data": {
    "text": "/profile delete",
    "silent": true,
    "channelId": "tec01-session-001"
  }
}
```

回复必须包含：

```json
{
  "messageType": "silent",
  "silent": true,
  "title": ""
}
```

静默只影响 UI 展示，不跳过权限、确认码和删除流程。

## 8. 下游 UI 建议

1. 点击删除时先发送 `/profile delete`，展示 profile 名称、运行状态、技能数量和不可逆警告。
2. 将返回的 `confirmationToken` 保存在当前删除弹窗状态，不写入普通日志或埋点。
3. 用户确认后发送 `/profile delete confirm <token>`。
4. 收到 `profile.delete.accepted` 后将 profile 标记为“删除中”，刷新 profile 列表。
5. 收到 `PROFILE_*` 错误时终止删除状态并展示对应错误提示。
6. 不要把 `path` 当作用户可编辑参数；它只用于诊断展示。
