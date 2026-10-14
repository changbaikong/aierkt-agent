"""稳定的项目级 .env 加载。

【为什么需要这个文件】
进程的工作目录是会变的：`python chat_cli.py`（根目录）、
`cd fronted/... && npm run dev` 后启动后端、pytest 从任意目录跑……
如果直接用 `load_dotenv()` 找 `.env`，它会从**当前工作目录**往上找，
于是"从哪个目录启动"就会影响读不读得到配置。

这里把路径钉死在**仓库根目录**：`PROJECT_ROOT` 由 `__file__` 反推
（`agent_core/project_env.py` 的上一级），跟调用方在哪里启动无关。
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


# agent_core/ 的上一级 = 仓库根目录；resolve() 是为了消掉符号链接与相对路径。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ENV_PATH = PROJECT_ROOT / ".env"


def load_project_env(*, override: bool = False) -> Path:
    """Load the repository-root .env regardless of the process working directory."""
    # override=False（默认）：已存在的真实环境变量优先级更高，
    #   方便 CI/生产用环境变量临时覆盖 .env，而不用改文件。
    # override=True：强制以 .env 为准（少数需要"文件说了算"的场景）。
    load_dotenv(dotenv_path=PROJECT_ENV_PATH, override=override)
    return PROJECT_ENV_PATH
