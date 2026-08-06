"""내규 조문 청킹 + 상용구 필터.

regulations/<slug>/index.html 을 조(條) 단위로 쪼개고, 검색 가치가 없는
상용구 조문(부칙·경과조치·다른 내규의 개정·시행일 등)을 걸러낸다.

빌드 스크립트(임베딩 생성)와 런타임(하이브리드 검색)이 같은 규칙을 쓰도록
한 곳에 모아둔다.
"""
from __future__ import annotations

import html as _html
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REG_DIR = os.path.join(BASE_DIR, "regulations")
MANIFEST = os.path.join(BASE_DIR, "regulations_manifest.json")

# 조문 시작 위치(제N조 / 제N조의M)
_ART_SPLIT = re.compile(r"(?=제\s*\d+\s*조(?:의\s*\d+)?\s*[(（])")
_ART_HEAD = re.compile(r"^제\s*(\d+)\s*조(?:의\s*(\d+))?\s*[(（]([^)）]{0,60})[)）]")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

# ── 상용구(검색 노이즈) 판정 ──────────────────────────────────────────────
# 조문제목이 아래에 해당하면 본문 검색 대상에서 제외한다.
# 실제 내용이 없는 형식 조항이라 흔한 어절을 많이 공유해 상위에 잘못 올라온다.
_BOILER_TITLE = (
    "시행일", "경과조치", "적용례", "다른 내규의 개정", "다른 규정의 개정",
    "다른 법령의 개정", "폐지규정", "재검토기한", "유효기간",
)
# 제목이 이걸로 '시작'하면 상용구 (예: "다른 내규의 개정에 따른 …")
_BOILER_PREFIX = ("다른 내규의 개정", "다른 규정의 개정", "경과조치", "적용례")
# 부칙 영역 표식
_ADDENDA = re.compile(r"^\s*부\s*칙")


def _norm(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


def html_to_text(h: str) -> str:
    """규정 HTML → 평문. 블록 경계를 줄바꿈으로 남긴다."""
    t = re.sub(r"(?i)<\s*(script|style)\b.*?<\s*/\s*\1\s*>", " ", h, flags=re.S)
    t = re.sub(r"(?i)<\s*(br|/p|/div|/tr|/h[1-6])\s*/?>", "\n", t)
    t = _TAG.sub(" ", t)
    t = _html.unescape(t)
    t = re.sub(r"[ \t ]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t)


def is_boilerplate(title: str, body: str) -> bool:
    """상용구 조문인지. 제목 우선, 없으면 본문 길이/내용으로 보수적 판정."""
    t = _norm(title)
    if t:
        if t in _BOILER_TITLE:
            return True
        if any(t.startswith(p) for p in _BOILER_PREFIX):
            return True
    b = _norm(body)
    # "이 규정은 ○○부터 시행한다" 한 문장짜리 시행일 조항
    if len(b) <= 120 and re.search(r"부터\s*시행한다", b):
        return True
    # 내용이 사실상 없는 조각
    if len(b) < 25:
        return True
    return False


# 조문 시작 후보(본문 어디서나) — 유효성은 _valid_start 로 따로 판정
_ART_AT = re.compile(r"제\s*(\d+)\s*조(?:의\s*(\d+))?\s*[(（]([^)）\n]{0,60})[)）]")


def _valid_start(text: str, pos: int) -> bool:
    """이 위치의 '제N조(...)'가 실제 조문 시작인지.

    본문에 인용된 다른 법령 조문(「상법」제391조(이사회의 결의 방법), 동법 제391조의3 …)을
    조문 시작으로 오인하지 않도록, 앞 문맥을 보고 판정한다.
    """
    if pos == 0:
        return True
    before = text[max(0, pos - 24):pos]
    # 법령 인용 표기 뒤 → 인용
    if re.search(r"[「」『』]\s*$", before):
        return False
    if re.search(r"(?:법|령|규칙|규정|정관|조례|협약|지침)\s*$", before):
        return False
    if re.search(r"(?:동법|같은\s*법|이\s*법|해당\s*법)\s*$", before):
        return False
    tail = before.rstrip()
    if not tail:
        return True
    # 줄 시작(뒤에 공백만 있어도 줄 시작으로 본다)
    if re.search(r"\n[ \t 　]*$", before):
        return True
    # 장·절·관 제목 바로 뒤 (예: "제1장 총 칙  제1조(목적) …")
    if re.search(r"제\s*\d+\s*[장절관편]\s*[^\n]{0,12}$", tail):
        return True
    # 문장이 끝난 자리
    if re.search(r"[.。:：]$", tail):
        return True
    if tail.endswith((">", "】", "]")):
        return True
    return False


def split_articles(text: str, title: str = ""):
    """평문 → 조문 청크 목록. [{no, art_title, body, boiler}]"""
    # 부칙 시작 위치(있으면 그 이후 조문은 개정 이력 영역)
    m_add = _ADDENDA.search(text) or re.search(r"\n\s*부\s*칙", text)
    add_pos = m_add.start() if m_add else len(text) + 1

    starts = [m for m in _ART_AT.finditer(text) if _valid_start(text, m.start())]
    out = []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        no = m.group(1) + (("의" + m.group(2)) if m.group(2) else "")
        art_title = _norm(m.group(3))
        # 머리말(제N조(제목))은 head 로 따로 붙이므로 본문에서는 제거
        body = _norm(text[m.end():end])
        out.append({
            "no": no,
            "art_title": art_title,
            "body": body,
            "boiler": bool(m.start() >= add_pos or is_boilerplate(art_title, body)),
        })
    return out


def load_manifest():
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def iter_chunks(slugs=None, max_chars: int = 1800):
    """전체(또는 지정 slug)의 조문 청크를 순회.

    반환: {slug, title, no, art_title, text, boiler}
    text 는 '규정명 제N조(제목) 본문' 형태 — 임베딩 시 문맥을 주기 위해 제목을 포함.
    """
    man = {m.get("slug"): m for m in load_manifest() if m.get("slug")}
    targets = slugs if slugs else list(man.keys())
    for slug in targets:
        meta = man.get(slug) or {}
        title = meta.get("title") or slug.replace("_", " ")
        path = os.path.join(REG_DIR, slug, "index.html")
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = html_to_text(f.read())
        except Exception:
            continue
        for a in split_articles(text, title):
            head = f"{title} 제{a['no']}조"
            if a["art_title"]:
                head += f"({a['art_title']})"
            body = a["body"]
            if len(body) > max_chars:          # 과도하게 긴 조문은 잘라 임베딩
                body = body[:max_chars] + "…"
            yield {
                "slug": slug, "title": title, "no": a["no"],
                "art_title": a["art_title"], "boiler": a["boiler"],
                "text": f"{head} {body}",
            }


def stats():
    tot = boiler = 0
    for c in iter_chunks():
        tot += 1
        boiler += 1 if c["boiler"] else 0
    return {"total": tot, "boilerplate": boiler, "searchable": tot - boiler}


if __name__ == "__main__":
    s = stats()
    print(f"조문 청크 {s['total']:,}개 · 상용구 {s['boilerplate']:,}개 제외 "
          f"→ 검색 대상 {s['searchable']:,}개")
