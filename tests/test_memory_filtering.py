"""测试过滤精排层"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from datetime import datetime, timedelta
from agent_core.memory_filtering import FilteringRanker


def test_time_decay():
    """测试时间衰减（EMA）"""
    ranker = FilteringRanker(
        half_life_days=180,
        min_length=10,
        min_score_threshold=0.01  # 放宽阈值，让衰减后的旧记忆也能通过
    )

    # 创建两个候选：一个新，一个旧
    now = datetime.now()
    candidates = [
        {
            "id": "1",
            "content": "Python 2 使用 file() 函数读取文件，这是旧版本的做法，现在已经不推荐使用了。",
            "score": 0.9,  # 分数很高
            "created_at": (now - timedelta(days=365)).isoformat(),  # 1年前（更合理的测试数据）
            "embedding": [1.0] + [0.0] * 127,  # 独特的 embedding
        },
        {
            "id": "2",
            "content": "Python 3 使用 open() 函数读取文件，这是标准做法，推荐使用。",
            "score": 0.75,  # 分数稍低
            "created_at": now.isoformat(),  # 但很新
            "embedding": [0.0] * 64 + [1.0] + [0.0] * 63,  # 完全不同的 embedding
        },
    ]

    result = ranker.filter_and_rerank(candidates)

    # 新答案应该排在前面（因为时间衰减）
    assert len(result) == 2, f"应该有2个结果，实际有{len(result)}个"
    assert result[0]["id"] == "2", "新答案应该排在前面"
    assert result[1]["id"] == "1", "旧答案应该被降权但仍保留"

    # 检查衰减后的分数
    assert result[0]["adjusted_score"] > result[1]["adjusted_score"], \
        f"新答案分数({result[0]['adjusted_score']:.3f})应该高于旧答案({result[1]['adjusted_score']:.3f})"


def test_quality_filter():
    """测试质量过滤"""
    ranker = FilteringRanker(min_score_threshold=0.5, min_length=10)

    candidates = [
        {
            "id": "1",
            "content": "高质量答案：这是一个详细的解答，包含了所有必要的信息。",
            "score": 0.9,
            "created_at": datetime.now().isoformat(),
            "embedding": [0.1] * 128,
        },
        {
            "id": "2",
            "content": "低质量答案：这个回答不够详细，信息不完整。",
            "score": 0.3,  # 低于阈值
            "created_at": datetime.now().isoformat(),
            "embedding": [0.2] * 128,
        },
    ]

    result = ranker.filter_and_rerank(candidates)

    # 低分的应该被过滤掉
    assert len(result) == 1
    assert result[0]["id"] == "1"


def test_length_filter():
    """测试长度过滤"""
    ranker = FilteringRanker(min_length=40, max_length=800)

    candidates = [
        {
            "id": "1",
            "content": "太短了",  # 太短
            "score": 0.9,
            "created_at": datetime.now().isoformat(),
            "embedding": [0.1] * 128,
        },
        {
            "id": "2",
            "content": "这是一个长度合适的答案，足够长但不会太长。" * 5,  # 合适
            "score": 0.8,
            "created_at": datetime.now().isoformat(),
            "embedding": [0.2] * 128,
        },
        {
            "id": "3",
            "content": "这是一个超级长的答案，太长了用户根本看不完。" * 100,  # 太长
            "score": 0.85,
            "created_at": datetime.now().isoformat(),
            "embedding": [0.3] * 128,
        },
    ]

    result = ranker.filter_and_rerank(candidates)

    # 只有长度合适的应该被保留
    assert len(result) == 1
    assert result[0]["id"] == "2"


def test_duplicate_filter():
    """测试去重过滤"""
    ranker = FilteringRanker(
        duplicate_threshold=0.99,  # 提高阈值，只有几乎完全相同的才算重复
        min_length=10
    )

    # 两个内容几乎一样的候选（embedding 很接近）
    candidates = [
        {
            "id": "1",
            "content": "Python 使用 open() 函数读取文件，这是标准做法。",
            "score": 0.9,
            "created_at": datetime.now().isoformat(),
            "embedding": [0.1] * 128,  # 相同的 embedding
        },
        {
            "id": "2",
            "content": "Python 使用 open() 函数读取文件，这是推荐做法。",
            "score": 0.85,
            "created_at": datetime.now().isoformat(),
            "embedding": [0.1] * 128,  # 相同的 embedding（完全一样）
        },
        {
            "id": "3",
            "content": "C++ 使用 fstream 类来读取文件，这是标准库提供的方法。",
            "score": 0.8,
            "created_at": datetime.now().isoformat(),
            "embedding": [0.0] * 64 + [1.0] + [0.0] * 63,  # 完全不同的 embedding
        },
    ]

    result = ranker.filter_and_rerank(candidates)

    # 两个重复的只保留分数更高的
    assert len(result) == 2, f"应该有2个结果（去掉1个重复），实际有{len(result)}个"
    assert result[0]["id"] == "1", "分数高的保留"
    # 第二个可能是 id=3，因为 id=2 被去重了
    remaining_ids = [r["id"] for r in result]
    assert "1" in remaining_ids and "3" in remaining_ids, f"应该保留 id=1 和 id=3，实际: {remaining_ids}"
    assert result[0]["id"] == "1"  # 分数高的保留
    assert result[1]["id"] == "3"  # 不重复的保留


def test_full_pipeline():
    """测试完整流程"""
    ranker = FilteringRanker(
        half_life_days=180,
        min_score_threshold=0.3,
        min_length=40,
        max_length=800,
        duplicate_threshold=0.99,  # 提高阈值，只去除完全重复的
    )

    now = datetime.now()
    candidates = [
        {
            "id": "1",
            "content": "Python 用 open() 读文件的完整教程：首先导入 os 模块，然后使用 with 语句确保文件正确关闭。" * 3,
            "score": 0.92,
            "created_at": now.isoformat(),
            "embedding": [0.1] * 128,
        },
        {
            "id": "2",
            "content": "短",  # 太短
            "score": 0.88,
            "created_at": now.isoformat(),
            "embedding": [0.0, 0.15] + [0.0] * 126,  # 独特的 embedding
        },
        {
            "id": "3",
            "content": "Python 用 open() 读文件的完整教程：首先导入 os 模块，然后使用 with 语句确保文件正确关闭。" * 3,
            "score": 0.87,
            "created_at": now.isoformat(),
            "embedding": [0.1] * 128,  # 和第 1 个重复
        },
        {
            "id": "4",
            "content": "C++ 用 fstream 读文件，这需要包含 fstream 头文件，然后创建 ifstream 对象进行读取。" * 3,
            "score": 0.25,  # 分数太低
            "created_at": now.isoformat(),
            "embedding": [0.0] * 32 + [0.9] + [0.0] * 95,  # 独特的 embedding
        },
        {
            "id": "5",
            "content": "用 with open() as f: content = f.read() 这样读文件最安全，可以自动处理异常和关闭。" * 3,
            "score": 0.89,
            "created_at": now.isoformat(),
            "embedding": [0.0] * 64 + [0.2] + [0.0] * 63,  # 独特的 embedding
        },
    ]

    result = ranker.filter_and_rerank(candidates)

    # 应该只剩下 2 个（1 和 5）
    # id=2 被长度过滤
    # id=3 被去重过滤（和 id=1 重复）
    # id=4 被质量过滤（分数太低）
    assert len(result) == 2, f"应该有2个结果，实际有{len(result)}个"
    result_ids = [r["id"] for r in result]
    assert "1" in result_ids, "id=1 应该被保留"
    assert "5" in result_ids, "id=5 应该被保留"
    assert result[0]["id"] == "1", "id=1 分数最高，应该排第一"


def test_dynamic_threshold():
    """测试动态阈值（如果过滤后太少，自动放宽）"""
    ranker = FilteringRanker(min_score_threshold=0.8)  # 很高的阈值

    # 所有候选分数都不高
    candidates = [
        {
            "id": "1",
            "content": "答案 1" * 20,
            "score": 0.5,
            "created_at": datetime.now().isoformat(),
            "embedding": [0.1] * 128,
        },
        {
            "id": "2",
            "content": "答案 2" * 20,
            "score": 0.4,
            "created_at": datetime.now().isoformat(),
            "embedding": [0.2] * 128,
        },
    ]

    result = ranker.filter_and_rerank(candidates)

    # 动态阈值应该放宽，至少保留一些候选
    assert len(result) > 0


if __name__ == "__main__":
    # 手动运行测试
    print("测试 1：时间衰减")
    test_time_decay()
    print("✅ 通过\n")

    print("测试 2：质量过滤")
    test_quality_filter()
    print("✅ 通过\n")

    print("测试 3：长度过滤")
    test_length_filter()
    print("✅ 通过\n")

    print("测试 4：去重过滤")
    test_duplicate_filter()
    print("✅ 通过\n")

    print("测试 5：完整流程")
    test_full_pipeline()
    print("✅ 通过\n")

    print("测试 6：动态阈值")
    test_dynamic_threshold()
    print("✅ 通过\n")

    print("🎉 所有测试通过！")
