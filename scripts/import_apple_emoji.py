"""把 Apple 表情图（emoji-data 项目 img-apple-160 集）导入情景对话表情包目录。

图片文件名即模型看到的表情描述（见 agent_core.scenario_runtime.StickerCatalog），
所以用中文名命名。目录按文件名排序最多取前 100 个作候选，这里恰好 100 个。

用法：python scripts/import_apple_emoji.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = REPO_ROOT / "data" / "scenario_stickers"

# 码点用小写十六进制（emoji-data 的文件名规则），文件名 = 中文名 + .png。
# (码点, 中文名)，共 100 个。
STICKERS: list[tuple[str, str]] = [
    # 笑 / 开心
    ("1f600", "开怀大笑"),
    ("1f606", "眯眼大笑"),
    ("1f601", "嘿嘿坏笑"),
    ("1f605", "尴尬笑"),
    ("1f602", "笑哭"),
    ("1f923", "笑得打滚"),
    ("1f60a", "微笑"),
    ("1f609", "眨眼"),
    ("1f973", "庆祝派对"),
    # 爱慕
    ("1f60d", "花痴"),
    ("1f970", "被爱包围"),
    ("1f618", "飞吻"),
    ("1f60b", "好吃"),
    # 尴尬 / 无语
    ("1f61c", "吐舌眨眼"),
    ("1f92a", "疯狂"),
    ("1f917", "拥抱"),
    ("1f92d", "捂嘴偷笑"),
    ("1f92b", "嘘"),
    ("1f914", "思考"),
    ("1f928", "挑眉"),
    ("1f610", "面无表情"),
    ("1f636", "无言以对"),
    ("1f612", "无语"),
    ("1f644", "翻白眼"),
    ("1f633", "脸红害羞"),
    ("1f97a", "求你了"),
    ("1f60f", "得意撇嘴"),
    # 难过 / 累
    ("1f614", "沮丧"),
    ("1f62b", "崩溃"),
    ("1f629", "累瘫"),
    ("1f971", "打哈欠"),
    ("1f622", "泪目"),
    ("1f62d", "大哭"),
    ("1f634", "睡着"),
    ("1f924", "流口水"),
    # 生气 / 凶
    ("1f624", "不服气"),
    ("1f620", "生气"),
    ("1f621", "大怒"),
    ("1f92c", "爆粗口"),
    ("1f608", "恶魔坏笑"),
    # 害怕 / 惊讶
    ("1f631", "吓到了"),
    ("1f628", "害怕"),
    ("1f632", "目瞪口呆"),
    # 生病
    ("1f637", "戴口罩"),
    ("1f912", "感冒"),
    ("1f922", "恶心"),
    ("1f92e", "呕吐"),
    ("1f975", "热死了"),
    ("1f976", "冷死了"),
    # 酷 / 搞怪 / 奇幻
    ("1f60e", "墨镜耍酷"),
    ("1f913", "书呆子"),
    ("1f921", "小丑"),
    ("1f480", "骷髅"),
    ("1f4a9", "便便"),
    ("1f47b", "幽灵"),
    ("1f916", "机器人"),
    # 手势
    ("1f44b", "挥手"),
    ("270c", "耶"),
    ("1f91e", "祈愿手指"),
    ("1f91f", "爱你手势"),
    ("1f918", "摇滚手势"),
    ("1f919", "打电话手势"),
    ("1f44d", "点赞"),
    ("1f44e", "点踩"),
    ("1f44a", "碰拳"),
    ("1f44f", "鼓掌"),
    ("1f64c", "高举双手"),
    ("1f91d", "握手"),
    ("1f64f", "合十拜托"),
    ("1f4aa", "肌肉加油"),
    ("1f937", "摊手耸肩"),
    ("1f926", "扶额"),
    # 爱心 / 符号
    ("2764", "红心"),
    ("1f494", "心碎"),
    ("1f498", "一箭穿心"),
    ("1f4af", "一百分"),
    ("2728", "闪亮"),
    ("2b50", "星星"),
    ("1f525", "火"),
    ("1f4a5", "爆炸"),
    ("1f389", "庆祝拉炮"),
    ("1f38a", "五彩纸屑"),
    ("1f4a4", "睡觉符号"),
    ("1f308", "彩虹"),
    # 动物
    ("1f436", "狗脸"),
    ("1f431", "猫脸"),
    ("1f43c", "熊猫"),
    ("1f430", "兔子"),
    ("1f42f", "老虎"),
    # 食物
    ("2615", "咖啡"),
    ("1f35a", "米饭"),
    ("1f35c", "面条"),
    ("1f370", "蛋糕"),
    ("1f37a", "啤酒"),
    ("1f9e7", "红包"),
    # 物件
    ("1f4f1", "手机"),
    ("1f381", "礼物"),
    ("2614", "雨伞"),
    ("1f319", "月亮"),
    ("1f4a1", "灵感灯泡"),
]

# 160px 正好匹配前端 .scenario-sticker 最大 160×160 的显示尺寸。
URL_TEMPLATES = (
    "https://cdn.jsdelivr.net/npm/emoji-datasource-apple@15/img/apple/160/{file}",
    "https://cdn.jsdelivr.net/gh/iamcal/emoji-data@master/img-apple-160/{file}",
    "https://raw.githubusercontent.com/iamcal/emoji-data/master/img-apple-160/{file}",
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 5 * 1024 * 1024
REQUEST_DELAY = 0.15


def candidate_files(codepoint: str) -> list[str]:
    base = codepoint.lower()
    return [f"{base}.png", f"{base}-fe0f.png"]


def fetch(codepoint: str) -> bytes | None:
    for file in candidate_files(codepoint):
        for template in URL_TEMPLATES:
            url = template.format(file=file)
            try:
                resp = requests.get(url, timeout=20, headers={"User-Agent": "scenario-sticker-import/1.0"})
            except requests.RequestException as exc:
                print(f"  请求失败 {url}: {exc.__class__.__name__}")
                continue
            if resp.status_code == 200 and resp.content.startswith(PNG_MAGIC):
                return resp.content
            if resp.status_code == 404:
                continue
            print(f"  异常响应 {url}: HTTP {resp.status_code}")
    return None


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    assert len(STICKERS) <= 100, "超过目录 100 个候选上限"
    assert len({name for _, name in STICKERS}) == len(STICKERS), "中文名有重复"

    target_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    ok = skipped = failed = 0
    failures: list[str] = []
    for index, (codepoint, name) in enumerate(STICKERS, 1):
        dest = target_dir / f"{name}.png"
        if dest.exists():
            skipped += 1
            continue
        data = fetch(codepoint)
        if data is None or len(data) > MAX_BYTES:
            failed += 1
            failures.append(f"{name}({codepoint})")
            print(f"[{index}/{len(STICKERS)}] 失败：{name} ({codepoint})")
            continue
        dest.write_bytes(data)
        ok += 1
        print(f"[{index}/{len(STICKERS)}] 已下载：{name} <- {codepoint}")
        time.sleep(REQUEST_DELAY)

    print(f"\n完成：新增 {ok}，已存在跳过 {skipped}，失败 {failed}")
    if failures:
        print("失败清单：", "、".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
