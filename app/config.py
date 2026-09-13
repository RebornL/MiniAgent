"""app.config —— config.json 的惰性读取。

导入期不碰 config.json（它在 .gitignore 里，全新 clone 上不存在），
只有真正要调真实 LLM 时才读它——这条由 `test_import_works_on_a_fresh_clone_without_config_json` 守住。
"""
import json
from pathlib import Path

from openai import OpenAI

__all__ = ["_get_client", "_load_config"]


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
