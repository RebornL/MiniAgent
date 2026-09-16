"""app.config —— 后端知识的唯一 locus + config.json 的惰性读取。

「用哪个后端」（model / provider 构造）与「会话存哪」（store_dir）的字面量只活在这里；
入口（assembly / cli / __main__）只收 `llm` seam，不再穿层传构造知识。

导入期不碰 config.json（它在 .gitignore 里，全新 clone 上不存在），
只有真正要调真实 LLM 时才读它——这条由 `test_import_works_on_a_fresh_clone_without_config_json` 守住。
"""
import json
from pathlib import Path
from typing import Callable

from openai import OpenAI

from providers.deepseek import DeepSeekProvider

#: 唯一归属：改后端改这里（model 与 provider 构造都在本模块）。
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_STORE_DIR = "./agent_sessions"

__all__ = ["DEFAULT_MODEL", "DEFAULT_STORE_DIR", "_default_llm", "_get_client", "_load_config"]


def _load_config() -> dict:
    """从 config.json 加载配置，文件不存在则报错提示"""
    config_path = Path(__file__).resolve().parents[1] / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"配置文件缺失: {config_path}\n"
            "请参考 README.md 创建 config.json，包含 base_url 和 api_key"
        )
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """惰性构造 client：只有真正要调真实 LLM 时才读 config.json。

    导入期不得触碰 config.json（它在 .gitignore 里，全新 clone 上不存在），
    否则 `import app` 会直接 FileNotFoundError。
    """
    global _client
    if _client is None:
        config = _load_config()
        _client = OpenAI(base_url=config["base_url"], api_key=config["api_key"])
    return _client


def _default_llm(on_delta: Callable[[str], None] | None = None) -> DeepSeekProvider:
    """默认 provider：model 与 client 都归本模块（惰性读 config.json）。

    `on_delta=None` = 非流式默认（装配的非交互路径）；要边收边打印的呈现方
    （`app.cli.chat_loop`）自己传回调——展示策略归拥有呈现的层。
    """
    return DeepSeekProvider(_get_client(), DEFAULT_MODEL, on_delta=on_delta)
