"""DreamRole —— 一个自己用的 agent。

这个包只放服务端逻辑：模型接入、工具、会话主循环、事件流、界面接口。
前端在 `web/`，是纯静态的 HTML + JS，没有构建步骤（少一层工具链就少一类
「打包成 exe 之后白屏」的故障）。
"""
from __future__ import annotations

# 版本号只有一个来源：`config.APP_VERSION`（界面、/api/bootstrap、打包脚本都读它）。
# 这里曾经写死 `"1.0.0"`，于是同一份代码里有两个版本号 —— 界面显示 1.4.1、
# `app.__version__` 说 1.0.0，谁都不知道该信哪个。现在它只是个转发。
from .config import APP_VERSION as __version__  # noqa: E402
