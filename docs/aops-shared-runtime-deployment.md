# Hermes 主机级共享 Runtime 部署说明

## 目标

共享 Runtime 让同一台 Linux 虚拟机上的多个系统用户复用同一个 Hermes Python、venv、依赖和 AOPS overlay。用户的 `.hermes` 配置、Token、profile、技能、记忆、日志和 cron 数据仍完全隔离。

共享模式必须由 root 管理，release 对普通用户只读。灰度粒度为系统用户：同一系统用户下 default 和所有 named profile 使用同一版本，不同系统用户可以绑定不同版本。

## 目录布局

默认主机目录：

```text
/data/hermes-tec01/runtime/
├── releases/<bundle-sha>/
├── bindings/<system-user>/current
├── cache/bundles/<bundle-sha>.tar.gz
├── staging/
└── locks/
```

用户侧仍保留兼容路径：

```text
~/hermes-agent/venv/bin/python
~/.local/bin/hermes
```

因此现有 systemd unit、诊断命令和 profile 管理命令无需改变路径。

## 单用户安装或灰度更新

```bash
sudo bash install-oneclick.sh \
  --runtime-layout shared \
  --shared-runtime-root /data/hermes-tec01/runtime \
  --set targetUser=oma \
  --set env.AOPS_BOT_TOKEN='<token>'
```

第一次迁移会保留旧的 per-user Runtime，切换 binding 并验证网关。配置应用或任一目标网关启动失败时，脚本恢复旧 Runtime、配置快照和迁移前运行的网关；成功后才清理旧 Runtime。

## 批量更新

批量脚本默认启用共享模式：

```bash
sudo bash tec01_multiuser_update.sh \
  --installer-url 'https://example/install-oneclick.sh' \
  --template-url 'https://example/aops-profile-template.yaml'
```

只灰度指定用户：

```bash
sudo bash tec01_multiuser_update.sh --user oma
```

需要临时保持旧布局时显式指定：

```bash
sudo bash tec01_multiuser_update.sh --runtime-layout per-user
```

## 版本与清理

- release 以离线包完整 SHA256 为唯一标识。
- 相同 SHA 只下载、解压、建 venv 和安装依赖一次。
- 保留最近 3 个完整 release。
- 仍被任一用户 binding 引用的 release 永不自动删除。
- release 构建使用文件锁和 `.complete` 标记；半成品不会被用户绑定。

## 验证

```bash
readlink -f /data/hermes-tec01/runtime/bindings/oma/current
readlink -f /home/oma/hermes-agent/venv
sudo -u oma /home/oma/hermes-agent/hermes --version
sudo -u oma /home/oma/hermes-agent/hermes gateway status
```

安装工作目录中的 `shared-runtime-summary.json` 会记录当前 release、上一个 release、是否复用、是否切换以及是否回滚。

## 安全约束

- `shared` 模式不允许非 root 执行，也不会静默回退到独立安装。
- `/data/hermes-tec01/runtime` 必须位于本地、可执行、非 NFS/CIFS、非 `noexec` 文件系统。
- 普通用户不能写 release 或 binding，防止一个用户篡改其他用户正在使用的代码。
- 共享 Runtime 禁止 gateway lazy install；新增依赖必须进入新版离线包。
