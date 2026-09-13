"""
工具超时模块 —— 防止工具函数卡死 Agent
"""
import concurrent.futures
from typing import Callable


DEFAULT_TOOL_TIMEOUT = 30  # 秒


def call_with_timeout(
    func: Callable,
    args: tuple = (),
    kwargs: dict | None = None,
    timeout: float = DEFAULT_TOOL_TIMEOUT,
) -> str:
    """
    在线程池中执行工具函数，超时则返回错误信息。

    为什么用线程池而不是 asyncio:
      - 工具函数是同步的（read_file, requests.get 等）
      - 线程池对同步阻塞 IO 最自然
      - 不要求用户改工具函数

    返回:
        正常: 工具函数的返回值（转 str）
        超时: f"工具执行超时（{timeout}秒）"
        异常: f"工具执行错误: {e}"
    """
    kwargs = kwargs or {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            result = future.result(timeout=timeout)
            return str(result)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return f"工具执行超时（{timeout}秒），已取消执行"
        except Exception as e:
            return f"工具执行错误: {e}"