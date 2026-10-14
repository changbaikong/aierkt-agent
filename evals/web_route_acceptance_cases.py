"""Fixed case manifest for the phase-four real Web acceptance campaign."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodeFixture:
    name: str
    source: str
    tests: str


@dataclass(frozen=True)
class WebAcceptanceCase:
    case_id: str
    category: str
    prompt: str
    expected_route: str
    fixture: CodeFixture | None = None


def _cases(category: str, expected_route: str, prompts: list[str]) -> list[WebAcceptanceCase]:
    return [
        WebAcceptanceCase(
            case_id=f"WEB-{category.upper().replace('_', '-')}-{index:02d}",
            category=category,
            prompt=prompt,
            expected_route=expected_route,
        )
        for index, prompt in enumerate(prompts, start=1)
    ]


CODE_FIXTURES = (
    CodeFixture(
        "addition",
        "def solve(a, b):\n    return a - b\n",
        "from app import solve\nassert solve(2, 3) == 5\nassert solve(-2, 1) == -1\n",
    ),
    CodeFixture(
        "trim",
        "def solve(text):\n    return text.lstrip()\n",
        "from app import solve\nassert solve('  hi  ') == 'hi'\nassert solve(' x ') == 'x'\n",
    ),
    CodeFixture(
        "chunk",
        "def solve(items, size):\n    return [items[i:i + size] for i in range(0, len(items) - 1, size)]\n",
        "from app import solve\nassert solve([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]\nassert solve([], 2) == []\n",
    ),
    CodeFixture(
        "parse_int",
        "def solve(text):\n    return int(text)\n",
        "from app import solve\nassert solve(' 42 ') == 42\nassert solve('bad') is None\nassert solve('') is None\n",
    ),
    CodeFixture(
        "windows_path",
        "def solve(path):\n    return path.replace('\\\\', '/')\n",
        "from app import solve\nassert solve(r'C:\\\\work\\\\file.txt') == 'C:/work/file.txt'\nassert solve('folder//file.txt') == 'folder/file.txt'\n",
    ),
)


WEB_CASES = tuple([
    *_cases("direct", "direct", [
        "用通俗中文解释什么是递归。",
        "解释单元测试和集成测试的区别，不需要查资料。",
        "给表示重试次数的变量提供三个命名建议。",
        "解释什么是幂等接口，只讲概念。",
        "说明竞态条件是什么，不需要读取本地文件。",
        "把这句话改得更自然：今天开会讨论项目进度。",
        "把 HTTP 翻译成中文并简单解释。",
        "写一句简短的提交说明，不执行任何操作。",
    ]),
    *_cases("network", "network", [
        "搜索 Python 官方网站最近发布的一条新闻并附来源。",
        "查询 pytest 当前稳定版并附官方来源。",
        "搜索 Windows 11 最近一次公开更新说明并附来源。",
        "搜索 OpenAI 官方最近发布的一项产品更新并附来源。",
    ]),
    *_cases("subagent", "subagent", [
        "比较三个公开来源对 Python 与 Rust 学习曲线的看法，做一份只读总结。",
        "研究多个公开来源，总结常见数据库迁移失败原因，不执行系统操作。",
        "比较三个公开资料对远程办公效率的结论，不修改任何代码。",
    ]),
    *_cases("readonly_code", "direct", [
        "一般来说 Python 上下文管理器是什么？不要检查本地项目。",
        "解释依赖注入的优缺点，不需要读取代码仓库。",
        "讲解数据库索引的基本原理，不运行脚本。",
        "解释代码中的竞态条件，只讲思路。",
        "如何设计一个幂等接口？只给设计建议。",
    ]),
    *_cases("missing_project_root", "direct", [
        "帮我修一下这个项目里的登录问题。",
        "给我的代码仓库补上测试。",
        "在项目里运行 Python 脚本看看为什么这么慢。",
        "检查这个仓库并直接修改有问题的地方。",
        "重构我刚才说的工程，但我还没告诉你目录。",
    ]),
    *[
        WebAcceptanceCase(
            case_id=f"WEB-CODE-AGENT-{index:02d}",
            category="code_agent",
            prompt=(
                "在 {project_root} 中修复 app.py，使 test_app.py 全部通过。"
                "不得修改 test_app.py；必须用受管 Python 实际运行 test_app.py，并如实报告结果。"
            ),
            expected_route="code_agent",
            fixture=fixture,
        )
        for index, fixture in enumerate(CODE_FIXTURES, start=1)
    ],
])
