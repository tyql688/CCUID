from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
    GsListStrConfig,
)

CONFIG_DEFAULT: dict[str, GSC] = {
    "IdleTimeoutSec": GsIntConfig(
        "Session 空闲回收秒数",
        "超时后子进程关闭，下次命令通过 native_id 自动 resume",
        1800,
        max_value=86400,
    ),
    "ShowThinking": GsBoolConfig("回发思考内容", "是否把 agent 的 thinking 回发给用户", False),
    "ToolDisplay": GsStrConfig(
        "工具调用显示档位",
        "off=不发；brief=只发 tool 标题；full=额外加每次 update 的 content 摘要（diff/terminal 输出等）",
        "off",
        options=["off", "brief", "full"],
    ),
    "PermissionPolicy": GsStrConfig(
        "ACP 权限请求策略",
        "ask=每次问用户；其余三项=自动选 agent 对应 kind 的 PermissionOption（找不到则 cancelled）",
        "allow_always",
        options=["ask", "allow_once", "allow_always", "reject_once"],
    ),
    "PromptApproveTimeoutSec": GsIntConfig(
        "权限审批等待秒数",
        "ask 模式下用户多久没回复就当 cancelled",
        60,
        max_value=3600,
    ),
    "ShowAutoPermissions": GsBoolConfig(
        "回发自动决策的权限卡",
        "false=自动 allow/reject 时不发权限卡（默认）；true=每次权限请求都发。ask 模式始终发",
        False,
    ),
    "BusyBehavior": GsStrConfig(
        "session 忙时新消息的处理",
        "queue=进队列按顺序处理（默认）；reject=直接拒绝。cc 停 会清空整条队列",
        "queue",
        options=["queue", "reject"],
    ),
    "OutputFormat": GsStrConfig(
        "输出格式",
        "text=纯文本/合并转发；image=agent 回复用 markdown 图渲染；auto=按长度自动切换",
        "image",
        options=["text", "image", "auto"],
    ),
    "RenderScale": GsIntConfig(
        "渲染像素密度",
        "1=标清省流量；2=高清(默认, Retina/手机屏锐利)；3=超清(体积约 1×9，没必要)",
        2,
        max_value=3,
    ),
    "AttachmentSandbox": GsBoolConfig(
        "附件只发 workdir 内",
        "关=任何 agent 回答里的本地路径都直发；开=只发当前 session workdir 内的文件，挡 /etc/passwd / ~/.ssh 之类",
        False,
    ),
    "AgentProxyMode": GsBoolConfig(
        "Agent CLI 使用代理",
        "关闭=不额外注入代理；开启=使用 AgentProxyUrl 只注入 AgentProxyAgents 列出的 agent 子进程",
        False,
    ),
    "AgentProxyUrl": GsStrConfig(
        "Agent CLI 代理地址",
        "AgentProxyMode 开启时使用，如 http://127.0.0.1:7890 或 socks5://127.0.0.1:7890；空=不注入",
        "http://127.0.0.1:7890",
    ),
    "AgentProxyAgents": GsListStrConfig(
        "使用代理的 Agent CLI",
        "AgentProxyMode 开启时只给这些 agent 注入代理；all=全部；空列表=不注入任何 agent",
        [],
        options=["all", "claude", "codex", "cursor", "opencode"],
    ),
    "AgentNoProxy": GsStrConfig(
        "Agent CLI 不走代理地址",
        "AgentProxyMode 开启时写入 NO_PROXY/no_proxy，逗号分隔",
        "127.0.0.1,localhost,::1",
    ),
}
