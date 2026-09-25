# DSH Harness 全局配置清单（2026-09-04 建立）

> 目的：完整记录本机 harness 相关所有配置的位置、内容、敏感度与重建步骤，
> 配合 dsh-config-manager 定时备份实现全局配置可恢复、可迁移。

## 1. 配置资产总表

| # | 配置 | 位置 | 内容 | 敏感度 | 备份方式 |
|---|---|---|---|---|---|
| 1 | DSH 主设置 | `~/.dsh/settings.yaml` | UI/主题/视觉模型/默认模型/网络暴露 | ⚠️ 含明文 API key | config-manager（vault 镜像） |
| 2 | Web profile 插件/MCP | `~/.dsh/profiles/web/cordis.patch.yml` | 插件覆盖 + ticktick MCP（client id/secret 明文！）+ remote-web-ui + ui-ux | 🔴 含明文 client secret | config-manager plugins/mcp 分区 |
| 3 | 插件清单 | `~/.dsh/profiles/web/package.json` | 25+ 插件版本（dsh-config-manager 0.1.55、upstream-radar 0.45.0 等） | 低 | config-manager plugins 分区 |
| 4 | pnpm 供应链策略 | `~/.dsh/profiles/web/pnpm-workspace.yaml` | minimumReleaseAgeExclude 白名单（7 包）、allowBuilds、nodeLinker | 低 | 手动（不在备份分区） |
| 5 | desktop/headless profile | `~/.dsh/profiles/{desktop,headless}/` | 各自的 cordis.yml/patch | 低 | 手动 |
| 6 | 全局技能 | `~/.agents/skills/<6主题>/` | configure-hindsight-local-llm、remote-web-ui、ticktick-mcp 等 | ⚠️ 部分含路径/密钥线索 | 手动（dsh-config-manager 不含此目录） |
| 7 | Hindsight 配置 | `~/.hindsight/profiles/coding-agent.env` | LLM failover 链（含 deepseek key） | 🔴 含明文 key | 手动 + env.bak 惯例 |
| 8 | Hindsight daemon | `~/.hindsight/coding-agent.json` + launchd | daemon 端口 9077 | 低 | 手动 |
| 9 | 视觉引擎 | `~/.modlens/config.json` | provider/baseUrl/model | ⚠️ | 手动 |
| 10 | memsearch 记忆 | `~/.memsearch/` + `~/.agents/` | 记忆库、技能 | ⚠️ | 手动（git？） |
| 11 | 远程转发 | `~/.local/bin/dsh-remote-fwd.py` + `~/Library/LaunchAgents/com.user.dsh-remote-fwd.plist` | easytier 转发器 | 低 | 手动 |
| 12 | launchd 常驻 | `~/Library/LaunchAgents/com.user.{ollama,hindsight}.plist` | 自启服务 | 低 | 手动 |
| 13 | ollama 模型 | 游戏本 `C:\Users\test_\.ollama`（待迁 D:\env） | qwen3 8b/14b GGUF | 低（大文件） | 非配置类，重建用 GGUF |
| 14 | easytier 网格 | `/Users/lijc/backup/easytier/mac-config.toml` | 虚拟网配置（flags 已注释） | 低 | 手动 |
| 15 | SSH 密钥 | `/Volumes/keys/ssh/ssh.toml` + `~/.ssh/id_ed25519` | 远程主机凭据 | 🔴 | 不备份（密钥挂载卷） |

## 2. config-manager 备份状态

- 工具：`dsh-config-manager@0.1.55`（web profile 插件，CLI `~/.dsh/profiles/web/node_modules/dsh-config-manager/lib/cli/index.js`）
- 数据目录：`~/.dsh/dsh-config-manager/`（exports/ snapshots/ vault/ sync/）
- **定时备份：已启用** `sync/backup-schedule.json`：`enabled:true, interval:24h`，产物为 DCA1 加密容器（自动含加密）
- 手动备份命令（agent）：`config_backup`（默认不含 secret，凭据镜像 vault）；带密码可加密导出
- 恢复：`config_restore <snapshotId>`（默认 dry-run 预览）；CLI `dsh-config-manager restore/snapshots`
- 远端同步：未配置（git/webdav 均可选，`config_sync_push/pull`）

## 3. 密钥与敏感信息位置（重建/迁移时人工处理）

| 凭据 | 位置 | 说明 |
|---|---|---|
| DEEPSEEK_API_KEY | `~/.dsh/settings.yaml`、`~/.hindsight/.../coding-agent.env` | 两份独立 key？已镜像 vault refs |
| ticktick client secret | `~/.dsh/profiles/web/cordis.patch.yml` | 🔴 明文，MCP 挂载必需 |
| Windows SSH 密码 | `/Volumes/keys/ssh/ssh.toml` `Windows_test_` 键 | 🔒 禁止复制/落盘，现读现用 |
| 视觉引擎 key | `~/.modlens/config.json` | 明文 |
| vault 凭据镜像 | `~/.dsh/dsh-config-manager/vault/.credentials.yaml` | config-manager 自动维护，勿手改 |

## 4. 重建步骤（换机/崩溃恢复）

1. `dsh-config-manager restore`（或导入备份 zip → 自动回填 vault 凭据）
2. 重装缺失插件：`dsh plugin --profile web add <pkg>@<ver>`（参考 package.json）
3. 手动恢复 `pnpm-workspace.yaml` 的 minimumReleaseAgeExclude 白名单
4. 手动恢复全局技能 `~/.agents/skills/`、hindsight env、launchd plist、转发器
5. 重输/恢复 secrets（见 §3）

## 5. 后续待办

- [ ] pnpm-workspace.yaml / 技能 / hindsight env 纳入定期备份（手动或扩展 config-manager 外挂）
- [ ] config-manager 配远端同步（git/WebDAV）做异地容灾
- [ ] upstream-radar 配置 + GitHub VM 隔离测试 + 全自动更新管线（另行文档）
