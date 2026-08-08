"""내규 조문 임베딩 생성 → 리포지토리에 커밋할 벡터 파일 출력.

조문 단위로 임베딩해 int8 로 양자화하고, 벡터(.bin)와 메타데이터(.json)를 만든다.
코퍼스가 작아(수천 청크) 벡터DB 없이 파일 + 코사인 계산으로 충분하다.

**출력 파일 자체가 체크포인트다.** 이미 임베딩된 조문은 다시 만들지 않으므로,
무료 등급 일일 쿼터(embed_content_free_tier_requests, 1일 1,000건)에 걸려 중단돼도
다음 날 같은 명령을 다시 실행하면 남은 조문만 이어서 생성한다.
중간 결과도 주기적으로 저장되므로 부분 인덱스만으로도 검색이 동작한다.

사용법:
    export GEMINI_API_KEY=...           # 또는 --api-key
    python scripts/build_embeddings.py                 # 전체(미완료분만) 생성
    python scripts/build_embeddings.py --slug 인사규정   # 특정 규정만 강제 재생성
    python scripts/build_embeddings.py --dry-run       # 대상만 확인
    python scripts/build_embeddings.py --restart       # 기존 벡터 무시하고 처음부터

출력:
    regulations_vectors.bin    int8 양자화 벡터 (N × D)
    regulations_vectors.json   {model, dim, count, scale, complete, chunks:[...]}
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
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

DEFAULT_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-001")
# 저장 용량·검색 속도를 위해 차원 축소(MRL). 3072→768 로도 검색 품질 손실이 작다.
DEFAULT_DIM = int(os.environ.get("EMBED_DIM", "768"))
API = "https://generativelanguage.googleapis.com/v1beta/models"
BATCH = int(os.environ.get("EMBED_BATCH", "20"))     # 요청당 청크 수
SLEEP = float(os.environ.get("EMBED_SLEEP", "0.3"))  # 배치 사이 대기
RETRIES = int(os.environ.get("EMBED_RETRIES", "6"))  # 429 재시도 횟수
FLUSH_EVERY = int(os.environ.get("EMBED_FLUSH", "5"))  # 몇 배치마다 파일에 저장할지

# 429 응답의 "Please retry in 15.4s" 안내
_RETRY_AFTER = re.compile(r"retry in ([\d.]+)s")


class QuotaExhausted(RuntimeError):
    """일일 쿼터 소진 — 지금까지 만든 벡터를 저장하고 종료한다."""


def _retry_after(text: str):
    m = _RETRY_AFTER.search(text or "")
    return float(m.group(1)) if m else None


def embed_batch(texts, api_key: str, model: str, task: str = "RETRIEVAL_DOCUMENT",
                dim: int = DEFAULT_DIM):
    """텍스트 배치 → 임베딩 벡터 목록."""
    url = f"{API}/{model}:batchEmbedContents?key={api_key}"
    req = {"model": f"models/{model}", "taskType": task}
    if dim:
        req["outputDimensionality"] = dim
    body = {"requests": [dict(req, content={"parts": [{"text": t}]}) for t in texts]}
    last = None
    for attempt in range(RETRIES):
        try:
            r = requests.post(url, json=body, timeout=120)
            if r.status_code == 200:
                return [e["values"] for e in r.json()["embeddings"]]
            last = f"{r.status_code} {r.text[:400]}"
            if r.status_code == 429:
                if attempt == RETRIES - 1:
                    raise QuotaExhausted(last)
                wait = _retry_after(r.text) or min(60.0, 10 * 2 ** attempt)
                print(f"    쿼터 대기 {wait:.0f}초 ({attempt + 1}/{RETRIES})", flush=True)
                time.sleep(wait + 2)
                continue
            if r.status_code in (500, 502, 503, 504):
                time.sleep(2 ** min(attempt, 5))
                continue
            break
        except QuotaExhausted:
            raise
        except Exception as e:                       # 네트워크 오류 재시도
            last = str(e)
            time.sleep(2 ** min(attempt, 5))
    raise RuntimeError(f"임베딩 실패: {last}")


def quantize(vec):
    """단위벡터 → int8. 복원 시 scale 을 곱해 되돌린다."""
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [max(-127, min(127, int(round(v / norm * 127)))) for v in vec]


def chunk_key(c, seen) -> str:
    """청크의 안정적인 식별자. 같은 (규정, 조번호)가 겹치면 순번을 붙인다."""
    base = f"{c['slug']}|{c['no']}|{c['art_title']}"
    n = seen.get(base, 0)
    seen[base] = n + 1
    return base if n == 0 else f"{base}#{n}"


def load_existing(model: str, dim: int):
    """이미 만들어 둔 벡터 → {key: bytes}. 모델·차원이 다르면 버린다."""
    if not (os.path.exists(META_PATH) and os.path.exists(BIN_PATH)):
        return {}, None
    try:
        with open(META_PATH, encoding="utf-8") as f:
            meta = json.load(f)
        d = int(meta["dim"])
        if meta.get("model") != model or (dim and d != dim):
            print(f"기존 벡터는 {meta.get('model')}/{d}차원 — 설정이 달라 무시합니다.")
            return {}, None
        raw = open(BIN_PATH, "rb").read()
        if len(raw) != d * int(meta["count"]):
            print("기존 벡터 파일 크기가 메타와 달라 무시합니다.")
            return {}, None
        out = {}
        for i, m in enumerate(meta["chunks"]):
            k = m.get("k")
            if k:
                out[k] = (raw[i * d:(i + 1) * d], m)
        return out, d
    except Exception as e:
        print(f"기존 벡터 로드 실패({e}) — 처음부터 생성합니다.")
        return {}, None


def save(chunks, keys, have, model: str, dim: int, total: int):
    """현재까지 만든 벡터를 파일로 저장(부분 인덱스도 검색에 쓸 수 있다)."""
    metas, blobs = [], []
    for c, k in zip(chunks, keys):
        got = have.get(k)
        if not got:
            continue
        vec, m = got
        metas.append({"k": k, "slug": c["slug"], "title": c["title"], "no": c["no"],
                      "art_title": c["art_title"],
                      "preview": m.get("preview") or c["text"][:240]})
        blobs.append(vec)
    with open(BIN_PATH, "wb") as f:
        for v in blobs:
            f.write(v)
    meta = {"model": model, "dim": dim, "count": len(metas),
            "scale": 1.0 / 127.0, "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_chunks": total, "complete": len(metas) >= total,
            "chunks": metas}
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return len(metas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY", ""))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dim", type=int, default=DEFAULT_DIM,
                    help="임베딩 차원(MRL 축소). 0이면 모델 기본값")
    ap.add_argument("--slug", action="append",
                    help="특정 규정만 강제 재생성(반복 지정 가능)")
    ap.add_argument("--limit", type=int, default=0,
                    help="이번 실행에서 만들 최대 청크 수(일일 쿼터 배분용)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-boilerplate", action="store_true",
                    help="부칙·경과조치 등 상용구도 포함(기본: 제외)")
    ap.add_argument("--restart", action="store_true",
                    help="기존 벡터를 무시하고 처음부터 생성")
    args = ap.parse_args()

    # 인덱스는 항상 전체 기준으로 만든다(--slug 는 '다시 만들 대상'을 고르는 용도).
    chunks = [c for c in rc.iter_chunks()
              if args.include_boilerplate or not c["boiler"]]
    seen: dict = {}
    keys = [chunk_key(c, seen) for c in chunks]
    print(f"전체 청크: {len(chunks):,}개")
    if args.dry_run:
        for c in chunks[:5]:
            print("  ", c["text"][:80])
        print("  ... --dry-run 이므로 생성하지 않았습니다.")
        return
    if not chunks:
        print("생성할 청크가 없습니다."); return
    if not args.api_key:
        sys.exit("GEMINI_API_KEY 를 설정하거나 --api-key 로 전달하세요.")

    have, dim = ({}, None) if args.restart else load_existing(args.model, args.dim)
    if args.slug:                                   # 지정 규정은 강제로 다시 만든다
        drop = set(args.slug)
        have = {k: v for k, v in have.items() if k.split("|", 1)[0] not in drop}
    if have:
        print(f"기존 벡터 {len(have):,}개 재사용 — 남은 {len(chunks) - len(have):,}개 생성")

    todo = [i for i, k in enumerate(keys) if k not in have]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("이미 모든 청크가 생성되어 있습니다.")
    dim = dim or (args.dim or None)

    stopped = None
    try:
        for b, i in enumerate(range(0, len(todo), BATCH)):
            idxs = todo[i:i + BATCH]
            batch = [chunks[j] for j in idxs]
            if b and SLEEP:
                time.sleep(SLEEP)
            vecs = embed_batch([c["text"] for c in batch], args.api_key,
                               args.model, dim=args.dim)
            for j, c, v in zip(idxs, batch, vecs):
                if dim is None:
                    dim = len(v)
                if len(v) != dim:
                    sys.exit(f"차원 불일치: {len(v)} != {dim} — 모델을 확인하세요.")
                have[keys[j]] = (bytes((x & 0xFF) for x in quantize(v)),
                                 {"preview": c["text"][:240]})
            print(f"  {len(have):>5,}/{len(chunks):,} 임베딩 완료", flush=True)
            if FLUSH_EVERY and (b + 1) % FLUSH_EVERY == 0:
                save(chunks, keys, have, args.model, dim, len(chunks))
    except QuotaExhausted as e:
        stopped = e
        print("\n일일 쿼터가 소진되어 중단합니다. 지금까지 만든 벡터를 저장합니다.")

    if dim is None:
        sys.exit("생성된 벡터가 없습니다.")
    n = save(chunks, keys, have, args.model, dim, len(chunks))
    mb = os.path.getsize(BIN_PATH) / 1024 / 1024
    print(f"\n저장: {n:,}/{len(chunks):,}청크 × {dim}차원 → {mb:.1f}MB")
    print(f"  {BIN_PATH}")
    print(f"  {META_PATH}")
    if n < len(chunks):
        print(f"\n미완료 {len(chunks) - n:,}개 — 쿼터가 회복되면 같은 명령을 다시 실행하세요."
              "\n(이미 만든 벡터는 재사용되고 남은 조문만 생성합니다.)")
        if stopped:
            print(f"  마지막 응답: {str(stopped)[:200]}")
    else:
        print("\n두 파일을 커밋하면 배포본에서 의미 검색이 활성화됩니다.")


if __name__ == "__main__":
    main()
