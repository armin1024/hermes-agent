# AOPS 用户文案国际化审计

## 汇总

- 用户可见文案 sink 出现次数：91
- 已接入 AOPS i18n：91
- 未国际化静态文案：0
- 英文词条数：155
- 中文词条数：155
- 中英文键一致：是

统计命令：

```bash
uv run python scripts/audit_aops_i18n.py --format markdown --fail-on-unlocalized
```

## 语言配置

AOPS 静态用户文案使用 `display.language`：

```yaml
display:
  language: zh
```

支持的 AOPS 词库为：

- `locales/aops_en.yaml`
- `locales/aops_zh.yaml`

`zh` 和 `zh-hant` 当前使用简体中文 AOPS 词库，其他语言使用英文 AOPS
基准词库。AOPS 离线安装模板默认设置为 `zh`。

已有 profile 可通过一键更新脚本覆盖：

```bash
--set 'configYaml.display.language=zh' \
--set 'options.overwriteFields=["configYaml.display.language"]'
```

## 统计范围

- `gateway/aops_commands.py`
- `gateway/platforms/aops.py`
- `gateway/aops_skillhub_bridge.py`
- `gateway/aops_profile_delete.py`
- `gateway/aops_skill_uninstall.py`
- `gateway/run.py` 中通过 `_aops_runtime_text()` 输出的 AOPS 共享运行态消息

共享 `gateway/run.py` 中的 AOPS Slash 审批分支使用同一 AOPS 词库，并由
`test_aops_new_uses_actions_slash_confirm` 和
`test_aops_slash_confirm_resolution_closes_original_and_approval_messages`
覆盖。AOPS 会话重置后的模型、提供方、上下文、服务地址和上下文来源说明也使用
同一词库。AOPS 重置回复不调用随机 Tip；非 AOPS 平台继续使用 Hermes 官方共享
文案和随机 Tip。

共享 Gateway 运行态消息也通过 `_aops_runtime_text()` 仅对 AOPS 做本地化，包括：

- 未设置主会话的首次提示；
- busy queue/steer/interrupt 状态和首次提示；
- `/queue`、`/steer` 的过程状态；
- 长任务心跳、迭代信息和已知活动标签；
- 无活动超时预警；
- 自动会话重置；
- 上下文压缩失败及回退提示。

这些路径由 `test_aops_shared_runtime_notices_are_localized`、
`test_aops_busy_and_automatic_notices_are_localized` 和
`test_aops_busy_ack_is_localized` 覆盖。非 AOPS 路径继续使用原有官方文案。

审计工具检查 AOPS 响应中的 `message` 字段，以及本地命令、cron、附件和
SkillHub 的错误消息参数。新增静态中文或英文文案必须调用 `aops_t()` 或
`aops_error()`。

## 统计豁免

以下内容保持原样，不作为未国际化文案：

- 协议字段、枚举值、错误码和命令语法；
- 模型生成的回复；
- 工具参数、stdout 和 stderr；
- 作为 `details.rawMessage` 保留的第三方原始错误；
- 文件日志和 WebSocket 内部诊断。

动态底层错误使用中文或英文摘要，同时保留原始错误：

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

## 当前结果

未国际化静态 AOPS 用户文案：**0**。
