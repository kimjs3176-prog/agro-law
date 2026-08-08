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
BATCH = int(os.environ.get("EMBED_BATCH", "20"))    # 요청당 청크 수
SLEEP = float(os.environ.get("EMBED_SLEEP", "1.0")) # 배치 사이 대기(분당 쿼터 회피)
RETRIES = int(os.environ.get("EMBED_RETRIES", "30"))# 429 는 리셋될 때까지 길게 버틴다
CKPT_PATH = os.path.join(BASE, ".embed_checkpoint.jsonl")

# 429 응답의 "Please retry in 15.4s" 안내
_RETRY_AFTER = re.compile(r"retry in ([\d.]+)s")


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
                # 무료 등급 쿼터. 서버가 알려주는 대기 시간을 우선 따른다.
                wait = _retry_after(r.text) or min(60.0, 10 * 2 ** attempt)
                print(f"    쿼터 대기 {wait:.0f}초 ({attempt + 1}/{RETRIES})", flush=True)
                time.sleep(wait + 2)
                continue
            if r.status_code in (500, 502, 503, 504):
                time.sleep(2 ** min(attempt, 5))
                continue
            break
        except Exception as e:                       # 네트워크 오류 재시도
            last = str(e)
            time.sleep(2 ** min(attempt, 5))
    raise RuntimeError(f"임베딩 실패: {last}")


def quantize(vec):
    """단위벡터 → int8. 복원 시 scale 을 곱해 되돌린다."""
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [max(-127, min(127, int(round(v / norm * 127)))) for v in vec]


def ckpt_load(sig):
    """체크포인트 로드. 서명(모델·차원·청크수)이 다르면 무시한다."""
    if not os.path.exists(CKPT_PATH):
        return {}
    done, head = {}, None
    with open(CKPT_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue                      # 중단으로 잘린 마지막 줄
            if head is None:
                head = rec
                if rec != sig:
                    print("체크포인트 서명이 달라 무시합니다(--restart 와 동일).")
                    return {}
                continue
            done[rec["i"]] = rec
    return done


def ckpt_init(sig):
    with open(CKPT_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps(sig, ensure_ascii=False) + "\n")


def ckpt_append(recs):
    with open(CKPT_PATH, "a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY", ""))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dim", type=int, default=DEFAULT_DIM,
                    help="임베딩 차원(MRL 축소). 0이면 모델 기본값")
    ap.add_argument("--slug", action="append", help="특정 규정만 갱신(반복 지정 가능)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-boilerplate", action="store_true",
                    help="부칙·경과조치 등 상용구도 포함(기본: 제외)")
    ap.add_argument("--restart", action="store_true",
                    help="체크포인트를 버리고 처음부터 다시 생성")
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

    # 체크포인트: 배치마다 결과를 남겨 쿼터 소진·중단 후 이어서 생성한다.
    sig = {"model": args.model, "dim": args.dim, "n": len(chunks),
           "slug": sorted(args.slug) if args.slug else None,
           "boiler": bool(args.include_boilerplate)}
    if args.restart and os.path.exists(CKPT_PATH):
        os.remove(CKPT_PATH)
    done = {} if args.restart else ckpt_load(sig)
    if done:
        print(f"체크포인트에서 {len(done):,}개 재사용 — 남은 {len(chunks) - len(done):,}개만 생성")
    else:
        ckpt_init(sig)

    todo = [i for i in range(len(chunks)) if i not in done]
    for b, i in enumerate(range(0, len(todo), BATCH)):
        idxs = todo[i:i + BATCH]
        batch = [chunks[j] for j in idxs]
        if b and SLEEP:
            time.sleep(SLEEP)                  # 분당 요청 쿼터 회피
        vecs = embed_batch([c["text"] for c in batch], args.api_key,
                           args.model, dim=args.dim)
        recs = []
        for j, c, v in zip(idxs, batch, vecs):
            recs.append({"i": j, "q": quantize(v),
                         "m": {"slug": c["slug"], "title": c["title"], "no": c["no"],
                               "art_title": c["art_title"],
                               "preview": c["text"][:240]}})
        ckpt_append(recs)
        for r in recs:
            done[r["i"]] = r
        print(f"  {len(done):>5,}/{len(chunks):,} 임베딩 완료", flush=True)

    new_meta, new_vecs = [], []
    for j in range(len(chunks)):
        r = done[j]
        if dim is None:
            dim = len(r["q"])
        if len(r["q"]) != dim:
            sys.exit(f"차원 불일치: {len(r['q'])} != {dim} — 모델을 확인하세요.")
        new_vecs.append(bytes((x & 0xFF) for x in r["q"]))
        new_meta.append(r["m"])

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

    if os.path.exists(CKPT_PATH):
        os.remove(CKPT_PATH)                   # 정상 완료 → 체크포인트 정리

    mb = os.path.getsize(BIN_PATH) / 1024 / 1024
    print(f"\n완료: {len(new_meta):,}청크 × {dim}차원 → {mb:.1f}MB")
    print(f"  {BIN_PATH}")
    print(f"  {META_PATH}")
    print("\n두 파일을 커밋하면 배포본에서 의미 검색이 활성화됩니다.")


if __name__ == "__main__":
    main()
