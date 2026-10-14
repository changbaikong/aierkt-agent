"""
过滤精排层完整示例

演示如何在 Agent Loop 中使用过滤精排层
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timedelta
from agent_core.memory_filtering import FilteringRanker


def example_basic_usage():
    """基础用法示例"""
    print("=" * 60)
    print("示例 1：基础用法")
    print("=" * 60)

    # 创建过滤精排器
    ranker = FilteringRanker(
        half_life_days=180,        # 半衰期 180 天
        min_score_threshold=0.3,   # 最低分数阈值
        min_length=40,             # 最短 40 字符
        max_length=800,            # 最长 800 字符
        duplicate_threshold=0.85,  # 去重阈值
    )

    # 模拟召回层返回的候选记忆
    now = datetime.now()
    candidates = [
        {
            "id": "1",
            "content": "Python 用 open() 函数读取文件，例如 f = open('data.txt', 'r')，然后用 f.read() 读取全部内容。" * 3,
            "score": 0.92,
            "created_at": now.isoformat(),
            "embedding": [0.1] * 128,
        },
        {
            "id": "2",
            "content": "读文件",  # 太短，会被过滤
            "score": 0.88,
            "created_at": now.isoformat(),
            "embedding": [0.15] * 128,
        },
        {
            "id": "3",
            "content": "Python 文件操作包括读取、写入、追加..." * 100,  # 太长，会被过滤
            "score": 0.75,
            "created_at": now.isoformat(),
            "embedding": [0.12] * 128,
        },
        {
            "id": "4",
            "content": "用 with open() as f: content = f.read() 这样读文件最安全，因为会自动关闭文件。" * 3,
            "score": 0.89,
            "created_at": now.isoformat(),
            "embedding": [0.11] * 128,
        },
        {
            "id": "5",
            "content": "Python 用 open() 函数读取文件，例如 f = open('data.txt', 'r')，然后用 f.read() 读取全部内容。" * 3,
            "score": 0.87,
            "created_at": now.isoformat(),
            "embedding": [0.1] * 128,  # 和第 1 个重复
        },
        {
            "id": "6",
            "content": "C++ 用 fstream 读文件" * 20,
            "score": 0.25,  # 分数太低，会被过滤
            "created_at": now.isoformat(),
            "embedding": [0.9] * 128,
        },
    ]

    print(f"原始候选数量：{len(candidates)}")

    # 过滤精排
    result = ranker.filter_and_rerank(candidates)

    print(f"过滤后数量：{len(result)}\n")

    print("最终结果：")
    for i, item in enumerate(result, 1):
        print(f"{i}. ID={item['id']} 分数={item['adjusted_score']:.3f} 内容={item['content'][:50]}...")

    print("\n")


def example_time_decay():
    """时间衰减示例"""
    print("=" * 60)
    print("示例 2：时间衰减效果")
    print("=" * 60)

    ranker = FilteringRanker(half_life_days=180)

    now = datetime.now()
    candidates = [
        {
            "id": "old",
            "content": "Python 2 用 file() 函数读文件（旧版本，已过时）" * 5,
            "score": 0.95,  # 分数很高
            "created_at": (now - timedelta(days=1000)).isoformat(),  # 但很旧
            "embedding": [0.1] * 128,
        },
        {
            "id": "new",
            "content": "Python 3 用 open() 函数读文件（新版本，推荐使用）" * 5,
            "score": 0.75,  # 分数稍低
            "created_at": now.isoformat(),  # 但很新
            "embedding": [0.2] * 128,
        },
    ]

    print("原始候选：")
    for c in candidates:
        age_days = (now - datetime.fromisoformat(c["created_at"])).days
        print(f"  {c['id']}: 原始分数={c['score']:.2f}, 年龄={age_days}天")

    result = ranker.filter_and_rerank(candidates)

    print("\n过滤精排后：")
    for i, item in enumerate(result, 1):
        print(f"  {i}. {item['id']}: 衰减后分数={item['adjusted_score']:.3f}")

    print("\n💡 时间衰减让旧答案降权，新答案排在前面！\n")


def example_duplicate_removal():
    """去重示例"""
    print("=" * 60)
    print("示例 3：去重效果")
    print("=" * 60)

    ranker = FilteringRanker(duplicate_threshold=0.90)

    now = datetime.now()
    candidates = [
        {
            "id": "1",
            "content": "Python 用 open() 读文件的教程" * 10,
            "score": 0.92,
            "created_at": now.isoformat(),
            "embedding": [0.1] * 128,  # 相同的 embedding
        },
        {
            "id": "2",
            "content": "Python 用 open() 读文件的教程（重复）" * 10,
            "score": 0.85,
            "created_at": now.isoformat(),
            "embedding": [0.1] * 128,  # 相同的 embedding（重复！）
        },
        {
            "id": "3",
            "content": "JavaScript 用 fs.readFile() 读文件" * 10,
            "score": 0.88,
            "created_at": now.isoformat(),
            "embedding": [0.9] * 128,  # 不同的 embedding
        },
    ]

    print(f"原始候选数量：{len(candidates)}")

    result = ranker.filter_and_rerank(candidates)

    print(f"去重后数量：{len(result)}")
    print("\n保留的候选：")
    for i, item in enumerate(result, 1):
        print(f"  {i}. ID={item['id']} 分数={item['adjusted_score']:.3f}")

    print("\n💡 重复的候选被过滤掉，只保留分数更高的那个！\n")


def example_integration_with_agent():
    """集成到 Agent 的完整示例"""
    print("=" * 60)
    print("示例 4：集成到 Agent Loop")
    print("=" * 60)

    print("""
在 Agent Loop 中的工作流程：

用户提问："Python 怎么读文件？"
    ↓
【召回层】从记忆库（FAISS）召回 100 个候选
    ↓
【重排序层】用 RRF 融合 FTS + 向量检索，取前 30 个
    ↓
【过滤精排层】👈 就是我们刚实现的这一层！
    ├─ 时间衰减：旧记忆降权
    ├─ 质量过滤：扔掉低分的
    ├─ 长度过滤：扔掉太短/太长的
    ├─ 去重过滤：扔掉重复的
    └─ 精排：按最终分数排序
    ↓
【组装上下文】把过滤后的记忆拼成上下文
    ↓
【发给 LLM】生成最终答案
""")

    print("代码集成位置：")
    print("""
# 在 agent_core/memory_retrieval.py 的 retrieve() 方法中：

def retrieve(self, scope: Scope, query: str, ...) -> RetrievalResult:
    # 1. 召回 warm 层候选
    warm_candidates = self._hybrid_candidates(scope, query, layer="warm")

    # 2. 🔥 过滤精排层
    warm_filtered = self.filtering_ranker.filter_and_rerank(warm_candidates)

    # 3. 按类型分组
    confirmed = [item for item in warm_filtered if ...]
    other_warm = [item for item in warm_filtered if ...]

    # 4. 同样处理 cold 层
    cold_candidates = self._hybrid_candidates(scope, query, layer="cold")
    cold = self.filtering_ranker.filter_and_rerank(cold_candidates)

    # 5. 返回结果
    return RetrievalResult(hot=hot, warm=warm, cold_evidence=cold, ...)
""")

    print("\n💡 现在你的 Agent 有完整的三层 RAG 架构了！\n")


if __name__ == "__main__":
    example_basic_usage()
    example_time_decay()
    example_duplicate_removal()
    example_integration_with_agent()

    print("=" * 60)
    print("🎉 示例运行完毕！")
    print("=" * 60)
    print("\n下一步：")
    print("1. 运行测试：python tests/test_memory_filtering.py")
    print("2. 调整参数：修改 FilteringRanker 的参数来优化效果")
    print("3. 监控日志：看 agent_core.memory.filtering 的日志输出")
