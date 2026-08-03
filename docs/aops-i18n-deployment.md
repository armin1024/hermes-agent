# AOPS Channel 国际化部署说明

## 1. 适用范围

本说明适用于 Hermes Agent `0.19.0` AOPS 离线包。当前 AOPS 静态用户文案支持：

- `zh`：简体中文；
- `zh-hant`：当前同样使用简体中文 AOPS 词库；
- `en`：英文；
- 其他语言：AOPS 专用文案暂时回退到英文。

语言只影响 Hermes/AOPS 自身生成的静态提示、命令结果和错误摘要，不翻译：

- 模型生成的回复；
- 工具 stdout、stderr 和返回数据；
- 第三方接口原始错误；
- 协议字段、错误码、命令名和枚举值；
- 文件日志中的内部 WebSocket 诊断。

第三方原始错误会保留在 `error.details.rawMessage`，便于排障。

AOPS 的共享 Gateway 运行态提示也遵循该语言设置，包括主会话提醒、busy
排队/插入状态、长任务心跳、无活动预警、自动会话重置和上下文压缩告警。

## 2. 新安装

分发模板默认已经配置：

```yaml
configYaml:
  display:
    language: zh
```

使用本次离线包和配套的一键安装脚本安装即可，无需额外参数。

如果模板由 Tec01 服务端托管，应确认服务端模板也包含上述配置，并更新：

```yaml
bundle:
  url: http://<内网地址>/hermes-aops-offline-bundle-v0.19.0-<hash>-linux-x86_64.tar.gz
  sha256: <本次离线包 SHA256>
```

## 3. 已安装用户更新

将新离线包上传至内网地址，更新模板的 `bundle.url` 和 `bundle.sha256`，然后继续使用现有
`install-oneclick.sh` 更新。

如果只需要显式设置中文，可执行：

```bash
curl -fsSL 'http://<内网地址>/install-oneclick.sh' | bash -s -- \
  --set 'configYaml.display.language=zh' \
  --set 'options.overwriteFields=["configYaml.display.language"]'
```

如果需要把语言设置同步到当前系统用户的所有已有 profile：

```bash
curl -fsSL 'http://<内网地址>/install-oneclick.sh' | bash -s -- \
  --sync-other-profiles true \
  --set 'configYaml.display.language=zh' \
  --set 'options.overwriteFields=["configYaml.display.language"]'
```

跨 profile 同步不会覆盖各 profile 的 `AOPS_BOT_TOKEN`。

## 4. 手工配置

Default profile：

```text
~/.hermes/config.yaml
```

Named profile：

```text
~/.hermes/profiles/<profile>/config.yaml
```

配置示例：

```yaml
display:
  language: zh
```

可选值：

```text
zh
zh-hant
en
```

如果进程环境设置了 `HERMES_LANGUAGE`，其优先级高于 `config.yaml`。排查语言不生效时执行：

```bash
systemctl --user show-environment | grep '^HERMES_LANGUAGE=' || true
grep -n -A5 '^display:' ~/.hermes/config.yaml
```

手工修改 `config.yaml` 后应重启对应 profile 网关，以清理进程内语言和 Agent 缓存：

```bash
hermes gateway restart
```

Named profile：

```bash
hermes -p <profile> gateway restart
```

通过一键更新应用配置时，脚本会按既有变更判断处理网关重启。

## 5. 离线包必须包含的文件

安装包 overlay 中必须存在：

```text
gateway/aops_i18n.py
locales/aops_en.yaml
locales/aops_zh.yaml
docs/aops-i18n-audit.md
docs/aops-i18n-deployment.md
```

打包前可以运行：

```bash
uv run python scripts/audit_aops_i18n.py \
  --format markdown \
  --fail-on-unlocalized
```

当前审计要求：

- 中英文词库键完全一致；
- AOPS 命令和错误响应的静态用户文案入口没有未国际化项。

## 6. 部署后验证

### 中文

发送静默或普通 AOPS 命令：

```text
/memory provider enable
/skills uninstall
/cron create {}
```

预期 `error.message` 或 `message` 为中文，错误码保持不变。

发送 `/new` 时，审批卡片正文、按钮以及批准/取消后的状态也应使用中文，例如：

```text
⚠️ 确认 /new
这将创建一个全新会话，并丢弃当前会话历史记录。
```

批准后，AOPS 重置结果固定显示本次会话的运行信息，不显示随机 Tip：

```text
✨ 会话已重置！重新开始。

◆ 模型：`yf-aops-qwen35-122b`
◆ 提供方：custom
◆ 上下文：131K 令牌（自动检测）
```

如果 custom/local 模型配置了服务地址，还会显示：

```text
◆ 服务地址：http://model.internal/v1
```

模型名、提供方值、上下文数值和 URL 保持原值。只有字段标签以及“配置指定 / 默认值 /
自动检测”等来源说明随 `display.language` 切换。AOPS `/new`、`/reset` 不再调用
Hermes CLI 的随机 Tip；Terminal、Dashboard、Telegram、Discord 等非 AOPS 路径保持
官方行为。

长任务超过通知间隔时，中文 AOPS 心跳示例：

```text
⏳ 处理中 — 3 分钟 — 迭代 3/90，等待补充信息
```

未配置主会话时，首次提示示例：

```text
📬 当前未设置 AOPS 的主会话。Hermes 会将定时任务结果和跨平台消息投递到主会话。

发送 /sethome 可将当前会话设为主会话，也可以忽略此提示。
```

busy、`/queue`、`/steer`、自动重置和上下文压缩提示同样不应出现
`Working`、`Queued`、`Interrupting` 等英文静态标签。工具名称、模型名称和第三方
错误原文仍保持原值。

### 英文

将当前 profile 配置为：

```yaml
display:
  language: en
```

重启网关后再次发送相同命令，预期静态提示切换为英文。

AOPS 英文重置结果仍不显示随机 Tip，运行信息示例：

```text
✨ Session reset! Starting fresh.

◆ Model: `yf-aops-qwen35-122b`
◆ Provider: custom
◆ Context: 131K tokens (detected)
```

### 原始诊断

底层网络、文件或第三方调用失败时，响应形态为：

```json
{
  "error": {
    "code": "SILENT_COMMAND_FAILED",
    "message": "操作失败。",
    "details": {
      "rawMessage": "original exception message"
    }
  }
}
```

UI 应优先向普通用户展示 `error.message`，排障详情页面可展示
`error.details.rawMessage`。

## 7. 常见问题

### 返回了 `common.operation_failed` 这样的 key

说明 AOPS 词库未安装到 Python data 目录，或部署的离线包不是本次构建版本。检查：

```bash
find ~/hermes-agent/venv -path '*/locales/aops_zh.yaml' -o -path '*/locales/aops_en.yaml'
```

若没有结果，重新使用包含 AOPS locale overlay 的离线包更新。

### 配置为中文但仍返回英文

依次检查：

1. 当前消息实际由哪个 profile 网关处理；
2. 对应 profile 的 `config.yaml`；
3. 是否存在优先级更高的 `HERMES_LANGUAGE=en`；
4. 网关是否已在配置变更后重启；
5. AOPS 中英文词库是否随离线包安装。

### 工具输出仍是英文

这是预期行为。工具输出属于外部程序或工具数据，不做自动翻译，避免改变 JSON、命令输出和
机器可解析内容。
