"""`python -m app` —— 真实入口：装配 harness 并进入交互式对话（需要根目录 config.json）。
"""
from app import config
from app.cli import chat_loop


# ─── 4. 跑起来 ────────────────────────────────────
if __name__ == "__main__":
    base_prompt = (
        "你是一个有用的助手。"
        "你可以使用 load_skill 加载需要的技能模块，用 unload_skill 释放不再需要的模块。每次回答前先加载 structured-output"
        "遇到不确定的事实时，请先加载对应技能再操作，不要猜测。"
    )

    chat_loop(config._get_client(), base_prompt, "deepseek-v4-flash")
