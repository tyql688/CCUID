from __future__ import annotations

from ..cc_config.prefix import cc_prefix


class QueueMsg:
    NO_SESSION = "当前无 session"
    EMPTY = "queue 为空"
    PREVIEW_ATTACHMENTS_ONLY = "(仅附件)"

    @classmethod
    def list_hint(cls) -> str:
        p = cc_prefix()
        return f"{p}dequeue <qid> 删一条；{p}stop 全砍"

    @classmethod
    def usage_dequeue(cls) -> str:
        return f"用法：{cc_prefix()}dequeue <qid>"

    @classmethod
    def not_found(cls, qid: int) -> str:
        return f"#{qid} 不在 queue 里"

    @classmethod
    def is_running(cls, qid: int) -> str:
        return f"#{qid} 正在执行，用 {cc_prefix()}stop 打断"

    @classmethod
    def forbidden(cls, qid: int, owner_uid: str) -> str:
        return f"#{qid} 是 {owner_uid} 的，无权删"

    @classmethod
    def cancelled(cls, qid: int) -> str:
        return f"✓ 已取消 #{qid}"


class ChatMsg:
    NO_PENDING = "没有待审核请求\n刚发过命令时，agent 可能还在处理"
    DESC_FALLBACK = "(无描述)"

    @classmethod
    def approve_unavailable(cls, target: str, offered: str, desc: str) -> str:
        return f"无法选择 {target}（仅有 {offered}）\n已取消：{desc}"

    @classmethod
    def deny_unavailable(cls, offered: str, desc: str) -> str:
        return f"无法拒绝（仅有 {offered}）\n已取消：{desc}"

    @classmethod
    def approved(cls, *, always: bool) -> str:
        return "已永久允许，agent 继续执行" if always else "已允许，agent 继续执行"

    @classmethod
    def denied(cls) -> str:
        return "已拒绝，agent 已放弃此操作"

    @classmethod
    def reset_done(cls, engine: str) -> str:
        return f"{engine}: 已重置"

    @classmethod
    def clear_done(cls, engine: str) -> str:
        return f"{engine}: 工作区已清空"

    @classmethod
    def clear_not_found(cls, engine: str) -> str:
        return f"{engine}: 工作区不存在，无需清理"

    @classmethod
    def stop_done(cls, n: int) -> str:
        return f"已打断 {n} 个任务"

    @classmethod
    def engine_set(cls, engine: str) -> str:
        return f"engine: {engine}"


class ModelMsg:
    NO_SESSION = "session 未启动，先发条 prompt 让 agent 起来再查 model"
    NO_MODELS = "当前 engine 没返回 model 列表"

    @classmethod
    def list_hint(cls) -> str:
        p = cc_prefix()
        return f"→ {p}model <id> 切换；id 可写 model_id / 序号 / name 子串"

    @classmethod
    def not_found(cls, token: str) -> str:
        return f"找不到匹配的 model: {token}"

    @classmethod
    def switched(cls, model_id: str, name: str) -> str:
        return f"✓ 已切换到 {name} ({model_id})"

    @classmethod
    def switch_failed(cls, err: str) -> str:
        return f"切换失败: {err}"


class ModeMsg:
    NO_SESSION = "session 未启动，先发条 prompt 让 agent 起来再查 mode"
    NO_MODES = "当前 engine 没返回 mode 列表"

    @classmethod
    def list_hint(cls) -> str:
        p = cc_prefix()
        return f"→ {p}mode <id> 切换；id 可写 mode_id / 序号 / name 子串"

    @classmethod
    def not_found(cls, token: str) -> str:
        return f"找不到匹配的 mode: {token}"

    @classmethod
    def switched(cls, mode_id: str, name: str) -> str:
        return f"✓ 已切换到 {name} ({mode_id})"

    @classmethod
    def switch_failed(cls, err: str) -> str:
        return f"切换失败: {err}"


class CommandMsg:
    NO_SESSION = "session 未启动，先发条 prompt 让 agent 返回 slash commands"
    EMPTY = "当前 session 没返回 slash commands"

    @classmethod
    def not_found(cls, token: str) -> str:
        label = " ".join(token.strip().split())
        return f"找不到 slash command: {label}\n先用 {cc_prefix()}slash 查看当前 session 支持的命令"


class RemoteSessionMsg:
    UNSUPPORTED = "当前 engine 未声明支持 session/list"
    EMPTY = "没有可显示的历史 session"
    EMPTY_CURRENT_WORKDIR = "当前工作区没有可显示的历史 session"

    @classmethod
    def usage_resume(cls) -> str:
        return f"用法：{cc_prefix()}resume <序号|session_id>"

    @classmethod
    def resume_not_found(cls, token: str, scope_label: str = "当前工作区") -> str:
        return f"{scope_label}找不到可恢复 session: {token}"

    @classmethod
    def resumed(cls, session_id: str, title: str | None) -> str:
        label = "(无标题)" if title is None or title == "" else title
        return f"已绑定历史 session：{label}\n{session_id}\n下一条消息会从这里继续"

    @classmethod
    def failed(cls, err: str) -> str:
        return f"获取 session 列表失败: {err}"

    @classmethod
    def resume_failed(cls, err: str) -> str:
        return f"恢复 session 失败: {err}"

    @classmethod
    def next_cursor(cls, cursor: str) -> str:
        return f"nextCursor: `{cursor}`"


class AdminMsg:
    EMPTY = "(空)"

    @classmethod
    def user_grant(cls, uid: str, *, added: bool) -> str:
        return f"{uid}: {'已授权' if added else '已存在'}"

    @classmethod
    def user_revoke(cls, uid: str, *, removed: bool) -> str:
        return f"{uid}: {'已取消' if removed else '不存在'}"

    @classmethod
    def group_grant(cls, gid: str, mode_value: str, *, added: bool) -> str:
        return f"{gid}: {'已授权' if added else '已更新'}, {mode_value}"

    @classmethod
    def group_revoke(cls, gid: str, *, removed: bool) -> str:
        return f"{gid}: {'已取消' if removed else '不存在'}"

    @classmethod
    def list_grants(cls, user_line: str, group_line: str, n_users: int, n_groups: int) -> str:
        return f"**CCUID 授权**\n用户 ({n_users}): {user_line}\n群组 ({n_groups}):\n{group_line}"


class StatusMsg:
    NO_ACTIVE = "无活跃 session"
