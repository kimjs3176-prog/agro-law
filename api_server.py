"""
농업 법령 검색 서비스
배포: Vercel / Render / Railway
로컬: python run_local.py
"""

import os, json, re, time, threading, webbrowser
import concurrent.futures as _cf
import xml.etree.ElementTree as ET
import urllib3
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests as req_lib
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
CORS(app)

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
def _make_session() -> req_lib.Session:
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
        pool_connections=4,
        pool_maxsize=10,
    )
    s = req_lib.Session()
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    s.headers.update(HEADERS)
    s.verify = False
    return s

_SESSION = _make_session()   # 프로세스 내 전역 재사용

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
            r = _SESSION.get(
                f"{BASE}/lawSearch.do",
                params={**params, "OC": OC, "type": "JSON"},
                timeout=timeout,
            )
            r.raise_for_status()
            return json.loads(_decode(r.content))
        except (req_lib.exceptions.ConnectionError,
                req_lib.exceptions.ChunkedEncodingError) as e:
            last_err = e
            import time; time.sleep(1.5 * (attempt + 1))
            _SESSION.close()   # 세션 재연결 유도
            continue
    raise last_err

def _law_get_xml(endpoint: str, params: dict, timeout=None) -> ET.Element:
    timeout = timeout or _T_XML
    last_err = None
    for attempt in range(3):
        try:
            r = _SESSION.get(
                f"{BASE}/{endpoint}",
                params={**params, "OC": OC, "type": "XML"},
                timeout=timeout,
            )
            r.raise_for_status()
            text = _decode(r.content).strip().lstrip("\ufeff")
            text = re.sub(r"<\?xml[^?]*\?>", "", text, count=1).strip()
            if not text:
                raise ValueError("빈 XML 응답")
            return ET.fromstring(text)
        except (req_lib.exceptions.ConnectionError,
                req_lib.exceptions.ChunkedEncodingError) as e:
            last_err = e
            import time; time.sleep(1.5 * (attempt + 1))
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

def _render_jo(u: ET.Element) -> str:
    """
    <조문단위> 하나를 읽어서 깔끔한 조문 텍스트를 반환한다.
    번호 태그(<항번호> 등)는 제목으로만 사용하고, 내용 태그가 이미
    '1. 내용…' 형태로 시작하면 번호를 붙이지 않는다.
    """
    lines = []

    def _already_numbered(txt: str) -> bool:
        """텍스트가 이미 번호(1. / 가. / ① 등)로 시작하는지 확인"""
        return bool(re.match(r"^(\d+\.|[가-힣]\.|①|②|③|④|⑤|⑥|⑦|⑧|⑨|⑩)", txt))

    def _render_node(node: ET.Element, depth: int = 0):
        tag = node.tag
        indent = "  " * depth

        # ── 번호 태그: 건너뜀 (부모가 내용 태그에서 이미 포함) ──
        if tag in _NO_TAGS:
            return

        # ── 조문 내용 계열 태그 ──
        if tag in ("조문내용", "항내용", "호내용", "목내용"):
            txt = _node_text(node)
            if txt:
                # 이미 "1." 등으로 시작하면 그대로, 아니면 인덴트만 추가
                lines.append(f"{indent}{txt}")
            # tail 텍스트
            if node.tail and node.tail.strip():
                lines.append(f"{indent}{node.tail.strip()}")
            return

        # ── 항 ──
        if tag == "항":
            no_el  = node.find("항번호")
            con_el = node.find("항내용")
            no_txt  = _node_text(no_el)  if no_el  is not None else ""
            con_txt = _node_text(con_el) if con_el is not None else ""

            # "1." 등이 내용에 이미 포함되어 있으면 번호 생략
            if con_txt and _already_numbered(con_txt):
                lines.append(f"{indent}{con_txt}")
            elif no_txt and con_txt:
                lines.append(f"{indent}{no_txt} {con_txt}")
            elif con_txt:
                lines.append(f"{indent}{con_txt}")

            # 호 처리
            for ho in node.findall("호"):
                _render_node(ho, depth + 1)
            return

        # ── 호 ──
        if tag == "호":
            no_el  = node.find("호번호")
            con_el = node.find("호내용")
            no_txt  = _node_text(no_el)  if no_el  is not None else ""
            con_txt = _node_text(con_el) if con_el is not None else ""

            if con_txt and _already_numbered(con_txt):
                lines.append(f"{indent}{con_txt}")
            elif no_txt and con_txt:
                lines.append(f"{indent}{no_txt} {con_txt}")
            elif con_txt:
                lines.append(f"{indent}{con_txt}")

            for mok in node.findall("목"):
                _render_node(mok, depth + 2)
            return

        # ── 목 ──
        if tag == "목":
            no_el  = node.find("목번호")
            con_el = node.find("목내용")
            no_txt  = _node_text(no_el)  if no_el  is not None else ""
            con_txt = _node_text(con_el) if con_el is not None else ""

            if con_txt and _already_numbered(con_txt):
                lines.append(f"{indent}{con_txt}")
            elif no_txt and con_txt:
                lines.append(f"{indent}{no_txt} {con_txt}")
            elif con_txt:
                lines.append(f"{indent}{con_txt}")
            return

        # ── 기타: 자식 순회 ──
        for child in node:
            _render_node(child, depth)

    # 조문단위 직접 자식 순회
    for child in u:
        if child.tag in ("조번호", "조문제목", "조제목"):
            continue  # 조 번호·제목은 별도 필드로 이미 추출
        _render_node(child, 0)

    return "\n".join(l for l in lines if l.strip())


# ── 법령MST 취득 (XML 검색 → 태그 추출) ──────────────────────────────────────
def _get_mst(law_name: str, target: str = "law") -> str:
    try:
        root = _law_get_xml("lawSearch.do", {"target": target, "query": law_name, "display": "1"})
        for tag in ("법령MST", "법령Mst", "행정규칙MST", "행정규칙Mst", "lawMst", "MST", "mst"):
            el = root.find(f".//{tag}")
            if el is not None and el.text and el.text.strip():
                print(f"[MST] '{law_name}'({target}) → {el.text.strip()} (태그:{tag})")
                return el.text.strip()
        # 발견된 태그 목록 디버그
        tags = {e.tag for e in root.iter()}
        print(f"[MST] '{law_name}'({target}) XML 태그 목록: {sorted(tags)}")
        # 일련번호 fallback
        for seq_tag in ("법령일련번호", "행정규칙일련번호"):
            seq_el = root.find(f".//{seq_tag}")
            if seq_el is not None and seq_el.text:
                print(f"[MST] fallback {seq_tag}={seq_el.text.strip()}")
                return seq_el.text.strip()
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

            # 조번호에서 첫 번째 숫자만 추출 ("제5조의2" → "5")
            m_no  = re.search(r"\d+", no_raw)
            no_d  = m_no.group() if m_no else ""

            # 조문제목에서 앞의 "제N조" 중복 접두사 제거
            title = _clean_article_title(title, no_d)

            content = _render_jo(u).strip()
            amend   = _get_amend_info(u)
            if no_d or title or content:
                art = {"조문번호": no_d, "조문제목": title,
                       "조문내용": content, "type": "article"}
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
            m_no   = re.search(r"\d+", no_raw)
            no_d   = m_no.group() if m_no else ""
            title  = _clean_article_title(title, no_d)
            content = _render_jo(jo).strip()
            amend   = _get_amend_info(jo)
            if no_d or title or content:
                art = {"조문번호": no_d, "조문제목": title,
                       "조문내용": content, "type": "article"}
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
            m_no    = re.search(r"\d+", no_raw)
            no_d    = m_no.group() if m_no else ""
            title   = _clean_article_title(title, no_d)
            content = _render_jo(parent).strip()
            amend   = _get_amend_info(parent)
            if no_d or title:
                art = {"조문번호": no_d, "조문제목": title,
                       "조문내용": content, "type": "article"}
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
def _fetch_matching_articles(law_name: str, kw: str, doc_type: str = "law"):
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
        return {"law_name": lname or law_name, "keyword": kw, "articles": matched}
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
    print(f"[article-search] '{query}' 대상 {len(all_target)}개 "
          f"(법령:{len(CANDIDATE_LAWS)} + 행정규칙:{len(CANDIDATE_ADMRUL)} + 추가:{len(extra_laws)})")

    kw = query.lower()

    def fetch_and_filter(law_name: str):
        try:
            entry = stage1_meta.get(law_name) or {}
            doc_type = entry.get("doc_type", "law")
            meta = entry.get("meta", {})
            res = _fetch_matching_articles(law_name, kw, doc_type=doc_type)
            if not res:
                return None
            lname = res["law_name"]
            # admrul은 meta에 행정규칙명 필드가 있을 수 있음
            if not meta:
                meta = stage1_meta.get(lname, {}).get("meta", {})
            return {
                "법령명한글":   lname,
                "법령구분명":   meta.get("법령구분명") or meta.get("행정규칙종류명") or ("행정규칙" if doc_type == "admrul" else "법률"),
                "소관부처명":   meta.get("소관부처명", ""),
                "공포일자":     meta.get("공포일자") or meta.get("발령일자", ""),
                "법령일련번호": meta.get("법령일련번호") or meta.get("행정규칙일련번호", ""),
                "_matched_count": len(res["articles"]),
            }
        except Exception as e:
            print(f"[article-search] {law_name} 오류: {e}")
        return None

    results, truncated = [], False
    try:
        with _cf.ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(fetch_and_filter, name): name for name in all_target}
            for future in _cf.as_completed(futures, timeout=40):
                try:
                    res = future.result()
                    if res:
                        results.append(res)
                except Exception as e:
                    print(f"[article-search] future 오류: {e}")
    except _cf.TimeoutError:
        truncated = True
        print(f"[article-search] 타임아웃, {len(results)}/{len(all_target)}건 탐색")

    results.sort(key=lambda x: x.get("_matched_count", 0), reverse=True)
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
        with _cf.ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(fetch_law, name): name for name in all_target}
            for future in _cf.as_completed(futures, timeout=45):
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
    """실무 시나리오 검색: 서버 AI(환경변수 키) → 관련 법령·조문 반환"""
    body    = request.get_json(force=True) or {}
    query   = body.get("query", "").strip()
    if not query or len(query) < 5:
        return jsonify({"error": "질문을 5자 이상 입력해 주세요."}), 400

    # ── 환경변수에서 AI 설정 읽기 ────────────────────────────────────────────
    provider = os.environ.get("SCENARIO_AI_PROVIDER", "gemini").lower()
    api_key  = os.environ.get(f"{provider.upper()}_API_KEY", "").strip()
    model    = os.environ.get("SCENARIO_AI_MODEL", "").strip()
    if not api_key:
        return jsonify({"error":
            "시나리오 검색 기능이 아직 활성화되지 않았습니다. "
            "서버 환경변수(GEMINI_API_KEY 등)를 설정하세요."
        }), 503

    _DEFAULTS = {"gemini": "gemini-2.5-flash", "claude": "claude-haiku-4-5-20251001",
                 "gpt": "gpt-4.1-mini", "openai": "gpt-4.1-mini"}
    mdl = model or _DEFAULTS.get(provider, "gemini-2.5-flash")

    # ── AI Step-1: 의도 분석 → JSON ──────────────────────────────────────────
    AVAILABLE_LAWS = (
        "농지법, 종자산업법, 농약관리법, 비료관리법, 가축전염병예방법, "
        "식물방역법, 농업재해보험법, 농촌진흥법, 농어업재해대책법, "
        "특허법, 실용신안법, 디자인보호법, 상표법, 식물신품종보호법, "
        "부정경쟁방지 및 영업비밀보호에 관한 법률, "
        "기술이전 및 사업화 촉진에 관한 법률"
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
        ai_text = ""
        if provider == "gemini":
            url = (f"https://generativelanguage.googleapis.com/v1beta/models"
                   f"/{mdl}:generateContent?key={api_key}")
            resp = req_lib.post(url, timeout=25,
                headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": f"{system_prompt}\n\n{user_msg}"}]}],
                      "generationConfig": {"maxOutputTokens": 600, "temperature": 0.2}})
            if resp.status_code != 200:
                err = resp.json().get("error", {}).get("message", "Gemini 오류")
                return jsonify({"error": f"AI 오류: {err}"}), 502
            ai_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]

        elif provider in ("claude",):
            resp = req_lib.post("https://api.anthropic.com/v1/messages", timeout=25,
                headers={"Content-Type": "application/json",
                         "x-api-key": api_key, "anthropic-version": "2023-06-01"},
                json={"model": mdl, "max_tokens": 600,
                      "system": system_prompt,
                      "messages": [{"role": "user", "content": user_msg}]})
            if resp.status_code != 200:
                err = resp.json().get("error", {}).get("message", "Claude 오류")
                return jsonify({"error": f"AI 오류: {err}"}), 502
            ai_text = resp.json()["content"][0]["text"]

        elif provider in ("gpt", "openai"):
            resp = req_lib.post("https://api.openai.com/v1/chat/completions", timeout=25,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key}"},
                json={"model": mdl, "max_tokens": 600, "temperature": 0.2,
                      "messages": [{"role": "system", "content": system_prompt},
                                   {"role": "user",   "content": user_msg}]})
            if resp.status_code != 200:
                err = resp.json().get("error", {}).get("message", "GPT 오류")
                return jsonify({"error": f"AI 오류: {err}"}), 502
            ai_text = resp.json()["choices"][0]["message"]["content"]
        else:
            return jsonify({"error": f"지원하지 않는 AI 프로바이더: {provider}"}), 400

        # JSON 파싱 (코드블록 제거 후)
        clean = re.sub(r"```(?:json)?|```", "", ai_text).strip()
        ai = json.loads(clean)

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

    # laws가 비어있으면 CANDIDATE_LAWS 전체 대상
    if not laws_to_search:
        laws_to_search = list(CANDIDATE_LAWS)[:6]

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
        with _cf.ThreadPoolExecutor(max_workers=4) as ex:
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

    return jsonify({
        "success":    True,
        "query":      query,
        "category":   category,
        "basis_type": basis_type,
        "guidance":   guidance,
        "steps":      steps,
        "caution":    caution,
        "keywords":   keywords,
        "laws":       results,
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
            resp = req_lib.post(
                "https://api.anthropic.com/v1/messages",
                json={"model": mdl, "max_tokens": 1500,
                      "system": system_prompt,
                      "messages": [{"role": "user", "content": user_msg}]},
                headers={"Content-Type": "application/json",
                         "x-api-key": api_key,
                         "anthropic-version": "2023-06-01"},
                timeout=30,
            )
            d = resp.json()
            if resp.status_code != 200:
                return jsonify({"error": d.get("error", {}).get("message", "Claude API 오류")}), resp.status_code
            return jsonify({"result": d["content"][0]["text"]})

        # ── OpenAI GPT ─────────────────────────────────────────────────────────
        elif provider == "gpt":
            mdl = model or "gpt-4o"
            resp = req_lib.post(
                "https://api.openai.com/v1/chat/completions",
                json={"model": mdl, "max_tokens": 1500,
                      "messages": [{"role": "system", "content": system_prompt},
                                   {"role": "user",   "content": user_msg}]},
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            d = resp.json()
            if resp.status_code != 200:
                return jsonify({"error": d.get("error", {}).get("message", "GPT API 오류")}), resp.status_code
            return jsonify({"result": d["choices"][0]["message"]["content"]})

        # ── Google Gemini ──────────────────────────────────────────────────────
        elif provider == "gemini":
            mdl = model or "gemini-2.5-flash"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={api_key}"
            resp = req_lib.post(
                url,
                json={"contents": [{"parts": [{"text": f"{system_prompt}\n\n{user_msg}"}]}],
                      "generationConfig": {"maxOutputTokens": 1500}},
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            d = resp.json()
            if resp.status_code != 200:
                err = d.get("error", {}).get("message", "Gemini API 오류")
                return jsonify({"error": err}), resp.status_code
            text = d["candidates"][0]["content"]["parts"][0]["text"]
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


# ── 실행 ─────────────────────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 5100))

if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print("=" * 50)
    print("  🌾  농업 법령 검색 서비스 v3.0")
    print(f"  🔗  {url}")
    print("  종료: Ctrl+C")
    print("=" * 50)
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
