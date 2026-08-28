# CCUID

<p align="center">
  <a href="https://github.com/tyql688/CCUID"><img src="ICON.png" width="256" height="256" alt="CCUID"></a>
</p>
<h1 align="center">CCUID 1.1.0</h1>
<h4 align="center">把 cli coding agents 装进 gscore</h4>
<div align="center">
  <a href="https://docs.sayu-bot.com/" target="_blank">安装文档</a> &nbsp;·&nbsp;
  <a href="https://github.com/Genshin-bots/gsuid_core" target="_blank">gsuid_core</a> &nbsp;·&nbsp;
  <a href="https://agentclientprotocol.com" target="_blank">ACP</a>
</div>

---

通过 [ACP](https://agentclientprotocol.com) 把 CLI coding agents 接进 gsuid_core，在 IM 上对话。认证由各 CLI 自己管理，CCUID 不保存 API key；使用前需先按对应 CLI 的方式完成登录。支持的 engine 见下表。

CCUID 会按 agent 声明的 ACP capability 传入图片和语音，也能把 agent 返回的内联图片、语音转发到聊天平台；未声明对应能力时会明确拒绝，不会静默丢附件。

> [!CAUTION]
> **不建议开放群组使用，后果自负。**
>
> agent 能跑 shell、读写文件 ≈ 把 bot 主人本机操作权暴露给群成员。务必：
>
> - 只对**白名单授权用户**开放

## 支持的 engines

| Engine     | 启动命令                                              | 安装/登录                                                                                                                                                       |
| ---------- | ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `claude`   | `npx -y @agentclientprotocol/claude-agent-acp@0.70.0` | [claude-agent-acp](https://github.com/agentclientprotocol/claude-agent-acp)；先执行 Claude CLI 登录                                                             |
| `codex`    | `npx -y @agentclientprotocol/codex-acp@1.7.0`         | [codex-acp](https://github.com/agentclientprotocol/codex-acp)                                                                                                   |
| `cursor`   | `cursor-agent acp`                                    | [cursor CLI](https://docs.cursor.com/cli/installation)                                                                                                          |
| `opencode` | `opencode acp`                                        | [opencode](https://opencode.ai/docs/acp/)                                                                                                                       |
| `kimi`     | `kimi acp`                                            | [Kimi Code CLI](https://moonshotai.github.io/kimi-code/) / [Kimi CLI](https://moonshotai.github.io/kimi-cli/en/guides/getting-started.html)（二选一，不能共存） |
| `gemini`   | `gemini --acp`                                        | [Gemini CLI ACP mode](https://geminicli.com/docs/cli/acp-mode/)                                                                                                 |
| `grok`     | `grok agent stdio`                                    | [Grok CLI](https://x.ai/cli)                                                                                                                                    |

## 其他工具

- **多账号切换**： [cc-switch](https://github.com/farion1231/cc-switch)
- **会话查看**：[cc-session](https://github.com/tyql688/cc-session)

## 安装

需要 Python 3.11+、`agent-client-protocol>=0.12.1,<0.13` 和已装好的 [gsuid_core](https://github.com/Genshin-bots/gsuid_core)。固定版 Claude ACP adapter 要求 Node.js 22+，Codex ACP adapter 要求 Node.js 20+。

```
core安装插件CCUID
```

重启 Core 即生效。Claude / Codex 走 npx，建议预拉固定版本避免冷启动：

```bash
npx -y @agentclientprotocol/claude-agent-acp@0.70.0 --version
npx -y @agentclientprotocol/codex-acp@1.7.0 --version
```

命令与配置项见 `cc帮助`。

## 注意事项

- 首次使用前用对应 CLI 完成登录。
- **装完 agent CLI 后必须重启 gscore**，并且要在**新开的终端**里启动——已经跑着的 gscore 读不到新 CLI 的 PATH，会让 `cc doctor` 误报 missing。
- ⚠ Gemini CLI 2026-06-18 起对 Google One / 免费层用户停服；Antigravity CLI 暂不支持 ACP。
- 如需只让 agent CLI 走代理，在 CCUID 配置里设置：

  ```text
  AgentProxyMode=true
  AgentProxyUrl=http://127.0.0.1:7890
  AgentProxyAgents=["codex"]  # 全部 agent 写 ["all"]
  AgentNoProxy=127.0.0.1,localhost,::1
  ```

  `AgentProxyMode=false` 默认不注入；`true` 按 `AgentProxyAgents` 注入，`["all"]` 表示全部，留空表示不注入。改完重启 gscore，或执行 `cc new`。

## 许可

[GPL-3.0](LICENSE)

## 致谢

- [gsuid_core](https://github.com/Genshin-bots/gsuid_core)
- [Agent Client Protocol](https://agentclientprotocol.com)
