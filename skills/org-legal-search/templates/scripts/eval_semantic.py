"""의미 검색 품질 점검 — 자연어 질의로 정답 규정이 상위에 오는지 확인.

    export GEMINI_API_KEY=...
    python scripts/eval_semantic.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api_server as S  # noqa: E402

# (자연어 질의, 정답으로 인정할 규정명 조각들)
CASES = [
    ("출장 갔다가 밤늦게 돌아오면 수당을 더 받을 수 있나요?", ["여비", "복무"]),
    ("아이를 낳으면 회사에서 며칠 쉴 수 있어?", ["복무", "인사"]),
    ("연구비를 잘못 썼을 때 어떤 처분을 받나요?", ["연구", "회계", "감사"]),
    ("퇴직할 때 받는 돈은 어떻게 계산해?", ["퇴직", "보수"]),
    ("기술을 기업에 넘길 때 얼마를 받아야 하나요?", ["기술이전", "기술평가"]),
    ("직원이 다른 회사 일을 겸직해도 되나요?", ["복무", "인사"]),
]


def main():
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        sys.exit("GEMINI_API_KEY 를 설정하세요.")
    c = S._vec_load()
    if c["mat"] is None:
        sys.exit("벡터 인덱스가 없습니다. scripts/build_embeddings.py 를 먼저 실행하세요.")
    print(f"인덱스: {c['meta']['count']:,}청크 × {c['meta']['dim']}차원 "
          f"({c['meta'].get('model')})\n")

    ok = 0
    for q, want in CASES:
        hits = S.semantic_search(q, key, top_k=8)
        if not hits:
            print(f"✗ {q}\n    (결과 없음)\n")
            continue
        top = [h["title"] for h in hits]
        rank = next((i + 1 for i, t in enumerate(top)
                     if any(w in t for w in want)), 0)
        ok += 1 if rank and rank <= 3 else 0
        mark = "✓" if rank and rank <= 3 else ("~" if rank else "✗")
        print(f"{mark} {q}   → 정답순위 {rank or '없음'}")
        for h in hits[:4]:
            print(f"      {h['score']:.3f}  {h['title']} 제{h['no']}조"
                  f"({h['art_title']})")
        print()
    print(f"상위 3위 안 정답: {ok}/{len(CASES)}")


if __name__ == "__main__":
    main()
