# Hermes Agent 离线安装包（AOPS 版）

这个离线包以官方 `offline bundle` 为底包，额外叠加了当前仓库里的 AOPS channel 改动，适合 Linux x86_64 内网环境。

> AOPS channel 的完整接口文档见 `docs/aops-channel-interface.md`。后续 AOPS channel、Tec01 一键安装、profile、多 agent、静默命令、模型/工具集配置或 runtime 上报发生变化时，必须同步更新该文档。

## 版本信息

- Hermes 版本：`__VERSION__`
- 包内容标识：`__CONTENT_ID__`
- 离线包文件：`__BUNDLE_ARCHIVE__`
- 解压目录：`__BUNDLE_DIR__`

## 包含内容

- Hermes Agent 基础离线依赖
- Linux Python 3.11 独立运行时
- AOPS 运行时 overlay
- AOPS 示例配置
- 一键安装脚本

## 本次升级点

- 自动会话标题生成已固定为简体中文
  - 无论首轮对话原文是什么语言，自动生成的 session title 都会要求使用简体中文
  - 手动 `/title` 设置的标题不受影响
- AOPS 返回的 `title` 字段已对齐当前 session title
  - `/title` 设置或查看到的标题会同步用于 AOPS channel 推送 payload 的 `data.title`
  - 会话忙碌时发送 `/title` 也会保留这份 title metadata
- AOPS 本地 `/cron remove <id|name>` 已支持删除定时任务
  - 支持按任务 ID 或唯一任务名称删除
  - 支持静默消息触发，返回结构化 `cron.removed` 结果
- `tec-client-ip` 上报逻辑已调整为“每个系统用户首次生成一次随机 UUID，之后持久复用”
  - 持久文件保存在当前 Hermes root 下的 `aops/client-id-v2-<user-hash>`
  - 同一个系统用户切换 AOPS 地址、bot token、home channel 或 profile 时继续使用同一个值
  - 升级用户若已有旧版 `aops/client-id-<user-hash>`，仅在升级安装 launcher 明确允许时迁移复用旧值，避免新装用户误吃残留文件
- `/help` 已升级为 `local-command-tree.v2`
  - `usage` 仅用于展示
  - 参数必填判断看 `completions[].required`
  - 静态枚举候选看 `completions[].choices`
- `/cron` 与 `/cron history` 已补齐更多任务元数据
  - `description`
  - `lastDurationMs`
  - `durationMs`
- 离线包目录名和归档文件名都带版本号与提交号，便于内网留档和回滚

## 前端二次确认配置

如果前端会根据 `/help` 返回中的 `dangerous: true` 做二次确认，可以在 AOPS 配置里声明需要确认的命令。

`config.yaml` 示例：

```yaml
platforms:
  aops:
    extra:
      dangerous_commands:
        - /skills
        - /curator run
        - /curator restore
```

环境变量示例：

```bash
AOPS_DANGEROUS_COMMANDS="/skills,/curator run,/curator restore"
```

配置后：

- `/help` 对应命令节点会返回 `dangerous: true`
- 前端可在执行前弹二次确认
- 未配置的命令返回 `dangerous: false`

## 快速安装

```bash
tar -xzf __BUNDLE_ARCHIVE__
cd __BUNDLE_DIR__
bash install.sh --link --init-config
```

如果机器上已经有旧版 `~/hermes-agent`，直接重新执行同一条安装命令即可。安装脚本会进入升级模式：

- 保留现有 `~/.hermes/config.yaml` 和 `~/.hermes/.env`
- 复用已有虚拟环境并重新覆盖离线依赖与 AOPS overlay
- 自动备份旧 launchers 和旧 overlay 到 `INSTALL_DIR/upgrade-backups/<timestamp>/`
- 如果之前已经在 `~/.local/bin` 建过链接，会自动延续

如果不想写到默认目录：

```bash
bash install.sh /opt/hermes-agent --link --init-config
```

## 安装后常用命令

```bash
~/hermes-agent/hermes
~/hermes-agent/hermes-gateway
~/hermes-agent/hermes-gateway run
~/hermes-agent/hermes-dashboard
```

如果安装时带了 `--link`，也可以直接执行：

```bash
hermes
hermes-gateway
hermes-dashboard
```

## AOPS 初始化

安装脚本带 `--init-config` 时，会在 `~/.hermes/` 下初始化：

- `config.yaml`
- `.env`

如果这两个文件已经存在，安装脚本不会覆盖，只会继续沿用原配置。
示例配置统一使用 `AOPS_BOT_URL`。当前版本只读取 `AOPS_BOT_URL`，不再和 `AOPS_BASE_URL` 挂钩。

也可以手工从下面两个模板复制：

- `examples/config.aops.example.yaml`
- `examples/aops.env.example`

## 运行方式

```bash
hermes-gateway run
```

或者：

```bash
~/hermes-agent/hermes-gateway run
```

## 说明

这个包为了适配你当前的内网场景，采用了“官方离线包 + AOPS 运行时 overlay”方案：

- 优点：不依赖联网重新解依赖，也不要求本地重新构建 wheel
- 适用：当前这批 AOPS 改动以 Python 源码为主
- 审计：安装后会把 overlay 文件额外保存在 `INSTALL_DIR/source-overlay/`
