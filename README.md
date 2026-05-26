# CCUID

<p align="center">
  <a href="https://github.com/tyql688/CCUID"><img src="ICON.png" width="256" height="256" alt="CCUID"></a>
</p>
<h1 align="center">CCUID 1.0.0</h1>
<h4 align="center">把 cli coding agents 装进 gscore</h4>
<div align="center">
  <a href="https://docs.sayu-bot.com/" target="_blank">安装文档</a> &nbsp;·&nbsp;
  <a href="https://github.com/Genshin-bots/gsuid_core" target="_blank">gsuid_core</a> &nbsp;·&nbsp;
  <a href="https://agentclientprotocol.com" target="_blank">ACP</a>
</div>

---

通过 [ACP](https://agentclientprotocol.com) 把 cli coding agents 接进 gsuid_core，在 IM 上对话。**复用本机已登录的 CLI 凭证**，不要求额外 API key。支持的 engine 见下表。

> [!CAUTION]
> **不建议开放群组使用，后果自负。**
>
> agent 能跑 shell、读写文件 ≈ 把 bot 主人本机操作权暴露给群成员。务必：
>
> - 只对**白名单授权用户**开放

## 支持的 engines

| Engine     | 启动命令                                 | 安装/登录                                                            |
| ---------- | ---------------------------------------- | -------------------------------------------------------------------- |
| `claude`   | `npx -y @zed-industries/claude-code-acp` | [claude-code-acp](https://github.com/zed-industries/claude-code-acp) |
| `codex`    | `npx -y @zed-industries/codex-acp`       | [codex-acp](https://github.com/zed-industries/codex-acp)             |
| `cursor`   | `cursor-agent acp`                       | [cursor CLI](https://docs.cursor.com/cli/installation)               |
| `opencode` | `opencode acp`                           | [opencode](https://opencode.ai/docs/acp/)                            |

## 其他工具

- **多账号切换**： [cc-switch](https://github.com/farion1231/cc-switch)
- **会话查看**：[cc-session](https://github.com/tyql688/cc-session)

## 安装

需要 Python 3.11+ 和已装好的 [gsuid_core](https://github.com/Genshin-bots/gsuid_core)。

```
core安装插件CCUID
```

重启 Core 即生效。claude / codex 走 npx（需要 Node.js 18+），建议预拉一次避免冷启动：

```bash
npx -y @zed-industries/claude-code-acp --version
npx -y @zed-industries/codex-acp --version
```

命令与配置项见 `cc帮助`。

## 注意事项

- 首次使用前用对应 CLI 完成登录。
- **装完 agent CLI 后必须重启 gscore**，并且要在**新开的终端**里启动——已经跑着的 gscore 读不到新 CLI 的 PATH，会让 `cc doctor` 误报 missing。
- OpenCode 的模型由它自己的 `~/.config/opencode/opencode.jsonc` 决定，具体 [issue](https://github.com/anomalyco/opencode/issues/4001)。没显式写 `model` 时，`opencode acp` 会使用 OpenCode 默认模型，常见显示为 `OpenCode Zen/Big Pickle`。需要固定模型就写：

  ```jsonc
  {
    "$schema": "https://opencode.ai/config.json",
    "model": "opencode/deepseek-v4-flash-free",
  }
  ```

  改完后重启 gscore，或对当前 OpenCode 会话执行 `cc new` 重新拉起 agent。

## 许可

[GPL-3.0](LICENSE)

## 致谢

- [gsuid_core](https://github.com/Genshin-bots/gsuid_core)
- [Agent Client Protocol](https://agentclientprotocol.com)
