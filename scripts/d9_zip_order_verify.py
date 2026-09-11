"""
D9 验证：embed 的返回顺序是否严格对应输入顺序
==============================================
目的：证明 add_documents 里 zip(chunks, vectors) 的配对是安全的。

不 import app.config（避免读 .env），直接验证 fastembed 本身的行为——
因为 embedding_service._embed_sync 只是：
    [vec.tolist() for vec in self._model.embed(texts)]
列表推导式天然保持产出顺序，所以只要 fastembed 有序，整条链路就有序。

跑法：uv run python scripts/d9_zip_order_verify.py
"""

from pathlib import Path

import numpy as np
from fastembed import TextEmbedding

CACHE_DIR = Path(__file__).resolve().parents[1] / "models"
TEXTS = ["我今天想吃火锅", "股票市场大跌", "小猫在睡觉"]

model = TextEmbedding(model_name="BAAI/bge-small-zh-v1.5", cache_dir=str(CACHE_DIR))


def embed(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in model.embed(texts)]


def main() -> None:
    batch = embed(TEXTS)
    print(f"输入 {len(TEXTS)} 条 -> 返回 {len(batch)} 个向量，维度 {len(batch[0])}\n")

    print("=== 1. 批量第 i 位 是否等于 单独算第 i 条 ===")
    for i, text in enumerate(TEXTS):
        single = embed([text])[0]
        same = np.allclose(batch[i], single, atol=1e-6)
        print(f"  [{i}] {text:8s} -> {'一致' if same else '不一致'}")

    print("\n=== 2. 打乱输入顺序，向量是否跟着文本走 ===")
    shuffled = [TEXTS[2], TEXTS[0], TEXTS[1]]
    batch_shuffled = embed(shuffled)
    for new_pos, text in enumerate(shuffled):
        old_pos = TEXTS.index(text)
        same = np.allclose(batch_shuffled[new_pos], batch[old_pos], atol=1e-6)
        print(
            f"  「{text}」原本第 {old_pos} 位 -> 打乱后第 {new_pos} 位："
            f"{'向量跟着文本走' if same else '向量跟丢了（顺序被打乱）'}"
        )

    print("\n=== 3. 语义校验（向量确实代表各自文本，不是复制粘贴） ===")
    for i in range(len(TEXTS)):
        for j in range(i + 1, len(TEXTS)):
            cos = float(np.dot(batch[i], batch[j]))
            print(f"  「{TEXTS[i]}」vs「{TEXTS[j]}」余弦相似度 = {cos:+.3f}")


if __name__ == "__main__":
    main()
