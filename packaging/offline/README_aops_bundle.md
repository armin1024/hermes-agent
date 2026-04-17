# Hermes Agent 离线安装包（AOPS 版）

这个离线包以官方 `offline bundle` 为底包，额外叠加了当前仓库里的 AOPS channel 改动，适合 Linux x86_64 内网环境。

## 包含内容

- Hermes Agent 基础离线依赖
- Linux Python 3.11 独立运行时
- AOPS 运行时 overlay
- AOPS 示例配置
- 一键安装脚本

## 快速安装

```bash
tar -xzf __BUNDLE_NAME__.tar.gz
cd __BUNDLE_NAME__
bash install.sh --link --init-config
```

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
