# Hermes Profile 级终端命令白名单配置指南

## 1. 功能说明

终端命令白名单用于限制 Hermes Agent 通过 `terminal` 工具执行的命令。启用后，只有与当前 profile 配置规则匹配的命令才能进入执行阶段。

该功能与原有安全配置的区别：

| 配置 | 作用 |
| --- | --- |
| `terminal.command_policy: allowlist` | 真正限制哪些命令可以执行；未匹配命令直接拒绝 |
| `command_allowlist` | 仅表示危险命令无需再次审批，不限制其他命令 |
| `approvals.deny` | 无条件禁止匹配的命令，即使白名单允许也不能执行 |
| `approvals.mode` | 控制危险命令是否需要人工或智能审批 |

终端白名单的判断顺序早于：

- `force=true`
- YOLO/完全访问模式
- 永久批准
- 危险命令审批
- 原有 `command_allowlist`

因此，任何审批状态都不能绕过终端白名单。

## 2. 生效范围

策略按 Hermes profile 隔离，配置文件路径为：

```text
default profile:
~/.hermes/config.yaml

named profile:
~/.hermes/profiles/<profile-name>/config.yaml
```

策略覆盖：

- AOPS 普通会话；
- AOPS 静默消息；
- 主 Agent；
- Subagent；
- cron 定时任务；
- CLI/Terminal 会话；
- local、Docker、SSH 等 terminal backend。

配置文件按路径、修改时间和大小缓存。修改 `config.yaml` 后，下一次 terminal 调用自动重新读取，无需重启 gateway。

正在执行中的命令不会被热切换中断。

## 3. 最小配置

只允许执行 `date`：

```yaml
terminal:
  command_policy: allowlist
  trusted_executable_dirs:
    - /usr/bin
    - /bin
    - /usr/local/bin
  command_patterns:
    - "date"
```

未配置或显式使用：

```yaml
terminal:
  command_policy: unrestricted
```

时保持 Hermes 原有终端行为。

合法模式只有：

```text
unrestricted
allowlist
```

模式或规则配置错误时采用 fail-closed：终端命令全部拒绝，不会自动降级成 unrestricted。

## 4. 完整配置结构

```yaml
terminal:
  # unrestricted | allowlist
  command_policy: allowlist

  # 短 executable 只能从这些目录中解析。
  trusted_executable_dirs:
    - /usr/bin
    - /bin
    - /usr/local/bin

  # 可选。配置后，命令工作目录必须位于这些目录内部。
  allowed_workdirs:
    - /home/hermes/workspace
    - /data/aops

  # 便捷字符串 Glob 规则。
  command_patterns:
    - "date"
    - "systemctl status aops-*.service"

  # 高级结构化规则。
  allowed_commands:
    - id: disk-usage
      executable: du
      args:
        - exact: -sh
        - path_under:
            - /data/aops
          must_exist: true
```

`command_patterns` 与 `allowed_commands` 取并集：匹配任意一条即可通过白名单阶段。

如果两者均为空：

```yaml
terminal:
  command_policy: allowlist
  command_patterns: []
  allowed_commands: []
```

表示禁止所有 terminal 命令。

## 5. executable 解析

### 5.1 短命令名

推荐写法：

```yaml
command_patterns:
  - "curl --version"
```

Hermes 不使用当前进程的 `$PATH` 查找 `curl`，而是按顺序从：

```yaml
trusted_executable_dirs:
  - /usr/bin
  - /bin
  - /usr/local/bin
```

解析。成功后，实际执行命令会改写成可信绝对路径，例如：

```text
/usr/bin/curl --version
```

这样可以避免恶意目录中的同名程序劫持命令。

### 5.2 绝对路径

也可以配置：

```yaml
command_patterns:
  - "/usr/local/bin/aops-query *"
```

绝对路径必须：

- 位于可信 executable 目录中；
- 文件存在；
- 具有可执行权限；
- 解析符号链接后仍位于可信目录中。

如果需要 `/opt/aops/bin/aops-query`，应同时增加：

```yaml
trusted_executable_dirs:
  - /opt/aops/bin
```

### 5.3 Docker/SSH

Docker、SSH 等远端 backend 会将短命令名转换成可信目录下的绝对路径。远端环境必须存在相同路径。

如果远端命令只存在于 `/usr/local/bin`，应把该目录排在前面，或直接配置绝对路径。

## 6. command_patterns Glob 规则

每条 pattern 会先使用 POSIX `shlex` 拆分为 argv，再逐参数匹配。它不是对整条原始 Shell 字符串做简单正则。

支持：

| 语法 | 含义 |
| --- | --- |
| `*` | 匹配当前一个参数内的任意数量字符 |
| `?` | 匹配当前一个参数内的单个字符 |
| `[abc]` | 匹配一个指定字符 |
| `[0-9]` | 匹配一个字符范围 |

示例：

```yaml
command_patterns:
  - "systemctl status aops-*.service"
```

允许：

```bash
systemctl status aops-api.service
systemctl status aops-worker.service
```

拒绝：

```bash
systemctl restart aops-api.service
systemctl status aops-api.service --no-pager
```

因为参数数量、参数顺序或固定参数不一致。

### 6.1 Glob 不跨参数

规则：

```yaml
command_patterns:
  - "show *"
```

允许：

```bash
show public
show "value with spaces"
```

拒绝：

```bash
show value extra
show --secret
```

单独的 `*` 不匹配以 `-` 开头的新选项，防止模型借助通配符追加危险参数。

### 6.2 不支持 `**`

以下配置非法：

```yaml
command_patterns:
  - "curl **"
  - "curl -d '{**}' http://example"
```

`**` 会让策略状态变成 `valid=false`，终端 fail-closed。

允许任意 JSON 对象应使用单个 `*`：

```yaml
- "curl -d '{*}' http://example"
```

### 6.3 引号

包含空格的一个参数必须用引号表示：

```yaml
command_patterns:
  - >-
    curl -H "Content-Type: application/json"
    http://aops.internal/api/v1/health
```

`"Content-Type: application/json"` 在匹配时是一个 argv 参数。

### 6.4 多行命令

Hermes 支持标准 Shell 续行：

```bash
curl -s "http://example" \
  -H "Accept: application/json"
```

白名单会在匹配前移除 `\` 与紧随其后的换行。因此它可以匹配 YAML 中的单行或折叠 pattern。

该修复已包含在离线包：

```text
hermes-aops-offline-bundle-v0.19.0-c6408c0-linux-x86_64.tar.gz
```

早期版本会把续行错误识别成额外的换行参数。

## 7. 禁止的 Shell 语法

白名单模式只允许单个命令，不允许 Shell 组合。

以下语法在规则匹配前直接拒绝：

```bash
command1 | command2
command > file
command >> file
command1 && command2
command1 || command2
command1 ; command2
command $(other-command)
command `other-command`
TOKEN=value command
command file*
```

即使将这些完整文本写入 `command_patterns`，仍不会放行。

例如：

```bash
curl ... | python3 -m json.tool
```

必须改为单独的 curl。Agent 直接读取 terminal 返回的 JSON，不需要通过 Python 或 jq 格式化。

如果业务确实需要多步处理，应封装成一个受信任的固定脚本或专用 Hermes Tool，再将该脚本加入白名单。

## 8. curl 配置示例

### 8.1 固定健康检查

```yaml
terminal:
  command_policy: allowlist
  trusted_executable_dirs:
    - /usr/bin
    - /bin
  command_patterns:
    - >-
      curl --disable --fail --silent --show-error
      http://91.0.14.90:30080/health
```

### 8.2 固定工作流接口，允许任意 JSON

```yaml
terminal:
  command_policy: allowlist
  trusted_executable_dirs:
    - /usr/bin
    - /bin
    - /usr/local/bin
  command_patterns:
    - >-
      curl -s -X POST http://91.0.14.90:30080/v1/workflows/run
      -H "Authorization: Bearer <AOPS_WORKFLOW_TOKEN>"
      -H "Content-Type: application/json"
      -d '{*}'
```

允许：

```bash
curl -s -X POST "http://91.0.14.90:30080/v1/workflows/run" \
  -H "Authorization: Bearer <AOPS_WORKFLOW_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"inputs":{"query":"安全平台负责人是谁","userId":"S000639"},"response_mode":"blocking","user":"abc-123"}'
```

其中 `-d '{*}'` 表示：

- `-d` 后必须只有一个参数；
- 参数必须以 `{` 开始并以 `}` 结束；
- JSON 字段、顺序、空格和内容可以变化；
- 不能借此增加新的 curl 参数。

以下命令仍会拒绝：

```bash
curl ... -d '{...}' | python3 -m json.tool
curl ... --upload-file /etc/passwd
curl ... http://other-host/upload
```

### 8.3 不建议使用任意 URL

不建议：

```yaml
- "curl -s -X POST http://*/v1/workflows/run -d '{*}'"
```

这会允许访问任意主机。应固定协议、IP/域名和端口，或使用结构化 URL matcher。

### 8.4 Token 安全

把 Token 写入 `command_patterns` 会让它以明文保存在 `config.yaml` 中。

更安全的方式是创建固定脚本：

```text
/usr/local/bin/aops-workflow-query
```

脚本内部从权限受控的配置读取 Token，Hermes 只允许：

```yaml
command_patterns:
  - "aops-workflow-query *"
```

复杂鉴权、文件上传和多步骤 HTTP 处理优先使用专用 Hermes Tool。

## 9. allowed_commands 结构化规则

复杂限制可以使用结构化规则。

### 9.1 exact

```yaml
allowed_commands:
  - id: service-status
    executable: systemctl
    args:
      - exact: status
      - exact: nginx.service
```

只允许：

```bash
systemctl status nginx.service
```

### 9.2 one_of

```yaml
allowed_commands:
  - id: service-status
    executable: systemctl
    args:
      - exact: status
      - one_of:
          - nginx.service
          - aops-api.service
          - aops-worker.service
```

### 9.3 regex

`regex` 使用完整匹配，不是子串搜索：

```yaml
allowed_commands:
  - id: journal-lines
    executable: journalctl
    args:
      - exact: -n
      - regex: "^[1-9][0-9]{0,2}$"
```

允许 `1..999`，拒绝其他参数。

### 9.4 path_under

```yaml
allowed_commands:
  - id: disk-usage
    executable: du
    args:
      - exact: -sh
      - path_under:
          - /data/aops
          - /var/log/aops
        must_exist: true
```

路径会解析为规范路径，不能通过 `..` 或符号链接逃出允许目录。

### 9.5 url

```yaml
allowed_commands:
  - id: workflow-api
    executable: curl
    args:
      - exact: -s
      - exact: -X
      - exact: POST
      - url:
          schemes: [http]
          hosts: [91.0.14.90]
          ports: [30080]
          path_regex: "^/v1/workflows/run$"
```

URL matcher 支持：

```yaml
schemes: [http, https]
hosts: [aops.internal]
ports: [443]
path_regex: "^/api/v1/health(?:\\?.*)?$"
```

如需同时限制 Header 和 JSON，应继续在 `args` 中依次增加对应 matcher。参数数量必须完全一致。

## 10. 工作目录限制

配置：

```yaml
allowed_workdirs:
  - /home/hermes/workspace
  - /data/aops
```

后，terminal 的最终工作目录必须位于这些目录或其子目录中。

拒绝示例：

```text
workdir=/tmp
workdir=/etc
```

错误码：

```text
TERMINAL_WORKDIR_NOT_ALLOWED
```

如果不配置或使用空数组：

```yaml
allowed_workdirs: []
```

则不额外限制工作目录。

## 11. execute_code 防绕过

`execute_code` 可以通过 Python 的 `subprocess`、`os.system` 等方式绕过 terminal，因此白名单模式会：

1. 从新 Agent 的工具 schema 中移除 `execute_code`；
2. 对缓存 Agent 在下一 turn 动态刷新工具 schema；
3. 在 `execute_code` 底层入口再次检查策略并硬拒绝；
4. 在 AOPS `/toolsets list` 中将 `code_execution` 标记为不可配置。

错误码：

```text
CODE_EXECUTION_DISABLED_BY_TERMINAL_POLICY
```

切换回 `unrestricted` 后，由策略自动隐藏的 `code_execution` 会在下一 turn 恢复。用户原本通过其他配置显式禁用的 toolset 不会被错误恢复。

## 12. AOPS 查询接口

支持：

```text
/security terminal
/security terminal status
```

响应示例：

```json
{
  "schemaVersion": "local-command-list.v1",
  "type": "security.terminal.status",
  "ok": true,
  "command": "/security terminal status",
  "terminal": {
    "mode": "allowlist",
    "valid": true,
    "patternCount": 4,
    "structuredRuleCount": 1,
    "trustedExecutableDirCount": 3,
    "allowedWorkdirCount": 1,
    "codeExecutionBlocked": true,
    "configPath": "/home/oma/.hermes/config.yaml",
    "errors": [],
    "effectiveImmediately": true,
    "restartRequired": false
  },
  "error": null
}
```

该命令：

- 只读；
- 支持 AOPS `silent=true`；
- 不返回完整白名单内容；
- 不返回 Token 或命令敏感参数；
- 静默响应继续保持 `title=""`。

`/security status` 也包含相同的 `terminal` 摘要。

## 13. 拒绝响应

未匹配白名单：

```json
{
  "output": "",
  "exit_code": -1,
  "error": "Command is not allowed by profile terminal policy.",
  "status": "blocked",
  "error_code": "TERMINAL_COMMAND_NOT_ALLOWED",
  "policy": "allowlist"
}
```

配置无效：

```json
{
  "output": "",
  "exit_code": -1,
  "status": "blocked",
  "error_code": "TERMINAL_POLICY_INVALID",
  "policy": "allowlist"
}
```

工作目录不允许：

```json
{
  "output": "",
  "exit_code": -1,
  "status": "blocked",
  "error_code": "TERMINAL_WORKDIR_NOT_ALLOWED",
  "policy": "allowlist"
}
```

## 14. 排障步骤

### 14.1 确认配置文件属于正确 profile

执行：

```text
/security terminal status
```

检查：

```json
"configPath": "/期望的/profile/config.yaml"
```

避免修改 default 后，却在 named profile 中测试。

### 14.2 确认策略有效

应满足：

```json
{
  "mode": "allowlist",
  "valid": true,
  "errors": []
}
```

常见无效原因：

- 使用 `**`；
- `trusted_executable_dirs` 不是数组；
- 可信目录不是绝对路径；
- executable 包含通配符；
- `allowed_commands.args` matcher 格式错误；
- YAML 缩进错误。

### 14.3 比较 argv，而不是只看文本

Glob 白名单要求：

- 参数数量一致；
- 参数顺序一致；
- 引号形成的参数边界一致；
- 固定文本一致。

例如白名单是：

```yaml
- "curl -s http://example -d '{*}'"
```

下面不会匹配：

```bash
curl http://example -s -d '{}'
curl -s http://example -d '{}' --fail
```

### 14.4 检查模型是否自行追加处理命令

常见追加项：

```bash
| jq
| python3 -m json.tool
| grep ...
> /tmp/result.json
```

这些都会被拒绝。应在相关 `SKILL.md` 中明确：

```text
只执行单个 curl；禁止管道、重定向和第二条格式化命令；
直接解析 terminal 返回的原始 JSON。
```

提示词只能降低模型自行改写的概率，白名单负责最终安全兜底。如果要求完全确定的调用形态，应使用固定包装脚本或专用 Tool。

### 14.5 检查 executable

在目标运行环境确认：

```bash
command -v curl
ls -l /usr/bin/curl
```

并确保实际目录包含在：

```yaml
trusted_executable_dirs:
```

### 14.6 检查版本

多行 `\` 续写必须使用包含续行修复的版本。当前可用离线包：

```text
v0.19.0-c6408c0
```

旧包可以临时使用单行命令，但建议升级。

## 15. 推荐实践

- 只开放业务确实需要的 executable；
- 固定 curl 的协议、主机、端口和路径；
- 明确列出所有允许的 curl 选项；
- 不使用允许任意主机的 URL Glob；
- 不允许 `--upload-file`、`-F`、`--config` 等高风险参数；
- 将复杂鉴权封装在权限受控的脚本或专用 Tool 中；
- 每个 profile 单独配置；
- 修改后使用 `/security terminal status` 检查；
- 先在测试 profile 验证，再同步到生产 profile；
- 保留 `approvals.deny` 作为白名单之上的额外禁止层；
- 不把 MCP、插件和其他自定义执行工具误认为受 terminal 白名单保护，它们需要分别禁用或审计。
