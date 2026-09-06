"""
KOAT 내규&국가법령 종합 검색 서비스
배포: Vercel / Render / Railway
로컬: python run_local.py
"""

import os, json, re, time, threading, webbrowser, base64
import concurrent.futures as _cf
import xml.etree.ElementTree as ET
# 신뢰할 수 없는 XML(업로드 파일·외부 법령 XML)의 엔티티 폭탄(billion laughs) 방어.
# defusedxml 이 있으면 그 파서를 쓰고, 없으면 표준 파서로 폴백한다.
try:
    import defusedxml.ElementTree as _DET
    def _xml_fromstring(s): return _DET.fromstring(s)
except Exception:
    def _xml_fromstring(s): return ET.fromstring(s)
import urllib3
from urllib.parse import quote
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests as req_lib
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
# CORS는 교차 출처(다른 웹사이트) 호출에만 적용된다. 이 앱의 프론트엔드는
# 동일 출처(/api/... 상대경로)라 아래 제한과 무관하게 항상 동작한다.
# 열린 CORS를 두면 임의의 외부 사이트가 브라우저에서 서버의 AI 키를 대신
# 소모할 수 있으므로, 허용 출처를 이 서비스 도메인·로컬 개발로 제한한다.
# 커스텀 도메인 등은 ALLOWED_ORIGINS(쉼표 구분)로 추가할 수 있다.
_origins_env = os.environ.get("ALLOWED_ORIGINS", "").strip()
if _origins_env:
    _cors_origins = [o.strip() for o in _origins_env.split(",") if o.strip()]
else:
    _cors_origins = [
        re.compile(r"^https://agro-law[\w.-]*\.vercel\.app$"),
        re.compile(r"^http://localhost(:\d+)?$"),
        re.compile(r"^http://127\.0\.0\.1(:\d+)?$"),
    ]
CORS(app, origins=_cors_origins)

OC   = os.environ.get("LAW_OC", "tjsl0919")
BASE = "https://www.law.go.kr/DRF"
HEADERS = {
    "User-Agent":      ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
    "Accept":          "application/json, text/html, */*;q=0.9",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.law.go.kr/",
    "Origin":          "https://www.law.go.kr",
    "Connection":      "keep-alive",
    "Cache-Control":   "no-cache",
}

# ── 재시도 정책이 적용된 requests 세션 ────────────────────────────────────────
def _make_session(verify: bool = True) -> req_lib.Session:
    retry = Retry(
        total=4,                              # 최대 4회 재시도
        backoff_factor=0.8,                   # 0.8→1.6→3.2→6.4s
        status_forcelist={429, 500, 502, 503, 504},
        allowed_methods={"GET", "POST"},
        raise_on_status=False,
        # ConnectionReset/ProtocolError 재시도 허용
        respect_retry_after_header=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=8,
        pool_maxsize=24,
    )
    s = req_lib.Session()
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    s.headers.update(HEADERS)
    s.verify = verify
    return s

# 기본 세션은 TLS 인증서를 검증한다(GitHub 토큰·Google API 키 전송에 사용).
_SESSION = _make_session()
# 법제처(law.go.kr)는 인증서 체인 문제가 있어 이 호출에만 검증을 끈다.
# 민감한 토큰을 보내는 GitHub/Google 세션과 분리해 노출을 막는다.
_LAW_SESSION = _make_session(verify=False)

# ── 최근 검색어 (서버 메모리) ─────────────────────────────────────────────────
recent_searches = []
favorites = []

def add_recent(q):
    global recent_searches
    q = q.strip()
    if q and q not in recent_searches:
        recent_searches.insert(0, q)
        recent_searches = recent_searches[:10]

# ── 공통 HTTP 헬퍼 ────────────────────────────────────────────────────────────
# 타임아웃: (연결 대기, 읽기 대기)
_T_JSON = (5, 12)   # JSON 검색
_T_XML  = (5, 20)   # XML 조문 전문
_T_LONG = (5, 30)   # 긴 응답 (조문 전문 대용량)

def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "euc-kr"):
        try:
            return raw.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")

def _law_get_json(params: dict, timeout=None) -> dict:
    timeout = timeout or _T_JSON
    last_err = None
    for attempt in range(3):
        try:
            r = _LAW_SESSION.get(
                f"{BASE}/lawSearch.do",
                params={**params, "OC": OC, "type": "JSON"},
                timeout=timeout,
            )
            r.raise_for_status()
            return json.loads(_decode(r.content))
        except (req_lib.exceptions.ConnectionError,
                req_lib.exceptions.ChunkedEncodingError) as e:
            last_err = e
            # 공유 세션을 close() 하면 동시 실행 중인 다른 워커 스레드의 연결이
            # 끊긴다. 재시도는 풀에서 새 연결을 자동으로 받으므로 sleep만 한다.
            time.sleep(1.5 * (attempt + 1))
            continue
    raise last_err

def _law_get_xml(endpoint: str, params: dict, timeout=None) -> ET.Element:
    timeout = timeout or _T_XML
    last_err = None
    for attempt in range(3):
        try:
            r = _LAW_SESSION.get(
                f"{BASE}/{endpoint}",
                params={**params, "OC": OC, "type": "XML"},
                timeout=timeout,
            )
            r.raise_for_status()
            text = _decode(r.content).strip().lstrip("\ufeff")
            text = re.sub(r"<\?xml[^?]*\?>", "", text, count=1).strip()
            if not text:
                raise ValueError("빈 XML 응답")
            return _xml_fromstring(text)
        except (req_lib.exceptions.ConnectionError,
                req_lib.exceptions.ChunkedEncodingError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
            continue
    raise last_err


def _is_valid_law_xml(root: ET.Element) -> bool:
    """법제처 XML 응답이 유효한 법령 데이터인지 확인."""
    for tag in ("message", "Message", "error", "Error"):
        el = root.find(f".//{tag}")
        if el is not None and el.text:
            txt = el.text.strip()
            if any(w in txt for w in ("없습니다", "없음", "오류", "error", "invalid")):
                return False
    return len({el.tag for el in root.iter()}) > 3


# 번호 태그 목록 (이 태그의 텍스트는 내용에서 제외)
_NO_TAGS = {"항번호","호번호","목번호","조번호","조문번호","항수","호수","목수",
            "조수","장번호","절번호","관번호","편번호"}
# 구조 컨테이너 (직접 텍스트 없이 자식만 가짐)
_SKIP_TAGS = {"조문단위","항","호","목","호목","조문","법령","조문내용그룹"}

def _node_text(el) -> str:
    """단일 노드의 직접 텍스트만 반환 (자식 텍스트 제외)"""
    return (el.text or "").strip()

# ── 항/호/목 계층 깊이 ────────────────────────────────────────────────────────
# 법제처 XML은 <목>을 <호>의 자식이 아니라 <항>의 자식(= <호>의 형제)으로 내려준다.
# 따라서 부모를 따라 깊이를 세면 목이 누락되므로, 태그별 고정 깊이를 사용한다.
_ITEM_DEPTH = {"항": 1, "호": 2, "목": 3, "호목": 3}
# 각 계층의 (번호 태그, 내용 태그)
_ITEM_TAGS = {
    "항":   ("항번호", "항내용"),
    "호":   ("호번호", "호내용"),
    "목":   ("목번호", "목내용"),
    "호목": ("목번호", "목내용"),
}
_CONTENT_TAGS = ("조문내용", "항내용", "호내용", "목내용")

# 내용 텍스트 맨 앞의 항·호·목 번호 패턴
#   ① / 1. / 1의2. / 가. / 가의2. / (1) / 1) / 가)
_NUM_PREFIX_RE = re.compile(
    r"^(?:[①-⑮㉑-㉟]"                       # 동그라미 숫자(항)
    r"|\d{1,3}(?:의\d{1,3})?\s*[.)]"        # 1.  1의2.  1)   (연도 '2024.' 오인 방지: 3자리 이내)
    r"|[가-힣](?:의\d{1,3})?\s*[.)]"        # 가.  가의2.  가)
    r"|\(\s*\d{1,3}\s*\)"                   # (1)
    r")"
)
def _split_no(depth: int, no: str, txt: str) -> tuple:
    """
    (번호, 내용) 을 확정한다.
      · 내용이 번호를 이미 품고 있으면 떼어내어 중복 표기를 막는다
        (예: 번호 '1의2.' + 내용 '1의2. 농업기계의 보급…' → ('1의2.', '농업기계의 보급…'))
      · 번호 태그가 비어 있어도 내용 앞의 번호를 인식해 계층 들여쓰기에 쓴다
      · depth 0(조문 본문)은 '제N조(제목)' 형태를 그대로 두어야 하므로 분리하지 않는다
    """
    no, txt = (no or "").strip(), (txt or "").strip()
    if depth <= 0 or not txt:
        return no, txt

    if no and txt.startswith(no):
        return no, txt[len(no):].lstrip()

    # 번호 태그가 없거나 표기가 달라도(1 vs 1.) 본문 앞 번호를 그대로 신뢰한다
    m = _NUM_PREFIX_RE.match(txt)
    if m:
        return m.group().strip(), txt[m.end():].lstrip()

    return no, txt


def _render_jo_struct(u: ET.Element) -> list:
    """
    <조문단위> 하나를 읽어 계층 구조를 가진 항목 리스트로 반환한다.
      [{"depth": 0, "no": "", "text": "제2조(정의) …"},
       {"depth": 2, "no": "1.", "text": "\"농업기계\"란 …"},
       {"depth": 3, "no": "가.", "text": "농림축산물의 생산에 …"}, …]
    depth 0=조문본문, 1=항, 2=호, 3=목.
    프론트엔드는 이 깊이로 매달린 들여쓰기(hanging indent)를 적용한다.
    """
    items = []

    def _emit(depth: int, no: str, txt: str):
        no, txt = _split_no(depth, no, txt)
        if no or txt:
            items.append({"depth": depth, "no": no, "text": txt})

    def _walk(node: ET.Element, depth: int):
        tag = node.tag

        if tag in _NO_TAGS:
            return                              # 번호 태그는 부모에서 처리

        if tag in _ITEM_TAGS:                   # 항 / 호 / 목
            d = _ITEM_DEPTH.get(tag, depth)
            no_tag, con_tag = _ITEM_TAGS[tag]
            _emit(d, _node_text(node.find(no_tag)) if node.find(no_tag) is not None else "",
                     _node_text(node.find(con_tag)) if node.find(con_tag) is not None else "")
            for child in node:                  # 하위 계층(호·목)은 문서 순서대로
                if child.tag in (no_tag, con_tag):
                    continue
                _walk(child, d)
            return

        if tag in _CONTENT_TAGS:                # 조문내용 등 단독 내용 태그
            _emit(depth, "", _node_text(node))
            if node.tail and node.tail.strip():
                _emit(depth, "", node.tail.strip())
            return

        for child in node:                      # 기타 컨테이너: 자식 순회
            _walk(child, depth)

    for child in u:
        if child.tag in ("조번호", "조문번호", "조문가지번호", "조문제목", "조제목"):
            continue                            # 번호·제목은 별도 필드로 추출
        _walk(child, 0)

    return items


def _struct_to_text(items: list) -> str:
    """계층 항목 리스트를 들여쓴 평문으로 변환(기존 조문내용 필드 호환)."""
    lines = []
    for it in items:
        indent = "  " * max(0, it.get("depth", 0) - 1)
        no, txt = it.get("no", ""), it.get("text", "")
        lines.append(f"{indent}{(no + ' ' + txt).strip() if no else txt}")
    return "\n".join(l for l in lines if l.strip())


def _render_jo(u: ET.Element) -> str:
    """<조문단위> 하나를 읽어서 깔끔한 조문 텍스트를 반환한다."""
    return _struct_to_text(_render_jo_struct(u))


# ── 법령MST 취득 (XML 검색 → 태그 추출) ──────────────────────────────────────
_MST_TAGS  = ("법령MST", "법령Mst", "행정규칙MST", "행정규칙Mst", "lawMst", "MST", "mst",
              "법령일련번호", "행정규칙일련번호")
_NAME_TAGS = ("법령명한글", "법령명", "행정규칙명")


def _mst_of(item: ET.Element) -> str:
    for tag in _MST_TAGS:
        v = (item.findtext(tag) or "").strip()
        if v:
            return v
    return ""


def _get_mst(law_name: str, target: str = "law") -> str:
    """
    법령명 → MST(일련번호). 법제처 검색은 질의어를 포함하는 다른 법령을 먼저 주는
    경우가 있어(예: '특허법' → '특허료 등의 징수규칙') 이름이 정확히 일치하는
    항목을 우선 선택한다.
    """
    try:
        root = _law_get_xml("lawSearch.do",
                            {"target": target, "query": law_name, "display": "20"})
        items = [el for el in root if _mst_of(el)] or \
                [el for el in root.iter() if el is not root and _mst_of(el)]
        want = _norm_key(law_name)

        best, best_rank = "", 99
        for it in items:
            nm = ""
            for tag in _NAME_TAGS:
                nm = (it.findtext(tag) or "").strip()
                if nm:
                    break
            n = _norm_key(nm)
            rank = (0 if n == want else            # 완전 일치
                    1 if n.startswith(want) else   # '특허법 시행령' 류
                    2 if want in n else 3)         # 부분 포함 → 무관
            if rank < best_rank:
                best, best_rank, best_nm = _mst_of(it), rank, nm
                if rank == 0:
                    break
        if best:
            print(f"[MST] '{law_name}'({target}) → {best} "
                  f"(선택:'{best_nm}' 일치도={best_rank})")
            return best

        # 항목 단위 추출이 실패하면 문서 전체에서 첫 태그 사용(구버전 동작)
        for tag in _MST_TAGS:
            el = root.find(f".//{tag}")
            if el is not None and el.text and el.text.strip():
                print(f"[MST] '{law_name}'({target}) fallback {tag}={el.text.strip()}")
                return el.text.strip()
        print(f"[MST] '{law_name}'({target}) 실패 — 태그: {sorted({e.tag for e in root.iter()})}")
    except Exception as e:
        print(f"[MST] 오류: {e}")
    return ""

# 구조 헤더 태그 (장·절·관·편 - 조문이 아님)
_STRUCT_TAGS = {"장", "절", "관", "편", "장번호", "절번호", "관번호", "편번호",
                "장제목", "절제목", "관제목", "편제목"}
_STRUCT_NO_TAGS  = ("장번호","절번호","관번호","편번호")
_STRUCT_TTL_TAGS = ("장제목","절제목","관제목","편제목")
_STRUCT_KIND_MAP = {"장번호":"장","절번호":"절","관번호":"관","편번호":"편"}

# 조문제목·조번호가 장/절/관/편임을 나타내는 패턴
_STRUCT_TITLE_RE = re.compile(r"^제\s*\d+\s*(?:장|절|관|편)")
_STRUCT_NO_RE    = re.compile(r"(?:장|절|관|편)")


def _is_struct_header(u: ET.Element) -> bool:
    """조문단위가 장/절/관/편 구조 헤더인지 판별"""
    child_tags = {c.tag for c in u}

    # ① 자식에 장/절/관/편 전용 태그가 있으면 확실한 헤더
    if child_tags & _STRUCT_TAGS:
        return True

    # ② 조번호 텍스트 자체가 "제N장/절/관/편" 형태인 경우 (법제처 일부 법령)
    jo_no_txt = (u.findtext("조번호") or u.findtext("조문번호") or "").strip()
    if jo_no_txt and _STRUCT_NO_RE.search(jo_no_txt):
        return True

    # ③ 조문제목이 "제N장/절/관/편" 패턴이면 헤더
    title_txt = (u.findtext("조문제목") or u.findtext("조제목") or "").strip()
    if title_txt and _STRUCT_TITLE_RE.match(title_txt):
        return True

    # ④ 조문내용 텍스트가 "제N장/절/관/편 …" 패턴이면 헤더
    #    예: <조번호>제1조</조번호><조문내용>제1장 총칙</조문내용>
    content_el = u.find("조문내용")
    if content_el is not None:
        content_txt = (content_el.text or "").strip()
        has_hang = bool(u.findall(".//항") or u.findall(".//호"))
        if content_txt and _STRUCT_TITLE_RE.match(content_txt) and not has_hang:
            return True

    # ⑤ 조번호가 전혀 없고 실질 내용(항/조문내용/호)도 없으면 헤더
    has_jo_no = bool(jo_no_txt)
    if not has_jo_no:
        has_content = bool(
            u.findall(".//항") or u.findall(".//조문내용") or u.findall(".//호")
        )
        if not has_content:
            return True

    return False


def _struct_label(u: ET.Element) -> tuple:
    """구조 헤더의 (레이블, 제목) 반환  예: ('제1장', '총칙')"""
    # ① 전용 번호 태그 우선
    no_label = ""
    for tag in _STRUCT_NO_TAGS:
        v = (u.findtext(tag) or "").strip()
        if v:
            kind = _STRUCT_KIND_MAP.get(tag, "")
            no_label = v if re.match(r"^제", v) else f"제{v}{kind}"
            break

    # ② 조번호 텍스트가 장/절 형태인 경우
    if not no_label:
        jo_txt = (u.findtext("조번호") or u.findtext("조문번호") or "").strip()
        if jo_txt and _STRUCT_NO_RE.search(jo_txt):
            no_label = jo_txt

    # ③ 전용 제목 태그
    title = ""
    for tag in _STRUCT_TTL_TAGS:
        v = (u.findtext(tag) or "").strip()
        if v:
            title = v; break

    # ④ 조문제목이 "제N장 XXX" 패턴이면 분리
    if not title:
        ttl_txt = (u.findtext("조문제목") or u.findtext("조제목") or "").strip()
        if ttl_txt:
            # "제2장 발명의 진흥" → no_label="제2장", title="발명의 진흥"
            m = re.match(r"^(제\s*\d+\s*(?:장|절|관|편))\s*(.*)", ttl_txt)
            if m:
                if not no_label:
                    no_label = m.group(1).replace(" ", "")
                title = m.group(2).strip()
            else:
                title = ttl_txt

    # ⑤ 조문내용이 "제N장 XXX" 패턴인 경우 (조번호만 있고 조문내용에 장 정보)
    #    예: 조번호="제1조", 조문내용="제1장 총칙"
    if not title:
        content_el = u.find("조문내용")
        if content_el is not None:
            ct = (content_el.text or "").strip()
            m = re.match(r"^(제\s*\d+\s*(?:장|절|관|편))\s*(.*)", ct)
            if m:
                if not no_label:
                    no_label = m.group(1).replace(" ", "")
                title = m.group(2).strip()

    # ⑥ 여전히 제목 없으면 자식 텍스트 fallback
    if not title:
        for c in u:
            if c.tag not in _NO_TAGS and c.tag not in _STRUCT_TAGS:
                txt = (c.text or "").strip()
                if txt and not _STRUCT_TITLE_RE.match(txt):
                    title = txt; break

    return no_label, title


def _art_label(no_d: str, branch: str, no_raw: str = "") -> str:
    """조문 표시번호: 조문번호 + 가지번호 → '제5조의2'."""
    if no_d:
        return f"제{no_d}조" + (f"의{branch}" if branch else "")
    m = re.match(r"^\s*(제\s*\d+\s*조(?:\s*의\s*\d+)?)", no_raw or "")
    return m.group(1).replace(" ", "") if m else ""


def _clean_article_title(title: str, art_no: str) -> str:
    """조문제목에서 앞의 '제N조(의M)' 중복 접두사 제거
    예: '제1조(목적)' → '(목적)' 또는 '목적'
        '제100조의2 등록 신청' → '등록 신청'
    """
    if not title or not art_no:
        return title
    # "제N조" 또는 "제N조의M" 접두사 제거
    cleaned = re.sub(r"^제\s*\d+\s*조(?:의\d+)?\s*", "", title).strip()
    # 남은 괄호만 있으면 제거: "(목적)" → "목적"
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1].strip()
    return cleaned if cleaned else title


# ── 조문 XML 파싱 ─────────────────────────────────────────────────────────────
def _parse_articles(root: ET.Element):
    all_tags = {el.tag for el in root.iter()}
    law_name = ""
    for tag in ("법령명한글", "법령명_한글", "법령명", "행정규칙명"):
        el = root.find(f".//{tag}")
        if el is not None and el.text and el.text.strip():
            law_name = el.text.strip(); break

    # 법령 공포일자 (기준일)
    law_date = ""
    for tag in ("공포일자", "시행일자"):
        el = root.find(f".//{tag}")
        if el is not None and el.text and el.text.strip():
            law_date = el.text.strip(); break

    def _get_amend_info(node: ET.Element) -> dict:
        """조문단위에서 개정·신설·삭제 정보 추출"""
        info = {}
        # 개정일자 태그들
        for tag in ("개정일자", "신설일자", "제정일자", "amendDate", "revisionDate"):
            el = node.find(tag)
            if el is not None and el.text and el.text.strip():
                info["amended_date"] = el.text.strip()
                break
        # 신설/개정/삭제 구분
        for tag in ("개정구분", "조문구분", "신구구분"):
            el = node.find(tag)
            if el is not None and el.text and el.text.strip():
                info["amend_type"] = el.text.strip()
                break
        # 법령 공포일자와 일치하면 최근 개정으로 마킹
        if law_date and not info.get("amended_date"):
            # 조문 자체에 날짜 없으면 법령 공포일을 기준으로 표시하지 않음
            pass
        return info

    articles = []

    # ── 전략 1: <조문단위> ──
    units = root.findall(".//조문단위")
    if units:
        print(f"[파싱] 전략1 조문단위 {len(units)}개")
        for u in units:
            if _is_struct_header(u):
                no_label, title = _struct_label(u)
                if title or no_label:
                    articles.append({
                        "조문번호": "", "조문제목": title,
                        "조문내용": "", "type": "header",
                        "header_no": no_label,
                    })
                continue

            no_raw = (u.findtext("조번호") or u.findtext("조문번호") or "").strip()
            title  = (u.findtext("조문제목") or u.findtext("조제목") or "").strip()
            branch = (u.findtext("조문가지번호") or "").strip()

            # 조번호에서 첫 번째 숫자만 추출 ("제5조의2" → "5")
            m_no  = re.search(r"\d+", no_raw)
            no_d  = m_no.group() if m_no else ""

            # 조문제목에서 앞의 "제N조" 중복 접두사 제거
            title = _clean_article_title(title, no_d)

            struct  = _render_jo_struct(u)
            content = _struct_to_text(struct).strip()
            amend   = _get_amend_info(u)
            if no_d or title or content:
                art = {"조문번호": no_d, "조문가지번호": branch,
                       "조문표시번호": _art_label(no_d, branch, no_raw),
                       "조문제목": title,
                       "조문내용": content, "조문구조": struct, "type": "article"}
                art.update(amend)
                articles.append(art)
        if articles:
            return law_name, law_date, articles

    # ── 전략 2: <조문> ──
    jos = root.findall(".//조문")
    if jos:
        print(f"[파싱] 전략2 조문 {len(jos)}개")
        for jo in jos:
            no_raw = (jo.findtext("조번호") or jo.findtext("번호") or "").strip()
            title  = (jo.findtext("조문제목") or jo.findtext("제목") or "").strip()
            branch = (jo.findtext("조문가지번호") or "").strip()
            m_no   = re.search(r"\d+", no_raw)
            no_d   = m_no.group() if m_no else ""
            title  = _clean_article_title(title, no_d)
            struct  = _render_jo_struct(jo)
            content = _struct_to_text(struct).strip()
            amend   = _get_amend_info(jo)
            if no_d or title or content:
                art = {"조문번호": no_d, "조문가지번호": branch,
                       "조문표시번호": _art_label(no_d, branch, no_raw),
                       "조문제목": title,
                       "조문내용": content, "조문구조": struct, "type": "article"}
                art.update(amend)
                articles.append(art)
        if articles:
            return law_name, law_date, articles

    # ── 전략 3: <조번호> 포함 부모 탐색 ──
    if "조번호" in all_tags:
        print("[파싱] 전략3 조번호 기반")
        seen = set()
        for parent in root.iter():
            no_el = parent.find("조번호")
            if no_el is None: continue
            no_raw = (no_el.text or "").strip()
            # 장/절/관/편 번호는 건너뜀
            if _STRUCT_NO_RE.search(no_raw): continue
            if no_raw in seen: continue
            seen.add(no_raw)
            title   = (parent.findtext("조문제목") or parent.findtext("제목") or "").strip()
            branch  = (parent.findtext("조문가지번호") or "").strip()
            m_no    = re.search(r"\d+", no_raw)
            no_d    = m_no.group() if m_no else ""
            title   = _clean_article_title(title, no_d)
            struct  = _render_jo_struct(parent)
            content = _struct_to_text(struct).strip()
            amend   = _get_amend_info(parent)
            if no_d or title:
                art = {"조문번호": no_d, "조문가지번호": branch,
                       "조문표시번호": _art_label(no_d, branch, no_raw),
                       "조문제목": title,
                       "조문내용": content, "조문구조": struct, "type": "article"}
                art.update(amend)
                articles.append(art)
        if articles:
            return law_name, law_date, articles

    print(f"[파싱] 실패 — 태그: {sorted(all_tags)[:40]}")
    return law_name, law_date, articles


# ── HTML ──────────────────────────────────────────────────────────────────────
INDEX_HTML = None  # HTML은 index.html 파일로 분리됨


# ── Flask 라우트 ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # 로컬 실행 시 index.html 서빙
    # Vercel에서는 vercel.json이 index.html을 직접 서빙함
    try:
        import os
        html_path = os.path.join(os.path.dirname(__file__), "index.html")
        with open(html_path, encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html; charset=utf-8")
    except FileNotFoundError:
        return Response("<h1>index.html not found</h1>", status=404)


def _normalize_admrul(item: dict) -> dict:
    """행정규칙 메타데이터를 법령 메타데이터 형식으로 정규화"""
    out = dict(item)
    # 법령명한글 통일
    if not out.get("법령명한글"):
        out["법령명한글"] = out.get("행정규칙명", "")
    # 법령구분명 통일 (훈령/고시/예규/지침/규정 등)
    if not out.get("법령구분명"):
        out["법령구분명"] = out.get("행정규칙종류명", "행정규칙")
    # 공포일자 통일 (행정규칙은 발령일자)
    if not out.get("공포일자"):
        out["공포일자"] = out.get("발령일자", "")
    # 법령일련번호 통일
    if not out.get("법령일련번호"):
        out["법령일련번호"] = out.get("행정규칙일련번호", "")
    # 법령URL 통일
    if not out.get("법령URL"):
        out["법령URL"] = out.get("행정규칙URL", "")
    # 내부 구분 플래그 (프론트엔드에서 URL 구성 등에 활용)
    out["_doc_type"] = "admrul"
    return out


@app.route("/api/search")
def search_laws():
    query   = request.args.get("query", "").strip()
    display = request.args.get("display", "20")
    if not query:
        return jsonify({"error": "검색어를 입력하세요"}), 400
    if len(query) < 2:
        return jsonify({"error": "검색어는 2자 이상 입력하세요"}), 400
    try:
        add_recent(query)

        def _do_law():
            d = _law_get_json({"target": "law", "query": query, "display": display})
            err = d.get("LawSearch", {}).get("message", "")
            items = d.get("LawSearch", {}).get("law", []) or []
            if isinstance(items, dict): items = [items]
            return items, err

        def _do_admrul():
            try:
                d = _law_get_json({"target": "admrul", "query": query, "display": "10"},
                                  timeout=(5, 10))
                ls = d.get("LawSearch", {})
                items = ls.get("admrul") or ls.get("law") or []
                if isinstance(items, dict): items = [items]
                return [_normalize_admrul(i) for i in items]
            except Exception as e:
                print(f"[search] admrul 오류: {e}")
                return []

        with _cf.ThreadPoolExecutor(max_workers=2) as ex:
            f_law    = ex.submit(_do_law)
            f_admrul = ex.submit(_do_admrul)
            laws, err = f_law.result()
            admrul_items = f_admrul.result()

        if err:
            return jsonify({"error": f"법제처 오류: {err}"}), 502

        # 중복 없이 행정규칙 병합 (법령 우선)
        law_names = {l.get("법령명한글", "") for l in laws}
        for item in admrul_items:
            nm = item.get("법령명한글", "")
            if nm and nm not in law_names:
                laws.append(item)
                law_names.add(nm)

        if laws:
            print(f"[search] '{query}' law={len(laws)-len(admrul_items)} "
                  f"admrul={len(admrul_items)} 총={len(laws)}")
        return jsonify({"success": True, "count": len(laws), "laws": laws})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/validate")
def validate_keyword():
    """키워드가 실제 검색 결과를 가지는지 확인"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"valid": False})
    try:
        data = _law_get_json({"target": "law", "query": q, "display": "1"})
        laws = data.get("LawSearch", {}).get("law")
        return jsonify({"valid": bool(laws)})
    except Exception:
        return jsonify({"valid": False})


# ── 법령 조문 인메모리 캐시 (TTL 30분) ──────────────────────────────────────
_LAW_ARTICLE_CACHE: dict = {}   # law_name → {"lname": str, "articles": list, "ts": float}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL  = 1800              # 30분

def _cache_get(law_name: str):
    with _CACHE_LOCK:
        entry = _LAW_ARTICLE_CACHE.get(law_name)
        if entry and time.time() - entry["ts"] < _CACHE_TTL:
            return entry["lname"], entry["articles"]
    return None, None

def _cache_set(law_name: str, lname: str, articles: list):
    with _CACHE_LOCK:
        _LAW_ARTICLE_CACHE[law_name] = {"lname": lname, "articles": articles, "ts": time.time()}


# ── 도메인 키워드 → 관련 법령 매핑 ────────────────────────────────────────────
# 시나리오 텍스트에서 도메인 키워드가 감지되면 해당 법령만 탐색
DOMAIN_LAW_MAP = {
    "농약":    ["농약관리법", "농촌진흥법"],
    "농지":    ["농지법"],
    "전용":    ["농지법"],
    "임대":    ["농지법"],
    "종자":    ["종자산업법", "식물신품종 보호법"],
    "품종":    ["식물신품종 보호법", "종자산업법"],
    "육종":    ["식물신품종 보호법"],
    "특허":    ["특허법", "발명진흥법"],
    "발명":    ["특허법", "발명진흥법"],
    "실용신안": ["실용신안법"],
    "상표":    ["상표법", "부정경쟁방지 및 영업비밀보호에 관한 법률"],
    "디자인":  ["디자인보호법"],
    "저작권":  ["저작권법"],
    "저작물":  ["저작권법"],
    "영업비밀": ["부정경쟁방지 및 영업비밀보호에 관한 법률"],
    "부정경쟁": ["부정경쟁방지 및 영업비밀보호에 관한 법률"],
    "비료":    ["비료관리법", "농촌진흥법"],
    "축산":    ["축산법"],
    "가축":    ["축산법"],
    "식품":    ["식품안전기본법"],
    "기술이전": ["기술의 이전 및 사업화 촉진에 관한 법률", "농업기술실용화 촉진법"],
    "사업화":  ["농업기술실용화 촉진법", "기술의 이전 및 사업화 촉진에 관한 법률"],
    "실용화":  ["농업기술실용화 촉진법"],
    "재해보험": ["농어업재해보험법"],
    "재해":    ["농어업재해보험법"],
    "유통":    ["농수산물 유통 및 가격안정에 관한 법률"],
    "수산물":  ["농수산물 유통 및 가격안정에 관한 법률"],
    "반도체":  ["반도체집적회로의 배치설계에 관한 법률"],
    "배치설계": ["반도체집적회로의 배치설계에 관한 법률"],
    "농촌진흥": ["농촌진흥법"],
    "보조금":  ["농촌진흥법", "농어업재해보험법"],
    "인증":    ["농촌진흥법", "식품안전기본법"],
    "유기농":  ["농촌진흥법", "식품안전기본법"],
}


# 농업·지식재산 분야 핵심 법령 (조문 검색 대상)
CANDIDATE_LAWS = [
    "특허법", "실용신안법", "디자인보호법", "상표법", "발명진흥법",
    "농촌진흥법", "종자산업법", "농약관리법", "비료관리법", "농지법",
    "식물신품종 보호법", "농업기술실용화 촉진법",
    "기술의 이전 및 사업화 촉진에 관한 법률",
    "부정경쟁방지 및 영업비밀보호에 관한 법률",
    "저작권법", "반도체집적회로의 배치설계에 관한 법률",
    "축산법", "식품안전기본법", "농어업재해보험법",
    "농수산물 유통 및 가격안정에 관한 법률",
]

# 항상 탐색할 핵심 행정규칙(운영규정·훈령·예규) 목록
# ※ 법령명 검색(Stage 1)에 걸리지 않는 운영규정도 내용 탐색 가능하도록 고정 포함
CANDIDATE_ADMRUL = [
    # 농촌진흥청 직무발명·기술이전 관련 훈령
    "농촌진흥청 직무발명의 관리에 관한 규정",
    "농촌진흥청 연구개발사업 운영규정",
    "농촌진흥청 연구성과 기술이전 및 사업화에 관한 규정",
    # 국가연구개발 지식재산 관련
    "국가연구개발사업의 관리 등에 관한 규정",
    # 발명진흥·기술이전 관련
    "공공연구기관 기술이전·사업화 촉진 운영규정",
    "직무발명 보상에 관한 규정",
]

# 키워드로 해당 법령 전체 조문을 불러와 매칭 조문 반환하는 공통 헬퍼
def _law_name_rel(name: str, query: str) -> int:
    """법령명과 질의어의 관계: 0=동일(본법), 1=같은 계열(시행령/규칙 등), 2=부분포함, 3=무관."""
    n = re.sub(r"\s+", "", name or "")
    q = re.sub(r"\s+", "", query or "")
    if not n or not q:
        return 3
    if n == q:
        return 0
    if n.startswith(q):
        return 1
    if q in n:
        return 2
    return 3


def _fetch_matching_articles(law_name: str, kw: str, doc_type: str = "law", always: bool = False):
    cache_key = f"{doc_type}:{law_name}"
    lname, articles = _cache_get(cache_key)
    if articles is None:
        mst = _get_mst(law_name, target=doc_type)
        if not mst:
            return None
        endpoint = "admRulService.do" if doc_type == "admrul" else "lawService.do"
        root = None
        for param in ("MST", "ID"):
            try:
                r = _law_get_xml(endpoint, {"target": doc_type, param: mst}, timeout=(5, 18))
                if _is_valid_law_xml(r):
                    root = r; break
            except Exception:
                continue
        if root is None:
            return None
        lname, _, articles = _parse_articles(root)
        _cache_set(cache_key, lname or law_name, articles)
    kwL = kw.lower()
    matched = [a for a in articles
               if a.get("type") == "article" and
               kwL in " ".join([a.get("조문내용",""), a.get("조문제목","")]).lower()]
    if matched:
        # 관련도 정렬: 제목 매칭 최우선 → 본문 내 키워드 빈도 → 조문번호
        def _rel(a):
            title = (a.get("조문제목","") or "").lower()
            content = (a.get("조문내용","") or "").lower()
            s = 0
            if kwL in title: s += 1000
            s += content.count(kwL) * 3
            return s
        matched.sort(key=lambda a: (-_rel(a),
                                    int(re.search(r"\d+", a.get("조문번호","") or "0").group()
                                        if re.search(r"\d+", a.get("조문번호","") or "0") else 0)))
        return {"law_name": lname or law_name, "keyword": kw, "articles": matched}
    if always:
        # 법령명이 질의어와 일치하는 경우, 본문에 자기 이름이 없어 매칭이 0이어도
        # 결과에서 빠지지 않도록 앞부분 조문을 제공한다(본법 누락 방지).
        head = [a for a in articles if a.get("type") == "article"][:15]
        if head:
            return {"law_name": lname or law_name, "keyword": kw,
                    "articles": head, "name_only": True}
    return None


@app.route("/api/search/article")
def search_by_article_keyword():
    """
    조문 내용 키워드 검색.
    법제처 API는 조문 내용 검색을 직접 지원하지 않으므로,
    핵심 법령 목록에서 조문을 불러와 서버 측 필터링한다.
    """
    query = request.args.get("query", "").strip()
    if not query:
        return jsonify({"error": "검색어를 입력하세요"}), 400
    if len(query) < 2:
        return jsonify({"error": "검색어는 2자 이상 입력하세요"}), 400

    # ── Stage 1: law + admrul 병렬 검색 ────────────────────────────────────────
    # law_name → {"meta": dict, "doc_type": "law"|"admrul"}
    stage1_meta: dict = {}

    def _stage1_law():
        try:
            d = _law_get_json({"target": "law", "query": query, "display": "10"}, timeout=(5, 10))
            items = d.get("LawSearch", {}).get("law", []) or []
            if isinstance(items, dict): items = [items]
            return [{"meta": l, "doc_type": "law",
                     "name": l.get("법령명한글", "").strip()} for l in items]
        except Exception as e:
            print(f"[article-search] Stage1(law) 오류: {e}")
        return []

    def _stage1_admrul_multi():
        """행정규칙 이름 검색 — 전체 쿼리 + 2글자 이상 개별 토큰으로 다중 쿼리"""
        results = []
        seen = set()
        # 검색할 쿼리 목록: 전체 쿼리 + 개별 토큰
        queries = [query] + [t for t in re.findall(r'[가-힣]{2,}', query) if t != query]
        for q in queries[:4]:   # 최대 4개 쿼리
            try:
                d = _law_get_json({"target": "admrul", "query": q, "display": "10"}, timeout=(5, 10))
                ls = d.get("LawSearch", {})
                items = ls.get("admrul") or ls.get("law") or []
                if isinstance(items, dict): items = [items]
                for l in items:
                    nm = (l.get("행정규칙명") or l.get("법령명한글") or "").strip()
                    if nm and nm not in seen:
                        seen.add(nm)
                        results.append({"meta": l, "doc_type": "admrul", "name": nm})
            except Exception as e:
                print(f"[article-search] Stage1(admrul q={q!r}) 오류: {e}")
        return results

    with _cf.ThreadPoolExecutor(max_workers=2) as _ex:
        _f_law    = _ex.submit(_stage1_law)
        _f_admrul = _ex.submit(_stage1_admrul_multi)
        for entry in _f_law.result():
            if entry["name"]:
                stage1_meta[entry["name"]] = entry
        for entry in _f_admrul.result():
            if entry["name"] and entry["name"] not in stage1_meta:
                stage1_meta[entry["name"]] = entry
    print(f"[article-search] Stage1 law={sum(1 for v in stage1_meta.values() if v['doc_type']=='law')} "
          f"admrul={sum(1 for v in stage1_meta.values() if v['doc_type']=='admrul')}")

    # CANDIDATE_LAWS + CANDIDATE_ADMRUL 전체 항상 탐색 + Stage 1 추가 발견 법령/행정규칙
    _known = set(CANDIDATE_LAWS) | set(CANDIDATE_ADMRUL)
    extra_laws = [l for l in stage1_meta if l not in _known]
    # CANDIDATE_ADMRUL doc_type 등록 (stage1_meta에 없는 항목은 admrul 기본값)
    for nm in CANDIDATE_ADMRUL:
        if nm not in stage1_meta:
            stage1_meta[nm] = {"meta": {}, "doc_type": "admrul", "name": nm}
    all_target = list(CANDIDATE_LAWS) + list(CANDIDATE_ADMRUL) + extra_laws
    # scope=priority: 질의어와 이름이 직접 관련된 법령만 먼저 조회(빠른 1차 응답용).
    # 프런트가 1차/전체를 병렬 호출해, 가장 관련 높은 결과를 먼저 그린다.
    scope = (request.args.get("scope") or "").strip()
    if scope == "priority":
        prio = [n for n in all_target if _law_name_rel(n, query) <= 2]
        if prio:
            all_target = prio[:8]
    print(f"[article-search] '{query}' 대상 {len(all_target)}개 scope={scope or 'all'} "
          f"(법령:{len(CANDIDATE_LAWS)} + 행정규칙:{len(CANDIDATE_ADMRUL)} + 추가:{len(extra_laws)})")

    kw = query.lower()

    def fetch_and_filter(law_name: str):
        try:
            entry = stage1_meta.get(law_name) or {}
            doc_type = entry.get("doc_type", "law")
            meta = entry.get("meta", {})
            # 질의어와 이름이 같거나 같은 계열(시행령·시행규칙)인 법령은
            # 본문 키워드 매칭이 없어도 결과에 포함한다.
            nrel = _law_name_rel(law_name, query)
            res = _fetch_matching_articles(law_name, kw, doc_type=doc_type,
                                           always=(nrel <= 1))
            if not res:
                return None
            lname = res["law_name"]
            # admrul은 meta에 행정규칙명 필드가 있을 수 있음
            if not meta:
                meta = stage1_meta.get(lname, {}).get("meta", {})
            # 매칭 조문 상위 15개 (내용 600자 절단) — 관련도순
            top_arts = [
                {"조문번호": a.get("조문번호", ""),
                 "조문제목": a.get("조문제목", ""),
                 "조문내용": a.get("조문내용", "")[:600] +
                             ("…" if len(a.get("조문내용", "")) > 600 else "")}
                for a in res["articles"][:15]
            ]
            return {
                "법령명한글":   lname,
                "법령구분명":   meta.get("법령구분명") or meta.get("행정규칙종류명") or ("행정규칙" if doc_type == "admrul" else "법률"),
                "소관부처명":   meta.get("소관부처명", ""),
                "공포일자":     meta.get("공포일자") or meta.get("발령일자", ""),
                "법령일련번호": meta.get("법령일련번호") or meta.get("행정규칙일련번호", ""),
                "matched_count": len(res["articles"]),
                "matched_articles": top_arts,
                "_matched_count": len(res["articles"]),
            }
        except Exception as e:
            print(f"[article-search] {law_name} 오류: {e}")
        return None

    results, truncated = [], False
    try:
        with _cf.ThreadPoolExecutor(max_workers=12) as ex:
            futures = {ex.submit(fetch_and_filter, name): name for name in all_target}
            for future in _cf.as_completed(futures, timeout=25):
                try:
                    res = future.result()
                    if res:
                        results.append(res)
                except Exception as e:
                    print(f"[article-search] future 오류: {e}")
    except _cf.TimeoutError:
        truncated = True
        print(f"[article-search] 타임아웃, {len(results)}/{len(all_target)}건 탐색")

    # 정렬: 본법 우선 → 시행령 → 시행규칙 → 그 외, 같은 순위 안에서 매칭 조문 수 순
    # (조문이 많이 걸렸다는 이유로 시행규칙이 본법보다 앞서던 문제 수정)
    qn = re.sub(r"\s+", "", query)

    def _law_order(x):
        name = re.sub(r"\s+", "", x.get("법령명한글", "") or "")
        kind = x.get("법령구분명", "") or ""
        # 1순위: 법령명과 질의어의 관계 (본법 → 같은 계열 → 부분포함 → 본문에만 언급)
        rel = _law_name_rel(name, qn)
        # 2순위: 같은 관계 안에서의 법령 위계 (법률 → 시행령 → 시행규칙)
        if name.endswith("시행규칙"):
            sub = 2
        elif name.endswith("시행령"):
            sub = 1
        elif kind in ("법률", "헌법"):
            sub = 0
        elif kind in ("대통령령",):
            sub = 1
        elif kind in ("총리령", "부령", "기획재정부령", "농림축산식품부령"):
            sub = 2
        else:
            sub = 3 if kind and kind != "법률" else 0
        return (rel, sub, -int(x.get("_matched_count", 0) or 0), len(name))

    results.sort(key=_law_order)
    for r in results:
        r.pop("_matched_count", None)

    print(f"[article-search] '{query}' → {len(results)}건 매칭")
    return jsonify({
        "success": True,
        "count": len(results),
        "laws": results,
        "truncated": truncated,
        "searched_total": len(all_target),
    })


@app.route("/api/search/basis")
def search_legal_basis():
    """업무 상황 설명 → 2단계 근거 법령·조문 검색
    Stage 1: 법제처 법령명 검색 API로 관련 법령 목록 확보
    Stage 2: 해당 법령 조문에서 시나리오 키워드(OR) 필터링
    """
    scenario = request.args.get("scenario", "").strip()
    if not scenario:
        return jsonify({"error": "업무 상황을 입력하세요"}), 400
    if len(scenario) < 4:
        return jsonify({"error": "업무 상황을 4자 이상 입력하세요"}), 400

    # ── Stage 1: law + admrul 병렬 검색 ────────────────────────────────────────
    # name → doc_type ("law" | "admrul")
    stage1_doc_types: dict = {}
    stage1_names: set = set()

    def _b_stage1_law():
        try:
            d = _law_get_json({"target": "law", "query": scenario, "display": "10"}, timeout=(5, 10))
            items = d.get("LawSearch", {}).get("law", []) or []
            if isinstance(items, dict): items = [items]
            return [("law", l.get("법령명한글", "").strip()) for l in items]
        except Exception as e:
            print(f"[basis] Stage1(law) 오류: {e}")
        return []

    def _b_stage1_admrul():
        results = []; seen = set()
        queries = [scenario] + [t for t in re.findall(r'[가-힣]{2,}', scenario) if t != scenario]
        for q in queries[:4]:
            try:
                d = _law_get_json({"target": "admrul", "query": q, "display": "10"}, timeout=(5, 10))
                ls = d.get("LawSearch", {})
                items = ls.get("admrul") or ls.get("law") or []
                if isinstance(items, dict): items = [items]
                for l in items:
                    nm = (l.get("행정규칙명") or l.get("법령명한글") or "").strip()
                    if nm and nm not in seen:
                        seen.add(nm); results.append(("admrul", nm))
            except Exception as e:
                print(f"[basis] Stage1(admrul q={q!r}) 오류: {e}")
        return results

    with _cf.ThreadPoolExecutor(max_workers=2) as _ex:
        _f_law    = _ex.submit(_b_stage1_law)
        _f_admrul = _ex.submit(_b_stage1_admrul)
        for doc_type, nm in _f_law.result():
            if nm:
                stage1_names.add(nm); stage1_doc_types[nm] = doc_type
        for doc_type, nm in _f_admrul.result():
            if nm and nm not in stage1_doc_types:
                stage1_names.add(nm); stage1_doc_types[nm] = doc_type
    print(f"[basis] Stage1 law+admrul={len(stage1_names)}")

    # CANDIDATE_LAWS + CANDIDATE_ADMRUL 전체 항상 탐색 + Stage 1 추가 법령/행정규칙
    _known = set(CANDIDATE_LAWS) | set(CANDIDATE_ADMRUL)
    extra_laws = [l for l in stage1_names if l not in _known]
    # CANDIDATE_ADMRUL doc_type 등록 (stage1_doc_types에 없는 항목은 admrul 기본값)
    for nm in CANDIDATE_ADMRUL:
        if nm not in stage1_doc_types:
            stage1_doc_types[nm] = "admrul"
    print(f"[basis] Stage1={len(stage1_names)} extra={len(extra_laws)}")

    # ── 업무 유형 분류 ────────────────────────────────────────────────────────
    basis_type = "일반"
    if any(w in scenario for w in ("허가", "등록", "신고", "승인", "인가", "면허", "인증", "출원", "자격")):
        basis_type = "허가·등록·신고"
    elif any(w in scenario for w in ("금지", "제한", "제재", "처벌", "위반", "과태료", "벌칙", "위법")):
        basis_type = "금지·제한"
    elif any(w in scenario for w in ("의무", "기준", "요건", "절차", "방법", "조건")):
        basis_type = "의무·기준"
    elif any(w in scenario for w in ("지원", "보조금", "보상", "혜택", "권리", "보호")):
        basis_type = "지원·권리"

    # ── Stage 2 키워드: 범용 불용어 제거, 도메인 특정 명사 우선 배치 ─────────
    STOPWORDS = {
        "이란", "하려면", "할때", "할때는", "경우", "무엇", "어떻게", "어떤",
        "필요한", "근거", "확인", "관련", "대한", "있나요", "있는지",
        "하는지", "에서", "에게", "에는", "으로", "위한", "받으려면",
    }
    tokens = re.findall(r'[가-힣]{2,}', scenario)
    raw_kws = [t for t in tokens if t not in STOPWORDS]
    domain_first = [kw for kw in DOMAIN_LAW_MAP if kw in scenario]
    rest = [kw for kw in raw_kws if kw not in set(domain_first)]
    keywords = (domain_first + rest)[:6]  # OR 조건, 최대 6개

    if not keywords:
        return jsonify({"error": "유효한 키워드를 추출할 수 없습니다. 더 구체적인 업무 내용을 입력해주세요."}), 400

    # ── Stage 2: 법령별 조문 병렬 탐색 (OR 매칭) ─────────────────────────────
    def fetch_law(law_name: str):
        try:
            doc_type = stage1_doc_types.get(law_name, "law")
            cache_key = f"{doc_type}:{law_name}"
            lname, articles = _cache_get(cache_key)
            if articles is None:
                mst = _get_mst(law_name, target=doc_type)
                if not mst: return None
                endpoint = "admRulService.do" if doc_type == "admrul" else "lawService.do"
                root = None
                for param in ("MST", "ID"):
                    try:
                        r = _law_get_xml(endpoint,
                                         {"target": doc_type, param: mst}, timeout=(5, 18))
                        if _is_valid_law_xml(r):
                            root = r; break
                    except Exception:
                        continue
                if root is None: return None
                lname, _, articles = _parse_articles(root)
                _cache_set(cache_key, lname or law_name, articles)

            kwL = [kw.lower() for kw in keywords]
            matched = [
                a for a in articles
                if a.get("type") == "article" and
                any(kw in " ".join([a.get("조문내용",""), a.get("조문제목","")]).lower()
                    for kw in kwL)
            ]
            if not matched: return None

            hit_kws = list({
                kw for kw in keywords
                for a in matched
                if kw.lower() in " ".join([a.get("조문내용",""),
                                            a.get("조문제목","")]).lower()
            })
            return {
                "law_name": lname or law_name,
                "articles": [
                    {"조문번호": a["조문번호"], "조문제목": a["조문제목"],
                     "조문내용": a["조문내용"][:400] + ("..." if len(a["조문내용"]) > 400 else "")}
                    for a in matched
                ],
                "matched_keywords": hit_kws,
            }
        except Exception as e:
            print(f"[basis] {law_name} 오류: {e}")
        return None

    all_target = list(CANDIDATE_LAWS) + list(CANDIDATE_ADMRUL) + extra_laws
    print(f"[basis] '{scenario[:20]}' 대상 {len(all_target)}개 "
          f"(법령:{len(CANDIDATE_LAWS)} + 행정규칙:{len(CANDIDATE_ADMRUL)} + 추가:{len(extra_laws)})")
    results, truncated = [], False
    try:
        with _cf.ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(fetch_law, name): name for name in all_target}
            for future in _cf.as_completed(futures, timeout=25):
                try:
                    res = future.result()
                    if res:
                        results.append({
                            "법령명한글":      res["law_name"],
                            "matched_articles": res["articles"],
                            "matched_keywords": res["matched_keywords"],
                            "relevance":        len(res["articles"]),
                        })
                except Exception as e:
                    print(f"[basis] future 오류: {e}")
    except _cf.TimeoutError:
        truncated = True
        print(f"[basis] 타임아웃, {len(results)}건 반환")

    results.sort(key=lambda x: x["relevance"], reverse=True)

    return jsonify({
        "success":           True,
        "scenario":          scenario,
        "extracted_keywords": keywords,
        "basis_type":        basis_type,
        "laws":              results,
        "truncated":         truncated,
        "searched_total":    len(all_target),
    })


@app.route("/api/scenario", methods=["POST"])
def scenario_search():
    """실무 시나리오 검색: AI(사용자 제공 키 우선, 없으면 서버 환경변수) → 관련 법령·조문"""
    body    = request.get_json(force=True) or {}
    query   = body.get("query", "").strip()
    if not query or len(query) < 5:
        return jsonify({"error": "질문을 5자 이상 입력해 주세요."}), 400

    # ── AI 설정: 요청 본문(사용자 AI 설정) 우선 → 서버 환경변수 폴백 ──────────
    provider = (body.get("provider") or os.environ.get("SCENARIO_AI_PROVIDER", "gemini")).lower()  # noqa: E501
    api_key  = (body.get("api_key")  or os.environ.get(f"{provider.upper()}_API_KEY", "")).strip()
    model    = (body.get("model")    or os.environ.get("SCENARIO_AI_MODEL", "")).strip()
    # ollama는 로컬 URL을 키 대신 사용하므로 키 없이도 허용
    if not api_key and provider != "ollama":
        return jsonify({"error":
            "시나리오 검색을 사용하려면 AI 키가 필요합니다. "
            "상단 [✦ AI 설정]에서 Gemini/Claude/GPT 키를 입력하세요."
        }), 503

    mdl = model or _default_model_for(provider, api_key)

    # ── AI Step-1: 의도 분석 → JSON ──────────────────────────────────────────
    # 정식 법령명으로 표기해야 AI가 그대로 echo → MST 조회가 정확히 매칭된다.
    # (CANDIDATE_LAWS·DOMAIN_LAW_MAP 의 표기와 일치시킬 것)
    AVAILABLE_LAWS = (
        "농지법, 종자산업법, 농약관리법, 비료관리법, 가축전염병예방법, "
        "식물방역법, 농어업재해보험법, 농촌진흥법, 농어업재해대책법, "
        "특허법, 실용신안법, 디자인보호법, 상표법, 식물신품종 보호법, "
        "부정경쟁방지 및 영업비밀보호에 관한 법률, "
        "기술의 이전 및 사업화 촉진에 관한 법률"
    )
    system_prompt = f"""당신은 대한민국 농업·지식재산 법령 전문가 AI입니다.
사용자의 실무 질문을 분석하여 JSON만 반환하세요 (코드블록·설명 없이 JSON 텍스트만).

분석 대상 법령 목록: {AVAILABLE_LAWS}

반환 형식:
{{
  "category": "농업" | "지식재산" | "공통",
  "basis_type": "허가·등록·신고" | "금지·제한" | "의무·기준" | "지원·권리" | "일반",
  "guidance": "이 질문에 대한 방향 안내 2~3문장 (마크다운 **굵게** 사용 가능)",
  "steps": ["단계1", "단계2", "단계3"],
  "laws": ["관련 법령명1", "관련 법령명2"],
  "keywords": ["조문 검색 키워드1", "키워드2", "키워드3"],
  "caution": "주의사항 (없으면 null)"
}}
laws는 위 목록에서만 선택(최대 4개), keywords는 3~6개."""

    user_msg = f"질문: {query}"

    try:
        if provider not in ("gemini", "claude", "gpt", "openai", "ollama"):
            return jsonify({"error": f"지원하지 않는 AI 프로바이더: {provider}"}), 400
        # 공통 생성기 사용 — Gemini 3.x 의 사고 토큰 예산까지 처리해 응답이 잘리지 않게 한다.
        ai_text, ai_err = _ai_generate(provider, api_key, mdl, system_prompt, user_msg,
                                       max_tokens=1200, temperature=0.2, json_mode=True)
        if ai_err:
            return jsonify({"error": f"AI 오류: {ai_err}"}), 502

        # JSON 파싱 (코드블록·앞뒤 설명 제거 후)
        clean = re.sub(r"```(?:json)?|```", "", ai_text).strip()
        try:
            ai = json.loads(clean)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", clean, re.S)      # 설명 사이에 낀 JSON 구제
            if not m:
                raise
            ai = json.loads(m.group(0))

    except json.JSONDecodeError:
        # AI가 JSON을 제대로 반환 못한 경우 — 키워드 기반 폴백
        print(f"[scenario] JSON 파싱 실패: {ai_text[:200]}")
        tokens = re.findall(r"[가-힣]{2,}", query)
        ai = {"category": "공통", "basis_type": "일반",
              "guidance": "관련 조문을 검색합니다.", "steps": [],
              "laws": [], "keywords": tokens[:5], "caution": None}
    except Exception as e:
        return jsonify({"error": f"AI 연결 오류: {str(e)}"}), 502

    laws_to_search  = ai.get("laws", []) or []
    keywords        = ai.get("keywords", []) or []
    guidance        = ai.get("guidance", "")
    steps           = ai.get("steps", []) or []
    category        = ai.get("category", "공통")
    basis_type      = ai.get("basis_type", "일반")
    caution         = ai.get("caution")

    if not keywords:
        tokens = re.findall(r"[가-힣]{2,}", query)
        keywords = tokens[:5]

    # laws 가 비면 질문 어휘로 후보를 좁힌다.
    # (예전에는 CANDIDATE_LAWS 앞 6개를 그대로 썼는데, 사내 복무 질문에 특허법·상표법이
    #  근거로 붙어 답변이 엉뚱해지는 원인이었다.)
    if not laws_to_search:
        picked = []
        for kw, names in DOMAIN_LAW_MAP.items():
            if kw in query:
                picked += [n for n in names if n not in picked]
        laws_to_search = picked[:4]      # 매칭이 없으면 빈 목록 = 법령 검색 생략

    # ── Step-2: 법령별 관련 조문 조회 ─────────────────────────────────────────
    def _fetch_for_law(law_name):
        """법령에서 keywords에 매칭되는 조문을 수집해 (표시명, 조문리스트) 반환.
        _fetch_matching_articles 는 {'law_name','keyword','articles'} dict 를
        돌려주므로 여기서 조문 리스트로 평탄화하고, 여러 키워드 결과를
        조문번호·제목 기준으로 중복 제거하여 합친다."""
        resolved = law_name
        seen, arts = set(), []
        try:
            for kw in keywords:
                res = _fetch_matching_articles(law_name, kw)
                if not res:
                    continue
                resolved = res.get("law_name") or resolved
                for a in res.get("articles", []):
                    key = (a.get("조문번호", ""), a.get("조문제목", ""))
                    if key not in seen:
                        seen.add(key)
                        arts.append(a)
        except Exception as e:
            print(f"[scenario] {law_name} 조문 조회 오류: {e}")
        return resolved, arts

    results = []
    try:
        with _cf.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_fetch_for_law, ln): ln for ln in laws_to_search}
            for fut in _cf.as_completed(futs, timeout=20):
                ln, arts = fut.result()
                if arts:
                    matched_kws = [kw for kw in keywords
                                   if any(kw in (a.get("조문내용", "") + a.get("조문제목", ""))
                                          for a in arts)]
                    results.append({
                        "법령명한글": ln,
                        "matched_articles": arts[:8],
                        "matched_keywords": matched_kws,
                        "relevance": len(matched_kws) * 10 + len(arts),
                    })
    except _cf.TimeoutError:
        print(f"[scenario] Step-2 타임아웃, {len(results)}건 반환")

    results.sort(key=lambda x: x["relevance"], reverse=True)

    # ── Step-3: 내규(사규 MCP) 동시 검색 — 시나리오는 내규+법령 전체 대상 ──────
    internal_text, internal_struct, internal_err = "", None, ""
    if SAGYU_MCP_URL:
        try:
            icli = _McpClient(SAGYU_MCP_URL)
            icli.initialize()
            itools = icli.list_tools()
            itool = _mcp_pick_search_tool(itools)
            if itool:
                seen_blocks, parts = set(), []
                # AI 키워드 상위 3개로 내규 검색 후 텍스트 병합
                for kw in (keywords[:3] or [query]):
                    try:
                        ires = icli.call_tool(itool.get("name"), _mcp_build_args(itool, kw))
                        txt = _mcp_extract_text(ires)
                        if txt and txt not in seen_blocks:
                            seen_blocks.add(txt); parts.append(txt)
                        st = ires.get("structuredContent") if isinstance(ires, dict) else None
                        if st and internal_struct is None:
                            internal_struct = st
                    except Exception as e:
                        print(f"[scenario] 내규 검색 오류(kw={kw}): {e}")
                internal_text = "\n\n".join(parts)
            else:
                internal_err = "내규 검색 도구 없음"
        except Exception as e:
            internal_err = str(e)
            print(f"[scenario] 내규 MCP 오류: {e}")

    # ── Step-4: 조회한 내규·법령을 근거로 자연어 답변 생성(RAG) ───────────────
    # 키워드 나열이 아니라, 실제 조문·내규 본문만 근거로 질문에 답하게 한다.
    answer, answer_err, sources = "", "", []
    ctx_parts, budget = [], 14000

    for r in results[:5]:                       # 법령 조문 근거
        ln = r.get("법령명한글", "")
        for a in (r.get("matched_articles") or [])[:4]:
            no = a.get("조문번호", ""); ti = a.get("조문제목", "")
            body_txt = re.sub(r"\s+", " ", (a.get("조문내용", "") or ""))[:700]
            tag = f"{ln} 제{no}조" + (f"({ti})" if ti else "")
            blk = f"[법령] {tag}\n{body_txt}"
            if budget - len(blk) < 0:
                break
            ctx_parts.append(blk); budget -= len(blk)
            sources.append({"type": "law", "label": tag})

    # 의미 검색으로 찾은 내규 조문 — 질문 어휘가 규정 표현과 달라도 근거를 잡아준다.
    sem_hits = []
    emb_available = False   # 내규 의미검색(임베딩)이 실제로 수행됐는지
    try:
        # 채팅 프로바이더와 무관하게 임베딩엔 Gemini 키를 쓴다(사용자 키→서버 키 폴백).
        gkey = (api_key if provider == "gemini" else "") or os.environ.get("GEMINI_API_KEY", "")
        emb_available = bool(gkey.strip())
        if emb_available:
            sem_hits = [h for h in semantic_search(query, gkey.strip(), top_k=10)
                        if h["score"] >= _SEM_MIN]
    except Exception as e:
        print(f"[scenario] 의미 검색 생략: {e}")
    for h in sem_hits[:8]:
        tag = f"{h['title']} 제{h['no']}조" + (f"({h['art_title']})" if h["art_title"] else "")
        prev = re.sub(r"\s+", " ", h.get("preview") or "")
        blk = "[내규] " + tag + "\n" + prev
        if budget - len(blk) < 0:
            break
        ctx_parts.append(blk); budget -= len(blk)
        sources.append({"type": "internal", "label": tag, "slug": h["slug"]})

    if internal_text:                            # 내규 근거(키워드 검색)
        itxt = re.sub(r"\n{3,}", "\n\n", internal_text)[:max(1500, min(5000, budget))]
        blk = f"[내규] 사내 규정 키워드 검색 결과\n{itxt}"
        ctx_parts.append(blk)
        sources.append({"type": "internal", "label": "사내 내규 검색 결과"})

    if ctx_parts and (api_key or provider == "ollama"):
        ctx = "\n\n---\n\n".join(ctx_parts)
        ans_system = (
            "당신은 한국농업기술진흥원(KOAT)의 법무·규정 담당 전문가입니다.\n"
            "아래 <자료>에 담긴 사내 내규와 국가법령 조문만을 근거로 질문에 답하세요.\n\n"
            "규칙:\n"
            "1. 자료에 없는 내용은 지어내지 말고, 필요하면 '제공된 자료에서 확인되지 않습니다'라고 밝히세요.\n"
            "2. 근거가 되는 조문을 문장 끝에 [내규명 제N조] 또는 [법령명 제N조] 형태로 표기하세요.\n"
            "3. 사내 내규와 국가법령이 모두 관련되면 '내규 기준'과 '법령 근거'를 나누어 설명하세요.\n"
            "4. 질문과 관계없는 자료는 언급하지 말고, 질문에 실제로 답하는 조문만 골라 쓰세요.\n"
            "5. 다음 순서를 모두 채워 완결된 답변을 쓰세요 — "
            "**결론**(1~2문장) → **근거 조문**(조문별로 무엇을 정하는지) → "
            "**절차**(실무자가 밟을 단계를 번호 목록으로) → **유의사항**.\n"
            "6. 한국어 존댓말, 마크다운(**굵게**, 번호·불릿 목록) 사용. "
            "1,000~1,500자로 충분히 설명하되 같은 말을 반복하지 마세요.\n"
            "7. 문장을 중간에 끊지 말고 반드시 끝맺으세요.\n"
            "8. 질문이 자료와 무관하면 억지로 답하지 말고 그 사실을 알린 뒤, "
            "어떤 규정을 확인하면 좋을지 안내하세요."
        )
        ans_user = f"<자료>\n{ctx}\n</자료>\n\n질문: {query}"
        answer, answer_err = _ai_generate(provider, api_key, mdl, ans_system, ans_user,
                                          max_tokens=4000, temperature=0.15)
        if answer_err:
            print(f"[scenario] 답변 생성 실패: {answer_err}")

    return jsonify({
        "success":    True,
        "query":      query,
        "category":   category,
        "basis_type": basis_type,
        "guidance":   guidance,
        "answer":     (answer or "").strip(),
        "answer_error": answer_err,
        "sources":    sources,
        "steps":      steps,
        "caution":    caution,
        "keywords":   keywords,
        "laws":       results,
        "internal_text":   internal_text,
        "internal_structured": internal_struct,
        "internal_error":  internal_err,
        # 내규 의미검색(임베딩) 수행 여부 — false면 UI가 '내규 근거 일부 미반영'을 안내.
        "internal_semantic": emb_available,
    })


@app.route("/api/law/articles")
def get_law_articles():
    """법령명으로 조문 전체 조회"""
    law_name = request.args.get("name", "").strip()
    if not law_name:
        return jsonify({"error": "name 파라미터가 필요합니다"}), 400
    try:
        # Step 1: XML 검색으로 법령MST 추출
        mst = _get_mst(law_name)

        # Step 2: MST로 조문 전문 조회 시도
        root = None
        tried = []

        if mst:
            for param_name in ("MST", "ID"):
                try:
                    root = _law_get_xml("lawService.do",
                                        {"target": "law", param_name: mst})
                    # 오류 메시지 확인
                    err_el = root.find(".//message") or root.find(".//Message")
                    if err_el is not None and err_el.text and "없" in err_el.text:
                        print(f"[articles] {param_name}={mst} → 오류: {err_el.text}")
                        root = None; tried.append(f"{param_name}={mst}(실패)")
                        continue
                    # 태그가 단 하나(Law)이고 내용에 "없습니다" 포함 확인
                    all_tags = {el.tag for el in root.iter()}
                    if len(all_tags) <= 2:
                        txt = "".join(el.text or "" for el in root.iter())
                        if "없" in txt:
                            root = None; tried.append(f"{param_name}={mst}(없음)")
                            continue
                    tried.append(f"{param_name}={mst}(성공)")
                    break
                except Exception as e:
                    tried.append(f"{param_name}={mst}({e})")
                    root = None

        # Step 3: 법령일련번호로 재시도 (XML 검색 결과에서)
        if root is None:
            try:
                search_root = _law_get_xml("lawSearch.do",
                                           {"target": "law", "query": law_name, "display": "1"})
                all_tags_s = {el.tag for el in search_root.iter()}
                print(f"[articles] XML 검색 태그: {sorted(all_tags_s)}")
                # 모든 가능한 ID 필드 시도
                for id_tag in ("법령MST", "법령Mst", "MST", "법령일련번호", "lsiSeq"):
                    id_el = search_root.find(f".//{id_tag}")
                    if id_el is not None and id_el.text and id_el.text.strip():
                        id_val = id_el.text.strip()
                        for param in ("MST", "ID"):
                            try:
                                r2 = _law_get_xml("lawService.do",
                                                  {"target": "law", param: id_val})
                                all_tags_r = {el.tag for el in r2.iter()}
                                if len(all_tags_r) > 3:
                                    root = r2
                                    tried.append(f"{param}={id_val}[{id_tag}](성공)")
                                    raise StopIteration
                            except StopIteration:
                                raise
                            except Exception as e2:
                                tried.append(f"{param}={id_val}({e2})")
            except StopIteration:
                pass
            except Exception as e3:
                print(f"[articles] Step3 오류: {e3}")

        # Step 4: 행정규칙(admrul) fallback — lawService.do 모두 실패한 경우
        if root is None:
            try:
                mst_admrul = _get_mst(law_name, target="admrul")
                if mst_admrul:
                    for param_name in ("MST", "ID"):
                        try:
                            r_adm = _law_get_xml("admRulService.do",
                                                 {"target": "admrul", param_name: mst_admrul},
                                                 timeout=(5, 20))
                            if _is_valid_law_xml(r_adm):
                                root = r_adm
                                tried.append(f"admrul:{param_name}={mst_admrul}(성공)")
                                break
                        except Exception as e_adm:
                            tried.append(f"admrul:{param_name}={mst_admrul}({e_adm})")
                else:
                    tried.append("admrul:MST 없음")
            except Exception as e4:
                print(f"[articles] Step4(admrul) 오류: {e4}")

        print(f"[articles] '{law_name}' 시도 내역: {tried}")

        if root is None:
            return jsonify({
                "success": True, "law_name": law_name, "count": 0, "articles": [],
                "message": f"조문 데이터를 가져올 수 없습니다. 법제처에서 직접 확인해주세요."
            })

        lname, law_date, articles = _parse_articles(root)
        return jsonify({"success": True, "law_name": lname or law_name,
                        "law_date": law_date,
                        "count": len(articles), "articles": articles})

    except req_lib.exceptions.ConnectTimeout:
        return jsonify({"error": "법제처 서버 연결 시간 초과 (5초). 잠시 후 다시 시도해주세요."}), 504
    except req_lib.exceptions.ReadTimeout:
        return jsonify({"error": "법제처 서버 응답 시간 초과. 법령 데이터가 클 수 있습니다. 잠시 후 다시 시도해주세요."}), 504
    except req_lib.exceptions.ConnectionError as e:
        return jsonify({"error": f"법제처 서버에 연결할 수 없습니다: {e}"}), 502
    except req_lib.exceptions.Timeout:
        return jsonify({"error": "법제처 API 응답 시간 초과"}), 504
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": f"오류: {e}"}), 500


@app.route("/api/favorites")
def get_favorites():
    return jsonify({"favorites": favorites})


@app.route("/api/favorites/toggle")
def toggle_favorite():
    global favorites
    name = request.args.get("name", "").strip()
    org  = request.args.get("org",  "").strip()
    typ  = request.args.get("type", "법률").strip()
    url  = request.args.get("url",  "").strip()
    if not name:
        return jsonify({"error": "name 필요"}), 400
    existing = next((f for f in favorites if f["name"] == name), None)
    if existing:
        favorites = [f for f in favorites if f["name"] != name]
        return jsonify({"added": False})
    else:
        favorites.insert(0, {"name": name, "org": org, "type": typ, "url": url})
        favorites = favorites[:30]
        return jsonify({"added": True})


@app.route("/api/favorites/remove")
def remove_favorite():
    global favorites
    name = request.args.get("name", "").strip()
    favorites = [f for f in favorites if f["name"] != name]
    return jsonify({"ok": True})


# ── AI 모델 자동 현행화 ─────────────────────────────────────────────────────
# Gemini는 모델명이 자주 갱신되므로 하드코딩하지 않고 ListModels로 최신을 고른다.
_AI_MODEL_FALLBACK = {"gemini": "gemini-flash-latest",
                      "claude": "claude-haiku-4-5-20251001",
                      "gpt": "gpt-4.1-mini", "openai": "gpt-4.1-mini"}
# 콜드 스타트에서 시나리오/해석 핫패스가 ListModels 왕복을 동기로 기다리지 않도록
# 즉시 반환할 기본 모델(캐시가 비었을 때 사용).
GEMINI_DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "") or "gemini-2.5-flash"
_GEMINI_MODEL_CACHE: dict = {"model": None, "ts": 0.0}
_GEMINI_CACHE_TTL = 6 * 3600          # 6시간
# 선호 Gemini 버전. 기본은 비워 두고 ListModels 기준 '사용 가능한 최신'을 자동 선택한다
# (현재 최신 계열은 3.x). 특정 버전으로 묶고 싶을 때만 GEMINI_MODEL_PREF=3.0 처럼 지정.
_GEMINI_PREF_DEFAULT = ""


# 텍스트 생성용이 아닌 계열(이미지·음성·로보틱스 등)은 generateContent 를 지원해도 제외한다.
_GEMINI_SKIP = re.compile(
    r"(image|nano-banana|tts|audio|native-audio|live|robotics|computer-use|omni|"
    r"embedding|deep-research|antigravity|gemma|lyria|veo|imagen)")


def _gemini_model_score(name: str):
    """모델명에서 (버전, 등급, 안정성) 점수를 뽑아 최신·상위 모델을 고른다."""
    n = name.lower()
    if _GEMINI_SKIP.search(n):
        return None
    m = re.search(r"gemini-(\d+)(?:\.(\d+))?", n)
    if not m:
        return None
    ver = int(m.group(1)) * 100 + int(m.group(2) or 0)
    # 등급: pro > flash > flash-lite  (본 서비스는 응답속도 중요 → flash 우대)
    if "flash-lite" in n:
        tier = 1
    elif "flash" in n:
        tier = 3
    elif "pro" in n:
        tier = 2
    else:
        tier = 0
    # 실험/프리뷰 버전은 안정 버전보다 후순위
    stable = 0 if re.search(r"(exp|preview|thinking|-\d{3,})", n) else 1
    return (ver, stable, tier)


def _gemini_latest_model(api_key: str) -> str:
    """Gemini ListModels로 모델명을 결정(6시간 캐시).

    GEMINI_MODEL_PREF(예: "4.6")로 선호 버전을 지정할 수 있고, 계정에서 해당 버전을
    쓸 수 있으면 그 버전을 우선 사용한다. 없으면 사용 가능한 최신 버전으로 폴백한다.
    (모델명을 고정하면 미출시/미허용 버전일 때 AI 기능 전체가 실패하므로 검증 후 사용)
    """
    now = time.time()
    if _GEMINI_MODEL_CACHE["model"] and now - _GEMINI_MODEL_CACHE["ts"] < _GEMINI_CACHE_TTL:
        return _GEMINI_MODEL_CACHE["model"]
    fallback = _AI_MODEL_FALLBACK["gemini"]
    if not api_key:
        return fallback
    try:
        r = _SESSION.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": api_key, "pageSize": 200}, timeout=8)
        if r.status_code != 200:
            print(f"[ai-model] ListModels 실패({r.status_code}) → 폴백 {fallback}")
            return fallback
        pref = (os.environ.get("GEMINI_MODEL_PREF") or _GEMINI_PREF_DEFAULT).strip()
        pref_ver = None
        if pref:
            pm = re.match(r"(\d+)(?:\.(\d+))?$", pref)
            if pm:
                pref_ver = int(pm.group(1)) * 100 + int(pm.group(2) or 0)
        best, best_score = None, None
        pbest, pbest_score = None, None      # 선호 버전 중 최적
        for m in (r.json().get("models") or []):
            name = (m.get("name") or "").split("/")[-1]
            methods = m.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            sc = _gemini_model_score(name)
            if not sc:
                continue
            if best_score is None or sc > best_score:
                best, best_score = name, sc
            if pref_ver is not None and sc[0] == pref_ver:
                if pbest_score is None or sc > pbest_score:
                    pbest, pbest_score = name, sc
            elif pref and not pref_ver and pref.lower() in name.lower():
                if pbest_score is None or sc > pbest_score:
                    pbest, pbest_score = name, sc
        if pbest:
            _GEMINI_MODEL_CACHE.update({"model": pbest, "ts": now})
            print(f"[ai-model] 선호 버전({pref}) 모델 사용: {pbest}")
            return pbest
        if pref:
            print(f"[ai-model] 선호 버전({pref}) 사용 불가 → 최신 버전으로 대체")
        if best:
            _GEMINI_MODEL_CACHE.update({"model": best, "ts": now})
            print(f"[ai-model] Gemini 최신 모델 선택: {best}")
            return best
    except Exception as e:
        print(f"[ai-model] 조회 오류: {e}")
    return fallback


def _default_model_for(provider: str, api_key: str = "") -> str:
    """프로바이더별 기본 모델.

    Gemini 는 캐시에 값이 있으면 그것을, 없으면 상수 기본값을 '즉시' 돌려준다.
    핫패스(시나리오/해석)에서 콜드 인스턴스가 ListModels 네트워크 왕복을 동기로
    기다려 플랫폼 504 로 죽는 것을 막기 위함이다. 동적 최신화는 캐시가 채워진
    뒤(또는 /api/ai/models 명시 호출 시)에만 반영된다.
    """
    if provider == "gemini":
        return _GEMINI_MODEL_CACHE.get("model") or GEMINI_DEFAULT_MODEL
    return _AI_MODEL_FALLBACK.get(provider, _AI_MODEL_FALLBACK["gemini"])


@app.route("/api/ai/models")
def ai_models():
    """현재 선택될 기본 모델 확인용(설정 화면 표시).

    이 엔드포인트는 명시 호출이므로 Gemini 는 실시간 ListModels 로 캐시를 채운다.
    """
    provider = (request.args.get("provider") or "gemini").lower()
    key = (request.args.get("api_key") or
           os.environ.get(f"{provider.upper()}_API_KEY", "")).strip()
    if provider == "gemini":
        mdl = _gemini_latest_model(key)
    else:
        mdl = _default_model_for(provider, key)
    return jsonify({"success": True, "provider": provider, "model": mdl,
                    "resolved": provider == "gemini" and bool(key),
                    "cached_at": _GEMINI_MODEL_CACHE["ts"] if provider == "gemini" else 0})


def _gemini_thinking_cfg(mdl: str):
    """모델 세대에 맞는 사고(thinking) 설정.

    Gemini 3.x 는 기본적으로 길게 '생각'하고, 그 토큰이 maxOutputTokens 를 함께 쓴다.
    설정하지 않으면 답변이 나오기도 전에 예산이 소진돼 본문이 잘린다(finishReason=MAX_TOKENS).
    """
    n = (mdl or "").lower()
    if "gemini-2.5" in n:
        return {"thinkingBudget": 0}          # 2.5 계열은 예산(int)만 받는다
    if re.search(r"gemini-(1\.5|2\.0)", n):
        return None                           # 사고 기능 없음
    return {"thinkingLevel": "low"}           # 3.x·'-latest' 별칭


def _ai_generate(provider: str, api_key: str, mdl: str, system: str, user: str,
                 max_tokens: int = 1800, temperature: float = 0.2,
                 json_mode: bool = False):
    """프로바이더 공통 텍스트 생성. 반환: (text, error_message). 실패 시 text=''."""
    try:
        if provider == "gemini":
            url = (f"https://generativelanguage.googleapis.com/v1beta/models"
                   f"/{mdl}:generateContent?key={api_key}")

            def call(limit, think):
                cfg = {"maxOutputTokens": limit, "temperature": temperature}
                if json_mode:
                    cfg["responseMimeType"] = "application/json"
                if think:
                    cfg["thinkingConfig"] = think
                return _ai_post_retry(lambda: req_lib.post(
                    url, timeout=60, headers={"Content-Type": "application/json"},
                    json={"contents": [{"parts": [{"text": f"{system}\n\n{user}"}]}],
                          "generationConfig": cfg}))

            think = _gemini_thinking_cfg(mdl)
            # 사고 토큰까지 감안해 넉넉히 잡는다(본문이 잘리는 것보다 낫다)
            limit = max(max_tokens, 3000)
            r = call(limit, think)
            if r.status_code == 400 and think and "thinking" in (r.text or "").lower():
                r = call(limit, None)           # 사고 설정 미지원 모델 폴백
            if r.status_code != 200:
                return "", _ai_error(r)[0]
            data = r.json()
            cands = data.get("candidates") or []
            if not cands:
                return "", "AI 응답이 비어 있습니다."

            def text_of(d):
                cs = d.get("candidates") or [{}]
                ps = cs[0].get("content", {}).get("parts") or []
                return "".join(p.get("text", "") for p in ps)

            txt = text_of(data)
            # 사고에 예산을 다 써서 본문이 잘렸으면 한 번 더 크게 잡아 재시도
            if cands[0].get("finishReason") == "MAX_TOKENS":
                r2 = call(min(limit * 3, 12000), think or {"thinkingLevel": "low"})
                if r2.status_code == 200:
                    t2 = text_of(r2.json())
                    if len(t2) > len(txt):
                        txt = t2
            if not txt.strip():
                return "", "AI가 본문을 생성하지 못했습니다(사고 토큰 초과). 잠시 후 다시 시도하세요."
            return txt, ""
        if provider == "claude":
            r = _ai_post_retry(lambda: req_lib.post(
                "https://api.anthropic.com/v1/messages", timeout=40,
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": mdl, "max_tokens": max_tokens, "temperature": temperature,
                      "system": system, "messages": [{"role": "user", "content": user}]}))
            if r.status_code != 200:
                return "", _ai_error(r)[0]
            return "".join(p.get("text", "") for p in r.json().get("content", [])), ""
        if provider in ("gpt", "openai"):
            r = _ai_post_retry(lambda: req_lib.post(
                "https://api.openai.com/v1/chat/completions", timeout=40,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"model": mdl, "temperature": temperature,
                      "max_tokens": max_tokens,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": user}]}))
            if r.status_code != 200:
                return "", _ai_error(r)[0]
            return r.json()["choices"][0]["message"]["content"], ""
        if provider == "ollama":
            base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
            r = req_lib.post(f"{base}/api/chat", timeout=60,
                             json={"model": mdl or "gemma2", "stream": False,
                                   "messages": [{"role": "system", "content": system},
                                                {"role": "user", "content": user}]})
            if r.status_code != 200:
                return "", f"Ollama 오류({r.status_code})"
            return (r.json().get("message") or {}).get("content", ""), ""
    except Exception as e:
        return "", f"AI 호출 오류: {e}"
    return "", f"지원하지 않는 프로바이더: {provider}"


def _ai_post_retry(do_post, tries=3):
    """AI 프로바이더 호출 — 과부하/일시오류(429/500/502/503/504)는 백오프 재시도."""
    import time as _t
    resp = None
    for i in range(tries):
        resp = do_post()
        if resp.status_code not in (429, 500, 502, 503, 504):
            return resp
        if i < tries - 1:
            _t.sleep(0.8 * (i + 1))
    return resp


def _ai_error(resp):
    """AI 응답 에러를 (사용자 메시지, 분류) 로 정규화."""
    try:
        msg = resp.json().get("error", {})
        msg = msg.get("message", "") if isinstance(msg, dict) else str(msg)
    except Exception:
        msg = (resp.text or "")[:200]
    low = (msg or "").lower()
    code = resp.status_code
    if code in (429, 503) or "overload" in low or "high demand" in low or "unavailable" in low or "quota" in low or "rate" in low:
        kind = "overload"
        user = f"AI 모델이 일시적으로 과부하 상태입니다 (재시도 후에도 실패). 잠시 뒤 다시 시도하거나 다른 모델을 선택하세요. (원문: {msg})"
    elif code in (401, 403) or "api key" in low or "permission" in low or "invalid" in low or "unauthenticated" in low:
        kind = "auth"
        user = f"API 키가 올바르지 않거나 권한이 없습니다. 키를 확인하세요. (원문: {msg})"
    else:
        kind = "error"
        user = msg or f"AI API 오류 ({code})"
    return user, kind


@app.route("/api/ai/interpret", methods=["POST"])
def ai_interpret():
    """멀티 프로바이더 조문 해석 (Claude / GPT / Gemini / Ollama)"""
    body = request.get_json(force=True) or {}
    provider    = body.get("provider", "claude").lower()
    api_key     = body.get("api_key", "").strip()
    model       = body.get("model", "").strip()
    law_name    = body.get("law_name", "")
    art_no      = body.get("art_no", "")
    art_title   = body.get("art_title", "")
    art_content = body.get("art_content", "")

    if not art_content:
        return jsonify({"error": "조문 내용이 없습니다."}), 400
    if provider != "ollama" and not api_key:
        return jsonify({"error": f"API 키가 없습니다. 상단 [✦ AI 설정]에서 {provider} 키를 입력하세요."}), 400

    system_prompt = f"""당신은 대한민국 농업·지식재산 분야 법률 전문 해석 AI입니다.
아래 조문을 **마크다운 형식**으로 구조화하여 해석하세요.

## 핵심 요약
이 조문이 말하는 핵심을 1~2문장으로.

## 쉬운 해설
법률 비전문가(농업인·중소기업 담당자)가 이해할 수 있도록 3~5문장으로 풀어서 설명하세요.

## 실무 포인트
농업인·기업 담당자가 주의해야 할 실질적 사항을 항목으로 정리하세요 (2~4개).

## 위반 시 제재 (해당하는 경우)
이 조문을 위반하면 어떤 불이익이 있는지 간략히.

---
대상 법령: 「{law_name}」 {art_no} {art_title}
답변은 한국어로, 각 섹션을 빠짐없이 작성하되 간결하게 유지하세요.
마크다운 **굵게**, ## 헤더, - 목록을 적극 활용하세요."""

    user_msg = f"조문 내용:\n{art_content}"

    try:
        # ── Claude ─────────────────────────────────────────────────────────────
        if provider == "claude":
            mdl = model or "claude-sonnet-4-5"
            resp = _ai_post_retry(lambda: req_lib.post(
                "https://api.anthropic.com/v1/messages",
                json={"model": mdl, "max_tokens": 1500,
                      "system": system_prompt,
                      "messages": [{"role": "user", "content": user_msg}]},
                headers={"Content-Type": "application/json",
                         "x-api-key": api_key,
                         "anthropic-version": "2023-06-01"},
                timeout=30,
            ))
            if resp.status_code != 200:
                user, kind = _ai_error(resp)
                return jsonify({"error": user, "kind": kind}), resp.status_code
            return jsonify({"result": resp.json()["content"][0]["text"]})

        # ── OpenAI GPT ─────────────────────────────────────────────────────────
        elif provider == "gpt":
            mdl = model or "gpt-4o"
            resp = _ai_post_retry(lambda: req_lib.post(
                "https://api.openai.com/v1/chat/completions",
                json={"model": mdl, "max_tokens": 1500,
                      "messages": [{"role": "system", "content": system_prompt},
                                   {"role": "user",   "content": user_msg}]},
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key}"},
                timeout=30,
            ))
            if resp.status_code != 200:
                user, kind = _ai_error(resp)
                return jsonify({"error": user, "kind": kind}), resp.status_code
            return jsonify({"result": resp.json()["choices"][0]["message"]["content"]})

        # ── Google Gemini ──────────────────────────────────────────────────────
        elif provider == "gemini":
            # 2.5/3.x 사고(thinking) 모델은 사고 토큰이 출력 예산을 잠식해
            # parts 가 비어 오는 경우가 있다. 이를 처리하는 공통 헬퍼로 통일한다.
            mdl = model or _default_model_for("gemini", api_key)
            text, err = _ai_generate("gemini", api_key, mdl,
                                     system_prompt, user_msg, max_tokens=1500)
            if err or not text:
                return jsonify({"error": err or "AI 응답이 비어 있습니다."}), 502
            return jsonify({"result": text})

        # ── Ollama (로컬) ──────────────────────────────────────────────────────
        elif provider == "ollama":
            base_url = (api_key or "http://localhost:11434").rstrip("/")
            mdl = model or "gemma3"
            resp = req_lib.post(
                f"{base_url}/api/chat",
                json={"model": mdl, "stream": False,
                      "messages": [{"role": "system", "content": system_prompt},
                                   {"role": "user",   "content": user_msg}]},
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            d = resp.json()
            if resp.status_code != 200:
                return jsonify({"error": d.get("error", "Ollama API 오류")}), resp.status_code
            return jsonify({"result": d["message"]["content"]})

        else:
            return jsonify({"error": f"지원하지 않는 프로바이더: {provider}"}), 400

    except req_lib.exceptions.Timeout:
        return jsonify({"error": f"AI 응답 시간 초과 ({provider})"}), 504
    except req_lib.exceptions.ConnectionError as e:
        return jsonify({"error": f"서버 연결 실패: {e}"}), 502
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/law/amendments")
def get_law_amendments():
    """
    최근 개정 조문 조회.
    1) 현재 법령 XML → 조문별 개정일 태그 탐색
    2) 개정일 태그 없으면 → 직전 버전 XML과 조문 내용 비교로 변경 감지
    3) 공포일자 기준 법령 자체 최신 개정 날짜와 함께 반환
    """
    law_name = request.args.get("name", "").strip()
    if not law_name:
        return jsonify({"error": "name 파라미터가 필요합니다"}), 400

    try:
        from datetime import datetime, timedelta

        # ── Step 1: 현재 법령 XML 취득 ─────────────────────────────────────────
        mst = _get_mst(law_name)
        root = None
        for param in ("MST", "ID"):
            if not mst: break
            try:
                r = _law_get_xml("lawService.do", {"target": "law", param: mst})
                if _is_valid_law_xml(r):
                    root = r; break
            except Exception:
                continue

        if root is None:
            return jsonify({"error": "법령 XML을 불러오지 못했습니다."}), 502

        law_name_real, law_date, articles = _parse_articles(root)
        law_date_fmt = re.sub(r"(\d{4})(\d{2})(\d{2})", r"\1.\2.\3", law_date) if law_date else ""

        # ── Step 2: 조문별 개정일 태그가 있는지 확인 ────────────────────────────
        amended_arts = [a for a in articles
                        if a.get("type") == "article" and a.get("amended_date")]

        if amended_arts:
            # 태그에서 개정일 직접 추출 성공
            # 최근 2년 이내 조문만 필터
            cutoff = (datetime.now() - timedelta(days=730)).strftime("%Y%m%d")
            recent = [a for a in amended_arts
                      if re.sub(r"\D", "", a.get("amended_date", "")) >= cutoff]
            recent.sort(key=lambda x: re.sub(r"\D", "", x.get("amended_date", "")), reverse=True)

            return jsonify({
                "success": True,
                "law_name": law_name_real or law_name,
                "law_date": law_date_fmt,
                "method": "tag",
                "amended_articles": [
                    {
                        "조문번호": a["조문번호"],
                        "조문제목": a["조문제목"],
                        "amended_date": re.sub(r"(\d{4})(\d{2})(\d{2})", r"\1.\2.\3",
                                               re.sub(r"\D", "", a["amended_date"])),
                        "amend_type": a.get("amend_type", "개정"),
                        "조문내용": a["조문내용"][:200] + ("..." if len(a["조문내용"]) > 200 else ""),
                    }
                    for a in recent
                ]
            })

        # ── Step 3: 태그 없으면 이전 버전 XML과 비교 ─────────────────────────
        # 법제처 JSON 검색으로 동일 법령 이전 버전 목록 취득
        hist_data = _law_get_json({"target": "law", "query": law_name, "display": "20"})
        hist_laws = hist_data.get("LawSearch", {}).get("law", []) or []
        if isinstance(hist_laws, dict): hist_laws = [hist_laws]

        # 현재 법령과 이름이 같은 것만 필터 → 공포일자 기준 정렬
        same_laws = [l for l in hist_laws
                     if (l.get("법령명한글") or "") == (law_name_real or law_name)]
        same_laws.sort(key=lambda x: x.get("공포일자", ""), reverse=True)

        prev_root = None
        if len(same_laws) >= 2:
            prev_law = same_laws[1]
            prev_no  = prev_law.get("법령일련번호", "")
            if prev_no:
                for param in ("ID", "MST"):
                    try:
                        pr = _law_get_xml("lawService.do", {"target": "law", param: prev_no})
                        if _is_valid_law_xml(pr):
                            prev_root = pr; break
                    except Exception:
                        continue

        changed = []
        if prev_root is not None:
            _, _, prev_articles = _parse_articles(prev_root)
            # 조문번호 기준으로 dict 구성
            prev_map = {a["조문번호"]: a for a in prev_articles if a.get("type") == "article"}
            curr_map = {a["조문번호"]: a for a in articles       if a.get("type") == "article"}

            for no, curr in curr_map.items():
                prev = prev_map.get(no)
                if prev is None:
                    # 신설 조문
                    changed.append({
                        "조문번호": no, "조문제목": curr["조문제목"],
                        "amended_date": law_date_fmt, "amend_type": "신설",
                        "조문내용": curr["조문내용"][:200] + ("..." if len(curr["조문내용"]) > 200 else ""),
                    })
                elif prev["조문내용"].strip() != curr["조문내용"].strip():
                    # 내용 변경
                    changed.append({
                        "조문번호": no, "조문제목": curr["조문제목"],
                        "amended_date": law_date_fmt, "amend_type": "개정",
                        "조문내용": curr["조문내용"][:200] + ("..." if len(curr["조문내용"]) > 200 else ""),
                    })
            for no, prev in prev_map.items():
                if no not in curr_map:
                    changed.append({
                        "조문번호": no, "조문제목": prev["조문제목"],
                        "amended_date": law_date_fmt, "amend_type": "삭제",
                        "조문내용": "(삭제된 조문)",
                    })

        return jsonify({
            "success": True,
            "law_name": law_name_real or law_name,
            "law_date": law_date_fmt,
            "method": "diff" if changed else "none",
            "amended_articles": changed,
        })

    except req_lib.exceptions.Timeout:
        return jsonify({"error": "법제처 API 응답 시간 초과"}), 504
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500



@app.route("/api/law/history")
def get_law_history():
    """법령 개정이력 조회"""
    law_name = request.args.get("name", "").strip()
    if not law_name:
        return jsonify({"error": "name 필요"}), 400
    try:
        # 법령 검색으로 이력 정보 수집
        data = _law_get_json({"target": "law", "query": law_name, "display": "20"})
        laws = data.get("LawSearch", {}).get("law", []) or []
        if isinstance(laws, dict): laws = [laws]

        # 같은 법령명의 이력 추출
        history = []
        for law in laws:
            name_k = law.get("법령명한글", "")
            # 이름이 유사한 것만 (정확히 같거나 포함)
            if law_name in name_k or name_k in law_name:
                pdate = law.get("공포일자", "")
                pno   = law.get("공포번호", "")
                typ   = law.get("법령구분명", "")
                if pdate:
                    date_str = re.sub(r"(\d{4})(\d{2})(\d{2})", r"\1.\2.\3", pdate)
                    desc = f"{typ} 공포" + (f" (법률 제{pno}호)" if pno else "")
                    history.append({"date": date_str, "desc": desc})

        history.sort(key=lambda x: x["date"], reverse=True)
        return jsonify({"success": True, "law_name": law_name, "history": history})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/law/prev_article")
def get_prev_article():
    """이전 버전 법령에서 특정 조문 내용 조회 (diff용)"""
    law_name = request.args.get("name", "").strip()
    art_no   = request.args.get("art_no", "").strip()
    if not law_name:
        return jsonify({"error": "name 필요"}), 400
    try:
        hist_data = _law_get_json({"target": "law", "query": law_name, "display": "20"})
        hist_laws = hist_data.get("LawSearch", {}).get("law", []) or []
        if isinstance(hist_laws, dict): hist_laws = [hist_laws]
        same = [l for l in hist_laws if (l.get("법령명한글","")) == law_name]
        same.sort(key=lambda x: x.get("공포일자",""), reverse=True)
        if len(same) < 2:
            return jsonify({"content": "(이전 버전 없음)"})
        prev = same[1]
        prev_no = prev.get("법령일련번호","")
        if not prev_no:
            return jsonify({"content": "(이전 버전 없음)"})
        for param in ("ID","MST"):
            try:
                root = _law_get_xml("lawService.do", {"target":"law", param: prev_no})
                _, _, arts = _parse_articles(root)
                art = next((a for a in arts
                            if a.get("type")=="article" and a.get("조문번호")==art_no), None)
                if art:
                    return jsonify({"content": art.get("조문내용",""),
                                    "date": re.sub(r"(\d{4})(\d{2})(\d{2})",r"\1.\2.\3",
                                                   prev.get("공포일자",""))})
            except Exception:
                continue
        return jsonify({"content": "(이전 버전 조문 없음)"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/law/art_history")
def get_art_history():
    """조문 단위 개정 히스토리 (버전별 내용 비교)"""
    law_name = request.args.get("name", "").strip()
    art_no   = request.args.get("art_no", "").strip()
    if not law_name:
        return jsonify({"error": "name 필요"}), 400
    try:
        hist_data = _law_get_json({"target": "law", "query": law_name, "display": "20"})
        hist_laws = hist_data.get("LawSearch", {}).get("law", []) or []
        if isinstance(hist_laws, dict): hist_laws = [hist_laws]
        same = [l for l in hist_laws if (l.get("법령명한글","")) == law_name]
        same.sort(key=lambda x: x.get("공포일자",""))

        history = []
        prev_content = None
        prev_date    = None

        for i, law in enumerate(same):
            lsi = law.get("법령일련번호","")
            if not lsi: continue
            root = None
            for param in ("ID","MST"):
                try:
                    r = _law_get_xml("lawService.do", {"target":"law", param: lsi})
                    if _is_valid_law_xml(r):
                        root = r; break
                except Exception:
                    continue
            if not root: continue

            try:
                _, _, arts = _parse_articles(root)
                art = next((a for a in arts
                            if a.get("type")=="article" and a.get("조문번호")==art_no), None)
                if not art: continue

                content = art.get("조문내용","")
                date_raw = law.get("공포일자","")
                date_fmt = re.sub(r"(\d{4})(\d{2})(\d{2})", r"\1.\2.\3", date_raw)
                is_last  = (i == len(same)-1)

                if prev_content is None:
                    entry_type = "신설"
                elif content.strip() != prev_content.strip():
                    entry_type = "개정"
                else:
                    prev_content = content
                    prev_date    = date_fmt
                    continue

                history.append({
                    "date": date_fmt,
                    "type": "현행" if is_last else entry_type,
                    "content": content,
                    "prev": prev_content,
                    "prev_date": prev_date or "",
                })
                prev_content = content
                prev_date    = date_fmt
            except Exception:
                continue

        history.reverse()
        return jsonify({"success": True, "law_name": law_name,
                        "art_no": art_no, "history": history})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ── 관련 판례·자치법규(조례) 연계 ────────────────────────────────────────────
def _fmt_law_date(s: str) -> str:
    """YYYYMMDD → YYYY.MM.DD (그 외 형식은 그대로 반환)"""
    d = re.sub(r"\D", "", s or "")
    return re.sub(r"(\d{4})(\d{2})(\d{2})", r"\1.\2.\3", d) if len(d) == 8 else (s or "").strip()


def _abs_law_url(link: str) -> str:
    """법제처 상세링크(상대경로 가능)를 절대 URL로 변환"""
    link = (link or "").strip()
    if not link:
        return ""
    if link.startswith("http"):
        return link
    if link.startswith("/"):
        return "https://www.law.go.kr" + link
    return "https://www.law.go.kr/" + link


def _search_root_items(data: dict, item_keys) -> list:
    """법제처 검색 JSON에서 결과 리스트 추출.
    루트 키(PrecSearch/OrdinSearch/LawSearch 등)와 항목 키가 타깃마다 달라
    최상위 dict 값들을 훑으며 item_keys 중 첫 매칭 리스트를 돌려준다."""
    if not isinstance(data, dict):
        return []
    for root_val in data.values():
        if isinstance(root_val, dict):
            for k in item_keys:
                v = root_val.get(k)
                if v:
                    return v if isinstance(v, list) else [v]
    return []


@app.route("/api/law/related")
def get_law_related():
    """법령명 기준 관련 판례·자치법규(조례) 연계 제안.
    법제처 통합검색 API의 판례(target=prec)·자치법규(target=ordin)를 병렬 조회한다."""
    law_name = request.args.get("name", "").strip()
    if not law_name:
        return jsonify({"error": "name 파라미터가 필요합니다"}), 400
    display = request.args.get("display", "12")

    def _do_prec():
        try:
            d = _law_get_json({"target": "prec", "query": law_name, "display": display},
                              timeout=(5, 12))
            out = []
            for it in _search_root_items(d, ("prec", "law")):
                if not isinstance(it, dict):
                    continue
                seq = (it.get("판례일련번호") or "").strip()
                out.append({
                    "title":     (it.get("사건명") or "").strip(),
                    "case_no":   (it.get("사건번호") or "").strip(),
                    "court":     (it.get("법원명") or "").strip(),
                    "date":      _fmt_law_date(it.get("선고일자")),
                    "case_type": (it.get("사건종류명") or "").strip(),
                    "id":        seq,
                    "url":       (f"https://www.law.go.kr/precInfoP.do?precSeq={seq}" if seq
                                  else _abs_law_url(it.get("판례상세링크"))),
                })
            return [o for o in out if o["title"]]
        except Exception as e:
            print(f"[related] prec 오류: {e}")
            return []

    def _do_ordin():
        try:
            d = _law_get_json({"target": "ordin", "query": law_name, "display": display},
                              timeout=(5, 12))
            out = []
            for it in _search_root_items(d, ("ordin", "law")):
                if not isinstance(it, dict):
                    continue
                nm = (it.get("자치법규명") or it.get("법령명한글") or "").strip()
                seq = (it.get("자치법규일련번호") or "").strip()
                out.append({
                    "name": nm,
                    "org":  (it.get("지자체기관명") or it.get("소관부처명") or "").strip(),
                    "kind": (it.get("자치법규종류") or "").strip(),
                    "date": _fmt_law_date(it.get("공포일자") or it.get("발령일자")),
                    "id":   seq,
                    "url":  (f"https://www.law.go.kr/ordinInfoP.do?ordinSeq={seq}" if seq
                             else _abs_law_url(it.get("자치법규상세링크"))),
                })
            return [o for o in out if o["name"]]
        except Exception as e:
            print(f"[related] ordin 오류: {e}")
            return []

    try:
        with _cf.ThreadPoolExecutor(max_workers=2) as ex:
            f_prec = ex.submit(_do_prec)
            f_ord  = ex.submit(_do_ordin)
            precedents = f_prec.result()
            ordinances = f_ord.result()
        print(f"[related] '{law_name}' 판례={len(precedents)} 자치법규={len(ordinances)}")
        return jsonify({
            "success":     True,
            "law_name":    law_name,
            "precedents":  precedents,
            "ordinances":  ordinances,
            "prec_count":  len(precedents),
            "ordin_count": len(ordinances),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ── 사규(내규) MCP 연동 ────────────────────────────────────────────────────────
# 외부 사규 MCP 서버(Streamable HTTP)를 호출하여 내규를 검색하고,
# 내규 본문 속 법령 참조는 프론트엔드에서 법령 조문 조회와 연계한다.
SAGYU_MCP_URL = os.environ.get(
    "SAGYU_MCP_URL", "https://tech-transfer-platform-zt79.vercel.app/mcp"
).strip()

# ── 내규 원본 PDF(Supabase Storage 등) ───────────────────────────────────────
# 원본 HWP/HWPX를 PDF로 변환해 올린 스토리지의 공개 URL 접두사.
#   예) https://xxxx.supabase.co/storage/v1/object/public/regulations/pdf
REG_PDF_BASE_URL = os.environ.get("REG_PDF_BASE_URL", "").strip().rstrip("/")
_REG_MANIFEST: list | None = None


def _load_reg_manifest() -> list:
    """규정명 → 원본 PDF 매핑(regulations_manifest.json). 없으면 빈 목록."""
    global _REG_MANIFEST
    if _REG_MANIFEST is not None:
        return _REG_MANIFEST
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "regulations_manifest.json")
        with open(p, encoding="utf-8") as f:
            _REG_MANIFEST = json.load(f)
    except Exception as e:
        print(f"[reg-manifest] 로드 실패: {e}")
        _REG_MANIFEST = []
    return _REG_MANIFEST


def _norm_key(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).lower()


def _save_reg_manifest(man: list) -> None:
    """manifest 저장 — 기존 파일과 같은 포맷(indent=1)으로 써서 diff 를 최소화한다."""
    tmp = REG_MANIFEST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, REG_MANIFEST_PATH)


def _find_reg_original(name: str) -> dict | None:
    """규정명으로 원본 PDF 항목을 찾는다(정확 일치 → 포함 관계)."""
    man = _load_reg_manifest()
    if not man:
        return None
    key = _norm_key(name)
    if not key:
        return None
    for m in man:                                  # 1) 정확 일치
        if _norm_key(m.get("title", "")) == key:
            return m
    cands = [m for m in man                        # 2) 포함 관계(가장 긴 제목 우선)
             if _norm_key(m.get("title", "")) and
             (_norm_key(m["title"]) in key or key in _norm_key(m["title"]))]
    if cands:
        return max(cands, key=lambda m: len(m.get("title", "")))
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  개정 내규 업로드 (HWPX·DOCX·HTML·TXT·PDF)
#    · 원본을 regulations/<슬러그>/ 에 보관하고 열람용 HTML 을 생성한다
#    · regulations_manifest.json 에 등록해 기존 내규 조회·전문 화면에서 바로 열린다
#    · 추출 본문(text.txt)은 내규 검색·전문 조회의 로컬 소스로 쓰인다
# ══════════════════════════════════════════════════════════════════════════════
import zipfile, io as _io, unicodedata
from datetime import datetime, timezone, timedelta

REG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regulations")
REG_MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "regulations_manifest.json")
# 업로드 토큰(설정 시 업로드에 필수) — 공개 배포본에서 무단 업로드 방지
REG_UPLOAD_TOKEN = os.environ.get("REG_UPLOAD_TOKEN", "").strip()
REG_UPLOAD_MAX_MB = int(os.environ.get("REG_UPLOAD_MAX_MB", "40"))
REG_CATEGORIES = ["정관", "규정", "규칙", "세칙", "예규", "매뉴얼", "기타"]
_ALLOWED_EXT = {".hwpx", ".hwp", ".docx", ".pdf", ".html", ".htm", ".txt", ".md"}
_KST = timezone(timedelta(hours=9))

# 규정명 끝말 → 내규 체계상의 구분.
# 기관 내규는 정관 > 규정 > 규칙 > 세칙 > 예규 순이고,
# 지침·요령·기준·수칙·계획 등 하위 문서는 모두 예규로 묶는다.
_REG_CAT_SUFFIX = (
    ("정관", "정관"),
    ("규정", "규정"),
    ("규칙", "규칙"),
    ("세칙", "세칙"),
    ("예규", "예규"),
    ("지침", "예규"), ("요령", "예규"), ("기준", "예규"), ("수칙", "예규"),
    ("준칙", "예규"), ("규준", "예규"), ("요강", "예규"), ("계획", "예규"),
    ("매뉴얼", "매뉴얼"), ("편람", "매뉴얼"), ("가이드", "매뉴얼"),
    ("안내서", "매뉴얼"), ("핸드북", "매뉴얼"),
)


def _guess_reg_category(title: str) -> str:
    """규정명으로 구분을 추정. 판단이 서지 않으면 '기타'.

    정관은 기관당 1건뿐이므로 이름이 '정관'으로 끝날 때만 인정한다.
    (예전에는 업로드 폼의 첫 선택지가 정관이라 '보직관리기준'처럼
     끝말이 목록에 없는 규정이 그대로 정관으로 등록되는 사고가 있었다.)
    """
    t = re.sub(r"\s+", "", (title or ""))
    t = re.sub(r"[(（\[【].*$", "", t)          # 뒤에 붙은 (제정 …)·[별표] 등 제거
    for suf, cat in _REG_CAT_SUFFIX:
        if t.endswith(suf):
            return cat
    return "기타"


def _now_kst() -> str:
    return datetime.now(_KST).strftime("%Y-%m-%d %H:%M")


def _reg_slug(title: str) -> str:
    """규정명 → 디렉터리 슬러그. 기존 manifest 규칙(공백→_)을 따른다."""
    s = unicodedata.normalize("NFC", (title or "").strip())
    s = re.sub(r"[\\/:*?\"<>|]+", "", s)          # 경로·윈도우 금지문자 제거
    s = re.sub(r"\s+", "_", s).strip("._")
    return s[:120]


def _reg_writable() -> bool:
    """regulations/ 에 실제로 쓸 수 있는지(Vercel 등 읽기전용 FS 판별)."""
    try:
        os.makedirs(REG_DIR, exist_ok=True)
        probe = os.path.join(REG_DIR, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return os.access(REG_MANIFEST_PATH, os.W_OK) or not os.path.exists(REG_MANIFEST_PATH)
    except Exception:
        return False


# ── 문서 → 블록(단락·표) 추출 ────────────────────────────────────────────────
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_blocks(root: ET.Element, para_tag: str, table_tag: str,
                row_tag: str, cell_tag: str, text_tags: set,
                break_tags: set) -> list:
    """
    OWPML(HWPX)·OOXML(DOCX) 공통 블록 추출.
    표는 단락 안에 중첩되어 나타나므로(HWPX: p > run > tbl) 표를 만나면
    앞까지의 텍스트를 단락으로 끊고 표 블록을 따로 만든다 — 표가 줄글로 풀리지 않게.
    """
    blocks = []
    buf = []

    def flush():
        txt = re.sub(r"[ \t]+", " ", "".join(buf)).strip()
        buf.clear()
        if not txt:
            blocks.append({"type": "p", "text": ""})
            return
        for line in txt.split("\n"):
            blocks.append({"type": "p", "text": line.strip()})

    def cell_text(tc: ET.Element) -> str:
        parts = []
        for el in tc.iter():
            t = _local(el.tag)
            if t in text_tags:
                parts.append(el.text or "")
            elif t == para_tag and parts:
                parts.append(" ")
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    def cell_span(tc: ET.Element):
        """셀 병합 정보(colspan, rowspan). HWPX는 hp:cellSpan, DOCX는 gridSpan/vMerge."""
        cs = rs = 1
        for sub in tc.iter():
            n = _local(sub.tag)
            if n == "cellSpan":                      # HWPX
                cs = int(sub.get("colSpan") or 1)
                rs = int(sub.get("rowSpan") or 1)
            elif n == "gridSpan":                    # DOCX
                try:
                    cs = int(list(sub.attrib.values())[0])
                except (ValueError, IndexError):
                    pass
        return max(cs, 1), max(rs, 1)

    def table_block(tbl: ET.Element):
        rows = []
        for tr in tbl.iter():
            if _local(tr.tag) != row_tag:
                continue
            cells = []
            for tc in tr:
                if _local(tc.tag) != cell_tag:
                    continue
                cs, rs = cell_span(tc)
                cells.append({"t": cell_text(tc), "cs": cs, "rs": rs})
            if cells:
                rows.append(cells)
        _merge_char_cells(rows)      # 세로쓰기로 글자마다 쪼개진 셀 복원
        while rows and not any(c["t"] for c in rows[0]):   # 앞뒤 빈 행 제거
            rows.pop(0)
        while rows and not any(c["t"] for c in rows[-1]):
            rows.pop()
        if not rows:
            return None
        # 1열 표는 제목·안내 박스로 쓰인 레이아웃 표 → 표 대신 단락으로
        if max(sum(c["cs"] for c in r) for r in rows) <= 1:
            for r in rows:
                blocks.append({"type": "p", "text": (r[0]["t"] if r else "").strip()})
            return None
        return {"type": "table", "rows": rows}

    def walk(node, in_para: bool):
        for child in list(node):
            tag = _local(child.tag)
            if tag == table_tag:
                flush()                       # 표 앞 텍스트를 단락으로 마무리
                tb = table_block(child)
                if tb:
                    blocks.append(tb)
                continue
            if tag == para_tag and not in_para:
                buf.clear()
                walk(child, True)
                flush()
                continue
            if tag in text_tags:
                buf.append(child.text or "")
                continue
            if tag in break_tags:
                buf.append("\n")
                continue
            walk(child, in_para)

    walk(root, False)
    if buf:
        flush()
    return blocks


# 업로드 zip(HWPX/DOCX) 압축 해제 폭탄(zip bomb) 방어용 상한
_ZIP_ENTRY_MAX = 80 * 1024 * 1024     # 단일 항목 최대 80MB(압축 해제 기준)
_ZIP_TOTAL_MAX = 200 * 1024 * 1024    # 누적 읽기 최대 200MB


class _ZipBudget:
    """zip 항목의 압축 해제 크기를 검사하며 안전하게 읽는 헬퍼."""
    def __init__(self, z):
        self.z = z
        self.total = 0

    def read(self, name: str) -> bytes:
        try:
            size = self.z.getinfo(name).file_size
        except KeyError:
            size = 0
        if size > _ZIP_ENTRY_MAX:
            raise ValueError("압축 해제 크기 제한 초과")
        if self.total + size > _ZIP_TOTAL_MAX:
            raise ValueError("압축 해제 크기 제한 초과")
        data = self.z.read(name)
        self.total += len(data)
        if self.total > _ZIP_TOTAL_MAX:
            raise ValueError("압축 해제 크기 제한 초과")
        return data


def _hwpx_blocks(raw: bytes) -> list:
    """HWPX(한/글 OWPML, zip) 본문 추출."""
    blocks = []
    with zipfile.ZipFile(_io.BytesIO(raw)) as z:
        budget = _ZipBudget(z)
        names = [n for n in z.namelist()
                 if re.match(r"Contents/section\d+\.xml$", n, re.I)]
        names.sort(key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        if not names:
            raise ValueError("HWPX 본문(Contents/section*.xml)을 찾을 수 없습니다.")
        for n in names:
            root = _xml_fromstring(budget.read(n))
            blocks += _xml_blocks(root, "p", "tbl", "tr", "tc",
                                  {"t"}, {"lineBreak"})
    return blocks


def _docx_blocks(raw: bytes) -> list:
    """DOCX(OOXML) 본문 추출."""
    with zipfile.ZipFile(_io.BytesIO(raw)) as z:
        budget = _ZipBudget(z)
        root = _xml_fromstring(budget.read("word/document.xml"))
    return _xml_blocks(root, "p", "tbl", "tr", "tc", {"t"}, {"br", "cr"})


def _text_blocks(text: str) -> list:
    return [{"type": "p", "text": l.rstrip()} for l in text.replace("\r\n", "\n").split("\n")]


_SCRIPT_RE = re.compile(
    r"<\s*(script|iframe|object|embed|applet|style)\b.*?<\s*/\s*\1\s*>",
    re.I | re.S)
_SCRIPT_OPEN_RE = re.compile(
    r"<\s*/?\s*(script|iframe|object|embed|applet|link|meta|base)\b[^>]*>", re.I)
_ON_ATTR_RE = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
# srcdoc/formaction 은 스크립트 실행 경로가 되므로 속성째 제거
_DANGER_ATTR_RE = re.compile(
    r"\s(srcdoc|formaction)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_JS_URL_RE = re.compile(
    r"(href|src)\s*=\s*(\"|')\s*(?:javascript|data|vbscript):[^\"']*(\2)", re.I)


def _sanitize_html(html: str) -> str:
    """업로드된 HTML에서 스크립트·이벤트 핸들러 제거(같은 출처에서 서빙되므로 필수).

    이는 심층 방어(defense-in-depth)일 뿐, 실제 신뢰 경계는 업로드 토큰이다.
    <table>/<tr>/<td>/<span>/<p>/<div>/style="..." 등 규정 서식에 필요한 요소는
    의도적으로 보존한다.
    """
    out = _SCRIPT_RE.sub("", html)
    out = _SCRIPT_OPEN_RE.sub("", out)
    out = _ON_ATTR_RE.sub("", out)
    out = _DANGER_ATTR_RE.sub("", out)
    out = _JS_URL_RE.sub(r"\1=\2#\2", out)
    return out


_ONE_HANGUL = re.compile(r"^[가-힣]$")


def _merge_char_cells(rows: list) -> list:
    """세로쓰기 라벨이 글자마다 별도 셀로 쪼개진 것을 한 셀로 합친다.

    한글 문서에서 '활 용 기' 같은 라벨은 칸을 나눠 글자를 하나씩 넣는 경우가 많다.
    그대로 두면 폭 좁은 빈 칸이 늘어서 표가 어수선해진다.
    합친 셀의 colspan 을 합계로 유지해 열 정렬은 그대로 둔다.
    (숫자·기호는 실제 자료일 수 있으므로 한글 한 글자만 대상으로 한다)
    """
    for r in rows:
        out, i = [], 0
        while i < len(r):
            j = i
            while (j < len(r)
                   and _ONE_HANGUL.match((r[j]["t"] or "").strip())
                   and r[j]["rs"] == r[i]["rs"]):
                j += 1
            if j - i >= 2:
                out.append({"t": "".join((c["t"] or "").strip() for c in r[i:j]),
                            "cs": sum(c["cs"] for c in r[i:j]),
                            "rs": r[i]["rs"]})
                i = j
            else:
                out.append(r[i])
                i += 1
        r[:] = out
    return rows


def _blocks_to_text(blocks: list) -> str:
    lines = []
    for b in blocks:
        if b["type"] == "table":
            for row in b["rows"]:
                lines.append(" | ".join(c["t"] for c in row))
        else:
            lines.append(b.get("text", ""))
    # 3줄 이상 연속 공백 줄은 2줄로 압축
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _esc(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


_ART_HEAD_RE = re.compile(r"^제\s*\d+\s*조(?:\s*의\s*\d+)?\s*(?:\(|$|\s)")
_CHAP_HEAD_RE = re.compile(r"^제\s*\d+\s*(?:편|장|절|관)\b")
_APX_HEAD_RE = re.compile(r"^\[?\s*(?:별표|별지|붙임|서식)")


def _table_html(rows: list) -> str:
    """병합(colspan/rowspan)을 반영해 표를 렌더. 첫 행이 머리글로 보일 때만 thead 사용."""
    def cells(row, tag):
        out = []
        for c in row:
            attr = ""
            if c["cs"] > 1:
                attr += f' colspan="{c["cs"]}"'
            if c["rs"] > 1:
                attr += f' rowspan="{c["rs"]}"'
            out.append(f'<{tag}{attr}>{_esc(c["t"])}</{tag}>')
        return "".join(out)

    if not rows:
        return ""
    # 머리글 판정: 첫 행이 모두 채워져 있고 짧으면 헤더로 본다.
    # (자료 행이 헤더로 올라가 열이 어긋나는 것을 막는다)
    first = rows[0]
    # 날짜·호수·순수 숫자가 있으면 머리글이 아니라 자료 행(예: 연혁 표의 '제정 2010.07.14 …')
    _data_like = re.compile(r"^\s*(?:\d{4}\s*[.\-]|제\s*[\d\-]+\s*호|[\d,]+)\s*\.?\s*$")
    is_head = (len(rows) > 1
               and all(c["t"].strip() for c in first)
               and all(len(c["t"]) <= 20 for c in first)
               and not any(c["rs"] > 1 for c in first)
               and not any(_data_like.match(c["t"]) for c in first))
    head = f"<thead><tr>{cells(first, 'th')}</tr></thead>" if is_head else ""
    body_rows = rows[1:] if is_head else rows
    tb = "".join(f"<tr>{cells(r, 'td')}</tr>" for r in body_rows)
    return f'<div class="tbl-wrap"><table>{head}<tbody>{tb}</tbody></table></div>'


def _blocks_to_view_html(title: str, meta: dict, blocks: list,
                         orig_name: str = "") -> str:
    """열람용 HTML 생성 — 조·장 제목을 구분해 기존 원본 뷰어와 동일하게 읽히도록."""
    body = []
    for b in blocks:
        if b["type"] == "table":
            body.append(_table_html(b["rows"]))
            continue
        t = (b.get("text") or "").strip()
        if not t:
            body.append('<p class="blank"></p>')
        elif _CHAP_HEAD_RE.match(t):
            body.append(f'<h2 class="chap">{_esc(t)}</h2>')
        elif _ART_HEAD_RE.match(t):
            body.append(f'<h3 class="art">{_esc(t)}</h3>')
        elif _APX_HEAD_RE.match(t):
            body.append(f'<h3 class="apx">{_esc(t)}</h3>')
        else:
            body.append(f"<p>{_esc(t)}</p>")

    metarows = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>"
        for k, v in [("규정 구분", meta.get("category")),
                     ("개정 구분", meta.get("revision")),
                     ("시행일자", meta.get("effective_date")),
                     ("담당 부서", meta.get("department")),
                     ("원본 파일", orig_name),
                     ("업로드", meta.get("uploaded_at"))] if v)
    note = meta.get("note") or ""
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>
 :root{{--tx:#1a1d21;--mu:#5b6472;--bd:#e5e8ec;--g:#1D9E75;--gl:#eafaf3;}}
 body{{font-family:'Malgun Gothic','맑은 고딕',system-ui,sans-serif;color:var(--tx);
   line-height:1.85;max-width:900px;margin:0 auto;padding:28px 26px 60px;font-size:15px;}}
 h1{{font-size:22px;text-align:center;margin:0 0 6px;letter-spacing:2px;}}
 .sub{{text-align:center;color:var(--mu);font-size:13px;margin-bottom:18px;}}
 .meta{{border-collapse:collapse;margin:0 auto 26px;font-size:13px;min-width:60%;}}
 .meta th,.meta td{{border:1px solid var(--bd);padding:5px 12px;text-align:left;}}
 .meta th{{background:var(--gl);color:var(--g);white-space:nowrap;font-weight:700;}}
 .note{{background:#fffbeb;border-left:3px solid #f59e0b;padding:8px 12px;
   font-size:13px;margin-bottom:22px;white-space:pre-wrap;}}
 h2.chap{{font-size:17px;margin:32px 0 12px;padding-bottom:5px;
   border-bottom:1px solid var(--bd);}}
 h3.art{{font-size:15px;margin:22px 0 6px;color:#0f172a;}}
 h3.apx{{font-size:15px;margin:26px 0 8px;color:var(--g);}}
 p{{margin:0 0 4px;white-space:pre-wrap;word-break:keep-all;}}
 p.blank{{height:8px;margin:0;}}
 .tbl-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:10px 0 18px;}}
 table{{border-collapse:collapse;font-size:12.5px;width:100%;table-layout:auto;}}
 th,td{{border:1px solid var(--bd);padding:5px 8px;vertical-align:top;
   word-break:keep-all;overflow-wrap:anywhere;line-height:1.55;}}
 thead th{{background:#f7f8fa;font-weight:700;text-align:center;}}
 /* 좁은 화면: 표를 원래 폭으로 두고 가로 스크롤(줄바꿈으로 뭉개지는 것 방지) */
 @media(max-width:820px){{ table{{width:auto;min-width:100%;}}
   th,td{{white-space:nowrap;}} }}
 @media print{{body{{padding:0;}}}}
</style></head><body>
<h1>{_esc(title)}</h1>
<div class="sub">{_esc(meta.get('revision') or '')}</div>
{f'<table class="meta">{metarows}</table>' if metarows else ''}
{f'<div class="note">{_esc(note)}</div>' if note else ''}
{chr(10).join(body)}
</body></html>"""


def _convert_upload(filename: str, raw: bytes, title: str, meta: dict) -> dict:
    """업로드 파일 → {view_html, text, converted, warning}."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".html", ".htm"):
        html = _sanitize_html(raw.decode("utf-8", errors="replace"))
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"[ \t]+", " ", text)
        return {"view_html": html, "text": re.sub(r"\n{3,}", "\n\n", text).strip(),
                "converted": True, "warning": ""}
    if ext == ".hwpx":
        blocks = _hwpx_blocks(raw)
    elif ext == ".docx":
        blocks = _docx_blocks(raw)
    elif ext in (".txt", ".md"):
        blocks = _text_blocks(_decode(raw))
    elif ext == ".pdf":
        # PDF 는 원본 그대로 열람(텍스트 추출은 외부 라이브러리 필요)
        src = "original.pdf"
        html = (f'<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">'
                f'<title>{_esc(title)}</title><style>html,body{{margin:0;height:100%;}}'
                f'embed{{width:100%;height:100%;border:0;}}</style></head><body>'
                f'<embed src="{src}" type="application/pdf"></body></html>')
        return {"view_html": html, "text": "", "converted": False,
                "warning": "PDF는 원본 그대로 열람됩니다. 본문 검색이 필요하면 HWPX 또는 DOCX로 올려주세요."}
    elif ext == ".hwp":
        blocks = []
    else:
        raise ValueError(f"지원하지 않는 형식입니다: {ext}")

    if ext == ".hwp":
        html = _blocks_to_view_html(
            title, meta,
            [{"type": "p", "text": "이 규정은 구버전 HWP(바이너리) 형식으로 업로드되어 "
                                   "본문을 자동 변환하지 못했습니다."},
             {"type": "p", "text": "한/글에서 '다른 이름으로 저장 → HWPX'로 저장해 다시 올리면 "
                                   "본문까지 조회·검색됩니다. 원본 파일은 아래 링크로 내려받을 수 있습니다."}],
            orig_name=filename)
        return {"view_html": html, "text": "", "converted": False,
                "warning": "HWP(구버전)는 본문 자동 변환을 지원하지 않습니다. HWPX로 저장해 올리면 본문까지 검색됩니다."}

    text = _blocks_to_text(blocks)
    if not text:
        raise ValueError("본문 텍스트를 추출하지 못했습니다. 파일이 손상되었는지 확인해주세요.")
    return {"view_html": _blocks_to_view_html(title, meta, blocks, orig_name=filename),
            "text": text, "converted": True, "warning": ""}


# ── 업로드된 내규의 로컬 본문 (내규 검색·전문 조회에 사용) ────────────────────
def _local_reg_text(slug: str) -> str:
    try:
        p = os.path.join(REG_DIR, slug, "text.txt")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return f.read()
    except Exception as e:
        print(f"[reg-upload] 로컬 본문 읽기 실패({slug}): {e}")
    return ""


def _uploaded_regs() -> list:
    return [m for m in _load_reg_manifest() if m.get("uploaded_at") or m.get("history")]


def _item_title(it) -> str:
    """구조화 검색 항목에서 규정명 추출(키 이름이 여러 형태)."""
    if isinstance(it, dict):
        return str(it.get("title") or it.get("name")
                   or it.get("제목") or it.get("규정명") or "")
    return ""


def _merge_local_hits(resp: dict, local_hits: list) -> dict:
    """업로드된 내규 검색 결과를 MCP 응답에 병합(업로드분을 앞쪽에 노출)."""
    if not local_hits:
        return resp
    resp["local"] = local_hits
    resp["local_count"] = len(local_hits)
    st = resp.get("structured")
    if isinstance(st, list):
        titles = {_norm_key(h["title"]) for h in local_hits}
        rest = [it for it in st
                if _norm_key(str((it or {}).get("title") or (it or {}).get("name") or "")) not in titles]
        resp["structured"] = local_hits + rest
    elif not st:
        resp["structured"] = local_hits
    return resp


def _local_reg_search(query: str, limit: int = 20) -> list:
    """업로드된 내규 본문에서 키워드 검색 → MCP 결과와 같은 형식의 항목 반환."""
    q = (query or "").strip()
    if not q:
        return []
    qL = q.lower()
    hits = []
    for m in _uploaded_regs():
        text = _local_reg_text(m.get("slug", ""))
        if not text:
            continue
        if qL in text.lower() or qL in _norm_key(m.get("title", "")):
            hits.append({"title": m.get("title", ""), "content": text,
                         "source": "upload", "revision": m.get("revision", ""),
                         "category": m.get("category", "")})
        if len(hits) >= limit:
            break
    return hits


# ── 번들된 규정 HTML 본문 인덱스 ──────────────────────────────────────────────
# regulations/<slug>/index.html 은 123건 전체가 있으나(의미검색 인덱스의 원천),
# 키워드 검색·전문 열람은 그동안 text.txt(업로드분)나 외부 MCP 에만 의존했다.
# 아래 헬퍼로 번들 HTML 본문을 검색·열람에 활용해 MCP 하드의존을 없앤다.
_REG_BODY_CACHE: dict = {}   # slug -> (mtime, 평문)


def _reg_body_text(slug: str) -> str:
    """규정 본문 평문. 업로드분 text.txt 우선, 없으면 번들 index.html 에서 추출."""
    if not slug:
        return ""
    t = _local_reg_text(slug)                       # 업로드분(text.txt)
    if t:
        return t
    path = os.path.join(REG_DIR, slug, "index.html")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    cached = _REG_BODY_CACHE.get(slug)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        import reg_chunks                            # stdlib 만 사용(지연 임포트)
        with open(path, encoding="utf-8", errors="replace") as f:
            body = reg_chunks.html_to_text(f.read())
    except Exception as e:
        print(f"[reg] 본문 추출 실패({slug}): {e}")
        return ""
    _REG_BODY_CACHE[slug] = (mtime, body)
    return body


def _catalog_reg_hits(query: str, exclude_titles=None, limit: int = 12) -> list:
    """번들 규정 HTML 전체에서 키워드 검색. MCP 가 못 준(또는 없을 때의) 규정을
    보완하기 위한 용도. exclude_titles(정규화된 규정명 set)는 건너뛴다."""
    q = (query or "").strip()
    if not q:
        return []
    qL = q.lower()
    exclude = exclude_titles or set()
    title_hits, body_hits = [], []
    for m in _load_reg_manifest():
        slug, title = m.get("slug", ""), m.get("title", "")
        if not slug or _norm_key(title) in exclude:
            continue
        name_match = qL in _norm_key(title) or qL in title.lower()
        body = _reg_body_text(slug)
        if not body:
            continue
        if not name_match and qL not in body.lower():
            continue
        item = {"title": title, "content": body[:20000], "source": "local",
                "revision": m.get("revision", ""), "category": m.get("category", "")}
        (title_hits if name_match else body_hits).append(item)
        if len(title_hits) + len(body_hits) >= limit:
            break
    return (title_hits + body_hits)[:limit]


def _local_only_search(query: str, local_hits: list, mcp_error: str = "") -> dict:
    """MCP 미설정/실패 시 번들 규정 본문만으로 검색 응답을 구성한다."""
    cat = _catalog_reg_hits(query, {_norm_key(h["title"]) for h in local_hits})
    resp = {"success": True, "query": query, "tool": "local-html",
            "text": "", "structured": local_hits + cat,
            "local_count": len(local_hits),
            "name_matches": _name_match_regs(query),
            "semantic": _semantic_for_search(query),
            "semantic_available": _semantic_available()}
    if mcp_error:
        resp["mcp_error"] = mcp_error
    return resp


REG_BACKUP_DIR = os.path.join(REG_DIR, ".backup")


def _backup_reg_dir(slug: str) -> str:
    """개정본으로 덮어쓰기 전 기존 규정 폴더를 보관한다(되돌리기용). 보관 경로명 반환."""
    src = os.path.join(REG_DIR, slug)
    if not os.path.isdir(src):
        return ""
    import shutil
    os.makedirs(REG_BACKUP_DIR, exist_ok=True)
    name = f"{slug}__{datetime.now(_KST).strftime('%Y%m%d-%H%M%S')}"
    dst = os.path.join(REG_BACKUP_DIR, name)
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    return name


def _restore_reg_dir(slug: str, backup_name: str) -> bool:
    """보관해 둔 이전 규정 폴더를 되돌린다."""
    if not backup_name:
        return False
    src = os.path.join(REG_BACKUP_DIR, os.path.basename(backup_name))
    dst = os.path.join(REG_DIR, slug)
    if not os.path.isdir(src):
        return False
    import shutil
    shutil.rmtree(dst, ignore_errors=True)
    shutil.move(src, dst)
    return True


def _write_reg_files(slug: str, view_html: str, text: str,
                     orig_filename: str, raw: bytes) -> str:
    """regulations/<slug>/ 에 열람 HTML·본문·원본을 쓴다. 저장된 원본 파일명 반환."""
    d = os.path.join(REG_DIR, slug)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
        f.write(view_html)
    if text:
        with open(os.path.join(d, "text.txt"), "w", encoding="utf-8") as f:
            f.write(text)
    ext = os.path.splitext(orig_filename)[1].lower()
    stored = ("original.pdf" if ext == ".pdf" else f"original{ext}")
    with open(os.path.join(d, stored), "wb") as f:
        f.write(raw)
    return stored


def _upsert_manifest(entry: dict, backup_name: str = "") -> dict:
    """
    manifest 에 등록/갱신. 같은 규정명이 있으면 개정판으로 교체하고
    이전 항목 전체를 이력에 남긴다(되돌리기로 복원 가능).
    """
    man = list(_load_reg_manifest())
    key = _norm_key(entry["title"])
    idx = next((i for i, m in enumerate(man)
                if _norm_key(m.get("title", "")) == key), -1)
    if idx >= 0:
        old = dict(man[idx])
        history = list(old.pop("history", None) or [])
        history.insert(0, {"revision": old.get("revision", ""),
                           "src": old.get("src", ""),
                           "replaced_at": entry.get("uploaded_at", ""),
                           "backup": backup_name,
                           "entry": old})
        entry = {**old, **entry, "history": history[:20]}
        man[idx] = entry
    else:
        man.append(entry)
    # 정렬하지 않는다 — 기존 항목 순서를 유지해 커밋 diff 를 최소화한다
    _save_reg_manifest(man)
    global _REG_MANIFEST
    _REG_MANIFEST = man
    return entry


def _upload_authorized() -> tuple[bool, str]:
    """업로드 허용 여부와 거부 사유를 반환한다.

    fail-closed 원칙: 업로드가 리포지토리에 그대로 커밋·배포되는 환경
    (_gh_enabled)에서 REG_UPLOAD_TOKEN 이 설정돼 있지 않으면 익명 업로드가
    저장소를 오염시킬 수 있으므로 거부한다. 토큰이 설정된 경우에는 일치해야
    한다. 로컬 쓰기 전용(비-GitHub) 개발 환경에서는 토큰 없이도 허용한다.
    """
    if not REG_UPLOAD_TOKEN:
        if _gh_enabled():
            return (False, "이 서버는 업로드가 리포지토리에 자동 커밋되므로 "
                           "REG_UPLOAD_TOKEN 설정이 필요합니다. 관리자에게 문의하세요.")
        return (True, "")
    tok = (request.form.get("token") or request.headers.get("X-Upload-Token") or "").strip()
    if tok == REG_UPLOAD_TOKEN:
        return (True, "")
    return (False, "업로드 토큰이 올바르지 않습니다.")


# ── GitHub 직접 커밋 (읽기전용 배포에서 업로드를 반영하는 경로) ────────────────
# 업로드 → 변환 → 리포지토리에 커밋 → Vercel 자동 배포.
# regulations/ 를 그대로 단일 출처로 유지하고, 개정 이력이 git 히스토리로 남는다.
def _env_clean(name: str, default: str = "") -> str:
    """환경변수 값 정리 — 붙여넣기 과정에서 딸려오는 따옴표·공백·개행을 털어낸다.

    Vercel 대시보드에 토큰을 붙여넣을 때 따옴표가 함께 들어가면 401 이 난다.
    """
    v = (os.environ.get(name, default) or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


GITHUB_TOKEN  = _env_clean("GITHUB_TOKEN")
GITHUB_REPO   = _env_clean("GITHUB_REPO").strip("/")               # 예: owner/repo
GITHUB_BRANCH = _env_clean("GITHUB_BRANCH", "main") or "main"
GITHUB_API    = _env_clean("GITHUB_API", "https://api.github.com").rstrip("/")
# GitHub 연결 점검 결과 캐시 — 업로드 화면에서 미리 알려주기 위한 용도
_GH_CHECK: dict = {"ts": 0.0, "ok": False, "error": ""}


def _gh_enabled() -> bool:
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def _gh_headers() -> dict:
    return {"Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _gh_hint(status: int, detail: str, path: str = "") -> str:
    """GitHub 오류를 담당자가 바로 조치할 수 있는 안내로 바꾼다."""
    d = (detail or "").lower()
    if status == 401:
        return ("GITHUB_TOKEN 이 유효하지 않습니다(만료·폐기되었거나 값이 잘못 입력됨). "
                "GitHub → Settings → Developer settings 에서 토큰을 새로 발급한 뒤 "
                "Vercel 환경변수 GITHUB_TOKEN 을 교체하고 재배포하세요. "
                "값에 따옴표·공백·줄바꿈이 섞이지 않았는지도 확인해주세요.")
    if status == 403:
        if "rate limit" in d:
            return "GitHub API 호출 한도를 초과했습니다. 잠시 후 다시 시도하세요."
        return (f"토큰에 저장소({GITHUB_REPO}) 쓰기 권한이 없습니다. "
                "Fine-grained 토큰이면 해당 저장소를 Repository access 에 포함하고 "
                "Contents 권한을 Read and write 로 설정하세요.")
    if status == 404:
        return (f"저장소나 브랜치를 찾을 수 없습니다(GITHUB_REPO={GITHUB_REPO or '미설정'}, "
                f"GITHUB_BRANCH={GITHUB_BRANCH}). 값이 'owner/repo' 형식인지, "
                "브랜치 이름이 맞는지, 비공개 저장소라면 토큰 권한 범위에 포함됐는지 확인하세요.")
    if status == 409:
        return "다른 커밋과 충돌했습니다. 잠시 후 다시 시도하세요."
    if status == 422:
        return f"GitHub 가 요청을 거부했습니다: {detail}"
    return f"GitHub 오류({status}): {detail}"


def _gh(method: str, path: str, **kw):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}{path}"
    r = _SESSION.request(method, url, headers=_gh_headers(), timeout=30, **kw)
    if r.status_code >= 400:
        detail = ""
        try:
            detail = (r.json() or {}).get("message", "")
        except Exception:
            detail = (r.text or "")[:160]
        print(f"[gh] {method} {path} → {r.status_code} {detail}")
        err = RuntimeError(_gh_hint(r.status_code, detail, path))
        err.status = r.status_code            # 404(정상 미존재)와 그 외 오류 구분용
        raise err
    return r.json() if r.content else {}


def _gh_check(force: bool = False) -> dict:
    """토큰·저장소·브랜치가 실제로 쓸 수 있는 상태인지 확인(5분 캐시).

    업로드를 끝까지 진행한 뒤에야 401 을 만나는 일이 없도록 화면에서 미리 알린다.
    """
    if not _gh_enabled():
        return {"ok": False, "error": ""}
    if not force and time.time() - _GH_CHECK["ts"] < 300:
        return {"ok": _GH_CHECK["ok"], "error": _GH_CHECK["error"]}
    try:
        _gh("GET", f"/git/ref/heads/{GITHUB_BRANCH}")
        _GH_CHECK.update({"ts": time.time(), "ok": True, "error": ""})
    except Exception as e:
        _GH_CHECK.update({"ts": time.time(), "ok": False, "error": str(e)})
    return {"ok": _GH_CHECK["ok"], "error": _GH_CHECK["error"]}


def _gh_commit_files(files: dict, message: str, deletes=None):
    """여러 파일을 한 커밋으로 반영. files={경로: bytes|str}, deletes=[경로].

    Git Data API(blob→tree→commit→ref)로 원자적으로 커밋한다.
    Contents API를 파일마다 호출하면 커밋이 쪼개지고 중간 실패 시 상태가 깨진다.
    """
    ref = _gh("GET", f"/git/ref/heads/{GITHUB_BRANCH}")
    head_sha = ref["object"]["sha"]
    base_tree = _gh("GET", f"/git/commits/{head_sha}")["tree"]["sha"]

    tree = []
    for path, content in (files or {}).items():
        if isinstance(content, str):
            content = content.encode("utf-8")
        blob = _gh("POST", "/git/blobs",
                   json={"content": base64.b64encode(content).decode("ascii"),
                         "encoding": "base64"})
        tree.append({"path": path, "mode": "100644", "type": "blob",
                     "sha": blob["sha"]})
    for path in (deletes or []):
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
    if not tree:
        raise RuntimeError("커밋할 파일이 없습니다.")

    new_tree = _gh("POST", "/git/trees",
                   json={"base_tree": base_tree, "tree": tree})
    commit = _gh("POST", "/git/commits",
                 json={"message": message, "tree": new_tree["sha"],
                       "parents": [head_sha]})
    _gh("PATCH", f"/git/refs/heads/{GITHUB_BRANCH}",
        json={"sha": commit["sha"], "force": False})
    return commit["sha"]


def _gh_get_manifest():
    """리포지토리의 현재 manifest 를 읽어온다(로컬 파일이 낡았을 수 있으므로).

    GitHub 연동이 켜진 상태에서 원격 읽기가 '네트워크/HTTP 오류'로 실패하면,
    낡은 로컬 manifest 로 커밋해 다른 인스턴스가 추가한 항목을 덮어써 유실시킬
    위험이 있다. 따라서 그런 경우엔 폴백하지 않고 예외를 올려 커밋을 중단시킨다.
    저장소에 아직 manifest 가 없는 정상적인 404 는 로컬/빈 목록으로 폴백해도 안전하다.
    """
    if not _gh_enabled():
        return list(_load_reg_manifest())
    try:
        d = _gh("GET", "/contents/regulations_manifest.json",
                params={"ref": GITHUB_BRANCH})
        raw = base64.b64decode(d.get("content", "") or "")
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        if getattr(e, "status", None) == 404:
            print(f"[gh] manifest 없음(404), 로컬 사용")
            return list(_load_reg_manifest())
        print(f"[gh] manifest 조회 실패(안전을 위해 중단): {e}")
        raise RuntimeError("저장소 상태를 읽지 못해 안전을 위해 중단했습니다. "
                           "잠시 후 다시 시도하세요.")


def _gh_dir(path: str, ref: str = ""):
    """저장소 디렉터리 목록. 없으면 []."""
    try:
        d = _gh("GET", f"/contents/{path}", params={"ref": ref or GITHUB_BRANCH})
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _gh_file(path: str, ref: str = ""):
    """저장소 파일 내용(bytes). 없으면 None."""
    try:
        d = _gh("GET", f"/contents/{path}", params={"ref": ref or GITHUB_BRANCH})
        if isinstance(d, dict) and d.get("content"):
            return base64.b64decode(d["content"])
        # 1MB 초과 파일은 content 가 비므로 blob 으로 받는다
        if isinstance(d, dict) and d.get("sha"):
            b = _gh("GET", f"/git/blobs/{d['sha']}")
            return base64.b64decode(b.get("content", "") or "")
    except Exception:
        pass
    return None


def _gh_prev_commit(path: str) -> str:
    """이 경로에 개정본이 올라오기 '직전' 상태의 커밋 sha. 없으면 ''.

    업로드 화면이 만든 커밋('내규 등록/개정: …')을 먼저 찾아 그 부모를 쓴다.
    그 뒤에 다른 수정 커밋이 끼어 있어도 개정 이전 원본을 정확히 되살리기 위함이다.
    """
    try:
        cs = _gh("GET", "/commits", params={"path": path, "sha": GITHUB_BRANCH,
                                            "per_page": 10})
        if not isinstance(cs, list) or not cs:
            return ""
        for c in cs:
            msg = ((c.get("commit") or {}).get("message") or "")
            if msg.startswith("내규 등록:") or msg.startswith("내규 개정:"):
                parents = c.get("parents") or []
                if parents:
                    return parents[0].get("sha", "")
                break
        return cs[1].get("sha", "") if len(cs) >= 2 else ""
    except Exception as e:
        print(f"[gh] 이전 커밋 조회 실패({path}): {e}")
    return ""


def _merge_manifest(man: list, entry: dict):
    """같은 규정명이 있으면 교체(이전 개정은 history 에 누적), 없으면 추가."""
    key = _norm_key(entry.get("title", ""))
    out, replaced, prev = [], False, None
    for m in man:
        if _norm_key(m.get("title", "")) == key:
            prev = {k: v for k, v in m.items() if k != "history"}
            hist = list(m.get("history") or [])
            # 이력 스키마를 _upsert_manifest 와 통일: 이전 개정 라벨 + 교체 시각 기록.
            # entry 는 유지(되돌리기 복원에 사용).
            hist.insert(0, {"revision": prev.get("revision", ""),
                            "replaced_at": entry.get("uploaded_at", ""),
                            "entry": prev})
            entry = dict(entry)
            entry["history"] = hist[:20]
            out.append(entry); replaced = True
        else:
            out.append(m)
    if not replaced:
        out.append(entry)
    return out, replaced


@app.route("/regulations/<path:subpath>")
def serve_regulation_file(subpath):
    """내규 원본 서식·업로드 문서 서빙(로컬 실행용 — Vercel은 vercel.json이 정적 처리)."""
    from flask import send_from_directory
    # 보관용 백업 폴더(.backup)는 노출하지 않는다
    if any(part.startswith(".") for part in subpath.replace("\\", "/").split("/")):
        return Response("<h1>404</h1>", status=404, mimetype="text/html; charset=utf-8")
    try:
        return send_from_directory(REG_DIR, subpath)
    except Exception:
        return Response("<h1>404 — 규정 파일을 찾을 수 없습니다</h1>",
                        status=404, mimetype="text/html; charset=utf-8")


@app.route("/upload")
@app.route("/upload.html")
def upload_page():
    """개정 내규 업로드 페이지."""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upload.html")
        with open(p, encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html; charset=utf-8")
    except FileNotFoundError:
        return Response("<h1>upload.html not found</h1>", status=404)


@app.route("/api/regs/upload/status")
def reg_upload_status():
    """업로드 가능 여부·카테고리·업로드 이력."""
    ups = _uploaded_regs()
    chk = _gh_check(force=bool(request.args.get("recheck")))
    return jsonify({
        "success": True,
        "writable": _reg_writable(),
        "github": _gh_enabled(),
        "github_repo": GITHUB_REPO if _gh_enabled() else "",
        "github_branch": GITHUB_BRANCH if _gh_enabled() else "",
        "github_ok": chk["ok"],
        "github_error": chk["error"],
        "token_required": bool(REG_UPLOAD_TOKEN),
        "max_mb": REG_UPLOAD_MAX_MB,
        "categories": REG_CATEGORIES,
        "allowed_ext": sorted(_ALLOWED_EXT),
        "total": len(_load_reg_manifest()),
        "uploaded": [
            {"title": m.get("title"), "revision": m.get("revision"),
             "category": m.get("category"), "slug": m.get("slug"),
             "html": m.get("html"), "src": m.get("src"),
             "uploaded_at": m.get("uploaded_at"),
             "uploader": m.get("uploader", ""),
             "searchable": bool(_local_reg_text(m.get("slug", ""))),
             "history": m.get("history") or []}
            for m in sorted(ups, key=lambda x: x.get("uploaded_at", ""), reverse=True)
        ],
    })


@app.route("/api/regs/names")
def reg_names_for_upload():
    """등록된 규정명 목록 — 업로드 화면의 '기존 규정 개정' 자동완성용."""
    man = _load_reg_manifest()
    return jsonify({"success": True, "names": [
        {"title": m.get("title", ""), "category": m.get("category", ""),
         "revision": m.get("revision", ""), "uploaded_at": m.get("uploaded_at", "")}
        for m in man if m.get("title")]})


@app.route("/api/regs/upload", methods=["POST"])
def reg_upload():
    """개정 내규 업로드 — 변환·저장·manifest 등록."""
    _ok, _why = _upload_authorized()
    if not _ok:
        return jsonify({"error": _why}), 401

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "파일을 선택해주세요."}), 400
    filename = os.path.basename(f.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_EXT:
        return jsonify({"error": f"지원하지 않는 형식입니다({ext}). "
                                f"가능: {', '.join(sorted(_ALLOWED_EXT))}"}), 400

    raw = f.read()
    if not raw:
        return jsonify({"error": "빈 파일입니다."}), 400
    if len(raw) > REG_UPLOAD_MAX_MB * 1024 * 1024:
        return jsonify({"error": f"파일이 너무 큽니다(최대 {REG_UPLOAD_MAX_MB}MB)."}), 413

    # 규정명: 입력값 우선, 없으면 파일명에서 추출 ("감사규정(2023년도 7월 일부개정).hwpx")
    title = (request.form.get("title") or "").strip()
    stem = os.path.splitext(filename)[0]
    m_par = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", stem)
    if not title:
        title = (m_par.group(1) if m_par else stem).strip()
    revision = (request.form.get("revision") or "").strip()
    if not revision and m_par:
        revision = m_par.group(2).strip()
    if not title:
        return jsonify({"error": "규정명을 입력해주세요."}), 400

    # 구분 결정: 개정판이면 기존 등록 구분을 잇고, 없으면 규정명으로 추정한다.
    # 사람이 고른 값이라도 이름과 어긋나는 '정관'은 받지 않는다(정관은 기관당 1건).
    category = (request.form.get("category") or "").strip()
    guessed = _guess_reg_category(title)
    prev_cat = ""
    for _m in (_load_reg_manifest() or []):
        if _norm_key(_m.get("title") or "") == _norm_key(title):
            prev_cat = (_m.get("category") or "").strip()
            break
    if category in ("", "자동", "자동 분류"):
        category = prev_cat or guessed
    if category == "정관" and guessed != "정관":
        print(f"[upload] '{title}' 구분 정관 → {prev_cat or guessed} 로 교정")
        category = prev_cat if prev_cat and prev_cat != "정관" else guessed
    if category not in REG_CATEGORIES:
        category = guessed

    meta = {
        "category": category,
        "revision": revision,
        "effective_date": (request.form.get("effective_date") or "").strip(),
        "department": (request.form.get("department") or "").strip(),
        "note": (request.form.get("note") or "").strip(),
        "uploader": (request.form.get("uploader") or "").strip()[:40],
        "uploaded_at": _now_kst(),
    }

    try:
        conv = _convert_upload(filename, raw, title, meta)
    except zipfile.BadZipFile:
        return jsonify({"error": "파일을 열 수 없습니다. HWPX/DOCX 파일이 손상되었을 수 있습니다."}), 400
    except Exception as e:
        return jsonify({"error": f"변환 실패: {e}"}), 400

    slug = _reg_slug(title)
    if not slug:
        return jsonify({"error": "규정명에서 저장 폴더명을 만들 수 없습니다."}), 400

    stored_ext = ".pdf" if ext == ".pdf" else ext
    entry = {
        "title": title,
        "revision": revision or meta["uploaded_at"][:10] + " 개정",
        "category": category,
        "slug": slug,
        "src": filename,
        # 기존 manifest 형식과 동일하게 인코딩하지 않은 경로로 저장
        "html": f"/regulations/{slug}/index.html",
        "pdf": f"pdf/{slug}.pdf",
        "effective_date": meta["effective_date"],
        "department": meta["department"],
        "note": meta["note"],
        "uploader": meta["uploader"],
        "uploaded_at": meta["uploaded_at"],
        "original": f"/regulations/{slug}/original{stored_ext}",
        "searchable": bool(conv["text"]),
    }

    # ── GitHub 직접 커밋: 읽기전용 배포에서도 업로드를 반영한다 ──
    want_zip = (request.args.get("as") or request.form.get("as") or "") == "zip"
    if not want_zip and _gh_enabled():
        try:
            base = f"regulations/{slug}"
            stored_name = f"original{stored_ext}"
            entry["original"] = f"/{base}/{stored_name}"
            man, replaced = _merge_manifest(_gh_get_manifest(), entry)
            files = {
                f"{base}/index.html": conv["view_html"],
                f"{base}/{stored_name}": raw,
                "regulations_manifest.json":
                    json.dumps(man, ensure_ascii=False, indent=1) + "\n",
            }
            if conv["text"]:
                files[f"{base}/text.txt"] = conv["text"]
            who = meta.get("uploader") or "익명"
            msg = (f"내규 {'개정' if replaced else '등록'}: {title}"
                   + (f" ({revision})" if revision else "")
                   + f"\n\n업로더: {who}"
                   + (f"\n개정사유: {meta['note']}" if meta.get("note") else "")
                   + "\n\n업로드 화면(/upload)에서 자동 커밋됨")
            sha = _gh_commit_files(files, msg)
            print(f"[reg-upload] GitHub 커밋 완료: {title} → {sha[:7]}")
            return jsonify({
                "success": True, "entry": entry, "replaced": replaced,
                "searchable": bool(conv["text"]), "warning": conv["warning"],
                "committed": True, "commit_sha": sha[:7],
                "commit_url": f"https://github.com/{GITHUB_REPO}/commit/{sha}",
                "message": "리포지토리에 커밋했습니다. 배포 반영까지 1~2분 걸립니다.",
            })
        except Exception as e:
            import traceback; traceback.print_exc()
            return jsonify({"error": f"GitHub 커밋 실패: {e}"}), 502

    # ── 읽기 전용 배포에서 GitHub 미설정: 변환 결과를 ZIP 으로 내려준다 ──
    if want_zip or not _reg_writable():
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            base = f"regulations/{slug}"
            z.writestr(f"{base}/index.html", conv["view_html"])
            if conv["text"]:
                z.writestr(f"{base}/text.txt", conv["text"])
            z.writestr(f"{base}/original{stored_ext}", raw)
            z.writestr("manifest_entry.json",
                       json.dumps(entry, ensure_ascii=False, indent=2))
            z.writestr("READ_ME.txt",
                       "이 ZIP 을 리포지토리 루트에 풀고 manifest_entry.json 의 내용을\n"
                       "regulations_manifest.json 배열에 추가(같은 규정명이 있으면 교체)한 뒤\n"
                       "커밋·푸시하면 배포본에 반영됩니다.\n")
        if not want_zip and not _reg_writable():
            print(f"[reg-upload] 읽기 전용 FS — ZIP 응답으로 대체: {title}")
        buf.seek(0)
        return Response(
            buf.read(), mimetype="application/zip",
            headers={"Content-Disposition":
                     f"attachment; filename*=UTF-8''{quote(slug)}.zip",
                     "X-Reg-Readonly": "1" if not _reg_writable() else "0",
                     "X-Reg-Warning": quote(conv["warning"] or "")})

    try:
        backup = _backup_reg_dir(slug)      # 기존 규정 폴더 보관(되돌리기용)
        stored = _write_reg_files(slug, conv["view_html"], conv["text"], filename, raw)
        entry["original"] = f"/regulations/{slug}/{stored}"
        entry = _upsert_manifest(entry, backup)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": f"저장 실패: {e}"}), 500

    print(f"[reg-upload] 등록 완료: {title} ({revision}) → {slug}")
    return jsonify({"success": True, "entry": entry,
                    "warning": conv["warning"],
                    "searchable": bool(conv["text"]),
                    "view_url": entry["html"],
                    "replaced": bool(entry.get("history"))})


def _gh_revert(slug: str):
    """GitHub 커밋으로 개정 되돌리기.

    이전 개정이 있으면 그 개정본을 올리기 직전 커밋에서 파일을 되살리고,
    신규 등록이었으면 폴더 파일을 지운다. manifest 와 함께 한 커밋으로 반영한다.
    """
    man = _gh_get_manifest()
    idx = next((i for i, m in enumerate(man) if m.get("slug") == slug), -1)
    if idx < 0:
        return jsonify({"error": "해당 규정을 찾을 수 없습니다."}), 404
    cur = man[idx]
    if not cur.get("uploaded_at") and not cur.get("history"):
        return jsonify({"error": "업로드로 등록된 규정만 되돌릴 수 있습니다."}), 400

    base = f"regulations/{slug}"
    now_files = [f.get("name") for f in _gh_dir(base) if f.get("type") == "file"]
    history = list(cur.get("history") or [])
    files, deletes, warning = {}, [], ""

    if history:                                   # ── 이전 개정본으로 복원 ──
        h = history.pop(0)
        prev = dict(h.get("entry") or {})
        prev.pop("history", None)
        if history:
            prev["history"] = history
        # 이번 개정을 커밋하기 직전 상태(= 이전 개정본)를 git 에서 되살린다
        ref = _gh_prev_commit(f"{base}/index.html")
        old_files = [f.get("name") for f in _gh_dir(base, ref)] if ref else []
        for name in old_files:
            data = _gh_file(f"{base}/{name}", ref)
            if data is not None:
                files[f"{base}/{name}"] = data
        for name in now_files:                    # 이전에 없던 파일(확장자 변경 등)은 정리
            if name not in old_files:
                deletes.append(f"{base}/{name}")
        if not files:
            warning = ("이전 개정본 파일을 저장소 이력에서 찾지 못해 등록 정보만 되돌렸습니다. "
                       "문서 내용은 현재 개정본이 그대로 남아 있습니다.")
            print(f"[gh-revert] 이전 파일 복원 실패: {base} (ref={ref or '없음'})")
        man[idx] = prev
        restored_rev = prev.get("revision", "")
    else:                                         # ── 신규 등록 → 등록 해제 ──
        man.pop(idx)
        deletes = [f"{base}/{n}" for n in now_files]
        restored_rev = ""

    files["regulations_manifest.json"] = (
        json.dumps(man, ensure_ascii=False, indent=1) + "\n")
    title = cur.get("title", slug)
    msg = (f"내규 되돌리기: {title}"
           + (f" → {restored_rev}" if restored_rev else " (등록 해제)")
           + "\n\n업로드 화면(/upload)에서 자동 커밋됨")
    sha = _gh_commit_files(files, msg, deletes=deletes)
    print(f"[gh-revert] 완료: {title} → {sha[:7]}")
    return jsonify({
        "success": True, "removed": title,
        "restored": bool(restored_rev), "restored_revision": restored_rev,
        "files_restored": not warning, "warning": warning,
        "committed": True, "commit_sha": sha[:7],
        "commit_url": f"https://github.com/{GITHUB_REPO}/commit/{sha}",
        "message": "리포지토리에 커밋했습니다. 배포 반영까지 1~2분 걸립니다.",
    })


@app.route("/api/regs/upload/delete", methods=["POST"])
def reg_upload_delete():
    """
    업로드한 개정 내규 되돌리기.
      · 이전 개정이 있으면 그 개정본(파일·manifest 항목)으로 복원한다
      · 이전 개정이 없으면(신규 등록) 등록을 해제하고 파일을 삭제한다
    """
    _ok, _why = _upload_authorized()
    if not _ok:
        return jsonify({"error": _why}), 401
    slug = (request.form.get("slug") or (request.json or {}).get("slug") or "").strip()
    if not slug or "/" in slug or "\\" in slug or slug.startswith("."):
        return jsonify({"error": "slug 값이 올바르지 않습니다."}), 400

    # 읽기 전용 배포(서버리스)에서는 업로드와 같은 경로로 GitHub 에 커밋해 되돌린다.
    if not _reg_writable():
        if not _gh_enabled():
            return jsonify({"error": "읽기 전용 환경이고 GitHub 연동도 없어 되돌릴 수 없습니다. "
                                     "GITHUB_TOKEN·GITHUB_REPO 를 설정하세요."}), 503
        try:
            return _gh_revert(slug)
        except Exception as e:
            import traceback; traceback.print_exc()
            return jsonify({"error": f"되돌리기 실패: {e}"}), 502

    man = list(_load_reg_manifest())
    idx = next((i for i, m in enumerate(man) if m.get("slug") == slug), -1)
    if idx < 0:
        return jsonify({"error": "해당 규정을 찾을 수 없습니다."}), 404
    cur = man[idx]
    if not cur.get("uploaded_at") and not cur.get("history"):
        return jsonify({"error": "업로드로 등록된 규정만 되돌릴 수 있습니다."}), 400

    history = list(cur.get("history") or [])
    restored_rev, files_restored = "", True
    if history:                                   # 이전 개정본으로 복원
        h = history.pop(0)
        prev = dict(h.get("entry") or {})
        prev.pop("history", None)
        if history:                               # 남은 이력이 없으면 키를 만들지 않는다
            prev["history"] = history
        files_restored = _restore_reg_dir(slug, h.get("backup", ""))
        man[idx] = prev
        restored_rev = prev.get("revision", "")
    else:                                         # 신규 등록 → 완전 삭제
        man.pop(idx)
        d = os.path.join(REG_DIR, slug)
        if os.path.isdir(d) and os.path.abspath(d).startswith(
                os.path.abspath(REG_DIR) + os.sep):
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    _save_reg_manifest(man)
    global _REG_MANIFEST
    _REG_MANIFEST = man

    return jsonify({"success": True, "removed": cur.get("title", ""),
                    "restored": bool(restored_rev),
                    "restored_revision": restored_rev,
                    "files_restored": files_restored,
                    "warning": ("" if files_restored else
                                "이전 개정본 파일 보관분을 찾지 못해 등록 정보만 되돌렸습니다. "
                                "regulations/ 폴더는 git 에서 복원해주세요(git checkout -- regulations/).")})


# ── 의미 검색(임베딩) + 키워드 하이브리드 ─────────────────────────────────────
# 벡터는 리포지토리에 함께 배포되는 int8 파일에서 읽는다(벡터DB 불필요).
_VEC_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "regulations_vectors.bin")
_VEC_META = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "regulations_vectors.json")
_VEC_CACHE: dict = {"loaded": False, "meta": None, "mat": None, "np": None}
# 코사인 하한. 관련 없는 조문은 대체로 0.5 아래에 몰려 있어 노이즈를 걸러낸다.
_SEM_MIN = float(os.environ.get("SEMANTIC_MIN_SCORE", "0.55"))


def _vec_load():
    """벡터 파일 로드(프로세스당 1회). numpy 가 없으면 의미 검색을 끈다."""
    if _VEC_CACHE["loaded"]:
        return _VEC_CACHE
    _VEC_CACHE["loaded"] = True
    try:
        import numpy as np
    except ImportError:
        print("[vec] numpy 미설치 — 의미 검색 비활성")
        return _VEC_CACHE
    try:
        with open(_VEC_META, encoding="utf-8") as f:
            meta = json.load(f)
        dim, cnt = meta["dim"], meta["count"]
        raw = np.fromfile(_VEC_BIN, dtype=np.int8)
        if raw.size != dim * cnt:
            print(f"[vec] 크기 불일치 {raw.size} != {dim*cnt} — 비활성")
            return _VEC_CACHE
        mat = raw.reshape(cnt, dim).astype(np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        _VEC_CACHE.update({"meta": meta, "mat": mat / norms, "np": np})
        print(f"[vec] 로드 {cnt:,}청크 × {dim}차원")
    except FileNotFoundError:
        pass                                   # 벡터 파일 없음 = 기능 미사용
    except Exception as e:
        print(f"[vec] 로드 실패: {e}")
    return _VEC_CACHE


def _embed_query(text: str, api_key: str, model: str, dim: int = 0):
    """질의 임베딩. 실패 시 None.

    문서 벡터를 MRL 로 축소해 저장했으면 질의도 같은 차원으로 뽑아야 한다.
    """
    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models"
               f"/{model}:embedContent?key={api_key}")
        body = {"model": f"models/{model}",
                "content": {"parts": [{"text": text}]},
                "taskType": "RETRIEVAL_QUERY"}
        if dim:
            body["outputDimensionality"] = dim
        r = _SESSION.post(url, timeout=15, json=body)
        if r.status_code != 200:
            print(f"[vec] 질의 임베딩 실패({r.status_code})")
            return None
        return r.json()["embedding"]["values"]
    except Exception as e:
        print(f"[vec] 질의 임베딩 오류: {e}")
        return None


def semantic_search(query: str, api_key: str, top_k: int = 20):
    """의미 검색. [{slug,title,no,art_title,preview,score}] 또는 []"""
    c = _vec_load()
    if c["mat"] is None or not api_key:
        return []
    np = c["np"]
    qv = _embed_query(query, api_key,
                      c["meta"].get("model", "gemini-embedding-001"),
                      dim=int(c["meta"].get("dim") or 0))
    if not qv or len(qv) != c["mat"].shape[1]:
        if qv:
            print(f"[vec] 질의 차원 불일치 {len(qv)} != {c['mat'].shape[1]}")
        return []
    q = np.asarray(qv, dtype=np.float32)
    n = float(np.linalg.norm(q)) or 1.0
    sims = c["mat"] @ (q / n)
    k = min(top_k, sims.shape[0])
    idx = np.argpartition(-sims, k - 1)[:k]
    idx = idx[np.argsort(-sims[idx])]
    chunks = c["meta"]["chunks"]
    return [{**chunks[int(i)], "score": float(sims[int(i)])} for i in idx]


def _semantic_for_search(query: str, top_k: int = 18):
    """내규 검색 응답에 실을 의미 검색 결과. 인덱스·키가 없으면 빈 목록."""
    try:
        key = (request.args.get("api_key")
               or os.environ.get("GEMINI_API_KEY", "")).strip()
        if not key or _vec_load()["mat"] is None:
            return []
        return [{"title": h["title"], "slug": h["slug"], "no": h["no"],
                 "art_title": h["art_title"], "preview": h["preview"],
                 "score": round(h["score"], 4)}
                for h in semantic_search(query, key, top_k=top_k)
                if h["score"] >= _SEM_MIN]
    except Exception as e:
        print(f"[internal-search] 의미 검색 생략: {e}")
        return []


def _semantic_available() -> bool:
    """의미 검색이 실제로 수행 가능한지(임베딩 키 + 벡터 인덱스 존재)."""
    key = (request.args.get("api_key") or os.environ.get("GEMINI_API_KEY", "")).strip()
    if not key:
        return False
    try:
        return _vec_load()["mat"] is not None
    except Exception:
        return False


@app.route("/api/internal/semantic")
def internal_semantic():
    """의미 검색 결과(규정 단위로 묶어 반환)."""
    q = (request.args.get("query") or "").strip()
    if len(q) < 2:
        return jsonify({"error": "검색어는 2자 이상 입력하세요"}), 400
    key = (request.args.get("api_key")
           or os.environ.get("GEMINI_API_KEY", "")).strip()
    c = _vec_load()
    if c["mat"] is None:
        return jsonify({"success": True, "available": False, "regs": [],
                        "message": "의미 검색 인덱스가 없습니다(벡터 파일 미배포)."})
    if not key:
        return jsonify({"success": True, "available": False, "regs": [],
                        "message": "Gemini 키가 필요합니다(AI 설정 또는 서버 환경변수)."})
    hits = semantic_search(q, key, top_k=int(request.args.get("top", 24)))
    regs: dict = {}
    for h in hits:
        r = regs.setdefault(h["title"], {"name": h["title"], "org": "사내 규정",
                                         "slug": h["slug"], "matched_articles": [],
                                         "score": 0.0})
        r["score"] = max(r["score"], h["score"])
        if len(r["matched_articles"]) < 5:
            r["matched_articles"].append({
                "조문번호": h["no"], "조문제목": h["art_title"],
                "조문내용": h["preview"]})
    out = sorted(regs.values(), key=lambda x: -x["score"])
    for r in out:
        r["matched_count"] = len(r["matched_articles"])
    return jsonify({"success": True, "available": True, "query": q,
                    "count": len(out), "regs": out,
                    "index": {"chunks": c["meta"]["count"],
                              "total_chunks": c["meta"].get("total_chunks"),
                              "complete": c["meta"].get("complete", True),
                              "model": c["meta"].get("model"),
                              "built_at": c["meta"].get("built_at")}})


@app.route("/api/internal/names")
def internal_names():
    """내규 명칭 목록(원본 manifest 기준) — 본문 참조가 내규인지 법령인지 판별용."""
    man = _load_reg_manifest()
    items = []
    for m in man:
        if not m.get("title"):
            continue
        # 개정 이력: 업로드 시 누적된 이전 개정본(최신순)
        hist = []
        for h in (m.get("history") or [])[:20]:
            e = h.get("entry") or h
            rv = (e.get("revision") or h.get("revision") or "").strip()
            if rv:
                hist.append({"revision": rv,
                             "effective_date": e.get("effective_date", ""),
                             "uploaded_at": e.get("uploaded_at", ""),
                             # 이 이전 개정본이 '언제 교체(개정)되었는지'
                             "replaced_at": h.get("replaced_at", "")})
        items.append({"title": m.get("title", ""), "category": m.get("category", ""),
                      "revision": m.get("revision", ""),
                      "effective_date": m.get("effective_date", ""),
                      "uploaded_at": m.get("uploaded_at", ""),
                      "history": hist})
    return jsonify({"success": True, "count": len(items), "names": items})


def _name_match_regs(query: str, limit: int = 30) -> list:
    """규정 '명칭'에 검색어가 포함된 내규 목록(본문 검색과 병행해 완성도를 높임)."""
    man = _load_reg_manifest()
    if not man or not query:
        return []
    q = _norm_key(query)
    hits = []
    for m in man:
        t = m.get("title", "")
        if not t:
            continue
        nt = _norm_key(t)
        if q in nt:
            exact = (nt == q)               # 규정명과 100% 일치
            # 완전일치 최우선 → 앞부분 일치 → 짧은 이름
            score = (10000 if exact else 0) + 200 - nt.index(q) - len(nt) * 0.1
            hits.append((score, {"title": t, "category": m.get("category", ""),
                                 "revision": m.get("revision", ""),
                                 "match": "exact" if exact else "name",
                                 "exact": exact}))
    hits.sort(key=lambda x: -x[0])
    return [h[1] for h in hits[:limit]]


@app.route("/api/internal/original")
def internal_original():
    """내규 원본(PDF) 위치 조회 — 전문 화면의 '원본 보기/다운로드'용."""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name 파라미터가 필요합니다"}), 400
    m = _find_reg_original(name)
    if not m:
        return jsonify({"success": True, "found": False, "name": name})
    out = {"success": True, "found": True, "name": name,
           "title": m.get("title"), "revision": m.get("revision"),
           "category": m.get("category"), "source_file": m.get("src", "")}
    # 원본 서식 HTML — 리포에 함께 배포되므로 항상 사용 가능
    slug = m.get("slug", "")
    if slug:
        out["html_url"] = "/regulations/" + quote(slug) + "/index.html"
    # PDF는 별도 스토리지를 설정한 경우에만
    if REG_PDF_BASE_URL:
        fname = os.path.basename(m.get("pdf", "")) or (slug + ".pdf")
        out["pdf_url"] = f"{REG_PDF_BASE_URL}/{quote(fname)}"
    return jsonify(out)
# MCP 도구 목록 캐시 (URL → {tools, ts}) — 워밍된 프로세스에서 tools/list 왕복 절약
_MCP_TOOLS_CACHE: dict = {}
# 내규 검색 키워드 후보(도구/인자 자동 선택용)
_MCP_SEARCH_HINTS = ("search", "검색", "find", "query", "lookup", "조회", "retrieve")
_MCP_RULE_HINTS   = ("rule", "규정", "사규", "내규", "regulation", "정관",
                     "지침", "policy", "bylaw", "문서", "document")


def _mcp_parse_response(resp) -> dict:
    """MCP 응답을 파싱. application/json 또는 text/event-stream(SSE) 모두 지원."""
    ct = (resp.headers.get("Content-Type") or "").lower()
    text = _decode(resp.content).strip()
    if "text/event-stream" in ct or text.startswith("event:") or "\ndata:" in text:
        # SSE: 'data:' 라인들에서 마지막으로 파싱 가능한 JSON을 사용
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    return json.loads(chunk)
                except Exception:
                    continue
        raise ValueError("SSE 응답 파싱 실패")
    return json.loads(text)


class _McpClient:
    """최소 기능 MCP Streamable HTTP 클라이언트 (initialize → tools/list → tools/call)."""
    def __init__(self, url: str):
        self.url = url
        self.sid = None
        self._id = 0
        self._tools = None

    def _post(self, method: str, params=None, notify: bool = False, timeout=(5, 25)):
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if not notify:
            payload["id"] = self._id
        if params is not None:
            payload["params"] = params
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        r = req_lib.post(self.url, json=payload, headers=headers, timeout=timeout)
        sid = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")
        if sid:
            self.sid = sid
        r.raise_for_status()
        if notify or not (r.content or b"").strip():
            return None
        data = _mcp_parse_response(r)
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(data["error"].get("message", "MCP 오류"))
        return data.get("result") if isinstance(data, dict) else data

    def initialize(self):
        res = self._post("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "agro-law", "version": "1.0"},
        })
        try:
            self._post("notifications/initialized", notify=True)
        except Exception:
            pass
        return res

    def list_tools(self):
        if self._tools is None:
            ent = _MCP_TOOLS_CACHE.get(self.url)
            if ent and (time.time() - ent["ts"] < 300):   # 5분 캐시 (워밍된 프로세스에서 왕복 절약)
                self._tools = ent["tools"]
            else:
                res = self._post("tools/list", {}) or {}
                self._tools = res.get("tools", []) or []
                if self._tools:
                    _MCP_TOOLS_CACHE[self.url] = {"tools": self._tools, "ts": time.time()}
        return self._tools

    def call_tool(self, name: str, args: dict):
        return self._post("tools/call", {"name": name, "arguments": args})


def _mcp_pick_search_tool(tools: list):
    """검색 성격의 도구를 휴리스틱으로 선택."""
    def score(t):
        blob = (str(t.get("name", "")) + " " + str(t.get("description", ""))).lower()
        s = 0
        if any(k in blob for k in _MCP_SEARCH_HINTS): s += 3
        if any(k in blob for k in _MCP_RULE_HINTS):   s += 2
        if any(k in blob for k in ("list", "목록")):   s += 1
        return s
    if not tools:
        return None
    ranked = sorted(tools, key=score, reverse=True)
    return ranked[0] if score(ranked[0]) > 0 else tools[0]


# 전문(전체 문서) 조회 성격의 도구 힌트 (검색/목록보다 우선)
_MCP_DOC_HINTS = ("get", "read", "detail", "fetch", "retrieve", "document",
                  "view", "content", "전문", "원문", "본문", "내용", "조회", "상세")

def _mcp_pick_doc_tool(tools: list):
    """전문(전체 문서) 조회 도구를 우선 선택. 없으면 None(→ 검색 도구 폴백)."""
    def score(t):
        blob = (str(t.get("name", "")) + " " + str(t.get("description", ""))).lower()
        s = 0
        if any(k in blob for k in _MCP_DOC_HINTS): s += 4
        if any(k in blob for k in _MCP_RULE_HINTS): s += 2
        # 검색/목록 도구는 전문 조회 목적에는 부적합 → 감점
        if any(k in blob for k in ("search", "검색", "list", "목록")): s -= 3
        return s
    if not tools:
        return None
    ranked = sorted(tools, key=score, reverse=True)
    return ranked[0] if score(ranked[0]) > 0 else None


def _mcp_build_doc_args(tool: dict, name: str, doc_id: str = "") -> dict:
    """전문 조회 도구 인자 구성. id 계열 속성이 있고 doc_id가 있으면 id 우선."""
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    props = schema.get("properties") or {}
    if props and doc_id:
        for k in props:
            if any(x in k.lower() for x in ("id", "seq", "no", "번호", "코드", "code")):
                return {k: doc_id}
    if props:
        # 이름/제목 계열 우선
        for k in list(schema.get("required") or []) + list(props):
            if any(x in k.lower() for x in ("name", "title", "규정", "제목", "명")):
                return {k: name}
    return _mcp_build_args(tool, name)


def _mcp_build_args(tool: dict, query: str) -> dict:
    """도구 inputSchema에서 질의 문자열을 담을 속성을 선택해 인자 구성."""
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    if not props:
        return {"query": query}
    def is_str(p):
        t = p.get("type")
        return t == "string" or (isinstance(t, list) and "string" in t)
    order = list(required) + [k for k in props if k not in required]
    chosen = None
    for k in order:
        p = props.get(k, {})
        if is_str(p):
            chosen = chosen or k
            if any(x in k.lower() for x in
                   ("query", "keyword", "term", "search", "text", "q", "name", "title", "검색")):
                chosen = k
                break
    args = {chosen or "query": query}
    return args


def _mcp_extract_text(result) -> str:
    """tools/call 결과의 content 배열에서 텍스트 추출."""
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    parts = []
    for c in result.get("content", []) or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text" and c.get("text"):
            parts.append(c["text"])
        elif isinstance(c.get("resource"), dict) and c["resource"].get("text"):
            parts.append(c["resource"]["text"])
    return "\n\n".join(parts).strip()


@app.route("/api/internal/status")
def internal_status():
    """사규 MCP 연결 상태 및 사용 가능한 도구 목록 확인."""
    if not SAGYU_MCP_URL:
        return jsonify({"connected": False, "message": "SAGYU_MCP_URL 미설정"})
    try:
        cli = _McpClient(SAGYU_MCP_URL)
        info = cli.initialize()
        tools = cli.list_tools()
        picked = _mcp_pick_search_tool(tools)
        return jsonify({
            "connected": True,
            "server": (info or {}).get("serverInfo", {}),
            "tools": [{"name": t.get("name"), "description": t.get("description", "")} for t in tools],
            "search_tool": picked.get("name") if picked else None,
        })
    except Exception as e:
        return jsonify({"connected": False, "message": str(e)})


@app.route("/api/internal/search")
def internal_search():
    """사규 MCP로 내규 검색. 결과 텍스트/구조화 데이터를 반환하며,
    본문 속 법령 참조는 프론트엔드에서 법령 조문 조회와 연계한다."""
    query = request.args.get("query", "").strip()
    if not query:
        return jsonify({"error": "검색어를 입력하세요"}), 400
    if len(query) < 2:
        return jsonify({"error": "검색어는 2자 이상 입력하세요"}), 400

    # 업로드된 개정 내규는 로컬 본문에서 먼저 찾는다(MCP 서버에는 아직 없음)
    local_hits = _local_reg_search(query)

    if not SAGYU_MCP_URL:
        # MCP 미설정이어도 번들된 규정 본문으로 자체 검색한다(외부 하드의존 제거).
        return jsonify(_local_only_search(query, local_hits))
    try:
        cli = _McpClient(SAGYU_MCP_URL)
        cli.initialize()
        tools = cli.list_tools()
        tool = _mcp_pick_search_tool(tools)
        if not tool:
            return jsonify({"error": "사규 MCP에서 사용 가능한 도구가 없습니다."}), 502
        # 결과 완성도: limit류 인자를 크게 채워 더 많은 규정을 받아옴
        args = _mcp_fill_limits(tool, _mcp_build_args(tool, query), big=50)
        result = cli.call_tool(tool.get("name"), args)
        text = _mcp_extract_text(result)
        structured = result.get("structuredContent") if isinstance(result, dict) else None
        is_error = bool(result.get("isError")) if isinstance(result, dict) else False
        if is_error and not local_hits:
            return jsonify({"error": text or "내규 검색 중 오류가 발생했습니다."}), 502
        resp = {
            "success": True,
            "query": query,
            "tool": tool.get("name"),
            "arguments": args,
            "text": "" if is_error else text,
            "structured": structured,
            # 명칭 검색 결과(본문 검색과 병행) — 규정명에 검색어가 들어간 내규
            "name_matches": _name_match_regs(query),
            # 의미 검색(임베딩) — 어휘가 달라 키워드로 못 찾는 조문을 보완
            "semantic": _semantic_for_search(query),
            "semantic_available": _semantic_available(),
        }
        merged = _merge_local_hits(resp, local_hits)
        # MCP·로컬·명칭검색에 없는 규정을 번들 본문으로 보강(결과 뒤에 덧붙임 —
        # MCP 순위는 유지). MCP 가 놓친 규정의 커버리지만 채운다.
        present = {_norm_key(_item_title(it)) for it in (merged.get("structured") or [])}
        present |= {_norm_key(h.get("title", "")) for h in (merged.get("name_matches") or [])}
        cat = _catalog_reg_hits(query, present)
        if cat:
            merged["structured"] = (merged.get("structured") or []) + cat
        return jsonify(merged)
    except (req_lib.exceptions.Timeout,
            req_lib.exceptions.ConnectionError) as e:
        # MCP 서버가 죽어도 번들된 규정 본문으로 계속 검색되도록 한다
        return jsonify(_local_only_search(
            query, local_hits, f"사규 MCP 서버 연결 실패: {e}"))
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(_local_only_search(query, local_hits, str(e)))


@app.route("/api/internal/doc")
def internal_doc():
    """내규 전문 조회 — 규정명을 질의로 MCP를 호출해 해당 규정의 전문 텍스트 반환."""
    name = request.args.get("name", "").strip()
    doc_id = request.args.get("id", "").strip()
    if not name:
        return jsonify({"error": "name 파라미터가 필요합니다"}), 400

    # 업로드로 등록된 개정 내규는 로컬 본문을 최신본으로 사용(MCP 왕복 없음)
    m_local = _find_reg_original(name)
    local_text = _local_reg_text(m_local.get("slug", "")) if m_local else ""
    if local_text and m_local.get("uploaded_at"):
        return jsonify({"success": True, "name": m_local.get("title", name),
                        "tool": "local-upload", "is_full": True,
                        "text": local_text, "structured": None,
                        "source": "upload", "revision": m_local.get("revision", ""),
                        "uploaded_at": m_local.get("uploaded_at", "")})

    # 번들된 규정 HTML 본문으로 전문을 구성(오프라인/무MCP 리더 지원).
    def _local_doc():
        if not m_local:
            return None
        body = _reg_body_text(m_local.get("slug", ""))
        if not body or len(body) < 40:
            return None
        return {"success": True, "name": m_local.get("title", name),
                "tool": "local-html", "is_full": True,
                "text": body, "structured": None, "source": "local",
                "revision": m_local.get("revision", "")}

    if not SAGYU_MCP_URL:
        doc = _local_doc()
        if doc:
            return jsonify(doc)
        return jsonify({"error": "사규 MCP 서버가 설정되지 않았습니다(SAGYU_MCP_URL)."}), 503
    try:
        cli = _McpClient(SAGYU_MCP_URL)
        cli.initialize()
        tools = cli.list_tools()
        # 전문 조회 도구 우선 → 없으면 검색 도구 폴백
        doc_tool = _mcp_pick_doc_tool(tools)
        if doc_tool:
            args = _mcp_build_doc_args(doc_tool, name, doc_id)
            tool = doc_tool
        else:
            tool = _mcp_pick_search_tool(tools)
            if not tool:
                return jsonify({"error": "사규 MCP에서 사용 가능한 도구가 없습니다."}), 502
            args = _mcp_build_args(tool, name)
        result = cli.call_tool(tool.get("name"), args)
        text = _mcp_extract_text(result)
        structured = result.get("structuredContent") if isinstance(result, dict) else None
        if isinstance(result, dict) and result.get("isError"):
            doc = _local_doc()                       # MCP 오류 시 번들 본문으로
            if doc:
                return jsonify(doc)
            return jsonify({"error": text or "내규 전문 조회 중 오류가 발생했습니다."}), 502
        # MCP 가 빈 응답이면 번들 본문으로 보완
        if not (text or "").strip() and not structured:
            doc = _local_doc()
            if doc:
                return jsonify(doc)
        return jsonify({"success": True, "name": name, "tool": tool.get("name"),
                        "is_full": bool(doc_tool), "text": text, "structured": structured})
    except req_lib.exceptions.Timeout:
        doc = _local_doc()
        if doc:
            return jsonify(doc)
        return jsonify({"error": "사규 MCP 서버 응답 시간 초과"}), 504
    except Exception as e:
        doc = _local_doc()
        if doc:
            doc["mcp_error"] = str(e)
            return jsonify(doc)
        return jsonify({"error": f"내규 전문 조회 오류: {e}"}), 500


# ── 내규 전체 목록 (사이드바 트리용) ──────────────────────────────────────
_MCP_LIST_HINTS = ("list", "목록", "all", "전체", "index", "catalog", "browse", "리스트")

def _mcp_pick_list_tool(tools: list):
    """전체 목록 조회 도구 우선 선택(list/목록/index 등). 없으면 None."""
    def score(t):
        blob = (str(t.get("name", "")) + " " + str(t.get("description", ""))).lower()
        s = 0
        if any(k in blob for k in _MCP_LIST_HINTS): s += 4
        if any(k in blob for k in _MCP_RULE_HINTS): s += 2
        if "search" in blob or "검색" in blob or "get" in blob: s -= 1
        return s
    if not tools:
        return None
    ranked = sorted(tools, key=score, reverse=True)
    return ranked[0] if score(ranked[0]) > 0 else None


# 규정명으로 볼 수 있는 접미어 (목록 추출용)
_RULE_NAME_SUFFIX = ("정관", "규정", "규칙", "예규", "지침", "세칙", "기준",
                     "요령", "매뉴얼", "메뉴얼", "지시", "훈령", "방침", "계획")
# 규정명에 있으면 문장·설명 조각으로 판단(제외)
_RULE_NAME_BAD = ("예시", "이하", "한다", "말한다", "된다", "따른다", "이란",
                  "삭제", "추가", "신설", "경우", "다음", "또는", "관리번호",
                  "에 따라", "에 따른", "하는 것",
                  # 본문·부칙에서 딸려오는 설명형 조각(규정명이 아님)
                  "관련", "참고", "제반", "별칙", "시행일", "각종", "상기", "해당")
# 규정명 앞머리에 "수식어 + 공백"으로 오면 조각으로 판단(예: "동 규정", "위 규정").
# 공백을 요구하므로 '위임전결규정'·'위원회운영규정' 등은 영향받지 않음.
_RULE_FRAG_PREFIX_RE = re.compile(r"^(?:해당|상기|동|본|위|기타|다음|각|별첨|별표|별지)\s+\S")
# 선행 불릿/리스트 마커 (반복 제거)
_RULE_MARKER_RE = re.compile(
    r"^\s*(?:\[\d+\]|\d+\s*[.)]|[가-힣]\s*[.)]|[①-⑳㉠-㉭]|"
    r"[-–—*○◯●■□▪▶▷◆◇◦·•※☆★✓❍→▶️])\s*")
# 정관은 기관당 1개 — 옛 명칭/약칭 등 본문 인용에서 딸려오는 정관명은 제외(KOAT 특화)
_STALE_ORG_TOKENS = ("농업기술실용화재단", "실용화재단", "농업기술실용화농진원", "농진원")

def _norm_rule_name(nm: str) -> str:
    """규정명 후보 정규화 — 주석 괄호/마커/예시 라벨 제거."""
    if not nm:
        return ""
    nm = nm.strip()
    # 선행 불릿/마커 반복 제거(■ □ ○ 가) 1) ① 등)
    for _ in range(4):
        new = _RULE_MARKER_RE.sub("", nm)
        if new == nm:
            break
        nm = new.strip()
    nm = nm.strip("《》「」『』[]•·-*").strip()
    # "… 예시:" / "… 예:" 라벨 앞부분 제거
    nm = re.sub(r"^.*?(?:예시|보기|예)\s*[:：]\s*", "", nm).strip()
    # 첫 괄호/따옴표/마커/콜론 이후 잘라 제목 stem 만 취득
    nm = re.split(r"[(（「『《》」』:：\"'”“]", nm)[0].strip()
    # 접속/조사 꼬리 정리
    nm = re.sub(r"\s*(?:및|또는|와|과|의)\s*$", "", nm).strip()
    return nm

def _is_valid_rule_name(nm: str) -> bool:
    if not nm or not (3 <= len(nm) <= 40):
        return False
    if nm in _RULE_NAME_SUFFIX:            # 맨 접미어(일반명사)
        return False
    if not nm.endswith(_RULE_NAME_SUFFIX):
        return False
    if any(bad in nm for bad in _RULE_NAME_BAD):
        return False
    # 수식어 + 공백으로 시작하는 조각("동 규정", "위 규정" 등) 제외
    if _RULE_FRAG_PREFIX_RE.match(nm):
        return False
    if re.search(r"[.。]", nm):
        return False
    # 정관: 옛 명칭·약칭 인용은 실제 내규가 아님
    if nm.endswith("정관") and any(tok in nm for tok in _STALE_ORG_TOKENS):
        return False
    return True

def _extract_rule_names(text: str, structured) -> list:
    """MCP 응답(구조화/텍스트)에서 서로 다른 규정명 목록을 정규화·검증하여 추출."""
    names = []
    seen = set()
    def add(raw):
        nm = _norm_rule_name(raw)
        if not _is_valid_rule_name(nm):
            return
        key = nm.replace(" ", "")          # 띄어쓰기 차이 중복 제거
        if key in seen:
            return
        seen.add(key); names.append(nm)
    if isinstance(structured, list):
        for it in structured:
            if isinstance(it, dict):
                add(it.get("title") or it.get("name") or it.get("규정명") or it.get("제목"))
            elif isinstance(it, str):
                add(it)
    if text:
        # 1) 《규정명》 / 「규정명」 마커
        for m in re.findall(r"[《「『]\s*([^》」』\n]{2,60})\s*[》」』]", text):
            add(m)
        # 2) 접미어로 끝나는 짧은 라인 (목록 텍스트)
        for line in text.splitlines():
            t = re.sub(r"^\s*(?:\[\d+\]|\d+[.)]|[-*○·•])\s*", "", line).strip()
            if 3 <= len(t) <= 40 and t.endswith(_RULE_NAME_SUFFIX):
                add(t)
    return names


# 내규 목록 캐시 (전체 수집 비용이 크므로 10분 캐시)
_INTERNAL_LIST_CACHE: dict = {"names": None, "ts": 0.0}
# 광역 검색 시드 (규정 접미어 + 행정 도메인 키워드)
_LIST_SEED_TERMS = _RULE_NAME_SUFFIX + (
    "인사", "보수", "복무", "여비", "회계", "재무", "예산", "자금", "계약", "감사",
    "보안", "정보", "개인정보", "연구", "기술", "사업", "조직", "위임전결", "이사회",
    "교육", "출장", "자산", "물품", "복리후생", "윤리", "성과", "용역", "공사", "안전",
    "채용", "급여", "휴가", "징계", "위원회", "직제", "문서", "정관", "특허", "발명",
)

def _mcp_fill_limits(tool, args, big=1000):
    """도구 스키마의 limit/size/count/display류 숫자 인자를 크게 채운다."""
    props = (tool.get("inputSchema") or tool.get("input_schema") or {}).get("properties") or {}
    for k, p in props.items():
        kl = k.lower()
        ty = p.get("type")
        isnum = ty in ("integer", "number") or (isinstance(ty, list) and ("integer" in ty or "number" in ty))
        if isnum and any(x in kl for x in ("limit", "size", "count", "max", "per", "display", "top", "num", "rows")):
            args.setdefault(k, big)
    return args

def _mcp_page_key(tool):
    props = (tool.get("inputSchema") or {}).get("properties") or {}
    for k in props:
        kl = k.lower()
        if any(x in kl for x in ("page", "offset", "cursor", "start", "skip")):
            return k
    return None

def _harvest_internal_names(cli, tools):
    """가능한 많은 규정명을 수집 — 목록도구(페이지네이션) + 광역 검색."""
    names, seen = [], set()
    def add(nm_list):
        for nm in nm_list:
            if nm not in seen:
                seen.add(nm); names.append(nm)
    def call_names(tool, extra):
        args = _mcp_fill_limits(tool, dict(extra))
        res = cli.call_tool(tool.get("name"), args)
        return _extract_rule_names(_mcp_extract_text(res),
                                   res.get("structuredContent") if isinstance(res, dict) else None)

    calls = 0
    MAX_CALLS = 45
    # 1) 목록 도구 (있으면) — 페이지네이션
    list_tool = _mcp_pick_list_tool(tools)
    if list_tool:
        props = (list_tool.get("inputSchema") or {}).get("properties") or {}
        required = (list_tool.get("inputSchema") or {}).get("required") or []
        base = {}
        for k in required:
            if props.get(k, {}).get("type") == "string":
                base[k] = "규정"   # 필수 질의가 있으면 광역어
        pk = _mcp_page_key(list_tool)
        for pg in range(0, 20):
            if calls >= MAX_CALLS: break
            extra = dict(base)
            if pk is not None:
                extra[pk] = (pg + 1) if "page" in pk.lower() else pg * 100
            before = len(seen)
            try:
                add(call_names(list_tool, extra)); calls += 1
            except Exception as e:
                print(f"[list] 목록도구 오류(p={pg}): {e}"); break
            if pk is None or len(seen) == before:
                break   # 페이지 파라미터 없음 또는 더 이상 증가 없음

    # 2) 검색 도구로 광역 수집(부족하거나 목록도구 없을 때)
    stool = _mcp_pick_search_tool(tools)
    if stool and len(names) < 140:
        dry = 0
        for q in _LIST_SEED_TERMS:
            if calls >= MAX_CALLS: break
            before = len(seen)
            try:
                add(call_names(stool, _mcp_build_args(stool, q))); calls += 1
            except Exception:
                pass
            dry = dry + 1 if len(seen) == before else 0
    # 파일명(규정명) 기준 정렬 — 한글 사전순
    names.sort(key=lambda n: (n or "").strip())
    print(f"[internal-list] 수집 {len(names)}건 (calls={calls})")
    return names


# 원본(정관/규정/규칙/세칙/예규/매뉴얼) 분류 표시 순서
_REG_CAT_ORDER = ("정관", "규정", "규칙", "세칙", "예규", "매뉴얼")


@app.route("/api/internal/list")
def internal_list():
    """내규 전체 목록 — 업로드 원본(regulations_manifest.json)을 기준으로 제공.

    MCP 검색 텍스트에서 이름을 수집하면 본문에 인용된 정부 지침·법령까지 섞이므로,
    실제 내규 원본 파일 목록을 정본으로 쓴다. 원본이 없을 때만 MCP 수집으로 대체.
    """
    man = _load_reg_manifest()
    if man:
        items, seen = [], set()
        for m in man:
            t = (m.get("title") or "").strip()
            if not t or _norm_key(t) in seen:
                continue
            seen.add(_norm_key(t))
            items.append({"title": t,
                          "category": (m.get("category") or "기타").strip(),
                          "revision": (m.get("revision") or "").strip(),
                          "slug": m.get("slug", "")})
        order = {c: i for i, c in enumerate(_REG_CAT_ORDER)}
        items.sort(key=lambda x: (order.get(x["category"], 99), x["title"]))
        return jsonify({"success": True, "count": len(items),
                        "names": [x["title"] for x in items],
                        "items": items, "source": "originals",
                        "categories": list(_REG_CAT_ORDER)})
    # ── 원본 목록이 없을 때만 MCP 수집(하위 호환) ──
    if not SAGYU_MCP_URL:
        return jsonify({"error": "사규 MCP 서버가 설정되지 않았습니다(SAGYU_MCP_URL)."}), 503
    force = request.args.get("t", "")  # 캐시 무시용 파라미터
    if not force and _INTERNAL_LIST_CACHE["names"] and (time.time() - _INTERNAL_LIST_CACHE["ts"] < 600):
        nm = _INTERNAL_LIST_CACHE["names"]
        return jsonify({"success": True, "count": len(nm), "names": nm, "cached": True,
                        "source": "mcp"})
    try:
        cli = _McpClient(SAGYU_MCP_URL)
        cli.initialize()
        tools = cli.list_tools()
        names = _harvest_internal_names(cli, tools)
        if names:
            _INTERNAL_LIST_CACHE["names"] = names
            _INTERNAL_LIST_CACHE["ts"] = time.time()
        return jsonify({"success": True, "count": len(names), "names": names,
                        "source": "mcp"})
    except req_lib.exceptions.Timeout:
        return jsonify({"error": "사규 MCP 서버 응답 시간 초과"}), 504
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": f"내규 목록 조회 오류: {e}"}), 500


@app.route("/api/recent")
def get_recent():
    return jsonify({"recent": recent_searches})

@app.route("/api/recent/add")
def add_recent_api():
    q = request.args.get("q", "").strip()
    if q:
        add_recent(q)
    return jsonify({"ok": True})


@app.route("/api/ping")
def ping():
    """서버 생존 확인 - 빠른 응답 (법제처 API 호출 없음)"""
    return jsonify({"server": True, "ok": True})


@app.route("/api/law_check")
def law_check():
    """법제처 API 연결 확인 - 별도 비동기 호출용"""
    try:
        # 1) JSON 검색 확인 (기본)
        data = _law_get_json(
            {"target": "law", "query": "농지법", "display": "1"},
            timeout=(5, 15),
        )
        message = data.get("LawSearch", {}).get("message", "")
        if message:
            return jsonify({"law_api": False, "message": f"법제처 응답 메시지: {message}"})

        laws = data.get("LawSearch", {}).get("law")
        if laws:
            return jsonify({"law_api": True})

        # 2) JSON 결과가 비어 있으면 XML 조회로 재검증
        root = _law_get_xml(
            "lawSearch.do",
            {"target": "law", "query": "농지법", "display": "1"},
            timeout=(5, 15),
        )
        has_result = bool(root.findall(".//law") or root.findall(".//법령"))
        return jsonify({"law_api": has_result})
    except Exception as e:
        return jsonify({"law_api": False, "message": str(e)})


@app.route("/api/debug/law")
def debug_law_xml():
    if os.environ.get("DEBUG_ENDPOINTS") != "1":
        return jsonify({"error": "not found"}), 404
    name = request.args.get("name", "").strip()
    mst  = request.args.get("mst", "").strip()
    try:
        if name:
            mst = _get_mst(name)
        if not mst:
            return "mst 또는 name 파라미터 필요", 400
        for param in ("MST", "ID"):
            r = req_lib.get(f"{BASE}/lawService.do",
                            params={"OC": OC, "target": "law", "type": "XML", param: mst},
                            headers=HEADERS, timeout=15, verify=False)
            text = _decode(r.content)
            if "없습니다" not in text:
                return Response(text, mimetype="text/xml; charset=utf-8")
        return Response(text, mimetype="text/xml; charset=utf-8")
    except Exception as e:
        return str(e), 500


# ══════════════════════════════════════════════════════════════════════════
# 업무 도우미 — 공공 API 프록시 (기업/특허/지원사업/조달)
# 서비스키는 환경변수로 주입한다. 키 미설정 시 need_key 응답으로 UI가 안내한다.
#   DATA_GO_KR_KEY  : 공공데이터포털 서비스키 (기업 상태조회·나라장터 입찰공고)
#   KIPRIS_API_KEY  : 키프리스 플러스 서비스키 (특허·상표·디자인)
#   BIZINFO_API_KEY : 기업마당 인증키(crtfcKey) (정부 지원사업 공고)
# 참고: yybmion/public-apis-4Kr
# ══════════════════════════════════════════════════════════════════════════
_ASSIST_ENV = {"company": "DATA_GO_KR_KEY", "patent": "KIPRIS_API_KEY",
               "support": "BIZINFO_API_KEY", "procurement": "DATA_GO_KR_KEY"}
_ASSIST_APPLY = {
    "company":     "https://www.data.go.kr/data/15081808/openapi.do",
    "patent":      "https://plus.kipris.or.kr/portal/main/main.do",
    "support":     "https://www.bizinfo.go.kr/",
    "procurement": "https://www.data.go.kr/data/15129394/openapi.do",
}
_ASSIST_PROVIDER = {
    "company": "국세청 사업자등록정보", "patent": "특허청 KIPRIS",
    "support": "기업마당(중소벤처기업부)", "procurement": "조달청 나라장터",
}

def _assist_key(kind: str) -> str:
    return (os.environ.get(_ASSIST_ENV.get(kind, ""), "") or "").strip()

def _assist_need_key(kind: str):
    return jsonify({"success": False, "need_key": True,
                    "provider": _ASSIST_PROVIDER.get(kind, ""),
                    "apply_url": _ASSIST_APPLY.get(kind, ""),
                    "message": "서비스키가 설정되지 않았습니다. 관리자에게 문의하거나 "
                               "환경변수를 설정하세요."})

@app.route("/api/assist/status")
def assist_status():
    """각 조회 기능의 서비스키 설정 여부(배지 표시용)."""
    return jsonify({k: bool(_assist_key(k))
                    for k in ("company", "patent", "support", "procurement")})

@app.route("/api/assist/company")
def assist_company():
    """기업 조회 — 국세청 사업자등록 상태조회(odcloud)."""
    key = _assist_key("company")
    if not key:
        return _assist_need_key("company")
    bno = re.sub(r"[^0-9]", "", request.args.get("bno", ""))
    if len(bno) != 10:
        return jsonify({"success": False, "error": "사업자등록번호 10자리를 입력하세요."})
    try:
        r = _SESSION.post("https://api.odcloud.kr/api/nts-businessman/v1/status",
                          params={"serviceKey": key}, json={"b_no": [bno]}, timeout=15)
        rows = (r.json() or {}).get("data") or []
        if not rows:
            return jsonify({"success": True, "count": 0, "items": []})
        row = rows[0]
        title = "-".join([bno[:3], bno[3:5], bno[5:]])
        meta = [["납세자 상태", row.get("b_stt") or row.get("b_stt_cd") or "-"],
                ["과세유형", row.get("tax_type") or "-"],
                ["폐업일", row.get("end_dt") or "-"]]
        return jsonify({"success": True, "count": 1, "items": [
            {"title": title, "subtitle": row.get("tax_type", ""), "meta": meta, "url": ""}]})
    except Exception as e:
        return jsonify({"success": False, "error": f"조회 실패: {e}"})

@app.route("/api/assist/patent")
def assist_patent():
    """특허 조회 — KIPRIS Plus 특허·실용신안 검색(출원인·기간·상태 필터, 목록형).

    쿼리 파라미터: applicant(출원인), query(자유검색어), status(등록상태 코드),
    date_from/date_to(출원일 YYYY 또는 YYYYMMDD), page.
    상태 코드(lastvalue): R 등록·A 공개·J 거절·F 소멸·C 취하·I 무효·G 포기(빈값=전체).
    """
    key = _assist_key("patent")
    if not key:
        return _assist_need_key("patent")
    applicant = request.args.get("applicant", "").strip()
    query = request.args.get("query", "").strip()
    status = request.args.get("status", "").strip()
    df = re.sub(r"\D", "", request.args.get("date_from", ""))
    dt = re.sub(r"\D", "", request.args.get("date_to", ""))
    page = re.sub(r"\D", "", request.args.get("page", "1")) or "1"
    if not applicant and not query:
        return jsonify({"success": False, "error": "출원인 또는 검색어를 입력하세요."})
    base = "http://plus.kipris.or.kr/kipo-api/kipi/patUtiModInfoSearchSevice"
    try:
        params = {"ServiceKey": key, "numOfRows": "30", "pageNo": page,
                  "patent": "true", "utility": "true", "sortSpec": "AD", "descSort": "true"}
        if applicant:
            params["applicant"] = applicant
        if query:
            params["word"] = query
        if status:
            params["lastvalue"] = status
        # 출원일 범위(YYYY→YYYY0101/YYYY1231). KIPRIS 는 'YYYYMMDD~YYYYMMDD' 형식.
        def _d(v, end=False):
            if not v:
                return ""
            return v[:8] if len(v) >= 8 else v + ("1231" if end else "0101")
        a, b = _d(df), _d(dt, True)
        if a or b:
            params["applicationDate"] = f"{a or '00000000'}~{b or '99991231'}"
        r = _SESSION.get(f"{base}/getAdvancedSearch", params=params, timeout=20)
        root = _xml_fromstring(r.content)
        total = ""
        te = root.find(".//totalCount")
        if te is not None:
            total = (te.text or "").strip()
        items = []
        for it in root.iter("item"):
            def g(*tags):
                for t in tags:
                    el = it.find(t)
                    if el is not None and (el.text or "").strip():
                        return el.text.strip()
                return ""
            appno = g("applicationNumber", "ApplicationNumber")
            items.append({
                "title": g("inventionTitle", "InventionName", "articleName") or "(제목 없음)",
                "applicant": g("applicantName", "Applicant"),
                "appno": appno,
                "appdate": g("applicationDate", "ApplicationDate"),
                "regno": g("registerNumber", "RegistrationNumber"),
                "status": g("registerStatus", "RegistrationStatus", "lastValue"),
                "ipc": g("ipcNumber", "InternationalpatentclassificationNumber")})
        return jsonify({"success": True, "count": len(items),
                        "total": total or str(len(items)), "items": items})
    except Exception as e:
        return jsonify({"success": False, "error": f"조회 실패: {e}"})

@app.route("/api/assist/patent/detail")
def assist_patent_detail():
    """특허 상세 — KIPRIS Plus 서지상세정보(출원번호 기준)."""
    key = _assist_key("patent")
    if not key:
        return _assist_need_key("patent")
    appno = re.sub(r"\D", "", request.args.get("appno", ""))
    if not appno:
        return jsonify({"success": False, "error": "출원번호가 필요합니다."})
    base = "http://plus.kipris.or.kr/kipo-api/kipi/patUtiModInfoSearchSevice"
    try:
        r = _SESSION.get(f"{base}/getBibliographyDetailInfoSearch",
                         params={"applicationNumber": appno, "ServiceKey": key}, timeout=20)
        root = _xml_fromstring(r.content)

        def first(*tags):
            for t in tags:
                el = root.find(f".//{t}")
                if el is not None and (el.text or "").strip():
                    return el.text.strip()
            return ""

        def joined(container_tag, name_tag):
            vals = []
            for el in root.iter(name_tag):
                v = (el.text or "").strip()
                if v and v not in vals:
                    vals.append(v)
            return " · ".join(vals)

        detail = {
            "title": first("inventionTitle", "InventionName", "articleName"),
            "appno": first("applicationNumber") or appno,
            "appdate": first("applicationDate"),
            "openno": first("openNumber", "publicationNumber"),
            "opendate": first("openDate", "publicationDate"),
            "regno": first("registerNumber", "registrationNumber"),
            "regdate": first("registerDate", "registrationDate"),
            "status": first("registerStatus", "lastValue", "registrationLastStatus"),
            "applicant": joined("applicantInfoArray", "name") or first("applicantName"),
            "inventor": joined("inventorInfoArray", "name") or first("inventorName"),
            "agent": first("agentName"),
            "ipc": joined("ipcInfoArray", "ipcNumber") or first("ipcNumber"),
            "abstract": first("astrtCont", "abstractContent", "abstract"),
        }
        detail["url"] = ("https://www.kipris.or.kr/khome/search/searchResult.do"
                         if not appno else
                         f"https://www.kipris.or.kr/khome/main/base/BasePatentSearch.do")
        if not any(v for k, v in detail.items() if k not in ("url",)):
            return jsonify({"success": False, "error": "상세 정보를 찾지 못했습니다."})
        return jsonify({"success": True, "detail": detail})
    except Exception as e:
        return jsonify({"success": False, "error": f"상세 조회 실패: {e}"})

@app.route("/api/assist/support")
def assist_support():
    """지원사업 조회 — 기업마당 bizinfo 지원사업 공고."""
    key = _assist_key("support")
    if not key:
        return _assist_need_key("support")
    query = request.args.get("query", "").strip()
    try:
        r = _SESSION.get("https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do",
                         params={"crtfcKey": key, "dataType": "json",
                                 "searchCnt": "20", "hashtags": query}, timeout=15)
        d = r.json() or {}
        arr = d.get("jsonArray") or (d.get("response", {}) or {}).get("body", {}).get("items") or []
        items = []
        for row in (arr if isinstance(arr, list) else [arr])[:20]:
            url = row.get("pblancUrl") or ""
            if url and url.startswith("/"):
                url = "https://www.bizinfo.go.kr" + url
            items.append({
                "title": row.get("pblancNm") or row.get("polcyNm") or "(공고명 없음)",
                "subtitle": row.get("jrsdInsttNm") or row.get("excInsttNm") or "",
                "meta": [["신청기간", row.get("reqstBeginEndDe") or "-"],
                         ["분야", row.get("pldirSportRealmLclasCodeNm") or "-"]],
                "url": url})
        return jsonify({"success": True, "count": len(items), "items": items})
    except Exception as e:
        return jsonify({"success": False, "error": f"조회 실패: {e}"})

@app.route("/api/assist/procurement")
def assist_procurement():
    """조달공고 조회 — 조달청 나라장터 입찰공고정보(최근 30일)."""
    key = _assist_key("procurement")
    if not key:
        return _assist_need_key("procurement")
    query = request.args.get("query", "").strip()
    base = os.environ.get("PROCUREMENT_API_URL",
        "http://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServc")
    end = time.strftime("%Y%m%d")
    start = time.strftime("%Y%m%d", time.localtime(time.time() - 30 * 86400))
    try:
        params = {"serviceKey": key, "pageNo": "1", "numOfRows": "20", "type": "json",
                  "inqryDiv": "1", "inqryBgnDt": start + "0000", "inqryEndDt": end + "2359"}
        if query:
            params["bidNtceNm"] = query
        r = _SESSION.get(base, params=params, timeout=15)
        body = (r.json() or {}).get("response", {}).get("body", {}) or {}
        arr = body.get("items") or []
        if isinstance(arr, dict):
            arr = arr.get("item") or []
        items = []
        for row in (arr if isinstance(arr, list) else [arr])[:20]:
            items.append({
                "title": row.get("bidNtceNm") or "(공고명 없음)",
                "subtitle": row.get("ntceInsttNm") or row.get("dminsttNm") or "",
                "meta": [["공고번호", row.get("bidNtceNo") or "-"],
                         ["공고일시", row.get("bidNtceDt") or "-"],
                         ["입찰마감", row.get("bidClseDt") or "-"]],
                "url": row.get("bidNtceDtlUrl") or row.get("bidNtceUrl") or ""})
        return jsonify({"success": True, "count": len(items), "items": items})
    except Exception as e:
        return jsonify({"success": False, "error": f"조회 실패: {e}"})


# ── 실행 ─────────────────────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 5100))

if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print("=" * 50)
    print("  🌾  KOAT 내규&국가법령 종합 검색 서비스")
    print(f"  🔗  {url}")
    print("  종료: Ctrl+C")
    print("=" * 50)
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
