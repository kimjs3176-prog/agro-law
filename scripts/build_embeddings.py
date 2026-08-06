"""내규 조문 임베딩 생성 → 리포지토리에 커밋할 벡터 파일 출력.

조문 단위로 임베딩해 int8 로 양자화하고, 벡터(.bin)와 메타데이터(.json)를 만든다.
코퍼스가 작아(수천 청크) 벡터DB 없이 파일 + 코사인 계산으로 충분하다.

사용법:
    export GEMINI_API_KEY=...           # 또는 --api-key
    python scripts/build_embeddings.py                 # 전체 생성
    python scripts/build_embeddings.py --slug 인사규정   # 특정 규정만 갱신
    python scripts/build_embeddings.py --dry-run       # 대상만 확인

출력:
    regulations_vectors.bin    int8 양자화 벡터 (N × D)
    regulations_vectors.json   {model, dim, count, scale, chunks:[{slug,title,no,...}]}
"""
from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reg_chunks as rc  # noqa: E402

try:
    import requests
except ImportError:
    sys.exit("requests 가 필요합니다: pip install requests")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_PATH = os.path.join(BASE, "regulations_vectors.bin")
META_PATH = os.path.join(BASE, "regulations_vectors.json")

DEFAULT_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-004")
API = "https://generativelanguage.googleapis.com/v1beta/models"
BATCH = 50          # Gemini batchEmbedContents 요청당 청크 수


def embed_batch(texts, api_key: str, model: str, task: str = "RETRIEVAL_DOCUMENT"):
    """텍스트 배치 → 임베딩 벡터 목록."""
    url = f"{API}/{model}:batchEmbedContents?key={api_key}"
    body = {"requests": [{"model": f"models/{model}",
                          "content": {"parts": [{"text": t}]},
                          "taskType": task} for t in texts]}
    last = None
    for attempt in range(4):
        try:
            r = requests.post(url, json=body, timeout=120)
            if r.status_code == 200:
                return [e["values"] for e in r.json()["embeddings"]]
            last = f"{r.status_code} {r.text[:200]}"
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            break
        except Exception as e:                       # 네트워크 오류 재시도
            last = str(e)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"임베딩 실패: {last}")


def quantize(vec):
    """단위벡터 → int8. 복원 시 scale 을 곱해 되돌린다."""
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [max(-127, min(127, int(round(v / norm * 127)))) for v in vec]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY", ""))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--slug", action="append", help="특정 규정만 갱신(반복 지정 가능)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-boilerplate", action="store_true",
                    help="부칙·경과조치 등 상용구도 포함(기본: 제외)")
    args = ap.parse_args()

    chunks = [c for c in rc.iter_chunks(slugs=args.slug)
              if args.include_boilerplate or not c["boiler"]]
    print(f"대상 청크: {len(chunks):,}개"
          + (f" (규정 {len(args.slug)}건 한정)" if args.slug else " (전체)"))
    if args.dry_run:
        for c in chunks[:5]:
            print("  ", c["text"][:80])
        print("  ... --dry-run 이므로 생성하지 않았습니다.")
        return
    if not chunks:
        print("생성할 청크가 없습니다."); return
    if not args.api_key:
        sys.exit("GEMINI_API_KEY 를 설정하거나 --api-key 로 전달하세요.")

    # 부분 갱신: 기존 벡터를 읽어 해당 slug 만 교체
    old_meta, old_vecs, dim = None, [], None
    if args.slug and os.path.exists(META_PATH) and os.path.exists(BIN_PATH):
        old_meta = json.load(open(META_PATH, encoding="utf-8"))
        dim = old_meta["dim"]
        raw = open(BIN_PATH, "rb").read()
        old_vecs = [raw[i * dim:(i + 1) * dim] for i in range(old_meta["count"])]
        print(f"기존 벡터 {old_meta['count']:,}개 로드 (dim={dim})")

    new_meta, new_vecs = [], []
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i:i + BATCH]
        vecs = embed_batch([c["text"] for c in batch], args.api_key, args.model)
        if dim is None:
            dim = len(vecs[0])
        for c, v in zip(batch, vecs):
            if len(v) != dim:
                sys.exit(f"차원 불일치: {len(v)} != {dim} — 모델을 확인하세요.")
            new_vecs.append(bytes((x & 0xFF) for x in quantize(v)))
            new_meta.append({"slug": c["slug"], "title": c["title"], "no": c["no"],
                             "art_title": c["art_title"],
                             "preview": c["text"][:240]})
        print(f"  {min(i + BATCH, len(chunks)):>5,}/{len(chunks):,} 임베딩 완료", flush=True)

    if old_meta:                                   # 부분 갱신 병합
        keep = [(m, v) for m, v in zip(old_meta["chunks"], old_vecs)
                if m["slug"] not in set(args.slug)]
        merged = keep + list(zip(new_meta, new_vecs))
        new_meta = [m for m, _ in merged]
        new_vecs = [v for _, v in merged]

    with open(BIN_PATH, "wb") as f:
        for v in new_vecs:
            f.write(v)
    meta = {"model": args.model, "dim": dim, "count": len(new_meta),
            "scale": 1.0 / 127.0, "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "chunks": new_meta}
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    mb = os.path.getsize(BIN_PATH) / 1024 / 1024
    print(f"\n완료: {len(new_meta):,}청크 × {dim}차원 → {mb:.1f}MB")
    print(f"  {BIN_PATH}")
    print(f"  {META_PATH}")
    print("\n두 파일을 커밋하면 배포본에서 의미 검색이 활성화됩니다.")


if __name__ == "__main__":
    main()
