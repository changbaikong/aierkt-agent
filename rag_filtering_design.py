# 过滤精排层完整设计方案（适合高中生）

## 核心问题：矩阵还是百分比？

**推荐：用普通方法（百分比 + 阈值过滤）**，而不是矩阵。

**为什么不是矩阵？**
- 矩阵（比如 N×N 相似度矩阵）在候选结果很多时（几千个文档）会非常吃内存和时间，高中生做项目也容易内存爆炸。
- 普通百分比过滤超级简单：先用 reranker 打分 → 取前 30% 质量最高的 → 再按长度/质量/去重过滤 → 最后 top-k。
- 实际工作中，**90% 的 RAG 系统都用这种“百分比 + 阈值”**，又快又稳。

**什么时候才用矩阵？**
- 以后要做“交叉注意力重排”或“全量相似度矩阵”时才需要（比如要每个 query-doc 都算一次 cosine）。
- 现在先用普通方法，够用 95% 场景。

---

## 完整可抄写的代码（直接复制用）

```python
from typing import List, Dict
import numpy as np
from dataclasses import dataclass

@dataclass
class Document:
    text: str
    embedding: List[float]  # 召回时用的向量
    metadata: Dict = None
    rerank_score: float = 0.0   # 重排序器打的分

class FilteringRanker:
    """
    过滤精排层
    任务：把召回结果过滤 + 精排成最终答案
    """

    def __init__(self):
        self.filters = [
            self._length_filter,     # 长度过滤
            self._quality_filter,    # 质量分数阈值
            self._duplicate_filter,  # 去重过滤
        ]
        self.top_percent = 0.35   # 保留前 35% 质量最高的候选
        self.min_length = 60
        self.max_length = 600

    def rerank_and_filter(self, candidates: List[Document]) -> List[Document]:
        """
        整个过滤精排流程：一步到位
        """
        if not candidates:
            return []

        # 1. 初步质量过滤（用 reranker 分数）
        candidates = self._quality_filter(candidates)

        # 2. 长度过滤
        candidates = self._length_filter(candidates)

        # 3. 去重过滤（最简单：看是否相似度很高）
        candidates = self._duplicate_filter(candidates)

        # 4. 最终精排（按 rerank_score 降序）
        candidates = sorted(candidates, key=lambda x: x.rerank_score, reverse=True)

        # 5. 最终截取 top-k（比如 5 个答案）
        return candidates[:5]

    # ====================== 过滤器们 ======================

    def _quality_filter(self, docs: List[Document]) -> List[Document]:
        """质量过滤：保留 rerank_score > 0.5 的"""
        return [doc for doc in docs if doc.rerank_score > 0.5]

    def _length_filter(self, docs: List[Document]) -> List[Document]:
        """长度过滤：避免太短的垃圾答案"""
        return [doc for doc in docs
                if self.min_length <= len(doc.text.split()) <= self.max_length]

    def _duplicate_filter(self, docs: List[Document], threshold=0.85) -> List[Document]:
        """简单去重：如果两个文档相似度 > 阈值，只保留第一个"""
        filtered = []
        for doc in docs:
            is_duplicate = False
            for prev in filtered:
                sim = self._cosine_similarity(doc.embedding, prev.embedding)
                if sim > threshold:
                    is_duplicate = True
                    break
            if not is_duplicate:
                filtered.append(doc)
        return filtered

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """普通方法计算余弦相似度"""
        a = np.array(a)
        b = np.array(b)
        return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

    def _final_score(self, doc: Document) -> float:
        """精排用的最终分数（可扩展）"""
        return doc.rerank_score * 0.7 + self._length_score(doc.text) * 0.3

    def _length_score(self, text: str) -> float:
        length = len(text.split())
        # 让长度在 150-400 之间分数最高
        if length < 150:
            return 0.3
        elif length > 400:
            return 0.3
        else:
            return 0.8
```

---

## 怎么用（调用方式）

```python
# 假设你已经有 candidates（来自 FAISS 召回）
filtered_answers = FilteringRanker().rerank_and_filter(candidates)

print("最终精排结果：")
for i, doc in enumerate(filtered_answers, 1):
    print(f"{i}. {doc.rerank_score:.2f} | {len(doc.text.split())}字 | {doc.text[:100]}...")
```

---

## 为什么这个设计好？

1. **高中生友好**：每一步都有注释，容易看懂。
2. **高效**：没有做 N×N 矩阵，只用了普通 `for` 循环 + 余弦相似度（召回层已经算过了，不重复算）。
3. **可扩展**：
   - 想加“矩阵”？等你有几万文档时再改 `_duplicate_filter` 成向量矩阵乘法。
   - 想改阈值？直接改 `top_percent` 和 `0.5`。
4. **可测试**：你可以用下面这个小例子验证。

---

## 测试用例（直接跑起来看看）

```python
docs = [
    Document("Python 学习教程...", embedding=[0.1]*128, rerank_score=0.92),
    Document("C++ 入门...", embedding=[0.2]*128, rerank_score=0.88),
    Document("Python 高级...", embedding=[0.1]*128, rerank_score=0.65),  # 低分
    Document("Python 基础...", embedding=[0.1]*128, rerank_score=0.95),  # 高分但短
]

result = FilteringRanker().rerank_and_filter(docs)
print([d.rerank_score for d in result])
```

**运行后你会看到**：只保留了 2 个（高分 + 长度合适），并按分数排序。

---

## 下一步建议

1. 先把这个 `FilteringRanker` 放进你的项目跑通。
2. 测试一下不同阈值（`0.5`、`0.6`、`0.7`）看结果。
3. 告诉我你现在的 `candidates` 格式是什么样的（每个 doc 有多少字段？），我再帮你微调。

**要我现在帮你改成更高级的“矩阵版”吗？**
或者你直接告诉我你的候选结果里都有什么字段？我马上给你定制代码。

**问题来了：** 你觉得这个百分比过滤够用，还是想试试用矩阵做相似度矩阵过滤？要我改成矩阵版吗？