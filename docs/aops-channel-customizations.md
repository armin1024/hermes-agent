# AOPS Channel Customizations

本文档记录当前定制分支中的 AOPS channel 行为，方便后续内网部署、升级和排障。

## 功能总览

- AOPS 连接只读取 `AOPS_BOT_URL`，不再读取或改写 `AOPS_BASE_URL`。
- `tec-client-ip` 上报值为当前系统用户级 UUID，不上报用户名、机器 ID 或 IP。
- AOPS 出站 `message_reply.data` 增加 `messageType`，取值为 `common`、`silent`、`cron`。
- 支持 AOPS 静默 SkillHub 后端命令：`/bash clawhub explore --json`、`install`、`uninstall`。
- 支持 AOPS 入站附件下载并进入 Hermes 多模态链路，静默 SkillHub 命令会跳过附件处理。
- 附件和媒体缓存统一写入 `~/.hermes/cache/{images,audio,videos,documents}`。
- AOPS cron 投递支持 `AOPS_HOME_CHANNEL`，历史记录查询按时间倒序返回。
- tec01 一键安装支持新装/更新、目标用户创建、配置下发、预装技能、Hindsight 和 USER.md 初始化。
- 离线包安装后会执行自检，确认实际导入的 overlay 不会创建旧 `image_cache/audio_cache` 目录。

## 配置

最小环境变量：

```bash
AOPS_BOT_TOKEN=replace-me
AOPS_BOT_URL=https://aops.example.com
AOPS_HOME_CHANNEL=user-001
AOPS_PUSH_TOOL_CALLS=true
AOPS_DM_POLICY=open
AOPS_ALLOW_FROM=user-001
AOPS_TRUSTED_AGENT_KEY_FROM=*
AOPS_DANGEROUS_COMMANDS="/skills,/curator run,/curator restore"
CLAWHUB_REGISTRY=http://clawhub.internal
AOPS_CONNECT_TIMEOUT=90
```

示例 `config.yaml`：

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
      dangerous_commands: ["/skills", "/curator run", "/curator restore"]
      agent_routes:
        main:
          default: true
          workspace: "~/.hermes"
```

`agent_routes.devops` 不再作为默认示例配置生成；如内网确实需要额外 agent，可自行添加。

## 身份上报

所有 AOPS HTTP/WebSocket 鉴权请求继续使用请求头 `tec-client-ip`，但值改为“当前系统用户唯一 UUID”。

- 持久文件路径：`<hermes-root>/aops/client-id-v2-<user-hash>`。
- `<hermes-root>` 使用默认 Hermes root，不随 profile 改变。
- 同一系统用户下多个 profile 共享同一个 UUID。
- 不同系统用户会得到不同 UUID。
- 新装不会读取旧 `client-id-*` 文件；升级安装 launcher 会设置 `AOPS_MIGRATE_LEGACY_CLIENT_ID=1`，允许迁移旧值。

## 消息协议

### 入站

AOPS 支持两种入站消息形态：

- `event: "message_posted"`，消息数据在 `data`。
- `assistant_reply`/`message_created` 风格回放，消息数据在嵌套 `data.message`。

如果 `data.text` 是 OpenAI messages 数组 JSON 字符串，后端会提取最后一条 `role=user` 的字符串 `content` 作为用户问题，并在 metadata 中标记 `aopsRawTextWasMessages=true` 便于排查。

### 出站

所有 AOPS `message_reply.data` 都包含：

```json
{
  "messageType": "common"
}
```

判定规则：

- metadata 中显式 `message_type="cron"` 时，`messageType="cron"`。
- 否则入站 `silent=true` 或 `metadata.silent=true` 时，整条回复链 `messageType="silent"`。
- 其余为 `messageType="common"`。

`silent` 字段仍保留，且仅透传本次入站消息的静默语义。

## 静默 SkillHub 命令

当入站消息满足以下条件时，不进入 LLM，也不走普通聊天流：

- 文本以 `/bash clawhub ` 开头。
- 入站 `silent=true`、`metadata.silent=true` 或 `messageType=silent`。

支持命令：

```bash
/bash clawhub explore --json
/bash clawhub install <slug>
/bash clawhub uninstall <slug>
```

行为说明：

- `explore --json` 直接请求 ClawHub 列表接口，返回顶层 `items` 数组。
- `install` 调用 Hermes Skills Hub 内部安装逻辑，等价强制、非交互安装。
- `uninstall` 调用 Hermes Skills Hub 内部卸载逻辑，非交互执行。
- `install` 和 `uninstall` 会额外返回一条 `done: true` 的静默结果。
- 其他 `/bash ...` 或不支持的 `clawhub` 子命令返回结构化错误。

ClawHub 地址只使用 `CLAWHUB_REGISTRY`：

```bash
CLAWHUB_REGISTRY=http://clawhub.internal
```

如果进程环境中没有该变量，桥接逻辑会尝试从当前用户的 `.bashrc`、`.bash_profile`、`.profile` 读取。

结构化结果示例：

```json
{
  "schemaVersion": "aops.skillhub.result.v1",
  "type": "commandResult",
  "ok": true,
  "command": "clawhub explore --json",
  "context": {
    "parentMessageId": "2106450357",
    "botId": null,
    "agentId": "main",
    "model": null,
    "silent": true
  },
  "items": []
}
```

## AOPS 本地命令

AOPS 本地命令入口继续支持 `/skills`、`/skills list`、`/cron` 等结构化结果。

- `/skills` 和 `/skills list` 会刷新 skill command 缓存后返回，避免新安装技能缺少 `command`。
- `/cron history <id>` 返回最新记录在前，默认最多 20 条。
- `/cron history before <id> [tsMs]` 返回锚点之前的更老记录，仍保持最新在前。
- `/cron history after <id> <tsMs>` 返回锚点之后的更新记录，仍保持最新在前。
- cron 自动投递到 AOPS 时可使用 `AOPS_HOME_CHANNEL` 作为 home channel，不需要用户手动 `/sethome`。
- `dangerous_commands` 仅标记命令危险状态，用于前端审批展示；是否禁止执行由 `blocked_commands` 控制。
- 不支持的命令会返回 `Command /xxx is not supported on AOPS`。

## 附件和多模态

AOPS 入站 `attachments` 会由 Bot 侧下载并缓存：

- 图片进入 `MessageType.PHOTO`，供视觉模型识别。
- 音频、视频、文档按 MIME 类型进入对应缓存和消息类型。
- 下载请求使用 AOPS 鉴权头，包括 `Authorization` 和 `tec-client-ip`。
- 下载失败不会静默丢失，会在用户问题后追加中文系统提示。
- 静默 SkillHub 命令跳过附件处理，避免无关附件干扰命令执行。

统一 AOPS 日志只记录与 Tec01 上游的附件 HTTP 交互：

- `http.attachment.request`
- `http.attachment.response`

日志写入 `~/.hermes/logs/aops/aops-YYYY-MM-DD.log`，每行包含时间、收发方向、上游事件、`messageType`、关键摘要和 `raw=` 原始 payload。普通 session 状态、busy handler、agent 内部状态和附件本地缓存过程不写入 AOPS 日志。

默认保留 7 天，可通过 `platforms.aops.extra.log_retention_days` 或 `AOPS_LOG_RETENTION_DAYS` 覆盖。清理范围包括新日志和旧版 `aops-wire-*.log`、`aops-messages-*.log`。

## 缓存和清理

新写入缓存目录固定为：

```text
~/.hermes/cache/images
~/.hermes/cache/audio
~/.hermes/cache/videos
~/.hermes/cache/documents
```

后续部署不再主动创建或写入：

```text
~/.hermes/image_cache
~/.hermes/audio_cache
~/.hermes/document_cache
~/.hermes/video_cache
```

网关运行时会定期清理新缓存目录：

- gateway cron ticker 默认每 60 秒 tick 一次。
- 每 60 个 tick 执行一次缓存清理，约每小时一次。
- 默认删除 mtime 超过 24 小时的文件。
- 只删除文件，不删除空目录。
- 不清理旧 `image_cache/audio_cache` 目录。

## 离线包部署

使用 AOPS 离线包：

```bash
bash install.sh --link --init-config
```

升级模式会复用已有 venv 并重新覆盖依赖和 overlay，不覆盖已有 `~/.hermes/config.yaml` 与 `~/.hermes/.env`。

安装结束前会运行自检：

- 使用刚安装的 `$VENV_DIR/bin/python`。
- 导入实际运行的 `hermes_cli.config`。
- 在临时 `HERMES_HOME` 执行 `ensure_hermes_home()`。
- 确认只创建 `cache/images`、`cache/audio`、`cache/videos`、`cache/documents`。
- 若创建了旧 `image_cache/audio_cache`，安装直接失败并打印实际导入路径。

安装成功输出会展示：

- install dir
- CLI launcher
- gateway launcher
- dashboard launcher
- venv Python

## tec01 一键安装

tec01 下发入口使用 `install-oneclick.sh` 加任务 JSON：

```bash
curl -fsSL "http://tec01.internal/hermes/install-oneclick.sh" | sudo bash -s -- \
  --task-url "http://tec01.internal/hermes/tasks/test-001.json"
```

任务 JSON 主要支持：

- `targetUser`: 安装到哪个 Linux 用户；用户不存在时脚本会创建。
- `bundleUrl` / `bundleSha256`: tec01 管理的离线包地址和校验值。
- `config.modelGateway`: 模型网关地址、模型名、API key。
- `config.aops`: `AOPS_BOT_TOKEN`、`AOPS_BOT_URL`、`AOPS_HOME_CHANNEL`、`CLAWHUB_REGISTRY`、`AOPS_API_KEY`、`AOPS_CONNECT_TIMEOUT` 等。
- `config.preinstallSkills`: 从 `CLAWHUB_REGISTRY` 预装的技能 slug 列表。
- `config.approvals.mode`: 使用官方 `approvals.mode` 取值，不自定义新值。
- `config.display.busy_input_mode`: 使用官方 `display.busy_input_mode` 取值，不自定义新值。
- `config.userInstructions.content`: 完整 Markdown，写入 `~/.hermes/memories/USER.md`。
- `config.hindsight`: 写入 `~/.hermes/hindsight/config.json`，`bankIdTemplate` 支持 `users-{user}`。

更新安装默认保留已有配置，只填充缺失项：

- `options.overwriteExistingConfig=false`: 默认值，已有 `.env`、`config.yaml`、`USER.md`、Hindsight 配置不被覆盖。
- `options.overwriteExistingConfig=true`: 覆盖任务 JSON 中提供的所有配置字段。
- `options.overwriteFields`: 只覆盖指定字段，例如 `["userInstructions", "aops.AOPS_BOT_URL"]`。

脚本会根据是否已存在 Hermes 安装判断新装或更新；新装后可自动 `hermes gateway start`，更新后可自动 `hermes gateway restart`。

## 排障

### 新包仍创建 `~/.hermes/image_cache`

优先确认实际执行的是新安装目录：

```bash
which hermes
readlink -f "$(which hermes)"
~/hermes-agent/venv/bin/python - <<'PY'
import hermes_cli.config as c
print(c.__file__)
PY
```

如果 `hermes_cli.config` 不在 `~/hermes-agent/venv/lib/python3.11/site-packages/`，说明仍在跑旧环境。

### AOPS 401 未授权

检查 `AOPS_BOT_TOKEN` 和 `AOPS_BOT_URL`，当前版本不读取 `AOPS_BASE_URL`。

### SkillHub 列表为空或解析失败

检查：

```bash
echo "$CLAWHUB_REGISTRY"
curl "$CLAWHUB_REGISTRY/api/v1/skills?limit=1"
```

如果 gateway 进程由 systemd 启动，确保 `CLAWHUB_REGISTRY` 在服务环境中可见，或写入运行用户的 `.bashrc` / `.profile`。

### 一个小时前的缓存文件没有被清理

这是预期行为。网关约每小时检查一次，但只删除超过 24 小时的缓存文件。
