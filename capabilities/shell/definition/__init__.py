"""shell.definition —— 命令执行能力的契约（Definition）。

这里只有稳定、低频的语义与形状，没有执行逻辑（那是 `capabilities.shell.provider` 的事）：

- **只收 argv**：一次调用给的是一个**已经切分好的 argv 列表**，不是 shell 字符串。
  本能力不拼接命令、不经 shell，`spawn` 也只接受 argv（见 `miniharness.process.contract`）。
- **结果形状**：`{exit_code, stdout, stderr, stdout_truncated, stderr_truncated}`。
  退出码沿用 `subprocess` 约定（POSIX 被信号杀死为负值；被终止的范围给出终止码）。
  **非零退出码是正常结果，不是工具失败**——「失败」是另一条通道上的中止结局码。
- **输出有上限**：每个流最多保留 `DEFAULT_OUTPUT_LIMIT` 字节，超出即截断，并在对应的
  `*_truncated` 标记与文本尾注（`truncated_note`）里标明：一个 `yes` 不该把内存吃光。
- **不拥有进程生命周期**：本能力不设超时、不终止。命令跑在受管范围里，因此可被策略层
  终止；「何时终止」是策略层的事（超时 / 取消接线见 T8）。
"""
from __future__ import annotations

__all__ = ["DEFAULT_OUTPUT_LIMIT", "RUN_COMMAND_TOOL", "truncated_note"]

#: 每个输出流最多保留的字节数：超出即截断并标记（内存与结果都因此有界）。
DEFAULT_OUTPUT_LIMIT = 64 * 1024

#: `run_command` 的工具定义（OpenAI 形状），与 `app.tools` 里的技能工具同形。
RUN_COMMAND_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "在受管范围里执行一个外部命令，返回退出码 / stdout / stderr。"
            "命令必须以 argv 列表逐项给出（不经 shell），例如 [\"git\", \"status\"]；"
            "不要传 shell 字符串。命令本身不设超时。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "命令与参数，逐项给出，如 [\"python\", \"-c\", \"print(1)\"]",
                },
                "cwd": {"type": "string", "description": "工作目录（可选）"},
            },
            "required": ["argv"],
        },
    },
}


def truncated_note(limit: int = DEFAULT_OUTPUT_LIMIT) -> str:
    """被截断的流末尾追加的标记文本（`*_truncated` 标记之外的人类可读说明）。"""
    return f"\n[输出已截断：每个流最多保留 {limit} 字节]"
