"""
농업 법령 검색 서비스
배포: Vercel / Render / Railway
로컬: python law_search.py
"""

import os, json, re, threading, webbrowser
import requests as req_lib
import xml.etree.ElementTree as ET
import urllib3
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
CORS(app)

# 보안: 운영 환경에서는 CORS 출처를 제한하세요
# CORS(app, origins=["https://your-domain.com"])

OC   = os.environ.get("LAW_OC", "wlghdkgus1234")
BASE = "https://www.law.go.kr/DRF"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.law.go.kr/",
}

# ── 최근 검색어 (서버 메모리) ─────────────────────────────────────────────────
recent_searches = []
favorites = []   # 즐겨찾기: [{name, org, type, url}]

def add_recent(q):
    global recent_searches
    q = q.strip()
    if q and q not in recent_searches:
        recent_searches.insert(0, q)
        recent_searches = recent_searches[:10]

# ── 공통 HTTP 헬퍼 ────────────────────────────────────────────────────────────
def _decode(raw):
    for enc in ("euc-kr", "utf-8"):
        try:
            return raw.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")

def _law_get_json(params: dict) -> dict:
    r = req_lib.get(f"{BASE}/lawSearch.do", params={**params, "OC": OC, "type": "JSON"},
                    headers=HEADERS, timeout=10, verify=False)
    r.raise_for_status()
    return json.loads(_decode(r.content))

def _law_get_xml(endpoint: str, params: dict) -> ET.Element:
    r = req_lib.get(f"{BASE}/{endpoint}", params={**params, "OC": OC, "type": "XML"},
                    headers=HEADERS, timeout=15, verify=False)
    r.raise_for_status()
    text = _decode(r.content).strip().lstrip("\ufeff")
    text = re.sub(r"<\?xml[^?]*\?>", "", text, count=1).strip()
    return ET.fromstring(text)

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
def _get_mst(law_name: str) -> str:
    try:
        root = _law_get_xml("lawSearch.do", {"target": "law", "query": law_name, "display": "1"})
        for tag in ("법령MST", "법령Mst", "lawMst", "MST", "mst"):
            el = root.find(f".//{tag}")
            if el is not None and el.text and el.text.strip():
                print(f"[MST] '{law_name}' → {el.text.strip()} (태그:{tag})")
                return el.text.strip()
        # 발견된 태그 목록 디버그
        tags = {e.tag for e in root.iter()}
        print(f"[MST] '{law_name}' XML 태그 목록: {sorted(tags)}")
        # 법령일련번호를 fallback으로
        seq_el = root.find(".//법령일련번호")
        if seq_el is not None and seq_el.text:
            print(f"[MST] fallback 법령일련번호={seq_el.text.strip()}")
            return seq_el.text.strip()
    except Exception as e:
        print(f"[MST] 오류: {e}")
    return ""

# 구조 헤더 태그 (장·절·관·편 - 조문이 아님)
_STRUCT_TAGS = {"장", "절", "관", "편", "장번호", "절번호", "관번호", "편번호",
                "장제목", "절제목", "관제목", "편제목"}

def _is_struct_header(u: ET.Element) -> bool:
    """조문단위가 장/절/관/편 구조 헤더인지 판별"""
    # 자식 태그 중 구조 헤더 태그가 있으면 헤더
    child_tags = {c.tag for c in u}
    if child_tags & _STRUCT_TAGS:
        return True
    # 조번호가 없고 조문제목만 있는데 내용(항)이 없으면 헤더
    no = (u.findtext("조번호") or u.findtext("조문번호") or "").strip()
    no_d = re.sub(r"[^0-9]", "", no)
    has_hang = bool(u.findall(".//항") or u.findall(".//조문내용"))
    if not no_d and not has_hang:
        return True
    return False

def _struct_label(u: ET.Element) -> tuple:
    """구조 헤더의 (번호, 제목) 반환"""
    no = ""
    for tag in ("장번호","절번호","관번호","편번호","조번호"):
        v = u.findtext(tag) or ""
        if v.strip(): no = v.strip(); break
    title = ""
    for tag in ("장제목","절제목","관제목","편제목","조문제목","조제목"):
        v = u.findtext(tag) or ""
        if v.strip(): title = v.strip(); break
    # 자식 직접 텍스트도 확인
    if not title:
        for c in u:
            if c.tag not in _NO_TAGS and c.text and c.text.strip():
                title = c.text.strip(); break
    return no, title

# ── 조문 XML 파싱 ─────────────────────────────────────────────────────────────
def _parse_articles(root: ET.Element):
    all_tags = {el.tag for el in root.iter()}
    law_name = ""
    for tag in ("법령명한글", "법령명_한글", "법령명"):
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
                no, title = _struct_label(u)
                if title:
                    articles.append({"조문번호": "", "조문제목": title,
                                     "조문내용": "", "type": "header",
                                     "header_no": no})
                continue

            no    = (u.findtext("조번호") or u.findtext("조문번호") or "").strip()
            title = (u.findtext("조문제목") or u.findtext("조제목") or "").strip()
            no_d  = re.sub(r"[^0-9]", "", no)
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
            no    = (jo.findtext("조번호") or jo.findtext("번호") or "").strip()
            title = (jo.findtext("조문제목") or jo.findtext("제목") or "").strip()
            no_d  = re.sub(r"[^0-9]", "", no)
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
            no = (no_el.text or "").strip()
            if no in seen: continue
            seen.add(no)
            title   = (parent.findtext("조문제목") or parent.findtext("제목") or "").strip()
            content = _render_jo(parent).strip()
            no_d    = re.sub(r"[^0-9]", "", no)
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
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>농업 법령 검색</title>
<style>
:root{--g:#1D9E75;--gd:#0F6E56;--gl:#E1F5EE;--gp:#085041;--bg:#f5f6f8;--sf:#fff;--bd:#e2e4e8;--tx:#1a1a1a;--mu:#6b7280;--ht:#9ca3af;--r:10px;--rs:6px;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Pretendard','Apple SD Gothic Neo','Malgun Gothic',sans-serif;background:var(--bg);color:var(--tx);}
/* 상단바 */
.topbar{background:var(--sf);border-bottom:1px solid var(--bd);padding:0 24px;display:flex;align-items:center;height:54px;gap:10px;position:sticky;top:0;z-index:200;}
.logo{width:30px;height:30px;background:var(--g);border-radius:8px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:15px;font-weight:700;flex-shrink:0;}
.brand{font-size:15px;font-weight:600;}.brand-sub{font-size:12px;color:var(--mu);}
.apibadge{margin-left:auto;font-size:11px;font-weight:500;padding:3px 10px;border-radius:20px;background:var(--gl);color:var(--gd);}
/* 레이아웃 */
.main{max-width:980px;margin:0 auto;padding:24px 20px;}
/* 검색 패널 */
.spanel{background:var(--sf);border:1px solid var(--bd);border-radius:var(--r);padding:18px 20px;margin-bottom:16px;}
/* 기관 탭 */
.tabs{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap;}
.tab{padding:6px 14px;border-radius:20px;border:1px solid var(--bd);font-size:13px;cursor:pointer;background:transparent;color:var(--mu);transition:.15s;white-space:nowrap;}
.tab:hover{border-color:var(--g);color:var(--g);}
.tab.active{background:var(--g);border-color:var(--g);color:#fff;}
/* 모드 토글 */
.mtoggle{display:flex;gap:0;margin-bottom:12px;border:1px solid var(--bd);border-radius:var(--rs);overflow:hidden;width:fit-content;}
.mbtn{padding:7px 18px;font-size:13px;cursor:pointer;border:none;background:transparent;color:var(--mu);transition:.15s;}
.mbtn:hover{background:var(--bg);}
.mbtn.active{background:var(--g);color:#fff;font-weight:600;}
/* 검색창 */
.srow{display:flex;gap:8px;}
.sinput{flex:1;padding:10px 14px;border:1px solid var(--bd);border-radius:var(--rs);font-size:14px;background:var(--bg);color:var(--tx);outline:none;transition:.15s;}
.sinput:focus{border-color:var(--g);background:#fff;box-shadow:0 0 0 3px rgba(29,158,117,.1);}
.sbtn{padding:10px 22px;border-radius:var(--rs);border:none;background:var(--g);color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:.15s;}
.sbtn:hover{background:var(--gd);}.sbtn:disabled{opacity:.5;cursor:not-allowed;}
/* 자주찾는 법령 */
.qlabel{font-size:11px;color:var(--ht);margin:12px 0 7px;font-weight:500;}
.chips{display:flex;gap:6px;flex-wrap:wrap;}
.chip{padding:4px 11px;border-radius:20px;border:1px solid var(--bd);font-size:12px;cursor:pointer;color:var(--mu);background:var(--bg);transition:.12s;}
.chip:hover{border-color:var(--g);color:var(--g);background:var(--gl);}
.chip.loading{opacity:.5;}
/* 최근 검색어 */
.recent{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:10px;min-height:22px;}
.rec-label{font-size:11px;color:var(--ht);font-weight:500;flex-shrink:0;}
.rec-chip{padding:3px 9px;border-radius:20px;background:#f0f0f0;font-size:11px;cursor:pointer;color:var(--mu);border:none;transition:.12s;}
.rec-chip:hover{background:var(--gl);color:var(--g);}
/* 필터바 */
.fbar{display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap;}
.fbar select{padding:6px 10px;border:1px solid var(--bd);border-radius:var(--rs);font-size:13px;background:var(--sf);color:var(--tx);cursor:pointer;outline:none;}
.fbar select:focus{border-color:var(--g);}
.rcount{font-size:13px;color:var(--mu);margin-left:auto;}
/* 상태바 */
.status{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--mu);margin-bottom:10px;min-height:20px;}
.spinner{width:13px;height:13px;border:2px solid var(--gl);border-top-color:var(--g);border-radius:50%;animation:spin .7s linear infinite;flex-shrink:0;}
@keyframes spin{to{transform:rotate(360deg);}}
/* 법령 카드 */
.results{display:flex;flex-direction:column;gap:8px;}
.card{background:var(--sf);border:1px solid var(--bd);border-radius:var(--r);overflow:hidden;transition:box-shadow .15s;}
.card:hover{box-shadow:0 2px 10px rgba(0,0,0,.07);}
.card-head{display:flex;align-items:center;gap:10px;padding:13px 16px;cursor:pointer;user-select:none;}
.card-head:hover{background:#fafbfc;}
.card.open .card-head{border-bottom:1px solid var(--bd);}
.ltype{font-size:11px;font-weight:600;color:var(--gd);background:var(--gl);padding:3px 8px;border-radius:20px;white-space:nowrap;flex-shrink:0;}
.lname{font-size:14px;font-weight:600;flex:1;min-width:0;}
.lorg{font-size:12px;color:var(--ht);white-space:nowrap;flex-shrink:0;}
.arrow{font-size:11px;color:var(--ht);transition:transform .2s;flex-shrink:0;}
.card.open .arrow{transform:rotate(180deg);}
.card-body{display:none;padding:14px 16px;}
.card.open .card-body{display:block;}
/* 메타 그리드 */
.mgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-bottom:14px;}
.mbox{background:var(--bg);border-radius:var(--rs);padding:9px 12px;}
.mlbl{font-size:11px;color:var(--ht);margin-bottom:3px;font-weight:500;}
.mval{font-size:13px;font-weight:600;color:var(--tx);}
/* 액션 버튼 */
.actions{display:flex;gap:8px;flex-wrap:wrap;}
.abtn{display:inline-flex;align-items:center;gap:5px;padding:7px 13px;border-radius:var(--rs);border:1px solid var(--bd);font-size:13px;cursor:pointer;text-decoration:none;color:var(--tx);background:var(--sf);transition:.15s;}
.abtn:hover{border-color:var(--g);color:var(--g);background:var(--gl);}
.abtn.primary{background:var(--g);border-color:var(--g);color:#fff;}
.abtn.primary:hover{background:var(--gd);}
/* 조문 패널 (카드 내) */
.art-wrap{margin-top:14px;border-top:1px solid var(--bd);padding-top:14px;}
.art-filterrow{display:flex;align-items:center;gap:8px;margin-bottom:10px;}
.art-finput{flex:1;padding:7px 10px;border:1px solid var(--bd);border-radius:var(--rs);font-size:13px;background:var(--bg);color:var(--tx);outline:none;}
.art-finput:focus{border-color:var(--g);}
.art-cnt{font-size:12px;color:var(--mu);white-space:nowrap;}
.art-item{border:1px solid var(--bd);border-radius:var(--rs);margin-bottom:5px;overflow:hidden;}
.art-head{display:flex;align-items:center;gap:8px;padding:9px 12px;cursor:pointer;background:var(--bg);}
.art-head:hover{background:#eef0f2;}
.art-no{font-size:11px;font-weight:700;color:var(--gd);background:var(--gl);padding:2px 7px;border-radius:20px;white-space:nowrap;flex-shrink:0;}
.art-title{font-size:13px;font-weight:600;flex:1;}
.art-body{display:none;padding:10px 14px;font-size:13px;line-height:1.95;white-space:pre-wrap;color:var(--tx);border-top:1px solid var(--bd);}
/* ── 분할 패널 레이아웃 ── */
.fullpanel{position:fixed;top:0;right:-900px;width:min(900px,100vw);height:100vh;background:var(--sf);border-left:1px solid var(--bd);z-index:500;display:flex;flex-direction:row;transition:right .3s cubic-bezier(.4,0,.2,1);box-shadow:-4px 0 24px rgba(0,0,0,.12);}
.fullpanel.open{right:0;}
/* 좌측 - 현재 법령 */
.fp-left{display:flex;flex-direction:column;flex:1;min-width:0;border-right:1px solid var(--bd);transition:flex .3s;}
.fp-header{display:flex;align-items:center;gap:6px;padding:10px 14px;border-bottom:1px solid var(--bd);flex-shrink:0;background:var(--sf);flex-wrap:wrap;}
.fp-title{font-size:14px;font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.fp-close{width:28px;height:28px;border:1px solid var(--bd);border-radius:6px;cursor:pointer;background:transparent;font-size:14px;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.fp-close:hover{background:var(--bg);}
.fp-toolbar{padding:8px 14px;border-bottom:1px solid var(--bd);display:flex;gap:7px;align-items:center;flex-shrink:0;flex-wrap:wrap;}
.fp-finput{flex:1;min-width:120px;padding:6px 10px;border:1px solid var(--bd);border-radius:var(--rs);font-size:13px;background:var(--bg);color:var(--tx);outline:none;}
.fp-finput:focus{border-color:var(--g);}
.fp-cnt{font-size:12px;color:var(--mu);white-space:nowrap;}
.fp-tbtn{padding:4px 9px;border:1px solid var(--bd);border-radius:var(--rs);font-size:12px;cursor:pointer;background:transparent;color:var(--mu);white-space:nowrap;transition:.12s;text-decoration:none;display:inline-flex;align-items:center;}
.fp-tbtn:hover{border-color:var(--g);color:var(--g);background:var(--gl);}
.fp-body{flex:1;overflow-y:auto;padding:12px 16px;}
/* 우측 - 준용 법령 (숨김→슬라이드 오픈) */
.fp-right{display:flex;flex-direction:column;width:0;overflow:hidden;transition:width .3s cubic-bezier(.4,0,.2,1);background:var(--bg);}
.fp-right.open{width:420px;min-width:320px;}
.fp-right-header{display:flex;align-items:center;gap:6px;padding:10px 14px;border-bottom:1px solid var(--bd);flex-shrink:0;background:var(--sf);}
.fp-right-title{font-size:13px;font-weight:600;color:var(--tx);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.fp-right-body{flex:1;overflow-y:auto;padding:10px 14px;}
/* 준용 법령 링크 */
.law-ref{color:#2563eb;border-bottom:1px solid #93c5fd;cursor:pointer;font-weight:500;padding:0 1px;border-radius:2px;transition:.12s;}
.law-ref:hover{background:#eff6ff;color:#1d4ed8;}
.law-ref.active{background:#dbeafe;color:#1e40af;border-bottom-color:#3b82f6;}
/* 준용 조문 아이템 */
.ref-art{border-bottom:1px solid var(--bd);padding:8px 0;}
.ref-art:last-child{border-bottom:none;}
.ref-art-head{display:flex;align-items:center;gap:7px;cursor:pointer;padding:3px 0;}
.ref-art-head:hover .ref-art-title{color:var(--g);}
.ref-art-no{font-size:11px;font-weight:700;color:var(--gd);background:var(--gl);padding:2px 7px;border-radius:20px;white-space:nowrap;flex-shrink:0;}
.ref-art-title{font-size:13px;font-weight:600;color:var(--tx);line-height:1.4;}
.ref-art-body{display:none;font-size:12px;line-height:1.85;white-space:pre-wrap;color:var(--tx);margin-top:6px;padding:8px 10px;background:var(--sf);border-radius:var(--rs);}
.ref-divider{font-size:11px;font-weight:700;color:var(--mu);padding:6px 0 4px;letter-spacing:.4px;}
/* 준용 법령 배지 - 조문 내에서 참조됨을 표시 */
.ref-highlight-banner{display:flex;align-items:center;gap:8px;padding:7px 12px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:var(--rs);font-size:12px;color:#1e40af;margin-bottom:10px;}
/* 조문 아이템 (패널 내) */
.fp-art{border-bottom:1px solid var(--bd);padding:10px 0;}
.fp-art:last-child{border-bottom:none;}
.fp-art-head{display:flex;align-items:flex-start;gap:8px;cursor:pointer;padding:3px 0;}
.fp-art-head:hover .fp-art-title{color:var(--g);}
.fp-art-no{font-size:11px;font-weight:700;color:var(--gd);background:var(--gl);padding:2px 7px;border-radius:20px;white-space:nowrap;flex-shrink:0;margin-top:1px;}
.fp-art-title{font-size:13px;font-weight:600;color:var(--tx);line-height:1.5;}
.fp-art-body{display:none;font-size:13px;line-height:1.95;white-space:pre-wrap;color:var(--tx);margin-top:8px;padding:8px 10px;background:var(--bg);border-radius:var(--rs);}
.fp-art-body mark{background:#fff176;color:inherit;border-radius:2px;padding:0 1px;}
.fp-empty{text-align:center;padding:40px;color:var(--ht);font-size:14px;}
.fp-loading{text-align:center;padding:40px;color:var(--mu);font-size:13px;}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:400;display:none;}
.overlay.show{display:block;}
/* 인쇄 */
@media print{.topbar,.spanel,.fbar,.status,.pagination,.footer,.overlay,.fullpanel{display:none!important;}.main{max-width:100%;padding:0;}.card{box-shadow:none;border:none;page-break-inside:avoid;}.card-body{display:block!important;}}
/* 페이지네이션 */
.pagination{display:flex;justify-content:center;gap:6px;margin-top:18px;flex-wrap:wrap;}
.pgbtn{padding:6px 12px;border:1px solid var(--bd);border-radius:var(--rs);font-size:13px;cursor:pointer;background:var(--sf);color:var(--mu);transition:.12s;}
.pgbtn:hover{border-color:var(--g);color:var(--g);}
.pgbtn.active{background:var(--g);border-color:var(--g);color:#fff;}
.pgbtn:disabled{opacity:.4;cursor:not-allowed;}
/* 빈 상태 */
.empty{text-align:center;padding:52px 20px;color:var(--ht);}
.empty-icon{font-size:42px;margin-bottom:12px;opacity:.35;}
.empty p{font-size:14px;line-height:1.8;}
/* 푸터 */
.footer{text-align:center;padding:22px 0 14px;font-size:12px;color:var(--ht);}
/* AI 해석 버튼·박스 */
.ai-btn{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:20px;border:1px solid #a78bfa;font-size:11px;cursor:pointer;color:#7c3aed;background:#f5f3ff;transition:.15s;flex-shrink:0;}
.ai-btn:hover{background:#ede9fe;}
.ai-btn:disabled{opacity:.5;cursor:not-allowed;}
.ai-box{margin-top:8px;padding:10px 12px;background:linear-gradient(135deg,#f5f3ff,#ede9fe);border-left:3px solid #7c3aed;border-radius:0 var(--rs) var(--rs) 0;font-size:13px;line-height:1.85;color:#3b0764;}
.ai-box-loading{color:#7c3aed;font-style:italic;}
/* 즐겨찾기 버튼 */
.fav-btn{width:28px;height:28px;border:1px solid var(--bd);border-radius:6px;cursor:pointer;background:transparent;font-size:14px;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:.15s;color:var(--ht);}
.fav-btn:hover{border-color:#f59e0b;color:#f59e0b;}
.fav-btn.active{border-color:#f59e0b;color:#f59e0b;background:#fffbeb;}
/* 복사 버튼 */
.copy-btn{display:inline-flex;align-items:center;gap:4px;padding:4px 9px;border-radius:var(--rs);border:1px solid var(--bd);font-size:11px;cursor:pointer;color:var(--mu);background:transparent;transition:.15s;flex-shrink:0;}
.copy-btn:hover{border-color:var(--g);color:var(--g);}
.copy-btn.copied{border-color:var(--g);color:var(--g);background:var(--gl);}
/* 개정이력 */
.hist-wrap{margin-top:10px;border-top:1px solid var(--bd);padding-top:10px;}
.hist-item{display:flex;gap:10px;padding:5px 0;border-bottom:1px solid var(--bd);font-size:12px;}
.hist-item:last-child{border-bottom:none;}
.hist-date{color:var(--mu);white-space:nowrap;flex-shrink:0;}
.hist-desc{color:var(--tx);}
/* 즐겨찾기 탭 */
.fav-tab-btn{padding:5px 12px;border-radius:20px;border:1px solid var(--bd);font-size:12px;cursor:pointer;background:transparent;color:var(--mu);transition:.15s;display:flex;align-items:center;gap:4px;}
.fav-tab-btn:hover{border-color:#f59e0b;color:#f59e0b;}
.fav-tab-btn.active{background:#fffbeb;border-color:#f59e0b;color:#92400e;font-weight:600;}
.fav-empty{text-align:center;padding:30px;color:var(--ht);font-size:13px;}
/* AI 설정 모달 */
.ai-modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:600;display:none;align-items:center;justify-content:center;}
.ai-modal-bg.show{display:flex;}
.ai-modal{background:var(--sf);border-radius:var(--r);width:min(540px,95vw);max-height:90vh;overflow-y:auto;box-shadow:0 8px 40px rgba(0,0,0,.18);display:flex;flex-direction:column;}
.ai-modal-hd{padding:16px 20px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:10px;}
.ai-modal-title{font-size:15px;font-weight:600;flex:1;}
.ai-modal-close{width:28px;height:28px;border:1px solid var(--bd);border-radius:6px;cursor:pointer;background:transparent;font-size:14px;}
.ai-modal-close:hover{background:var(--bg);}
.ai-modal-body{padding:20px;}
/* 프로바이더 탭 */
.prov-tabs{display:flex;gap:0;border:1px solid var(--bd);border-radius:var(--rs);overflow:hidden;margin-bottom:18px;}
.prov-tab{flex:1;padding:8px 4px;font-size:12px;text-align:center;cursor:pointer;border:none;background:transparent;color:var(--mu);transition:.15s;border-right:1px solid var(--bd);}
.prov-tab:last-child{border-right:none;}
.prov-tab:hover{background:var(--bg);}
.prov-tab.active{background:var(--g);color:#fff;font-weight:600;}
.prov-panel{display:none;}.prov-panel.show{display:block;}
/* 입력 그룹 */
.ai-field{margin-bottom:14px;}
.ai-label{font-size:12px;font-weight:600;color:var(--mu);margin-bottom:5px;display:flex;align-items:center;gap:6px;}
.ai-label a{font-size:11px;color:var(--g);text-decoration:none;}
.ai-label a:hover{text-decoration:underline;}
.ai-input{width:100%;padding:9px 12px;border:1px solid var(--bd);border-radius:var(--rs);font-size:13px;background:var(--bg);color:var(--tx);outline:none;font-family:monospace;}
.ai-input:focus{border-color:var(--g);background:#fff;}
.ai-select{width:100%;padding:9px 12px;border:1px solid var(--bd);border-radius:var(--rs);font-size:13px;background:var(--bg);color:var(--tx);outline:none;cursor:pointer;}
.ai-select:focus{border-color:var(--g);}
.ai-hint{font-size:11px;color:var(--ht);margin-top:4px;}
.ai-status-row{display:flex;align-items:center;gap:8px;margin-bottom:14px;padding:8px 12px;background:var(--bg);border-radius:var(--rs);font-size:12px;}
.ai-status-dot{width:8px;height:8px;border-radius:50%;background:var(--ht);flex-shrink:0;}
.ai-status-dot.ok{background:#22c55e;}
.ai-status-dot.err{background:#ef4444;}
.ai-modal-foot{padding:14px 20px;border-top:1px solid var(--bd);display:flex;gap:8px;justify-content:flex-end;}
.ai-save-btn{padding:8px 20px;border-radius:var(--rs);border:none;background:var(--g);color:#fff;font-size:13px;font-weight:600;cursor:pointer;}
.ai-save-btn:hover{background:var(--gd);}
.ai-test-btn{padding:8px 16px;border-radius:var(--rs);border:1px solid var(--bd);background:transparent;color:var(--mu);font-size:13px;cursor:pointer;}
.ai-test-btn:hover{border-color:var(--g);color:var(--g);}
/* 상단바 설정 버튼 */
.settings-btn{padding:5px 10px;border:1px solid var(--bd);border-radius:var(--rs);font-size:12px;cursor:pointer;background:transparent;color:var(--mu);transition:.15s;margin-left:6px;white-space:nowrap;}
.settings-btn:hover{border-color:var(--g);color:var(--g);background:var(--gl);}
.ai-active-badge{display:inline-block;font-size:10px;padding:1px 6px;border-radius:10px;background:#f5f3ff;color:#7c3aed;border:1px solid #a78bfa;margin-left:4px;vertical-align:middle;}
/* ── 조문 메모 ── */
.memo-btn{display:inline-flex;align-items:center;gap:4px;padding:4px 9px;border-radius:var(--rs);border:1px solid var(--bd);font-size:11px;cursor:pointer;color:var(--mu);background:transparent;transition:.15s;}
.memo-btn:hover{border-color:#f59e0b;color:#92400e;}
.memo-btn.has-memo{border-color:#f59e0b;color:#92400e;background:#fffbeb;}
.memo-area{margin-top:8px;display:none;}
.memo-textarea{width:100%;padding:8px 10px;border:1px solid #f59e0b;border-radius:var(--rs);font-size:12px;line-height:1.7;resize:vertical;min-height:70px;background:#fffbeb;color:var(--tx);outline:none;font-family:inherit;}
.memo-textarea:focus{box-shadow:0 0 0 2px rgba(245,158,11,.2);}
.memo-actions{display:flex;gap:6px;margin-top:5px;}
.memo-save{padding:4px 12px;border-radius:var(--rs);border:none;background:#f59e0b;color:#fff;font-size:12px;cursor:pointer;}
.memo-save:hover{background:#d97706;}
.memo-del{padding:4px 10px;border-radius:var(--rs);border:1px solid var(--bd);background:transparent;color:var(--mu);font-size:12px;cursor:pointer;}
/* ── 법령 비교 패널 ── */
.cmp-panel{position:fixed;top:0;left:0;width:100vw;height:100vh;background:var(--bg);z-index:700;display:none;flex-direction:column;}
.cmp-panel.show{display:flex;}
.cmp-header{background:var(--sf);border-bottom:1px solid var(--bd);padding:12px 20px;display:flex;align-items:center;gap:12px;flex-shrink:0;}
.cmp-title{font-size:15px;font-weight:600;}
.cmp-body{flex:1;display:grid;grid-template-columns:1fr 1fr;gap:0;overflow:hidden;}
.cmp-col{display:flex;flex-direction:column;border-right:1px solid var(--bd);overflow:hidden;}
.cmp-col:last-child{border-right:none;}
.cmp-col-hd{padding:10px 14px;background:var(--sf);border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:8px;flex-shrink:0;}
.cmp-col-title{font-size:13px;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.cmp-col-body{flex:1;overflow-y:auto;padding:12px 14px;}
.cmp-select-row{padding:10px 14px;background:var(--bg);border-bottom:1px solid var(--bd);display:flex;gap:8px;flex-shrink:0;}
.cmp-search{flex:1;padding:7px 10px;border:1px solid var(--bd);border-radius:var(--rs);font-size:13px;background:var(--sf);color:var(--tx);outline:none;}
.cmp-search:focus{border-color:var(--g);}
.cmp-load-btn{padding:7px 12px;border-radius:var(--rs);border:none;background:var(--g);color:#fff;font-size:13px;cursor:pointer;}
.cmp-art{border-bottom:1px solid var(--bd);padding:8px 0;}
.cmp-art:last-child{border-bottom:none;}
.cmp-art-no{font-size:11px;font-weight:700;color:var(--gd);background:var(--gl);padding:2px 7px;border-radius:20px;display:inline-block;margin-bottom:4px;}
.cmp-art-title{font-size:13px;font-weight:600;margin-bottom:4px;}
.cmp-art-body{font-size:12px;line-height:1.85;white-space:pre-wrap;color:var(--tx);}
.cmp-struct{font-size:11px;font-weight:700;color:var(--mu);padding:5px 0;letter-spacing:.5px;}
/* ── 법률 용어 툴팁 ── */
.law-term{border-bottom:1px dashed var(--g);cursor:help;color:inherit;}
.term-tooltip{position:fixed;z-index:800;max-width:280px;background:#1a1a2e;color:#e2e8f0;font-size:12px;line-height:1.7;padding:8px 12px;border-radius:var(--rs);pointer-events:none;display:none;box-shadow:0 4px 16px rgba(0,0,0,.3);}
.term-tooltip .tt-word{font-size:11px;font-weight:700;color:#86efac;margin-bottom:3px;}
/* ── 내보내기 버튼 ── */
.export-btn{padding:5px 11px;border:1px solid var(--bd);border-radius:var(--rs);font-size:12px;cursor:pointer;background:transparent;color:var(--mu);transition:.15s;white-space:nowrap;}
.export-btn:hover{border-color:var(--g);color:var(--g);background:var(--gl);}
/* ── 다크모드 ── */
body.dark{--bg:#141417;--sf:#1e1e24;--bd:#2e2e38;--tx:#e2e4e9;--mu:#9ca3af;--ht:#6b7280;}
body.dark .topbar{background:#1e1e24;}
body.dark .sinput,body.dark .ai-input,body.dark .cmp-search,body.dark .fp-finput,body.dark .art-finput{background:#141417;color:#e2e4e9;border-color:#2e2e38;}
body.dark .ai-box{background:linear-gradient(135deg,#1e1b40,#2e1b5e);color:#c4b5fd;}
body.dark .memo-textarea{background:#2a1f00;color:#fde68a;}
body.dark .cmp-col-body,.dark .fp-body{background:#141417;}
body.dark .card,.dark .spanel,.dark .fullpanel,.dark .cmp-panel,.dark .ai-modal{background:#1e1e24;border-color:#2e2e38;}
.dark-btn{padding:5px 9px;border:1px solid var(--bd);border-radius:var(--rs);font-size:13px;cursor:pointer;background:transparent;color:var(--mu);transition:.15s;line-height:1;}
.dark-btn:hover{background:var(--bg);}
/* ── 조문 북마크 ── */
.bm-btn{display:inline-flex;align-items:center;gap:3px;padding:4px 9px;border-radius:var(--rs);border:1px solid var(--bd);font-size:11px;cursor:pointer;color:var(--mu);background:transparent;transition:.15s;}
.bm-btn:hover{border-color:#3b82f6;color:#1d4ed8;}
.bm-btn.active{border-color:#3b82f6;color:#1d4ed8;background:#eff6ff;}
/* ── 통계 패널 ── */
.stat-panel{position:fixed;top:0;right:-460px;width:min(460px,100vw);height:100vh;background:var(--sf);border-left:1px solid var(--bd);z-index:500;display:flex;flex-direction:column;transition:right .3s cubic-bezier(.4,0,.2,1);box-shadow:-4px 0 24px rgba(0,0,0,.12);}
.stat-panel.open{right:0;}
.stat-hd{display:flex;align-items:center;gap:10px;padding:14px 18px;border-bottom:1px solid var(--bd);flex-shrink:0;}
.stat-title{font-size:15px;font-weight:600;flex:1;}
.stat-body{flex:1;overflow-y:auto;padding:16px 18px;}
.stat-section{margin-bottom:20px;}
.stat-section-title{font-size:12px;font-weight:600;color:var(--mu);letter-spacing:.5px;margin-bottom:10px;text-transform:uppercase;}
.stat-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--bd);font-size:13px;}
.stat-row:last-child{border-bottom:none;}
.stat-rank{width:20px;font-size:11px;color:var(--ht);text-align:right;flex-shrink:0;}
.stat-label{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.stat-bar-wrap{width:80px;height:6px;background:var(--bd);border-radius:3px;flex-shrink:0;}
.stat-bar{height:100%;background:var(--g);border-radius:3px;}
.stat-cnt{font-size:11px;color:var(--ht);width:28px;text-align:right;flex-shrink:0;}
.stat-empty{text-align:center;padding:30px;color:var(--ht);font-size:13px;}
/* ── 조문 상호참조 ── */
.art-xref{color:var(--g);border-bottom:1px dashed var(--g);cursor:pointer;font-weight:500;}
.art-xref:hover{background:var(--gl);}
/* ── 최근 개정 조문 ── */
.amend-badge{display:inline-block;font-size:10px;font-weight:700;padding:1px 7px;border-radius:10px;margin-left:6px;flex-shrink:0;vertical-align:middle;}
.amend-badge.개정{background:#fef3c7;color:#92400e;border:1px solid #fcd34d;}
.amend-badge.신설{background:#d1fae5;color:#065f46;border:1px solid #6ee7b7;}
.amend-badge.삭제{background:#fee2e2;color:#991b1b;border:1px solid #fca5a5;}
.amend-notice{display:flex;align-items:center;gap:8px;padding:8px 12px;background:#fffbeb;border:1px solid #fcd34d;border-radius:var(--rs);font-size:12px;color:#92400e;margin-bottom:10px;flex-shrink:0;}
.amend-notice .dot{width:8px;height:8px;border-radius:50%;background:#f59e0b;flex-shrink:0;}
.amend-item{border:1px solid var(--bd);border-radius:var(--rs);margin-bottom:8px;overflow:hidden;}
.amend-item-hd{display:flex;align-items:center;gap:8px;padding:10px 12px;cursor:pointer;background:var(--bg);}
.amend-item-hd:hover{background:#f0fdf4;}
.amend-item-body{display:none;padding:10px 12px;font-size:12px;line-height:1.8;white-space:pre-wrap;color:var(--tx);border-top:1px solid var(--bd);background:var(--sf);}
.amend-goto{padding:4px 10px;border-radius:var(--rs);border:1px solid var(--g);color:var(--g);font-size:11px;cursor:pointer;background:transparent;margin-left:auto;flex-shrink:0;white-space:nowrap;}
.amend-goto:hover{background:var(--gl);}
/* ── ① diff 뷰 ── */
.diff-panel{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:900;display:none;align-items:center;justify-content:center;}
.diff-panel.show{display:flex;}
.diff-box{background:var(--sf);border-radius:var(--r);width:min(860px,96vw);max-height:90vh;display:flex;flex-direction:column;box-shadow:0 8px 40px rgba(0,0,0,.2);}
.diff-hd{padding:14px 18px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:10px;flex-shrink:0;}
.diff-title{font-size:15px;font-weight:600;flex:1;}
.diff-body{display:grid;grid-template-columns:1fr 1fr;overflow:hidden;flex:1;}
.diff-col{display:flex;flex-direction:column;overflow:hidden;border-right:1px solid var(--bd);}
.diff-col:last-child{border-right:none;}
.diff-col-hd{padding:8px 14px;font-size:12px;font-weight:700;background:var(--bg);border-bottom:1px solid var(--bd);flex-shrink:0;}
.diff-col-hd.old{color:#991b1b;background:#fff1f2;}
.diff-col-hd.new{color:#065f46;background:#f0fdf4;}
.diff-content{flex:1;overflow-y:auto;padding:12px 14px;font-size:13px;line-height:1.9;white-space:pre-wrap;}
.diff-add{background:#d1fae5;color:#065f46;}
.diff-del{background:#fee2e2;color:#991b1b;text-decoration:line-through;}
.diff-same{color:var(--tx);}
/* ── ② 컬렉션 ── */
.coll-modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:600;display:none;align-items:center;justify-content:center;}
.coll-modal-bg.show{display:flex;}
.coll-modal{background:var(--sf);border-radius:var(--r);width:min(560px,95vw);max-height:88vh;display:flex;flex-direction:column;box-shadow:0 8px 40px rgba(0,0,0,.18);}
.coll-hd{padding:14px 18px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:10px;}
.coll-body{flex:1;overflow-y:auto;padding:16px 18px;}
.coll-folder{border:1px solid var(--bd);border-radius:var(--r);margin-bottom:10px;overflow:hidden;}
.coll-folder-hd{display:flex;align-items:center;gap:8px;padding:10px 14px;cursor:pointer;background:var(--bg);}
.coll-folder-hd:hover{background:#eef0f2;}
.coll-folder-icon{font-size:15px;flex-shrink:0;}
.coll-folder-name{font-size:14px;font-weight:600;flex:1;}
.coll-folder-cnt{font-size:11px;color:var(--ht);}
.coll-folder-body{display:none;padding:8px 12px;border-top:1px solid var(--bd);}
.coll-item{display:flex;align-items:center;gap:8px;padding:6px 4px;border-bottom:1px solid var(--bd);font-size:13px;}
.coll-item:last-child{border-bottom:none;}
.coll-item-name{flex:1;cursor:pointer;color:var(--g);}
.coll-item-name:hover{text-decoration:underline;}
.coll-del-btn{width:20px;height:20px;border:none;background:transparent;color:var(--ht);cursor:pointer;font-size:14px;flex-shrink:0;}
.coll-del-btn:hover{color:#ef4444;}
.coll-new-row{display:flex;gap:8px;margin-top:10px;}
.coll-input{flex:1;padding:7px 10px;border:1px solid var(--bd);border-radius:var(--rs);font-size:13px;background:var(--bg);color:var(--tx);outline:none;}
.coll-input:focus{border-color:var(--g);}
.coll-add-btn{padding:7px 14px;border-radius:var(--rs);border:none;background:var(--g);color:#fff;font-size:13px;cursor:pointer;}
/* ── ③ 네트워크 그래프 ── */
.net-panel{position:fixed;inset:0;background:var(--bg);z-index:700;display:none;flex-direction:column;}
.net-panel.show{display:flex;}
.net-hd{background:var(--sf);border-bottom:1px solid var(--bd);padding:12px 18px;display:flex;align-items:center;gap:12px;flex-shrink:0;}
.net-canvas{flex:1;position:relative;overflow:hidden;}
#netSvg{width:100%;height:100%;}
.net-node{cursor:pointer;}
.net-node circle{transition:.15s;}
.net-node:hover circle{filter:brightness(.88);}
.net-node text{pointer-events:none;font-size:11px;font-family:inherit;}
.net-edge{stroke:var(--bd);stroke-width:1.5;fill:none;marker-end:url(#arrow);}
.net-legend{position:absolute;bottom:16px;left:16px;background:var(--sf);border:1px solid var(--bd);border-radius:var(--rs);padding:8px 12px;font-size:11px;}
.net-legend-row{display:flex;align-items:center;gap:6px;margin-bottom:4px;}
.net-legend-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0;}
/* ── ④ 조문 히스토리 ── */
.hist-timeline{padding:4px 0;}
.hist-entry{display:flex;gap:14px;position:relative;padding-bottom:16px;}
.hist-entry::before{content:'';position:absolute;left:11px;top:22px;bottom:0;width:2px;background:var(--bd);}
.hist-entry:last-child::before{display:none;}
.hist-dot{width:24px;height:24px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;z-index:1;}
.hist-dot.신설{background:#d1fae5;color:#065f46;border:2px solid #6ee7b7;}
.hist-dot.개정{background:#fef3c7;color:#92400e;border:2px solid #fcd34d;}
.hist-dot.현행{background:var(--g);color:#fff;border:2px solid var(--gd);}
.hist-entry-body{flex:1;padding-bottom:4px;}
.hist-entry-date{font-size:11px;color:var(--ht);margin-bottom:3px;}
.hist-entry-content{font-size:12px;line-height:1.8;white-space:pre-wrap;color:var(--tx);background:var(--bg);padding:8px 10px;border-radius:var(--rs);}
/* ── ⑤ 자연어 검색 ── */
.nl-badge{display:inline-flex;align-items:center;gap:3px;font-size:10px;padding:1px 7px;border-radius:10px;background:#f0fdf4;color:#065f46;border:1px solid #6ee7b7;flex-shrink:0;}
.nl-score{font-size:10px;color:var(--ht);margin-left:auto;}
</style>
</head>
<body>
<div class="overlay" id="overlay" onclick="closePanel()"></div>

<!-- 법률 용어 툴팁 -->
<div class="term-tooltip" id="termTooltip"><div class="tt-word" id="ttWord"></div><div id="ttDesc"></div></div>

<!-- ① diff 뷰 모달 -->
<div class="diff-panel" id="diffPanel" onclick="if(event.target===this)closeDiff()">
  <div class="diff-box">
    <div class="diff-hd">
      <span class="diff-title" id="diffTitle">조문 변경 비교</span>
      <button class="fp-tbtn" onclick="closeDiff()">✕ 닫기</button>
    </div>
    <div class="diff-body">
      <div class="diff-col">
        <div class="diff-col-hd old" id="diffOldLabel">이전 버전</div>
        <div class="diff-content" id="diffOld"></div>
      </div>
      <div class="diff-col">
        <div class="diff-col-hd new" id="diffNewLabel">현재 버전</div>
        <div class="diff-content" id="diffNew"></div>
      </div>
    </div>
  </div>
</div>

<!-- ② 컬렉션 모달 -->
<div class="coll-modal-bg" id="collModalBg" onclick="if(event.target===this)closeCollModal()">
  <div class="coll-modal">
    <div class="coll-hd">
      <span style="font-size:15px;font-weight:600;flex:1;">📁 업무별 법령 컬렉션</span>
      <button class="fp-close" onclick="closeCollModal()">✕</button>
    </div>
    <div class="coll-body" id="collBody"></div>
    <div style="padding:12px 18px;border-top:1px solid var(--bd);">
      <div style="font-size:12px;color:var(--mu);margin-bottom:8px;">새 컬렉션 만들기</div>
      <div class="coll-new-row">
        <input class="coll-input" id="collNewName" placeholder="컬렉션 이름 (예: 종자 관리 업무)">
        <button class="coll-add-btn" onclick="createCollection()">만들기</button>
      </div>
    </div>
  </div>
</div>

<!-- ③ 네트워크 그래프 패널 -->
<div class="net-panel" id="netPanel">
  <div class="net-hd">
    <span style="font-size:15px;font-weight:600;">🕸 법령 관계 네트워크</span>
    <select id="netCenter" style="padding:6px 10px;border:1px solid var(--bd);border-radius:var(--rs);font-size:13px;background:var(--sf);color:var(--tx);" onchange="buildNetwork()"></select>
    <span style="font-size:12px;color:var(--ht);">중심 법령을 선택하면 준용·참조 관계를 시각화합니다</span>
    <button class="fp-tbtn" onclick="closeNetPanel()">✕ 닫기</button>
  </div>
  <div class="net-canvas" id="netCanvas">
    <svg id="netSvg">
      <defs>
        <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
          <path d="M0,0 L0,6 L8,3 z" fill="#9ca3af"/>
        </marker>
      </defs>
      <g id="netG"></g>
    </svg>
    <div class="net-legend">
      <div class="net-legend-row"><div class="net-legend-dot" style="background:#1D9E75;"></div>현재 법령</div>
      <div class="net-legend-row"><div class="net-legend-dot" style="background:#3b82f6;"></div>준용하는 법령</div>
      <div class="net-legend-row"><div class="net-legend-dot" style="background:#f59e0b;"></div>시행령·시행규칙</div>
    </div>
  </div>
</div>

<!-- 법령 비교 패널 -->
<div class="cmp-panel" id="cmpPanel">
  <div class="cmp-header">
    <span class="cmp-title">⚖ 법령 비교</span>
    <button class="fp-tbtn" onclick="closeCmp()">✕ 닫기</button>
  </div>
  <div class="cmp-body">
    <div class="cmp-col">
      <div class="cmp-select-row">
        <input class="cmp-search" id="cmpQ0" placeholder="비교할 법령명 입력" onkeydown="if(event.key==='Enter')loadCmpLaw(0)">
        <button class="cmp-load-btn" onclick="loadCmpLaw(0)">불러오기</button>
      </div>
      <div class="cmp-col-hd"><span class="cmp-col-title" id="cmpTitle0">— 법령을 입력하세요 —</span></div>
      <div class="cmp-col-body" id="cmpBody0"><div style="padding:20px;text-align:center;color:var(--ht);font-size:13px;">법령명을 입력하고 불러오기를 눌러주세요</div></div>
    </div>
    <div class="cmp-col">
      <div class="cmp-select-row">
        <input class="cmp-search" id="cmpQ1" placeholder="비교할 법령명 입력" onkeydown="if(event.key==='Enter')loadCmpLaw(1)">
        <button class="cmp-load-btn" onclick="loadCmpLaw(1)">불러오기</button>
      </div>
      <div class="cmp-col-hd"><span class="cmp-col-title" id="cmpTitle1">— 법령을 입력하세요 —</span></div>
      <div class="cmp-col-body" id="cmpBody1"><div style="padding:20px;text-align:center;color:var(--ht);font-size:13px;">법령명을 입력하고 불러오기를 눌러주세요</div></div>
    </div>
  </div>
</div>

<!-- AI 설정 모달 -->
<div class="ai-modal-bg" id="aiModalBg" onclick="if(event.target===this)closeAiModal()">
  <div class="ai-modal">
    <div class="ai-modal-hd">
      <span class="ai-modal-title">✦ AI 해석 모델 설정</span>
      <button class="ai-modal-close" onclick="closeAiModal()">✕</button>
    </div>
    <div class="ai-modal-body">
      <div class="ai-status-row">
        <div class="ai-status-dot" id="aiStatusDot"></div>
        <span id="aiStatusMsg" style="color:var(--mu);">저장된 설정이 없습니다. 아래에서 API 키를 입력하세요.</span>
      </div>
      <div class="prov-tabs">
        <button class="prov-tab active" onclick="setProv('claude')">Claude</button>
        <button class="prov-tab" onclick="setProv('gpt')">GPT</button>
        <button class="prov-tab" onclick="setProv('gemini')">Gemini</button>
        <button class="prov-tab" onclick="setProv('ollama')">Ollama(로컬)</button>
      </div>

      <!-- Claude -->
      <div class="prov-panel show" id="prov-claude">
        <div class="ai-field">
          <div class="ai-label">API 키 <a href="https://console.anthropic.com/keys" target="_blank">발급받기 →</a></div>
          <input class="ai-input" id="key-claude" type="password" placeholder="sk-ant-api03-...">
          <div class="ai-hint">Anthropic Console에서 발급. claude.ai 계정과 별개입니다.</div>
        </div>
        <div class="ai-field">
          <div class="ai-label">모델 선택</div>
          <select class="ai-select" id="model-claude">
            <option value="claude-sonnet-4-20250514">Claude Sonnet 4 (권장)</option>
            <option value="claude-opus-4-20250514">Claude Opus 4 (고성능)</option>
            <option value="claude-haiku-4-5-20251001">Claude Haiku 4.5 (빠름)</option>
          </select>
        </div>
      </div>

      <!-- GPT -->
      <div class="prov-panel" id="prov-gpt">
        <div class="ai-field">
          <div class="ai-label">API 키 <a href="https://platform.openai.com/api-keys" target="_blank">발급받기 →</a></div>
          <input class="ai-input" id="key-gpt" type="password" placeholder="sk-...">
          <div class="ai-hint">OpenAI Platform에서 발급. ChatGPT 계정과 별개입니다.</div>
        </div>
        <div class="ai-field">
          <div class="ai-label">모델 선택</div>
          <select class="ai-select" id="model-gpt">
            <option value="gpt-4o">GPT-4o (권장)</option>
            <option value="gpt-4o-mini">GPT-4o Mini (빠름·저렴)</option>
            <option value="gpt-4-turbo">GPT-4 Turbo</option>
          </select>
        </div>
      </div>

      <!-- Gemini -->
      <div class="prov-panel" id="prov-gemini">
        <div class="ai-field">
          <div class="ai-label">API 키 <a href="https://aistudio.google.com/apikey" target="_blank">발급받기 →</a></div>
          <input class="ai-input" id="key-gemini" type="password" placeholder="AIza...">
          <div class="ai-hint">Google AI Studio에서 무료 발급 가능합니다.</div>
        </div>
        <div class="ai-field">
          <div class="ai-label">모델 선택</div>
          <select class="ai-select" id="model-gemini">
            <option value="gemini-2.0-flash">Gemini 2.0 Flash (권장)</option>
            <option value="gemini-1.5-pro">Gemini 1.5 Pro</option>
            <option value="gemini-1.5-flash">Gemini 1.5 Flash (빠름)</option>
          </select>
        </div>
      </div>

      <!-- Ollama -->
      <div class="prov-panel" id="prov-ollama">
        <div class="ai-field">
          <div class="ai-label">Ollama 서버 주소</div>
          <input class="ai-input" id="key-ollama" type="text" placeholder="http://localhost:11434" value="http://localhost:11434">
          <div class="ai-hint">Ollama를 로컬에서 실행 중이어야 합니다. API 키 불필요.</div>
        </div>
        <div class="ai-field">
          <div class="ai-label">모델 이름</div>
          <input class="ai-input" id="model-ollama" type="text" placeholder="gemma3, llama3.2, qwen2.5 등">
          <div class="ai-hint">ollama pull [모델명] 으로 먼저 설치하세요.</div>
        </div>
      </div>
    </div>
    <div class="ai-modal-foot">
      <button class="ai-test-btn" onclick="testAiConn()">연결 테스트</button>
      <button class="ai-save-btn" onclick="saveAiSettings()">저장 후 닫기</button>
    </div>
  </div>
</div>

<!-- 법령 전문 슬라이드 패널 -->
<div class="fullpanel" id="fullpanel">
  <!-- 좌측: 현재 법령 -->
  <div class="fp-left" id="fpLeft">
    <div class="fp-header">
      <span class="fp-title" id="fpTitle">법령 전문</span>
      <button class="fav-btn" id="fpFavBtn" onclick="toggleFavFromPanel()" title="즐겨찾기">☆</button>
      <a id="fpExtLink" href="#" target="_blank" class="fp-tbtn">↗ 법제처</a>
      <button class="fp-tbtn" onclick="openCmpFromPanel()">⚖ 비교</button>
      <button class="fp-tbtn" id="fpAmendBtn" onclick="fpShowAmendments()">🔴 최근 개정</button>
      <button class="fp-tbtn" onclick="fpShowHistory()">📅 개정이력</button>
      <button class="fp-tbtn" onclick="fpPrint()">🖨 인쇄</button>
      <button class="fp-close" onclick="closePanel()">✕</button>
    </div>
    <div class="fp-toolbar">
      <input class="fp-finput" id="fpFilter" placeholder="조문번호(예: 2) 또는 키워드 검색" oninput="fpFilterArts()">
      <span class="fp-cnt" id="fpCnt"></span>
      <button class="fp-tbtn" onclick="fpExpandAll(true)">전체 펼치기</button>
      <button class="fp-tbtn" onclick="fpExpandAll(false)">전체 접기</button>
    </div>
    <div class="fp-body" id="fpBody"><div class="fp-loading">불러오는 중...</div></div>
  </div>
  <!-- 우측: 준용 법령 비교 (슬라이드) -->
  <div class="fp-right" id="fpRight">
    <div class="fp-right-header">
      <div style="display:flex;flex-direction:column;gap:2px;min-width:0;flex:1;">
        <span style="font-size:10px;color:var(--ht);">준용·참조 법령</span>
        <span class="fp-right-title" id="fpRightTitle"></span>
      </div>
      <a id="fpRightExtLink" href="#" target="_blank" class="fp-tbtn" style="flex-shrink:0;">↗ 법제처</a>
      <input class="fp-finput" id="fpRightFilter" placeholder="조문 검색" oninput="fpRightFilter()" style="width:120px;flex-shrink:0;">
      <button class="fp-close" onclick="closeRefPanel()" title="준용 법령 닫기">✕</button>
    </div>
    <div class="fp-right-body" id="fpRightBody">
      <div class="fp-loading">준용 법령을 불러오는 중...</div>
    </div>
  </div>
</div>

<!-- 상단바 -->
<div class="topbar">
  <div class="logo">農</div>
  <span class="brand">농업 법령 검색</span>
  <span class="brand-sub">법제처 국가법령정보 연동</span>
  <span class="apibadge" id="badge">● 확인 중</span>
  <button class="dark-btn" id="darkBtn" onclick="toggleDark()" title="다크모드">🌙</button>
  <button class="settings-btn" onclick="openStatPanel()">📊 통계</button>
  <button class="settings-btn" onclick="openCollModal()">📁 컬렉션</button>
  <button class="settings-btn" onclick="openNetPanel()">🕸 관계도</button>
  <button class="settings-btn" onclick="openAiModal()">✦ AI 설정 <span class="ai-active-badge" id="aiActiveBadge" style="display:none"></span></button>
</div>

<!-- 통계 패널 -->
<div class="stat-panel" id="statPanel">
  <div class="stat-hd">
    <span class="stat-title">📊 검색 통계</span>
    <button class="fp-close" onclick="closeStatPanel()">✕</button>
  </div>
  <div class="stat-body" id="statBody">
    <div class="stat-empty">검색 기록이 없습니다.<br>법령을 검색하면 통계가 쌓입니다.</div>
  </div>
</div>

<div class="main">
  <div class="spanel">
    <!-- 기관 탭 -->
    <div class="tabs">
      <button class="tab active" onclick="setAgency('all')">전체</button>
      <button class="tab" onclick="setAgency('mafra')">농림축산식품부</button>
      <button class="tab" onclick="setAgency('rda')">농촌진흥청</button>
      <button class="tab" onclick="setAgency('koat')">한국농업기술진흥원</button>
      <button class="fav-tab-btn" id="favTabBtn" onclick="toggleFavTab()">☆ 즐겨찾기 <span id="favCount"></span></button>
    </div>
    <!-- 검색 모드 -->
    <div class="mtoggle">
      <button class="mbtn active" id="mlawbtn" onclick="setMode('law')">법령명 검색</button>
      <button class="mbtn" id="martbtn" onclick="setMode('article')">조문 내 키워드 검색</button>
    </div>
    <!-- 검색창 -->
    <div class="srow">
      <input id="q" class="sinput" placeholder="법령명 또는 키워드 입력" onkeydown="if(event.key==='Enter')doSearch()">
      <button id="sbtn" class="sbtn" onclick="doSearch()">검색</button>
    </div>
    <!-- 자주 찾는 법령 -->
    <div class="qlabel">자주 찾는 법령</div>
    <div class="chips" id="chips"><span style="font-size:12px;color:var(--ht);">검증 중...</span></div>
    <!-- 최근 검색어 -->
    <div class="recent" id="recentRow" style="display:none">
      <span class="rec-label">최근 검색</span>
      <div id="recentChips"></div>
    </div>
  </div>

  <!-- 필터바 -->
  <div class="fbar" id="fbar" style="display:none">
    <select id="sortSel" onchange="applyFilter()">
      <option value="name">법령명 순</option>
      <option value="date">공포일 최신순</option>
    </select>
    <select id="typeSel" onchange="applyFilter()"><option value="">유형 전체</option></select>
    <span class="rcount" id="rcount"></span>
    <button class="export-btn" onclick="exportCSV()" title="검색 결과를 CSV로 저장">⬇ CSV 저장</button>
    <button class="export-btn" onclick="openCmpPanel()" title="두 법령 나란히 비교">⚖ 법령 비교</button>
  </div>
  <!-- 자연어 검색 힌트 배너 (자연어 모드 활성 시) -->
  <div id="nlBanner" style="display:none;margin-bottom:10px;padding:8px 12px;background:#f0fdf4;border:1px solid #6ee7b7;border-radius:var(--rs);font-size:12px;color:#065f46;display:flex;align-items:center;gap:8px;">
    <span class="nl-badge">✨ 자연어</span>
    <span>질문 형태로 검색하면 관련 조문을 찾아드립니다. <b>예:</b> 농업인이 받을 수 있는 보조금 조건은?</span>
  </div>

  <div class="status" id="status"></div>
  <div class="results" id="results">
    <div class="empty"><div class="empty-icon">📋</div><p>법령 키워드를 검색하거나<br>위 빠른 검색 버튼을 눌러주세요</p></div>
  </div>
  <div class="pagination" id="pagination"></div>
  <div class="footer">ⓒ KIMJS. All rights reserved.</div>
</div>

<script>
// ── 상수 ──
const KEYWORDS = {
  all:   ['농지법','종자산업법','농촌진흥법','농약관리법','비료관리법','축산법','식물방역법','발명진흥법','특허법'],
  mafra: ['농지법','축산법','식물방역법','농수산물 유통 및 가격안정에 관한 법률','식품안전기본법','농어업재해보험법'],
  rda:   ['농촌진흥법','종자산업법','농약관리법','비료관리법','농업생명자원의 보존·관리 및 이용에 관한 법률'],
  koat:  ['농업기술실용화 촉진법','기술의 이전 및 사업화 촉진에 관한 법률','발명진흥법','특허법','실용신안법','디자인보호법','상표법','식물신품종 보호법','부정경쟁방지 및 영업비밀보호에 관한 법률']
};

let agency='all', mode='law', allLaws=[], filtered=[], page=1, fpArts=[];
const PS=10;

// ── 키워드 유효성 검증 ──
async function validateKeywords(){
  const kws = [...new Set(Object.values(KEYWORDS).flat())];
  const valid = new Set();
  // 병렬로 검증
  await Promise.all(kws.map(async kw=>{
    try{
      const r = await fetch(`/api/validate?q=${encodeURIComponent(kw)}`);
      const d = await r.json();
      if(d.valid) valid.add(kw);
    }catch{}
  }));
  // 각 기관 키워드 필터링
  window._validKws = valid;
  renderChips();
}

function renderChips(){
  const kws = KEYWORDS[agency].filter(k => !window._validKws || window._validKws.has(k));
  document.getElementById('chips').innerHTML = kws.length
    ? kws.map(k=>`<button class="chip" onclick="quick('${k.replace(/'/g,"\\'")}')"> ${k}</button>`).join('')
    : '<span style="font-size:12px;color:var(--ht);">검색 가능한 빠른 키워드가 없습니다</span>';
}

// ── 기관 탭 ──
function setAgency(ag){
  agency=ag; favMode=false;
  document.getElementById('favTabBtn').classList.remove('active');
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',['all','mafra','rda','koat'][i]===ag));
  renderChips();
}

// ── 검색 모드 ──
function setMode(m){
  mode=m;
  document.getElementById('mlawbtn').classList.toggle('active',m==='law');
  document.getElementById('martbtn').classList.toggle('active',m==='article');
  document.getElementById('q').placeholder = m==='law'
    ? '법령명 또는 키워드 입력 (예: 농지, 종자, 농약)'
    : '조문 내 키워드 입력 (예: 전용실시, 손해배상, 등록취소)';
  clearResults();
}
function clearResults(){
  document.getElementById('fbar').style.display='none';
  document.getElementById('pagination').innerHTML='';
  document.getElementById('results').innerHTML='<div class="empty"><div class="empty-icon">📋</div><p>검색어를 입력하고 검색 버튼을 눌러주세요</p></div>';
  setStatus('');
}

function quick(kw){document.getElementById('q').value=kw;doSearch();}

// ── 상태 ──
function setStatus(msg,loading=false){
  document.getElementById('status').innerHTML=msg
    ?(loading?`<div class="spinner"></div><span>${msg}</span>`:`<span>${msg}</span>`):'';
}

// ── 기관 추론 ──
function guessOrg(name){
  if(/농촌진흥|종자|농약|비료|작물/.test(name))return '농촌진흥청';
  if(/기술실용화|기술이전|사업화/.test(name))return '한국농업기술진흥원';
  if(/특허|실용신안|디자인보호|상표|발명|부정경쟁/.test(name))return '특허청';
  return '농림축산식품부';
}

// ── 검색 ──
async function doSearch(){
  const q=document.getElementById('q').value.trim();
  if(!q) return;

  // 통계 기록
  recordStat('search', q);

  document.getElementById('sbtn').disabled=true;
  clearResults();

  // 조문 키워드 모드
  if(mode==='article'){
    await doSearchArticle(q);
    document.getElementById('sbtn').disabled=false;
    return;
  }

  // 자연어 감지
  if(isNaturalLang && isNaturalLang(q)){
    await doNLSearch(q);
    return;
  }

  // 법령명 검색 (기본)
  setStatus('법제처 조회 중...',true);
  try{
    const r=await fetch(`/api/search?query=${encodeURIComponent(q)}&display=20`);
    const d=await r.json();
    if(d.error){setStatus(`오류: ${d.error}`);renderEmpty(d.error);return;}
    if(!d.success||!d.laws?.length){setStatus(`"${q}" 검색 결과가 없습니다`);renderEmpty(`"${q}"에 해당하는 법령을 찾지 못했습니다`);return;}
    addRecent(q);
    allLaws=d.laws; buildTypeFilter(); applyFilter();
    setStatus(`법제처 실시간 · ${allLaws.length}건`);
    document.getElementById('fbar').style.display='flex';
  }catch(e){setStatus('서버 연결 실패');renderEmpty('서버에 연결할 수 없습니다');}
  document.getElementById('sbtn').disabled=false;
}

// ── 조문 키워드 검색 ──
async function doSearchArticle(q){
  setStatus(`"${q}" 조문 내 키워드 검색 중...`,true);
  addRecent(q);
  try{
    const r=await fetch(`/api/search/article?query=${encodeURIComponent(q)}&display=20`);
    const d=await r.json();
    if(d.error){setStatus(`오류: ${d.error}`);renderEmpty(d.error);return;}
    if(!d.success||!d.laws?.length){
      setStatus(`"${q}" 조문 검색 결과가 없습니다`);
      renderEmpty(`"${q}"을(를) 포함한 조문이 있는 법령을 찾지 못했습니다`);
      return;
    }
    setStatus(`조문 키워드 검색 · ${d.laws.length}건 · 클릭하면 관련 조문이 표시됩니다`);
    renderArticleLawList(d.laws, q);  // 검색어 전달
  }catch(e){setStatus('서버 연결 실패');renderEmpty('서버에 연결할 수 없습니다');}
}

// ── 최근 검색어 ──
function addRecent(q){
  fetch(`/api/recent/add?q=${encodeURIComponent(q)}`).then(()=>loadRecent());
}
async function loadRecent(){
  try{
    const d=await (await fetch('/api/recent')).json();
    const row=document.getElementById('recentRow');
    if(!d.recent||!d.recent.length){row.style.display='none';return;}
    row.style.display='flex';
    document.getElementById('recentChips').innerHTML=
      d.recent.map(r=>`<button class="rec-chip" onclick="quick('${r.replace(/'/g,"\\'")}')"> ${r}</button>`).join('');
  }catch{}
}

// ── 필터 ──
function buildTypeFilter(){
  const types=[...new Set(allLaws.map(l=>l['법령구분명']||'법률'))].sort();
  document.getElementById('typeSel').innerHTML='<option value="">유형 전체</option>'+types.map(t=>`<option>${t}</option>`).join('');
}
function applyFilter(){
  const sort=document.getElementById('sortSel').value;
  const type=document.getElementById('typeSel').value;
  filtered=allLaws.filter(l=>!type||(l['법령구분명']||'법률')===type);
  if(sort==='date')filtered.sort((a,b)=>(b['공포일자']||'').localeCompare(a['공포일자']||''));
  else filtered.sort((a,b)=>(a['법령명한글']||'').localeCompare(b['법령명한글']||'','ko'));
  page=1; document.getElementById('rcount').textContent=`${filtered.length}건`; renderPage();
}

// ── 법령명 검색 결과 렌더 ──
function renderPage(){
  const start=(page-1)*PS, slice=filtered.slice(start,start+PS);
  if(!slice.length){renderEmpty('필터 조건에 맞는 법령이 없습니다');return;}
  document.getElementById('results').innerHTML=slice.map((law,ri)=>{
    const i=start+ri;
    const name=law['법령명한글']||'법령명 없음';
    const type=law['법령구분명']||'법률';
    const pdate=(law['공포일자']||'').replace(/(\d{4})(\d{2})(\d{2})/,'$1.$2.$3');
    const no=law['법령일련번호']||'';
    const org=law['소관부처명']||guessOrg(name);
    const lawUrl=`https://www.law.go.kr/법령/${encodeURIComponent(name)}`;
    return `
    <div class="card" id="c${i}">
      <div class="card-head" onclick="toggleCard(${i})">
        <span class="ltype">${type}</span>
        <span class="lname">${name}</span>
        <span class="lorg">${org}</span>
        <span class="arrow">▼</span>
      </div>
      <div class="card-body">
        <div class="mgrid">
          <div class="mbox"><div class="mlbl">소관기관</div><div class="mval">${org}</div></div>
          ${pdate?`<div class="mbox"><div class="mlbl">공포일자</div><div class="mval">${pdate}</div></div>`:''}
          ${no?`<div class="mbox"><div class="mlbl">법령일련번호</div><div class="mval">${no}</div></div>`:''}
          <div class="mbox"><div class="mlbl">법령 유형</div><div class="mval">${type}</div></div>
        </div>
        <div class="actions">
          <button class="abtn primary" onclick="openFullPanel('${name.replace(/'/g,"\\'")}','${lawUrl}','${org.replace(/'/g,"\\'")}','${type}')">📄 조문 전문 보기</button>
          <a class="abtn" href="${lawUrl}" target="_blank">↗ 법제처 바로가기</a>
          <a class="abtn" href="https://www.law.go.kr/lsSc.do?query=${encodeURIComponent(name)}" target="_blank">📋 관련 법령</a>
          ${makeFavBtnHtml(name,org,type,lawUrl)}
          ${makeCollBtnHtml(name,org,lawUrl)}
        </div>
      </div>
    </div>`;
  }).join('');
  renderPagination();
}

function toggleCard(i){document.getElementById('c'+i).classList.toggle('open');}

function renderPagination(){
  const total=Math.ceil(filtered.length/PS);
  if(total<=1){document.getElementById('pagination').innerHTML='';return;}
  let h=`<button class="pgbtn" onclick="goPage(${page-1})" ${page===1?'disabled':''}>이전</button>`;
  for(let i=1;i<=total;i++)h+=`<button class="pgbtn ${i===page?'active':''}" onclick="goPage(${i})">${i}</button>`;
  h+=`<button class="pgbtn" onclick="goPage(${page+1})" ${page===total?'disabled':''}>다음</button>`;
  document.getElementById('pagination').innerHTML=h;
}
function goPage(p){
  const t=Math.ceil(filtered.length/PS);
  if(p<1||p>t)return; page=p; renderPage(); window.scrollTo({top:0,behavior:'smooth'});
}

// ── 조문 검색 모드: 법령 목록 렌더 ──
function renderArticleLawList(laws, artKw=''){
  document.getElementById('results').innerHTML=laws.map((law,i)=>{
    const name=law['법령명한글']||'';
    const type=law['법령구분명']||'법률';
    const org=law['소관부처명']||guessOrg(name);
    const lawUrl=`https://www.law.go.kr/법령/${encodeURIComponent(name)}`;
    const kwAttr=artKw?` data-kw="${escHtml(artKw)}"` : '';
    return `
    <div class="card" id="alc${i}">
      <div class="card-head" onclick="toggleArtCard(${i},'${name.replace(/'/g,"\\'")}','${lawUrl}')">
        <span class="ltype">${type}</span>
        <span class="lname">${name}</span>
        <span class="lorg">${org}</span>
        <span class="arrow">▼</span>
      </div>
      <div class="card-body" id="alb${i}"${kwAttr}>
        <div style="text-align:center;padding:18px;color:var(--ht);font-size:13px;">
          <div class="spinner" style="margin:0 auto 8px;"></div>조문을 불러오는 중...
        </div>
      </div>
    </div>`;
  }).join('');
}

async function toggleArtCard(i, lawName, lawUrl){
  const card=document.getElementById('alc'+i);
  const body=document.getElementById('alb'+i);
  if(card.classList.contains('open')){card.classList.remove('open');return;}
  card.classList.add('open');
  if(body.dataset.loaded==='1')return;
  const kw = body.dataset.kw || '';
  try{
    const r=await fetch(`/api/law/articles?name=${encodeURIComponent(lawName)}`);
    const d=await r.json();
    if(d.error){body.innerHTML=`<div style="padding:14px;color:#991b1b;font-size:13px;">${d.error}</div>`;body.dataset.loaded='1';return;}
    body.innerHTML=renderInlineArticles(d.articles||[], lawName, lawUrl, kw);
    body.dataset.loaded='1';
  }catch{
    body.innerHTML=`<div style="padding:14px;color:#991b1b;font-size:13px;">조문 조회 중 오류가 발생했습니다.</div>`;
    body.dataset.loaded='1';
  }
}

function renderInlineArticles(arts, lawName, lawUrl, kw=''){
  if(!arts.length) return `<div style="padding:14px;font-size:13px;color:var(--ht);">조문 정보를 가져오지 못했습니다. <a href="${lawUrl}" target="_blank" style="color:var(--g);">법제처에서 직접 확인 →</a></div>`;
  const id='ia'+Date.now();
  const kwL = kw.toLowerCase();

  // 키워드 있으면 관련 조문 필터링
  let displayArts = arts;
  let filtered_flag = false;
  if(kw){
    const matched = arts.filter(a=>
      (a['조문내용']||'').toLowerCase().includes(kwL) ||
      (a['조문제목']||'').toLowerCase().includes(kwL)
    );
    if(matched.length){ displayArts=matched; filtered_flag=true; }
  }

  function highlight(txt){
    if(!kw) return escHtml(txt);
    return escHtml(txt).replace(new RegExp(escRegex(escHtml(kw)),'gi'), m=>`<mark style="background:#fff176;border-radius:2px;padding:0 1px;">${m}</mark>`);
  }

  return `
  <div class="art-wrap">
    <div class="art-filterrow">
      <input class="art-finput" placeholder="조문번호(예: 1) 또는 키워드 필터" value="${escHtml(kw)}"
        oninput="filterInlineArts('${id}',${arts.length},this.value)">
      <span class="art-cnt" id="${id}cnt">${filtered_flag?`관련 ${displayArts.length}/${arts.length}개`:`전체 ${arts.length}개 조문`}</span>
      <a class="abtn primary" href="${lawUrl}" target="_blank" style="padding:6px 10px;font-size:12px;white-space:nowrap;">↗ 전문</a>
    </div>
    <div id="${id}list">`+
    displayArts.map((a,i)=>{
      const isHeader = a['type']==='header';
      const no=a['조문번호']?`제${a['조문번호']}조`:'';
      const title=a['조문제목']||'';
      const content=a['조문내용']||'';
      const hno = a['header_no']||'';

      if(isHeader){
        return `<div class="art-item" id="${id}_${i}" style="background:var(--bg);border:1px solid var(--bd);">
          <div style="padding:7px 12px;font-size:12px;font-weight:700;color:var(--mu);">
            ${hno?hno+' ':''}${title}
          </div>
        </div>`;
      }

      const isMatch = kw && (
        content.toLowerCase().includes(kwL) || title.toLowerCase().includes(kwL)
      );
      return `<div class="art-item" id="${id}_${i}">
        <div class="art-head" onclick="toggleArt('${id}_${i}')">
          ${no?`<span class="art-no">${no}</span>`:''}
          <span class="art-title">${highlight(title||no||'조문')}</span>
          <span class="arrow" style="font-size:11px;color:var(--ht);transition:transform .2s;flex-shrink:0;">▼</span>
        </div>
        ${content?`<div class="art-body" style="${isMatch?'display:block;':''}">${highlight(content)}</div>`:''}
      </div>`;
    }).join('')+
    `</div></div>`;
}

function filterInlineArts(id, total, kw){
  kw=kw.toLowerCase(); let shown=0;
  document.querySelectorAll(`#${id}list .art-item`).forEach(el=>{
    const v=!kw||el.innerText.toLowerCase().includes(kw);
    el.style.display=v?'':'none'; if(v)shown++;
  });
  document.getElementById(id+'cnt').textContent=kw?`${shown}/${total}개`:`전체 ${total}개 조문`;
}

function toggleArt(id){
  const body=document.querySelector(`#${id} .art-body`);
  const arrow=document.querySelector(`#${id} .arrow`);
  if(!body)return;
  const open=body.style.display==='block';
  body.style.display=open?'none':'block';
  if(arrow)arrow.style.transform=open?'':'rotate(180deg)';
}

// ── 법령 전문 슬라이드 패널 (구버전 제거 - 아래 재정의 버전 사용) ──
// openFullPanel, renderFpArts는 아래 '즐겨찾기·AI 통합' 섹션에 정의됨

function escHtml(s){
  if(typeof s!=='string') return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function toggleFpArt(i){
  const body=document.getElementById('fpbody'+i);
  const arr=document.getElementById('fparr'+i);
  if(!body)return;
  const open=body.style.display==='block';
  body.style.display=open?'none':'block';
  if(arr)arr.style.transform=open?'':'rotate(180deg)';
}

function fpExpandAll(expand){
  document.querySelectorAll('.fp-art-body').forEach((b,i)=>{
    b.style.display=expand?'block':'none';
    const arr=document.getElementById('fparr'+i);
    if(arr)arr.style.transform=expand?'rotate(180deg)':'';
  });
}

function fpFilterArts(){
  const kw=document.getElementById('fpFilter').value.trim();
  const kwL=kw.toLowerCase();
  let shown=0, total=fpArts.filter(a=>a['type']!=='header').length;

  document.querySelectorAll('.fp-art').forEach((el,i)=>{
    const art=fpArts[i];
    if(!art) return;
    // 구조 헤더는 항상 표시
    if(art['type']==='header'){ el.style.display=''; return; }

    const no=art['조문번호']||'';
    const title=(art['조문제목']||'').toLowerCase();
    const content=(art['조문내용']||'').toLowerCase();
    const noMatch = kw && /^\d+$/.test(kw) && no===kw;
    const txtMatch = !kw || noMatch || title.includes(kwL) || content.includes(kwL);
    el.style.display=txtMatch?'':'none';
    if(!txtMatch) return;
    shown++;

    const bodyEl=document.getElementById('fpbody'+i);
    if(!bodyEl) return;
    if(!kw){ bodyEl.innerHTML=escHtml(art['조문내용']||''); return; }
    const raw=art['조문내용']||'';
    bodyEl.innerHTML=escHtml(raw).replace(
      new RegExp(escRegex(escHtml(kw)),'gi'),
      m=>`<mark>${m}</mark>`
    );
    bodyEl.style.display='block';
    const arr=document.getElementById('fparr'+i);
    if(arr) arr.style.transform='rotate(180deg)';
  });
  document.getElementById('fpCnt').textContent=kw?`${shown}/${total}개`:`${total}개 조문`;
}

function escRegex(s){return s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');}

function closePanel(){
  document.getElementById('fullpanel').classList.remove('open');
  document.getElementById('overlay').classList.remove('show');
  document.body.style.overflow='';
  document.getElementById('fpRight').classList.remove('open');
  document.getElementById('fullpanel').style.width='';
  refArts=[];
}

function renderEmpty(msg){document.getElementById('results').innerHTML=`<div class="empty"><div class="empty-icon">🔍</div><p>${msg}</p></div>`;}

// ── API 상태 ──
async function checkStatus(){
  const badge=document.getElementById('badge');
  try{
    const d=await (await fetch('/api/ping',{signal:AbortSignal.timeout(5000)})).json();
    if(d.law_api){badge.textContent='● 법제처 API 연결됨';badge.style.cssText='background:#E1F5EE;color:#0F6E56';}
    else{badge.textContent='⚠ API 오류';badge.style.cssText='background:#fffbeb;color:#92400e';}
  }catch{badge.textContent='● 서버 오프라인';badge.style.cssText='background:#fef2f2;color:#991b1b';}
}

// ── 즐겨찾기 ──────────────────────────────────────────────────────────────────
let favMode = false;
let currentPanelLaw = {name:'', url:''};

async function loadFavs(){
  try{
    const d=await (await fetch('/api/favorites')).json();
    const cnt=d.favorites?.length||0;
    document.getElementById('favCount').textContent=cnt?`(${cnt})`:'';
    return d.favorites||[];
  }catch{return [];}
}

async function toggleFav(name, org, type, url){
  const r=await fetch(`/api/favorites/toggle?name=${encodeURIComponent(name)}&org=${encodeURIComponent(org)}&type=${encodeURIComponent(type)}&url=${encodeURIComponent(url)}`);
  const d=await r.json();
  await loadFavs();
  return d.added;
}

async function toggleFavFromPanel(){
  const {name, url, org, type} = currentPanelLaw;
  if(!name) return;
  const added = await toggleFav(name, org||'', type||'법률', url||'');
  const btn = document.getElementById('fpFavBtn');
  btn.textContent = added ? '★' : '☆';
  btn.classList.toggle('active', added);
  if(favMode) renderFavList();
}

async function toggleFavTab(){
  favMode = !favMode;
  const btn = document.getElementById('favTabBtn');
  btn.classList.toggle('active', favMode);
  if(favMode){
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    renderFavList();
  } else {
    clearResults();
  }
}

async function renderFavList(){
  const favs = await loadFavs();
  document.getElementById('fbar').style.display='none';
  document.getElementById('pagination').innerHTML='';
  if(!favs.length){
    document.getElementById('results').innerHTML='<div class="fav-empty">⭐ 즐겨찾기한 법령이 없습니다.<br>법령 카드의 ☆ 버튼을 눌러 추가하세요.</div>';
    setStatus('');
    return;
  }
  setStatus(`즐겨찾기 ${favs.length}건`);
  document.getElementById('results').innerHTML=favs.map((f,i)=>`
    <div class="card" id="fc${i}">
      <div class="card-head" onclick="toggleCard2('fc${i}')">
        <span class="ltype">${f.type||'법률'}</span>
        <span class="lname">${f.name}</span>
        <span class="lorg">${f.org||''}</span>
        <button class="fav-btn active" onclick="event.stopPropagation();removeFav('${f.name.replace(/'/g,"\\'")}');renderFavList();" title="즐겨찾기 제거" style="margin-left:auto;">★</button>
        <span class="arrow" style="font-size:11px;color:var(--ht);margin-left:4px;">▼</span>
      </div>
      <div class="card-body">
        <div class="actions">
          <button class="abtn primary" onclick="openFullPanel('${f.name.replace(/'/g,"\\'")}','${f.url}')">📄 조문 전문 보기</button>
          <a class="abtn" href="${f.url}" target="_blank">↗ 법제처</a>
        </div>
      </div>
    </div>`).join('');
}

function toggleCard2(id){document.getElementById(id).classList.toggle('open');}

async function removeFav(name){
  await fetch(`/api/favorites/remove?name=${encodeURIComponent(name)}`);
  await loadFavs();
  await renderFavList();
}

async function updateFavBtn(panelName){
  const favs=await loadFavs();
  const isFav=favs.some(f=>f.name===panelName);
  const btn=document.getElementById('fpFavBtn');
  if(btn){btn.textContent=isFav?'★':'☆'; btn.classList.toggle('active',isFav);}
}

// ── 법령카드에 즐겨찾기 버튼 포함 렌더 ────────────────────────────────────────
// renderPage 에서 카드 액션에 즐겨찾기 버튼 추가
function makeFavBtnHtml(name, org, type, lawUrl){
  return `<button class="abtn" onclick="cardFavToggle(this,'${name.replace(/'/g,"\\'")}','${(org||'').replace(/'/g,"\\'")}','${type}','${lawUrl}')">☆ 즐겨찾기</button>`;
}
async function cardFavToggle(el, name, org, type, url){
  const added = await toggleFav(name, org, type, url);
  el.textContent = added ? '★ 즐겨찾기됨' : '☆ 즐겨찾기';
  el.style.color  = added ? '#92400e' : '';
  el.style.borderColor = added ? '#f59e0b' : '';
  el.style.background  = added ? '#fffbeb' : '';
}

// ── 개정이력 ─────────────────────────────────────────────────────────────────
async function fpShowHistory(){
  const name=document.getElementById('fpTitle').textContent;
  const lawUrl=document.getElementById('fpExtLink').href;
  if(!name||name==='법령 전문') return;
  const body=document.getElementById('fpBody');
  body.innerHTML='<div class="fp-loading"><div class="spinner" style="margin:0 auto 10px;"></div>개정이력 조회 중...</div>';

  const backBtn=`<button class="fp-tbtn" onclick="openFullPanel(decodeURIComponent('${encodeURIComponent(name)}'),decodeURIComponent('${encodeURIComponent(lawUrl)}'))">← 조문으로 돌아가기</button>`;

  try{
    const r=await fetch(`/api/law/history?name=${encodeURIComponent(name)}`);
    const d=await r.json();
    if(d.error||!d.history?.length){
      body.innerHTML=`<div style="padding:20px;">${backBtn}<div class="fp-empty" style="padding:20px 0;">개정이력 정보를 가져오지 못했습니다.</div></div>`;
      return;
    }
    body.innerHTML=`
      <div style="padding:4px 0 12px;">${backBtn}
        <div style="font-size:13px;font-weight:600;margin-top:12px;margin-bottom:8px;">「${escHtml(name)}」 개정이력</div>
      </div>
      <div class="hist-wrap">
        ${d.history.map(h=>`
          <div class="hist-item">
            <span class="hist-date">${escHtml(h.date||'')}</span>
            <span class="hist-desc">${escHtml(h.desc||'')}</span>
          </div>`).join('')}
      </div>`;
  }catch{
    body.innerHTML=`<div style="padding:20px;">${backBtn}<div class="fp-empty" style="padding:20px 0;">개정이력 조회 중 오류가 발생했습니다.</div></div>`;
  }
}

// ── renderFpArts 재정의 (AI해석·복사·메모·용어툴팁 포함) ──────────────────────
function renderFpArts(arts){
  const lawName=document.getElementById('fpTitle').textContent;
  document.getElementById('fpCnt').textContent=`${arts.filter(a=>a.type!=='header').length}개 조문`;
  if(!arts.length){
    document.getElementById('fpBody').innerHTML='<div class="fp-empty">조문 정보를 가져오지 못했습니다.</div>'; return;
  }
  document.getElementById('fpBody').innerHTML=arts.map((a,i)=>{
    const isHeader=a['type']==='header';
    const no=a['조문번호']?`제${a['조문번호']}조`:'';
    const hno=a['header_no']||'';
    const title=a['조문제목']||'';
    const content=a['조문내용']||'';

    if(isHeader){
      return `<div class="fp-art fp-struct-header" id="fpa${i}" style="background:var(--bg);border-bottom:1px solid var(--bd);padding:8px 4px;">
        <div style="font-size:12px;font-weight:700;color:var(--mu);letter-spacing:.5px;">${escHtml(hno?hno+' ':'')}${escHtml(title)}</div>
      </div>`;
    }
    const memoKey=`memo__${i}__${encodeURIComponent(lawName)}`;
    const savedMemo=localStorage.getItem(memoKey)||'';
    const hasMemo=!!savedMemo;
    const bmKey=`bm__${encodeURIComponent(lawName)}__${i}`;
    const isBm=!!localStorage.getItem(bmKey);
    return `<div class="fp-art" id="fpa${i}">
      <div class="fp-art-head" onclick="toggleFpArt(${i})">
        ${no?`<span class="fp-art-no">${escHtml(no)}</span>`:''}
        <span class="fp-art-title">${escHtml(title||no||'조문')}</span>
        ${hasMemo?`<span style="font-size:10px;color:#92400e;background:#fffbeb;padding:1px 6px;border-radius:10px;flex-shrink:0;margin-left:4px;">📝</span>`:''}
        ${isBm?`<span style="font-size:10px;color:#1d4ed8;background:#eff6ff;padding:1px 6px;border-radius:10px;flex-shrink:0;margin-left:2px;">🔖</span>`:''}
        <span style="margin-left:auto;font-size:11px;color:var(--ht);transition:transform .2s;flex-shrink:0;" id="fparr${i}">▼</span>
      </div>
      ${content?`<div class="fp-art-body" id="fpbody${i}">
        <div style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap;">
          <button class="ai-btn" data-idx="${i}" onclick="aiInterpretByIdx(this)">✦ AI 해석</button>
          <button class="copy-btn" data-idx="${i}" onclick="copyArticleByIdx(this)">⎘ 복사</button>
          <button class="memo-btn${hasMemo?' has-memo':''}" onclick="toggleMemo(${i},'${memoKey}')">📝 메모${hasMemo?' ✓':''}</button>
          <button class="bm-btn${isBm?' active':''}" id="bmbtn_${i}" onclick="toggleBookmark(${i},'${bmKey}')">🔖${isBm?' 북마크됨':' 북마크'}</button>
          <button class="fp-tbtn" style="font-size:11px;" onclick="showArtHistory(${i})">⏱ 히스토리</button>
        </div>
        <div class="memo-area" id="memo_area_${i}">
          <textarea class="memo-textarea" id="memo_ta_${i}" placeholder="이 조문에 대한 메모를 입력하세요...">${escHtml(savedMemo)}</textarea>
          <div class="memo-actions">
            <button class="memo-save" onclick="saveMemo(${i},'${memoKey}')">저장</button>
            <button class="memo-del" onclick="deleteMemo(${i},'${memoKey}')">삭제</button>
          </div>
        </div>
        ${applyXref(applyTermTooltip(escHtml(content)), lawName)}
        <div class="ai-box" id="aibox_${i}" style="display:none;"></div>
      </div>`:''}
    </div>`;
  }).join('');
}

// data-idx 기반 AI 해석 (onclick 안에 조문 내용 직접 삽입 없음)
// ── AI 설정 모달 ─────────────────────────────────────────────────────────────
const PROV_NAMES={claude:'Claude',gpt:'GPT',gemini:'Gemini',ollama:'Ollama'};
let currentProv='claude';

function openAiModal(){loadAiSettings();document.getElementById('aiModalBg').classList.add('show');}
function closeAiModal(){document.getElementById('aiModalBg').classList.remove('show');}

function setProv(prov){
  currentProv=prov;
  document.querySelectorAll('.prov-tab').forEach((t,i)=>{
    t.classList.toggle('active',['claude','gpt','gemini','ollama'][i]===prov);
  });
  document.querySelectorAll('.prov-panel').forEach(p=>p.classList.remove('show'));
  document.getElementById('prov-'+prov).classList.add('show');
}

function loadAiSettings(){
  try{
    const s=JSON.parse(localStorage.getItem('aiSettings')||'{}');
    if(s.provider) setProv(s.provider);
    ['claude','gpt','gemini','ollama'].forEach(p=>{
      const kEl=document.getElementById('key-'+p);
      const mEl=document.getElementById('model-'+p);
      if(kEl&&s['key_'+p]) kEl.value=s['key_'+p];
      if(mEl&&s['model_'+p]){
        if(mEl.tagName==='SELECT') mEl.value=s['model_'+p];
        else mEl.value=s['model_'+p];
      }
    });
    updateAiStatusUI(s);
  }catch{}
}

function saveAiSettings(){
  const s={provider:currentProv};
  ['claude','gpt','gemini','ollama'].forEach(p=>{
    const kEl=document.getElementById('key-'+p);
    const mEl=document.getElementById('model-'+p);
    if(kEl) s['key_'+p]=kEl.value.trim();
    if(mEl) s['model_'+p]=mEl.value.trim();
  });
  localStorage.setItem('aiSettings',JSON.stringify(s));
  updateAiStatusUI(s);
  closeAiModal();
}

function getAiSettings(){
  try{return JSON.parse(localStorage.getItem('aiSettings')||'{}')}catch{return {};}
}

function updateAiStatusUI(s){
  const dot=document.getElementById('aiStatusDot');
  const msg=document.getElementById('aiStatusMsg');
  const badge=document.getElementById('aiActiveBadge');
  const prov=s.provider||'';
  const key=s['key_'+prov]||'';
  const model=s['model_'+prov]||'';
  const ok=prov&&(key||prov==='ollama');
  if(dot) dot.className='ai-status-dot'+(ok?' ok':'');
  if(msg) msg.textContent=ok?`${PROV_NAMES[prov]||prov} · ${model||'기본 모델'} 설정됨`:'저장된 설정이 없습니다. API 키를 입력하세요.';
  if(badge){badge.style.display=ok?'':'none'; badge.textContent=PROV_NAMES[prov]||prov;}
}

async function testAiConn(){
  const btn=document.querySelector('.ai-test-btn');
  btn.textContent='테스트 중...'; btn.disabled=true;
  const dot=document.getElementById('aiStatusDot');
  const msg=document.getElementById('aiStatusMsg');
  const kEl=document.getElementById('key-'+currentProv);
  const mEl=document.getElementById('model-'+currentProv);
  const key=kEl?kEl.value.trim():'';
  const model=mEl?mEl.value.trim():'';
  try{
    const r=await fetch('/api/ai/interpret',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({provider:currentProv,api_key:key,model,
        law_name:'테스트',art_no:'제1조',art_title:'목적',
        art_content:'이 법은 테스트 목적으로 작성되었습니다.'})
    });
    const d=await r.json();
    if(d.result){
      dot.className='ai-status-dot ok';
      msg.textContent=`연결 성공 · ${PROV_NAMES[currentProv]||currentProv} 응답 확인됨`;
    } else {
      dot.className='ai-status-dot err';
      msg.textContent=`연결 실패: ${d.error||'알 수 없는 오류'}`;
    }
  }catch(e){dot.className='ai-status-dot err'; msg.textContent=`연결 오류: ${e.message}`;}
  btn.textContent='연결 테스트'; btn.disabled=false;
}

// ── AI 해석 (멀티 프로바이더) ─────────────────────────────────────────────────
async function aiInterpretByIdx(btnEl){
  const idx=parseInt(btnEl.dataset.idx);
  const art=fpArts[idx];
  if(!art) return;
  const lawName=document.getElementById('fpTitle').textContent;
  const no=art['조문번호']?`제${art['조문번호']}조`:'';
  const title=art['조문제목']||'';
  const content=art['조문내용']||'';
  const boxId=`aibox_${idx}`;
  const box=document.getElementById(boxId);

  const s=getAiSettings();
  const prov=s.provider||'claude';
  const apiKey=s['key_'+prov]||'';
  const model=s['model_'+prov]||'';

  if(!apiKey && prov!=='ollama'){
    box.style.display='block'; box.className='ai-box';
    box.textContent='⚠ API 키가 설정되지 않았습니다. 상단 [✦ AI 설정] 버튼을 눌러 설정하세요.';
    return;
  }

  btnEl.disabled=true; btnEl.textContent='해석 중...';
  box.style.display='block'; box.className='ai-box ai-box-loading';
  box.textContent=`${PROV_NAMES[prov]||prov}가 조문을 분석하고 있습니다...`;
  try{
    const r=await fetch('/api/ai/interpret',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({provider:prov,api_key:apiKey,model,
        law_name:lawName,art_no:no,art_title:title,art_content:content})
    });
    const d=await r.json();
    box.className='ai-box';
    box.textContent=d.result||d.error||'해석을 가져오지 못했습니다.';
  }catch{
    box.className='ai-box'; box.textContent='AI 연결 오류가 발생했습니다.';
  }
  btnEl.disabled=false; btnEl.textContent='✦ AI 해석';
}

// data-idx 기반 복사
function copyArticleByIdx(btnEl){
  const idx=parseInt(btnEl.dataset.idx);
  const art=fpArts[idx];
  if(!art) return;
  const no=art['조문번호']?`제${art['조문번호']}조`:'';
  const title=art['조문제목']||'';
  const content=art['조문내용']||'';
  const text=`${no?no+' ':''}${title}\n${content}`.trim();
  navigator.clipboard.writeText(text).then(()=>{
    btnEl.textContent='✓ 복사됨'; btnEl.classList.add('copied');
    setTimeout(()=>{btnEl.textContent='⎘ 복사'; btnEl.classList.remove('copied');},2000);
  }).catch(()=>{btnEl.textContent='복사 실패';});
}

// 버그5 수정: fpPrint header 타입 건너뜀
function fpPrint(){
  const title=document.getElementById('fpTitle').textContent;
  const arts=fpArts.filter(a=>a.type!=='header');
  if(!arts||!arts.length)return;
  const win=window.open('','_blank','width=800,height=900');
  const body=arts.map(a=>{
    const no=a['조문번호']?`제${a['조문번호']}조 `:'';
    const tit=a['조문제목']||'';
    const con=a['조문내용']||'';
    return `<div style="margin-bottom:20px;page-break-inside:avoid;">
      <div style="font-size:14px;font-weight:700;margin-bottom:6px;color:#1D9E75;">${escHtml(no+tit)}</div>
      <div style="font-size:13px;line-height:1.9;white-space:pre-wrap;">${escHtml(con)}</div>
    </div>`;
  }).join('');
  win.document.write(`<!DOCTYPE html><html><head><meta charset="UTF-8">
    <title>${escHtml(title)}</title>
    <style>body{font-family:'Malgun Gothic',sans-serif;padding:30px 40px;max-width:800px;margin:0 auto;}</style>
    </head><body>
    <h2 style="font-size:18px;margin-bottom:24px;padding-bottom:12px;border-bottom:2px solid #1D9E75;">「${escHtml(title)}」</h2>
    ${body}
    </body></html>`);
  win.document.close();
  win.focus();
  setTimeout(()=>win.print(), 500);
}

// ── openFullPanel 재정의 (즐겨찾기 상태 반영) ─────────────────────────────
async function openFullPanel(lawName, lawUrl, org, type){
  if(lawName && lawName!=='법령 전문') recordStat('view', lawName);
  currentPanelLaw={name:lawName, url:lawUrl, org:org||'', type:type||'법률'};
  document.getElementById('fpTitle').textContent=lawName;
  document.getElementById('fpExtLink').href=lawUrl;
  document.getElementById('fpFilter').value='';
  document.getElementById('fpCnt').textContent='';
  document.getElementById('fullpanel').classList.add('open');
  document.getElementById('overlay').classList.add('show');
  document.body.style.overflow='hidden';
  document.getElementById('fpBody').innerHTML='<div class="fp-loading"><div class="spinner" style="margin:0 auto 10px;"></div>조문을 불러오는 중...</div>';
  fpArts=[];
  updateFavBtn(lawName);
  try{
    const r=await fetch(`/api/law/articles?name=${encodeURIComponent(lawName)}`);
    const d=await r.json();
    if(d.error){
      document.getElementById('fpBody').innerHTML=`<div class="fp-empty">${d.error}<br><a href="${lawUrl}" target="_blank" style="color:var(--g);">법제처에서 직접 확인 →</a></div>`;
      return;
    }
    fpArts=d.articles||[];
    renderFpArts(fpArts);
    const amendBtn=document.getElementById('fpAmendBtn');
    if(amendBtn){ amendBtn.textContent='🔴 최근 개정'; amendBtn.style.color=''; amendBtn.style.borderColor=''; amendBtn.style.background=''; }
    setTimeout(()=>checkAmendBadgeAsync(lawName), 800);
  }catch{
    document.getElementById('fpBody').innerHTML='<div class="fp-empty">조문 조회 중 오류가 발생했습니다.</div>';
  }
}

// ── ① 조문 메모 (localStorage) ───────────────────────────────────────────────
function toggleMemo(idx, key){
  const area=document.getElementById('memo_area_'+idx);
  if(!area) return;
  const show=area.style.display==='block';
  area.style.display=show?'none':'block';
  if(!show) document.getElementById('memo_ta_'+idx)?.focus();
}
function saveMemo(idx, key){
  const ta=document.getElementById('memo_ta_'+idx);
  if(!ta) return;
  const val=ta.value.trim();
  if(val) localStorage.setItem(key, val);
  else localStorage.removeItem(key);
  // 메모 버튼 상태 갱신
  const btn=document.querySelector(`[onclick*="toggleMemo(${idx},"]`);
  if(btn){
    btn.textContent=val?'📝 메모 ✓':'📝 메모';
    btn.classList.toggle('has-memo', !!val);
  }
  // 머리 배지 갱신
  const head=document.querySelector(`#fpa${idx} .fp-art-head`);
  if(head){
    const existing=head.querySelector('.memo-badge');
    if(existing) existing.remove();
    if(val){
      const badge=document.createElement('span');
      badge.className='memo-badge';
      badge.style.cssText='font-size:10px;color:#92400e;background:#fffbeb;padding:1px 6px;border-radius:10px;flex-shrink:0;margin-left:4px;';
      badge.textContent='📝';
      head.querySelector('.fp-art-title').after(badge);
    }
  }
  document.getElementById('memo_area_'+idx).style.display='none';
}
function deleteMemo(idx, key){
  localStorage.removeItem(key);
  const ta=document.getElementById('memo_ta_'+idx);
  if(ta) ta.value='';
  saveMemo(idx, key);
}

// ── ② 법령 비교 ───────────────────────────────────────────────────────────────
function openCmpPanel(){document.getElementById('cmpPanel').classList.add('show');}
function closeCmp(){document.getElementById('cmpPanel').classList.remove('show');}

function openCmpFromPanel(){
  const name=document.getElementById('fpTitle').textContent;
  closePanel();
  openCmpPanel();
  if(name && name!=='법령 전문'){
    document.getElementById('cmpQ0').value=name;
    loadCmpLaw(0);
  }
}

async function loadCmpLaw(col){
  const q=document.getElementById('cmpQ'+col).value.trim();
  if(!q) return;
  const titleEl=document.getElementById('cmpTitle'+col);
  const bodyEl=document.getElementById('cmpBody'+col);
  titleEl.textContent='불러오는 중...';
  bodyEl.innerHTML='<div style="padding:20px;text-align:center;"><div class="spinner" style="margin:0 auto;"></div></div>';
  try{
    const r=await fetch(`/api/law/articles?name=${encodeURIComponent(q)}`);
    const d=await r.json();
    if(d.error||!d.articles?.length){
      titleEl.textContent=q;
      bodyEl.innerHTML=`<div style="padding:20px;text-align:center;color:var(--ht);font-size:13px;">${d.error||'조문 정보를 가져오지 못했습니다.'}</div>`;
      return;
    }
    titleEl.textContent=d.law_name||q;
    bodyEl.innerHTML=d.articles.map(a=>{
      if(a.type==='header'){
        return `<div class="cmp-struct">${escHtml(a['header_no']?a['header_no']+' ':'')}${escHtml(a['조문제목']||'')}</div>`;
      }
      const no=a['조문번호']?`제${a['조문번호']}조`:'';
      const title=a['조문제목']||'';
      const content=a['조문내용']||'';
      return `<div class="cmp-art">
        ${no?`<span class="cmp-art-no">${escHtml(no)}</span>`:''}
        ${title?`<div class="cmp-art-title">${escHtml(title)}</div>`:''}
        ${content?`<div class="cmp-art-body">${escHtml(content)}</div>`:''}
      </div>`;
    }).join('');
  }catch{
    titleEl.textContent=q;
    bodyEl.innerHTML='<div style="padding:20px;text-align:center;color:#991b1b;font-size:13px;">조회 중 오류가 발생했습니다.</div>';
  }
}

// 비교 패널 두 열 스크롤 동기화
document.addEventListener('DOMContentLoaded',()=>{
  const cols=['cmpBody0','cmpBody1'];
  let syncing=false;
  cols.forEach((id,i)=>{
    const el=document.getElementById(id);
    if(!el) return;
    el.addEventListener('scroll',()=>{
      if(syncing) return;
      syncing=true;
      const other=document.getElementById(cols[1-i]);
      if(other) other.scrollTop=el.scrollTop;
      setTimeout(()=>syncing=false,50);
    });
  });
});

// ── ③ 법률 용어 사전 ─────────────────────────────────────────────────────────
const TERMS = {
  '전용실시권':'특허권자가 타인에게 독점적으로 특허발명을 실시할 수 있는 권리를 설정해주는 계약. 설정 범위 내에서 실시권자만이 독점 실시할 수 있습니다.',
  '통상실시권':'특허권자의 허락 하에 특허발명을 비독점적으로 실시할 수 있는 권리. 여러 명에게 동시 부여 가능합니다.',
  '품종보호권':'신품종 육성자에게 부여되는 독점적 권리로, 보호품종의 종자를 업으로서 생산·증식·수출입할 수 있는 권리입니다.',
  '출원인':'특허·상표·디자인 등의 권리 취득을 신청한 개인 또는 법인을 의미합니다.',
  '선행기술':'특허 출원 전에 이미 공개된 기술. 특허성 판단의 기준이 됩니다.',
  '농업인':'1,000㎡ 이상의 농지를 경작하거나 연간 90일 이상 농업에 종사하는 자 등 농업·농촌기본법에서 정한 요건을 갖춘 사람입니다.',
  '농지':'전·답·과수원 그 밖에 법적 지목에 불구하고 실제로 농작물 경작에 이용되는 토지를 말합니다.',
  '기술이전':'기술을 보유한 자로부터 그 기술을 필요로 하는 자에게 양도·사용허락 등의 방법으로 이전하는 것입니다.',
  '사업화':'기술을 이용하여 제품을 개발·생산·판매하거나 서비스를 제공하는 활동입니다.',
  '손해배상':'권리 침해로 인한 재산상·비재산상 손해를 금전으로 배상하는 것을 말합니다.',
  '침해':'타인의 권리 범위를 허락 없이 실시하거나 사용하는 행위입니다.',
  '등록취소':'등록된 권리가 법적 요건 미충족 등의 사유로 소급하여 무효화되는 처분입니다.',
  '공포일':'법령이 제정·개정되어 관보에 게재된 날짜를 말합니다.',
  '시행일':'법령이 실제로 효력을 발생하여 적용되기 시작하는 날입니다.',
  '과태료':'행정법규 위반에 대해 부과되는 금전적 제재로, 형사 처벌인 벌금과는 구별됩니다.',
  '농약잔류허용기준':'식품에 잔류할 수 있는 농약의 최대 허용 농도로, 식품안전 기준입니다.',
};

function applyTermTooltip(html){
  let result=html;
  Object.entries(TERMS).forEach(([term, desc])=>{
    const escaped=term.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
    const safe_desc=desc.replace(/'/g,'&#39;');
    result=result.replace(
      new RegExp(`(?<!<[^>]*)(${escaped})(?![^<]*>)`,'g'),
      `<span class="law-term" onmouseenter="showTooltip(event,'${term}','${safe_desc}')" onmouseleave="hideTooltip()">$1</span>`
    );
  });
  return result;
}

function showTooltip(e, word, desc){
  const tt=document.getElementById('termTooltip');
  document.getElementById('ttWord').textContent=word;
  document.getElementById('ttDesc').textContent=desc;
  tt.style.display='block';
  const x=Math.min(e.clientX+12, window.innerWidth-300);
  const y=Math.min(e.clientY+16, window.innerHeight-120);
  tt.style.left=x+'px'; tt.style.top=y+'px';
}
function hideTooltip(){document.getElementById('termTooltip').style.display='none';}

// ── ④ 검색 결과 CSV 내보내기 ─────────────────────────────────────────────────
function exportCSV(){
  if(!filtered||!filtered.length){alert('내보낼 검색 결과가 없습니다.'); return;}
  const header=['법령명','법령구분','소관기관','공포일자','법령일련번호','법제처 링크'];
  const rows=filtered.map(law=>{
    const name=law['법령명한글']||'';
    const type=law['법령구분명']||'';
    const org=law['소관부처명']||guessOrg(name);
    const date=(law['공포일자']||'').replace(/(\d{4})(\d{2})(\d{2})/,'$1.$2.$3');
    const no=law['법령일련번호']||'';
    const url=`https://www.law.go.kr/법령/${encodeURIComponent(name)}`;
    return [name,type,org,date,no,url].map(v=>`"${String(v).replace(/"/g,'""')}"`).join(',');
  });
  const bom='\uFEFF'; // 한글 깨짐 방지 BOM
  const csv=bom+header.join(',')+'\n'+rows.join('\n');
  const blob=new Blob([csv],{type:'text/csv;charset=utf-8;'});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url; a.download=`법령검색결과_${new Date().toISOString().slice(0,10)}.csv`;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a); URL.revokeObjectURL(url);
}

// ── ⑤ 다크모드 ───────────────────────────────────────────────────────────────
function toggleDark(){
  const dark=!document.body.classList.contains('dark');
  document.body.classList.toggle('dark', dark);
  document.getElementById('darkBtn').textContent=dark?'☀️':'🌙';
  localStorage.setItem('darkMode', dark?'1':'0');
}
function initDark(){
  if(localStorage.getItem('darkMode')==='1'){
    document.body.classList.add('dark');
    const btn=document.getElementById('darkBtn');
    if(btn) btn.textContent='☀️';
  }
}

// ── ⑥ 조문 북마크 ────────────────────────────────────────────────────────────
function toggleBookmark(idx, key){
  const art=fpArts[idx];
  if(!art) return;
  const lawName=document.getElementById('fpTitle').textContent;
  const existing=localStorage.getItem(key);
  const btn=document.getElementById('bmbtn_'+idx);
  if(existing){
    localStorage.removeItem(key);
    if(btn){btn.textContent='🔖 북마크'; btn.classList.remove('active');}
  } else {
    const no=art['조문번호']?`제${art['조문번호']}조`:'';
    const title=art['조문제목']||'';
    const data=JSON.stringify({law:lawName, no, title, idx,
      url:`https://www.law.go.kr/법령/${encodeURIComponent(lawName)}`,
      savedAt:new Date().toISOString().slice(0,10)});
    localStorage.setItem(key, data);
    if(btn){btn.textContent='🔖 북마크됨'; btn.classList.add('active');}
  }
  // 통계 갱신
  recordStat('bookmark', lawName);
}

function getAllBookmarks(){
  const result=[];
  for(let i=0;i<localStorage.length;i++){
    const k=localStorage.key(i);
    if(!k.startsWith('bm__')) continue;
    try{
      const d=JSON.parse(localStorage.getItem(k));
      if(d) result.push({...d, key:k});
    }catch{}
  }
  return result.sort((a,b)=>b.savedAt.localeCompare(a.savedAt));
}

// ── ⑦ 검색 통계 ──────────────────────────────────────────────────────────────
function recordStat(type, value){
  try{
    const raw=localStorage.getItem('lawStats')||'{}';
    const stats=JSON.parse(raw);
    if(!stats[type]) stats[type]={};
    stats[type][value]=(stats[type][value]||0)+1;
    // 전체 검색 시간 기록 (최근 20개)
    if(type==='search'){
      if(!stats.history) stats.history=[];
      stats.history.unshift({q:value, t:new Date().toISOString()});
      stats.history=stats.history.slice(0,20);
    }
    localStorage.setItem('lawStats', JSON.stringify(stats));
  }catch{}
}

function openStatPanel(){
  renderStatPanel();
  document.getElementById('statPanel').classList.add('open');
}
function closeStatPanel(){document.getElementById('statPanel').classList.remove('open');}

function renderStatPanel(){
  const raw=localStorage.getItem('lawStats')||'{}';
  let stats={};
  try{stats=JSON.parse(raw);}catch{}

  const searches=stats.search||{};
  const views=stats.view||{};
  const bms=getAllBookmarks();

  // 검색 Top 순위
  function renderRank(obj, emptyMsg){
    const sorted=Object.entries(obj).sort((a,b)=>b[1]-a[1]).slice(0,8);
    if(!sorted.length) return `<div class="stat-empty">${emptyMsg}</div>`;
    const max=sorted[0][1]||1;
    return sorted.map(([label,cnt],i)=>`
      <div class="stat-row">
        <span class="stat-rank">${i+1}</span>
        <span class="stat-label" title="${escHtml(label)}">${escHtml(label)}</span>
        <div class="stat-bar-wrap"><div class="stat-bar" style="width:${Math.round(cnt/max*100)}%"></div></div>
        <span class="stat-cnt">${cnt}회</span>
      </div>`).join('');
  }

  // 북마크 목록
  const bmHtml=bms.length
    ? bms.map(b=>`
        <div class="stat-row" style="cursor:pointer;" onclick="openFullPanel(decodeURIComponent('${encodeURIComponent(b.law||'')}'),decodeURIComponent('${encodeURIComponent(b.url||'')}'))">
          <span class="stat-rank" style="color:#3b82f6;">🔖</span>
          <span class="stat-label">
            <span style="font-size:11px;color:var(--ht);">${escHtml(b.law||'')}</span><br>
            <span style="font-weight:600;">${escHtml(b.no||'')} ${escHtml(b.title||'')}</span>
          </span>
          <span class="stat-cnt" style="width:auto;color:var(--ht);">${b.savedAt||''}</span>
        </div>`).join('')
    : `<div class="stat-empty">북마크한 조문이 없습니다</div>`;

  // 총 검색 횟수
  const totalSearch=Object.values(searches).reduce((s,v)=>s+v,0);
  const totalView=Object.values(views).reduce((s,v)=>s+v,0);

  document.getElementById('statBody').innerHTML=`
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:18px;">
      <div class="mbox"><div class="mlbl">총 검색</div><div class="mval">${totalSearch}회</div></div>
      <div class="mbox"><div class="mlbl">조문 조회</div><div class="mval">${totalView}회</div></div>
      <div class="mbox"><div class="mlbl">북마크</div><div class="mval">${bms.length}개</div></div>
      <div class="mbox"><div class="mlbl">즐겨찾기</div><div class="mval" id="statFavCnt">-</div></div>
    </div>
    <div class="stat-section">
      <div class="stat-section-title">🔍 자주 검색한 키워드</div>
      ${renderRank(searches,'검색 기록이 없습니다')}
    </div>
    <div class="stat-section">
      <div class="stat-section-title">📄 자주 조회한 법령</div>
      ${renderRank(views,'조회 기록이 없습니다')}
    </div>
    <div class="stat-section">
      <div class="stat-section-title">🔖 북마크한 조문</div>
      ${bmHtml}
    </div>
    <div style="text-align:right;margin-top:12px;">
      <button class="memo-del" onclick="if(confirm('모든 통계를 초기화할까요?')){localStorage.removeItem('lawStats');renderStatPanel();}">통계 초기화</button>
    </div>`;

  // 즐겨찾기 수 비동기 업데이트
  loadFavs().then(favs=>{
    const el=document.getElementById('statFavCnt');
    if(el) el.textContent=`${favs.length}개`;
  });
}

// ── ⑧ 조문 내 참조 링크 (조번호 이동 + 법령명 준용 패널) ─────────────────────
let refArts = [];   // 우측 준용 패널 조문 데이터

function applyXref(html, lawName){
  // 1) 「법령명」 또는 ｢법령명｣ 패턴 → 준용 법령 링크
  html = html.replace(
    /[「｢]([^」｣\n]{2,40}(?:법|령|규칙|조례|훈령))[」｣]/g,
    (match, name) => {
      // 현재 법령 자기 참조는 링크 제외
      if(name === lawName) return match;
      return `<span class="law-ref" onclick="openRefLaw(decodeURIComponent('${encodeURIComponent(name)}'))" title="「${name}」 준용 조문 보기">「${escHtml(name)}」</span>`;
    }
  );
  // 2) 제N조 패턴 → 조문 이동 링크 (법령명 링크 내부는 제외)
  html = html.replace(
    /(?<![">])제(\d+)조(?:의\d+)?/g,
    (match, num) => `<span class="art-xref" onclick="jumpToArt(${parseInt(num)})" title="${match}로 이동">${match}</span>`
  );
  return html;
}

async function openRefLaw(refName){
  const right = document.getElementById('fpRight');
  const body  = document.getElementById('fpRightBody');
  const title = document.getElementById('fpRightTitle');
  const extLink = document.getElementById('fpRightExtLink');

  // 이미 같은 법령이면 닫기 토글
  if(right.classList.contains('open') && title.textContent === refName){
    closeRefPanel(); return;
  }

  // 이전 활성 링크 스타일 초기화
  document.querySelectorAll('.law-ref.active').forEach(el=>el.classList.remove('active'));
  // 클릭된 링크 활성화
  event.target.classList.add('active');

  right.classList.add('open');
  title.textContent = refName;
  extLink.href = `https://www.law.go.kr/법령/${encodeURIComponent(refName)}`;
  document.getElementById('fpRight').style.minWidth = '';
  body.innerHTML = `<div class="fp-loading"><div class="spinner" style="margin:0 auto 10px;"></div>「${escHtml(refName)}」 불러오는 중...</div>`;
  document.getElementById('fpRightFilter').value = '';
  refArts = [];

  // 패널 너비 조정: 전체 패널을 더 넓게
  document.getElementById('fullpanel').style.width = 'min(1200px, 100vw)';

  try{
    const r = await fetch(`/api/law/articles?name=${encodeURIComponent(refName)}`);
    const d = await r.json();
    if(d.error || !d.articles?.length){
      body.innerHTML = `<div class="fp-empty">${d.error||'조문 정보를 가져오지 못했습니다.'}<br>
        <a href="https://www.law.go.kr/법령/${encodeURIComponent(refName)}" target="_blank" style="color:var(--g);">법제처에서 직접 확인 →</a></div>`;
      return;
    }
    refArts = d.articles;
    renderRefArts(refArts, '');
  }catch(e){
    body.innerHTML = `<div class="fp-empty">조회 중 오류: ${escHtml(e.message)}</div>`;
  }
}

function renderRefArts(arts, kw){
  const body = document.getElementById('fpRightBody');
  const kwL  = kw.toLowerCase();
  const lawName = document.getElementById('fpRightTitle').textContent;

  const filtered = kw
    ? arts.filter(a => a.type!=='header' && (
        (a['조문내용']||'').toLowerCase().includes(kwL) ||
        (a['조문제목']||'').toLowerCase().includes(kwL) ||
        (a['조문번호']===kw.replace(/\D/g,''))
      ))
    : arts;

  if(!filtered.length){
    body.innerHTML = `<div class="fp-empty">검색 결과가 없습니다.</div>`;
    return;
  }

  const cnt = filtered.filter(a=>a.type!=='header').length;
  const banner = kw ? `<div class="ref-highlight-banner">🔍 "${escHtml(kw)}" 포함 조문 ${cnt}개</div>` : '';

  body.innerHTML = banner + filtered.map((a,i)=>{
    if(a.type === 'header'){
      return `<div class="ref-divider">${escHtml(a['header_no']?a['header_no']+' ':'')}${escHtml(a['조문제목']||'')}</div>`;
    }
    const no      = a['조문번호'] ? `제${a['조문번호']}조` : '';
    const title   = a['조문제목'] || '';
    const content = a['조문내용'] || '';
    const hl = kw
      ? escHtml(content).replace(new RegExp(escRegex(escHtml(kw)),'gi'), m=>`<mark style="background:#fef08a;">${m}</mark>`)
      : escHtml(content);
    const autoOpen = kw && (content.toLowerCase().includes(kwL) || title.toLowerCase().includes(kwL));

    return `<div class="ref-art" id="refa${i}">
      <div class="ref-art-head" onclick="toggleRefArt(${i})">
        ${no?`<span class="ref-art-no">${escHtml(no)}</span>`:''}
        <span class="ref-art-title">${escHtml(title||no||'조문')}</span>
        <span style="margin-left:auto;font-size:10px;color:var(--ht);transition:transform .2s;" id="refarr${i}">▼</span>
      </div>
      ${content?`<div class="ref-art-body" id="refbody${i}" style="${autoOpen?'display:block;':''}">${hl}</div>`:''}
    </div>`;
  }).join('');

  // 자동 오픈된 항목 화살표 초기화
  if(kw) filtered.forEach((_,i)=>{
    const arr = document.getElementById('refarr'+i);
    const bdy = document.getElementById('refbody'+i);
    if(arr && bdy && bdy.style.display==='block') arr.style.transform='rotate(180deg)';
  });
}

function toggleRefArt(i){
  const body = document.getElementById('refbody'+i);
  const arr  = document.getElementById('refarr'+i);
  if(!body) return;
  const open = body.style.display==='block';
  body.style.display = open?'none':'block';
  if(arr) arr.style.transform = open?'':'rotate(180deg)';
}

function fpRightFilter(){
  const kw = document.getElementById('fpRightFilter').value.trim();
  renderRefArts(refArts, kw);
}

function closeRefPanel(){
  document.getElementById('fpRight').classList.remove('open');
  document.getElementById('fullpanel').style.width = '';
  document.querySelectorAll('.law-ref.active').forEach(el=>el.classList.remove('active'));
  refArts = [];
}

function jumpToArt(artNo){
  // fpArts에서 해당 번호 조문 찾기
  const idx=fpArts.findIndex(a=>a.type!=='header' && parseInt(a['조문번호'])===artNo);
  if(idx===-1){
    // 없으면 법제처 해당 조문 페이지 오픈
    const lawName=document.getElementById('fpTitle').textContent;
    window.open(`https://www.law.go.kr/법령/${encodeURIComponent(lawName)}#${artNo}00`, '_blank');
    return;
  }
  // 해당 조문으로 스크롤 + 자동 펼치기
  const el=document.getElementById('fpa'+idx);
  if(!el) return;
  el.scrollIntoView({behavior:'smooth', block:'center'});
  const body=document.getElementById('fpbody'+idx);
  const arr=document.getElementById('fparr'+idx);
  if(body && body.style.display!=='block'){
    body.style.display='block';
    if(arr) arr.style.transform='rotate(180deg)';
  }
  // 잠깐 하이라이트
  el.style.transition='background .2s';
  el.style.background='#E1F5EE';
  setTimeout(()=>el.style.background='',1200);
}

// ── 최근 개정 조문 표시 ───────────────────────────────────────────────────────
async function fpShowAmendments(){
  const name=document.getElementById('fpTitle').textContent;
  const lawUrl=document.getElementById('fpExtLink').href;
  if(!name||name==='법령 전문') return;

  const btn=document.getElementById('fpAmendBtn');
  btn.textContent='조회 중...'; btn.disabled=true;

  const body=document.getElementById('fpBody');
  const backBtn=`<button class="fp-tbtn" style="margin-bottom:12px;" onclick="openFullPanel(decodeURIComponent('${encodeURIComponent(name)}'),decodeURIComponent('${encodeURIComponent(lawUrl)}'))">← 조문 전문으로 돌아가기</button>`;

  body.innerHTML=`<div class="fp-loading"><div class="spinner" style="margin:0 auto 10px;"></div>이전 버전과 비교 중... (수초 소요)</div>`;

  try{
    const r=await fetch(`/api/law/amendments?name=${encodeURIComponent(name)}`);
    const d=await r.json();

    if(d.error){
      body.innerHTML=`<div style="padding:16px;">${backBtn}<div class="fp-empty">${d.error}</div></div>`;
      return;
    }

    const arts=d.amended_articles||[];
    const dateLabel=d.law_date?`최근 공포일 ${d.law_date}`:'';
    const methodLabel=d.method==='diff'?'이전 버전과 비교'
                    :d.method==='tag'?'조문 개정일 태그'
                    :'개정 정보 없음';

    if(!arts.length){
      body.innerHTML=`<div style="padding:16px;">${backBtn}
        <div class="amend-notice"><div class="dot" style="background:#6ee7b7;"></div>
          <span>${dateLabel ? dateLabel+' · ' : ''}감지된 최근 개정 조문이 없습니다.</span>
        </div>
        <div style="font-size:12px;color:var(--ht);padding:8px 0;">※ 법제처 XML에 조문별 개정 정보가 없거나 이전 버전과 차이가 없는 경우입니다.<br>상세 이력은 📅 개정이력 버튼을 이용하세요.</div>
      </div>`;
      return;
    }

    // 신설/개정/삭제 그룹화
    const grouped={신설:[],개정:[],삭제:[]};
    arts.forEach(a=>{ const t=a.amend_type||'개정'; (grouped[t]||grouped['개정']).push(a); });

    function renderGroup(label, items){
      if(!items.length) return '';
      return `<div style="margin-bottom:16px;">
        <div style="font-size:12px;font-weight:700;color:var(--mu);margin-bottom:8px;display:flex;align-items:center;gap:6px;">
          <span class="amend-badge ${label}">${label}</span> ${items.length}개 조문
        </div>
        ${items.map((a,i)=>{
          const no=a['조문번호']?`제${a['조문번호']}조`:'';
          const title=a['조문제목']||'';
          const content=a['조문내용']||'';
          const adate=a['amended_date']||d.law_date||'';
          return `<div class="amend-item">
            <div class="amend-item-hd" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='block'?'none':'block'">
              ${no?`<span class="fp-art-no">${escHtml(no)}</span>`:''}
              <span style="font-size:13px;font-weight:600;">${escHtml(title||no||'조문')}</span>
              ${adate?`<span style="font-size:11px;color:var(--ht);margin-left:4px;">${adate}</span>`:''}
              <button class="amend-goto" onclick="event.stopPropagation();closeAmendView('${encodeURIComponent(name)}','${encodeURIComponent(lawUrl)}',${parseInt(a['조문번호'])||0})">조문 보기 →</button>
            </div>
            ${content?`<div class="amend-item-body">${escHtml(content)}</div>`:''}
          </div>`;
        }).join('')}
      </div>`;
    }

    body.innerHTML=`<div style="padding:4px 0 12px;">${backBtn}
      <div class="amend-notice">
        <div class="dot"></div>
        <span><b>${escHtml(name)}</b> · ${dateLabel} · ${methodLabel}</span>
      </div>
      ${renderGroup('신설', grouped['신설'])}
      ${renderGroup('개정', grouped['개정'])}
      ${renderGroup('삭제', grouped['삭제'])}
    </div>`;

    // 패널 제목 배지 업데이트
    updateAmendBadge(arts.length);

  }catch(e){
    body.innerHTML=`<div style="padding:16px;">${backBtn}<div class="fp-empty">조회 중 오류: ${e.message}</div></div>`;
  }
  btn.textContent='🔴 최근 개정'; btn.disabled=false;
}

function closeAmendView(nameEnc, urlEnc, artNo){
  openFullPanel(decodeURIComponent(nameEnc), decodeURIComponent(urlEnc)).then(()=>{
    if(artNo>0) setTimeout(()=>jumpToArt(artNo), 600);
  });
}

function updateAmendBadge(cnt){
  const btn=document.getElementById('fpAmendBtn');
  if(!btn) return;
  if(cnt>0){
    btn.textContent=`🔴 최근 개정 (${cnt})`;
    btn.style.color='#dc2626'; btn.style.borderColor='#fca5a5'; btn.style.background='#fff1f2';
  } else {
    btn.textContent='최근 개정 없음';
    btn.style.color=''; btn.style.borderColor=''; btn.style.background='';
  }
}

// openFullPanel에서 개정 배지 자동 체크 (조문 로드 후 백그라운드)
async function checkAmendBadgeAsync(lawName){
  try{
    const r=await fetch(`/api/law/amendments?name=${encodeURIComponent(lawName)}`);
    const d=await r.json();
    if(d.success) updateAmendBadge((d.amended_articles||[]).length);
  }catch{}
}


// 초기화
initDark();
checkStatus();
loadRecent();
loadFavs();
validateKeywords();
try{ updateAiStatusUI(getAiSettings()); }catch{}

// ══════════════════════════════════════════════════════════════════════════════
// ① 조문 변경 diff 뷰
// ══════════════════════════════════════════════════════════════════════════════
function openDiff(artNo, artTitle, oldText, newText, oldLabel, newLabel){
  document.getElementById('diffTitle').textContent=`조문 변경 비교: ${artNo} ${artTitle}`;
  document.getElementById('diffOldLabel').textContent=oldLabel||'이전 버전';
  document.getElementById('diffNewLabel').textContent=newLabel||'현재 버전';
  document.getElementById('diffOld').innerHTML=renderDiff(oldText, newText, 'old');
  document.getElementById('diffNew').innerHTML=renderDiff(oldText, newText, 'new');
  document.getElementById('diffPanel').classList.add('show');
}
function closeDiff(){document.getElementById('diffPanel').classList.remove('show');}

function renderDiff(oldT, newT, side){
  // 단어 단위 diff
  const oldW=oldT.split(/(\s+)/), newW=newT.split(/(\s+)/);
  const lcs=computeLCS(oldW, newW);
  const result=[], ol=oldW.length, nl=newW.length;
  let oi=0, ni=0, li=0;
  while(oi<ol||ni<nl){
    if(li<lcs.length && oi===lcs[li][0] && ni===lcs[li][1]){
      result.push({t:'same', v:oldW[oi]}); oi++; ni++; li++;
    } else if(ni<nl && (li>=lcs.length||lcs[li][1]!==ni) && (side==='new')){
      result.push({t:'add', v:newW[ni]}); ni++;
    } else if(oi<ol && (li>=lcs.length||lcs[li][0]!==oi) && (side==='old')){
      result.push({t:'del', v:oldW[oi]}); oi++;
    } else {
      if(oi<ol) oi++;
      if(ni<nl) ni++;
    }
  }
  return result.map(r=>
    r.t==='add' ? `<span class="diff-add">${escHtml(r.v)}</span>` :
    r.t==='del' ? `<span class="diff-del">${escHtml(r.v)}</span>` :
    escHtml(r.v)
  ).join('');
}

function computeLCS(a, b){
  // DP LCS (단어 배열) - 최대 200 단어로 제한
  const A=a.slice(0,200), B=b.slice(0,200);
  const m=A.length, n=B.length;
  const dp=Array.from({length:m+1},()=>new Uint16Array(n+1));
  for(let i=1;i<=m;i++) for(let j=1;j<=n;j++)
    dp[i][j]=A[i-1]===B[j-1]?dp[i-1][j-1]+1:Math.max(dp[i-1][j],dp[i][j-1]);
  const lcs=[];
  let i=m,j=n;
  while(i>0&&j>0){
    if(A[i-1]===B[j-1]){lcs.unshift([i-1,j-1]);i--;j--;}
    else if(dp[i-1][j]>dp[i][j-1]) i--;
    else j--;
  }
  return lcs;
}

// 개정 패널에서 diff 버튼 연결 (fpShowAmendments 보완)
async function showDiffForAmendment(artNo, artTitle, lawName, prevLawDate){
  try{
    // 현재 조문 내용 가져오기
    const curr=fpArts.find(a=>a['조문번호']===String(artNo)&&a.type!=='header');
    if(!curr){alert('현재 조문을 찾을 수 없습니다.');return;}
    // 이전 버전 조문 가져오기
    const r=await fetch(`/api/law/prev_article?name=${encodeURIComponent(lawName)}&art_no=${artNo}`);
    const d=await r.json();
    const oldContent=d.content||'(이전 버전 정보 없음)';
    openDiff(
      artNo?`제${artNo}조`:'', artTitle,
      oldContent, curr['조문내용']||'',
      `이전 버전`, `현재 (${prevLawDate||'최신'})`
    );
  }catch(e){
    alert(`diff 조회 오류: ${e.message}`);
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// ② 업무별 법령 컬렉션
// ══════════════════════════════════════════════════════════════════════════════
function getCollections(){
  try{return JSON.parse(localStorage.getItem('lawCollections')||'[]');}catch{return[];}
}
function saveCollections(c){localStorage.setItem('lawCollections',JSON.stringify(c));}

function openCollModal(){renderCollModal();document.getElementById('collModalBg').classList.add('show');}
function closeCollModal(){document.getElementById('collModalBg').classList.remove('show');}

function renderCollModal(){
  const colls=getCollections();
  const body=document.getElementById('collBody');
  if(!colls.length){
    body.innerHTML='<div style="text-align:center;padding:20px;color:var(--ht);font-size:13px;">컬렉션이 없습니다.<br>아래에서 새 컬렉션을 만들어보세요.</div>';
    return;
  }
  body.innerHTML=colls.map((c,ci)=>`
    <div class="coll-folder" id="cf${ci}">
      <div class="coll-folder-hd" onclick="toggleColl(${ci})">
        <span class="coll-folder-icon">📁</span>
        <span class="coll-folder-name">${escHtml(c.name)}</span>
        <span class="coll-folder-cnt">${c.laws?.length||0}개</span>
        <button class="coll-del-btn" onclick="event.stopPropagation();deleteCollection(${ci})" title="컬렉션 삭제">×</button>
      </div>
      <div class="coll-folder-body" id="cfb${ci}">
        ${(c.laws||[]).map((l,li)=>`
          <div class="coll-item">
            <span class="coll-item-name" onclick="openFullPanel('${escHtml(l.name).replace(/'/g,"\\'")}','${encodeURIComponent(l.url)}');closeCollModal();">${escHtml(l.name)}</span>
            <span style="font-size:11px;color:var(--ht);">${escHtml(l.org||'')}</span>
            <button class="coll-del-btn" onclick="removeLawFromColl(${ci},${li})">×</button>
          </div>`).join('')}
        ${(c.laws||[]).length===0?'<div style="font-size:12px;color:var(--ht);padding:4px 0;">법령이 없습니다. 검색 결과 카드에서 추가하세요.</div>':''}
      </div>
    </div>`).join('');
}

function toggleColl(i){
  const b=document.getElementById('cfb'+i);
  if(b) b.style.display=b.style.display==='block'?'none':'block';
}

function createCollection(){
  const name=document.getElementById('collNewName').value.trim();
  if(!name)return;
  const colls=getCollections();
  if(colls.find(c=>c.name===name)){alert('같은 이름의 컬렉션이 있습니다.');return;}
  colls.push({name, laws:[], createdAt:new Date().toISOString().slice(0,10)});
  saveCollections(colls);
  document.getElementById('collNewName').value='';
  renderCollModal();
}

function deleteCollection(i){
  if(!confirm('컬렉션을 삭제할까요?'))return;
  const c=getCollections(); c.splice(i,1); saveCollections(c); renderCollModal();
}

function addLawToCollection(lawName, org, url){
  const colls=getCollections();
  if(!colls.length){alert('먼저 컬렉션을 만들어주세요. (상단 📁 컬렉션 버튼)');return;}
  const sel=colls.map((c,i)=>`${i+1}. ${c.name}`).join('\n');
  const idx=parseInt(prompt(`추가할 컬렉션 번호를 입력하세요:\n${sel}`))-1;
  if(isNaN(idx)||idx<0||idx>=colls.length)return;
  if(colls[idx].laws.find(l=>l.name===lawName)){alert('이미 추가된 법령입니다.');return;}
  colls[idx].laws.push({name:lawName, org, url, addedAt:new Date().toISOString().slice(0,10)});
  saveCollections(colls);
  alert(`「${lawName}」을 '${colls[idx].name}'에 추가했습니다.`);
}

function removeLawFromColl(ci, li){
  const c=getCollections(); c[ci].laws.splice(li,1); saveCollections(c); renderCollModal();
}

// renderPage에서 컬렉션 추가 버튼 포함
function makeCollBtnHtml(name, org, url){
  return `<button class="abtn" onclick="addLawToCollection('${name.replace(/'/g,"\\'")}','${(org||'').replace(/'/g,"\\'")}','${url}')">📁 컬렉션</button>`;
}

// ══════════════════════════════════════════════════════════════════════════════
// ③ 법령 관계 네트워크 시각화
// ══════════════════════════════════════════════════════════════════════════════
function openNetPanel(){
  // 즐겨찾기 + 최근 검색에서 법령 목록 수집
  loadFavs().then(favs=>{
    const sel=document.getElementById('netCenter');
    const opts=new Set();
    favs.forEach(f=>opts.add(f.name));
    allLaws.forEach(l=>opts.add(l['법령명한글']||''));
    sel.innerHTML=[...opts].filter(Boolean).map(n=>`<option value="${escHtml(n)}">${escHtml(n)}</option>`).join('');
    document.getElementById('netPanel').classList.add('show');
    if(sel.options.length) buildNetwork();
  });
}
function closeNetPanel(){document.getElementById('netPanel').classList.remove('show');}

async function buildNetwork(){
  const centerName=document.getElementById('netCenter').value;
  if(!centerName) return;
  const g=document.getElementById('netG');
  g.innerHTML='<text x="50%" y="50%" text-anchor="middle" fill="#9ca3af" font-size="14">법령 조문 로드 중...</text>';

  try{
    const r=await fetch(`/api/law/articles?name=${encodeURIComponent(centerName)}`);
    const d=await r.json();
    const arts=d.articles||[];

    // 조문에서 「법령명」 패턴 추출
    const refSet=new Set();
    const lawRefRe=/[「｢]([^」｣\n]{2,40}(?:법|령|규칙|조례))[」｣]/g;
    arts.forEach(a=>{
      let m;
      while((m=lawRefRe.exec(a['조문내용']||''))!==null){
        if(m[1]!==centerName) refSet.add(m[1]);
      }
    });
    const refs=[...refSet].slice(0,12);

    // SVG 그래프 그리기
    const W=document.getElementById('netCanvas').offsetWidth||800;
    const H=document.getElementById('netCanvas').offsetHeight||500;
    const cx=W/2, cy=H/2;
    const nodes=[{name:centerName,x:cx,y:cy,type:'center'}];
    const edges=[];

    // 시행령/규칙 자동 감지 (법령명에 "시행령"/"시행규칙" 포함)
    const subNames=[centerName+'시행령', centerName.replace(/법$/,'')+'법 시행령'];

    refs.forEach((name,i)=>{
      const angle=(2*Math.PI/refs.length)*i - Math.PI/2;
      const r=Math.min(W,H)*0.35;
      const isSub=name.includes('시행령')||name.includes('시행규칙');
      nodes.push({name,x:cx+r*Math.cos(angle),y:cy+r*Math.sin(angle),type:isSub?'sub':'ref'});
      edges.push({from:0,to:nodes.length-1});
    });

    const colors={center:'#1D9E75',ref:'#3b82f6',sub:'#f59e0b'};
    const edgeSvg=edges.map(e=>{
      const f=nodes[e.from],t=nodes[e.to];
      const dx=t.x-f.x,dy=t.y-f.y,len=Math.sqrt(dx*dx+dy*dy);
      const ux=dx/len,uy=dy/len;
      return `<line class="net-edge" x1="${f.x+ux*28}" y1="${f.y+uy*28}" x2="${t.x-ux*32}" y2="${t.y-uy*32}"/>`;
    }).join('');

    const nodeSvg=nodes.map((n,i)=>{
      const col=colors[n.type]||'#6b7280';
      const isCenter=n.type==='center';
      const r=isCenter?34:26;
      const label=n.name.length>8?n.name.slice(0,7)+'…':n.name;
      return `<g class="net-node" onclick="openFullPanel('${n.name.replace(/'/g,"\\'")}','https://www.law.go.kr/법령/${encodeURIComponent(n.name)}','','')" transform="translate(${n.x},${n.y})">
        <circle r="${r}" fill="${col}" opacity="${isCenter?1:.85}"/>
        <text text-anchor="middle" dy="4" fill="#fff" font-weight="${isCenter?700:500}">${escHtml(label)}</text>
      </g>`;
    }).join('');

    g.innerHTML=edgeSvg+nodeSvg;
  }catch(e){
    document.getElementById('netG').innerHTML=`<text x="50%" y="50%" text-anchor="middle" fill="#ef4444" font-size="13">오류: ${escHtml(e.message)}</text>`;
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// ④ 조문 히스토리 타임라인 (조문 단위 개정 이력)
// ══════════════════════════════════════════════════════════════════════════════
async function showArtHistory(idx){
  const art=fpArts[idx];
  if(!art) return;
  const lawName=document.getElementById('fpTitle').textContent;
  const lawUrl=document.getElementById('fpExtLink').href;
  const no=art['조문번호']?`제${art['조문번호']}조`:'';
  const title=art['조문제목']||'';
  const body=document.getElementById('fpBody');
  const backBtn=`<button class="fp-tbtn" style="margin-bottom:14px;" onclick="openFullPanel(decodeURIComponent('${encodeURIComponent(lawName)}'),decodeURIComponent('${encodeURIComponent(lawUrl)}'))">← 조문 전문으로 돌아가기</button>`;

  body.innerHTML=`<div class="fp-loading"><div class="spinner" style="margin:0 auto 10px;"></div>${escHtml(no)} 히스토리 조회 중...</div>`;

  try{
    const r=await fetch(`/api/law/art_history?name=${encodeURIComponent(lawName)}&art_no=${art['조문번호']||''}`);
    const d=await r.json();

    if(d.error||!d.history?.length){
      body.innerHTML=`<div style="padding:16px;">${backBtn}
        <div class="fp-empty">히스토리 정보를 가져오지 못했습니다.<br><small>법제처에 이전 버전 데이터가 없거나 조문 변경이 없었을 수 있습니다.</small></div>
      </div>`;
      return;
    }

    const timelineHtml=d.history.map(h=>`
      <div class="hist-entry">
        <div class="hist-dot ${h.type||'개정'}">${h.type==='신설'?'新':h.type==='현행'?'現':'改'}</div>
        <div class="hist-entry-body">
          <div class="hist-entry-date">${h.date||''} · ${h.type||'개정'}</div>
          <div class="hist-entry-content">${escHtml(h.content||'')}</div>
          ${h.prev&&h.content?`<button class="fp-tbtn" style="margin-top:6px;font-size:11px;" onclick="openDiff('${no}','${escHtml(title)}',decodeURIComponent('${encodeURIComponent(h.prev||'')}'),decodeURIComponent('${encodeURIComponent(h.content||'')}'),'${escHtml(h.prev_date||'이전')}','${escHtml(h.date||'현재')}')">🔀 변경 비교 보기</button>`:''}
        </div>
      </div>`).join('');

    body.innerHTML=`<div style="padding:4px 0 12px;">${backBtn}
      <div style="font-size:13px;font-weight:600;margin-bottom:14px;">${escHtml(no)} ${escHtml(title)} — 개정 히스토리</div>
      <div class="hist-timeline">${timelineHtml}</div>
    </div>`;
  }catch(e){
    body.innerHTML=`<div style="padding:16px;">${backBtn}<div class="fp-empty">오류: ${escHtml(e.message)}</div></div>`;
  }
}

// renderFpArts에서 히스토리 버튼 표시를 위해 패치
// ══════════════════════════════════════════════════════════════════════════════
// ⑤ 자연어 조문 검색
// ══════════════════════════════════════════════════════════════════════════════
function isNaturalLang(q){
  // 물음표, 서술형 종결어, 조건어 패턴 감지
  return /[?？]|은\s*무엇|이란|조건|요건|경우|방법|절차|할\s*수\s*있|해야\s*하|받을\s*수|어떻/.test(q);
}

function extractNLKeywords(q){
  // 조사·어미 제거 후 핵심 명사 추출
  const stopwords=['이란','무엇','어떻게','경우','대한','관한','있는','없는','하는','되는',
                   '위한','따른','정한','규정','조건','요건','방법','절차','할수','있을'];
  const words=q.replace(/[?？은는이가을를의도에서으로와과]/g,' ')
               .split(/\s+/)
               .filter(w=>w.length>=2 && !stopwords.includes(w));
  return [...new Set(words)];
}

function scoreArticleNL(art, keywords){
  const text=(art['조문내용']||'')+(art['조문제목']||'');
  let score=0;
  keywords.forEach(kw=>{
    const re=new RegExp(kw.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'g');
    const matches=text.match(re);
    if(matches) score+=matches.length*(kw.length>=4?3:1);
  });
  return score;
}

// ⑤ 자연어 검색은 doSearch 내부에 통합됨 (위 doSearch 참조)
async function doNLSearch(q){
  document.getElementById('sbtn').disabled=true;
  clearResults();
  setStatus(`✨ 자연어 검색: "${q}"`, true);

  const keywords=extractNLKeywords(q);
  if(!keywords.length){
    // 키워드 추출 실패 시 일반 검색으로 폴백
    document.getElementById('sbtn').disabled=false;
    setStatus('법제처 조회 중...',true);
    try{
      const r=await fetch(`/api/search?query=${encodeURIComponent(q)}&display=20`);
      const d=await r.json();
      if(d.error){setStatus(`오류: ${d.error}`);renderEmpty(d.error);return;}
      if(!d.success||!d.laws?.length){setStatus(`"${q}" 검색 결과가 없습니다`);renderEmpty(`"${q}"에 해당하는 법령을 찾지 못했습니다`);return;}
      addRecent(q);
      allLaws=d.laws; buildTypeFilter(); applyFilter();
      setStatus(`법제처 실시간 · ${allLaws.length}건`);
      document.getElementById('fbar').style.display='flex';
    }catch{setStatus('서버 연결 실패');renderEmpty('서버에 연결할 수 없습니다.');}
    document.getElementById('sbtn').disabled=false;
    return;
  }

  // 키워드로 법령 검색 후 조문 스코어링
  try{
    // 핵심 키워드 2개로 검색
    const searchKw=keywords.slice(0,2).join(' ');
    const r=await fetch(`/api/search?query=${encodeURIComponent(searchKw)}&display=10`);
    const d=await r.json();
    if(!d.success||!d.laws?.length){
      setStatus(`"${q}" 관련 법령을 찾지 못했습니다`);
      renderEmpty('자연어 검색 결과가 없습니다. 법령명 검색을 시도해보세요.');
      document.getElementById('sbtn').disabled=false; return;
    }

    addRecent(q);
    allLaws=d.laws; buildTypeFilter(); applyFilter();

    // 자연어 힌트 배너 표시
    document.getElementById('nlBanner').style.display='flex';
    setStatus(`✨ 자연어 검색 · ${d.laws.length}건 · 「${keywords.join('」「')}」 관련 법령`);
    document.getElementById('fbar').style.display='flex';

    // 조문 내 키워드 스코어 기반 정렬 (백그라운드)
    scoreAndReorderAsync(allLaws, keywords);
  }catch{
    setStatus('서버 연결 실패');
    renderEmpty('서버에 연결할 수 없습니다.');
  }
  document.getElementById('sbtn').disabled=false;
}

async function scoreAndReorderAsync(laws, keywords){
  // 법령별로 조문 로드 후 스코어 계산 (최대 5개)
  const scored=[];
  for(const law of laws.slice(0,5)){
    try{
      const name=law['법령명한글']||'';
      const r=await fetch(`/api/law/articles?name=${encodeURIComponent(name)}`);
      const d=await r.json();
      const arts=(d.articles||[]).filter(a=>a.type==='article');
      const score=arts.reduce((s,a)=>s+scoreArticleNL(a,keywords),0);
      const topArt=arts.sort((a,b)=>scoreArticleNL(b,keywords)-scoreArticleNL(a,keywords))[0];
      scored.push({...law, _nlScore:score, _topArt:topArt});
    }catch{}
  }
  if(!scored.length) return;
  // 스코어 기준 재정렬
  scored.sort((a,b)=>b._nlScore-a._nlScore);
  const remaining=allLaws.filter(l=>!scored.find(s=>s['법령명한글']===l['법령명한글']));
  allLaws=[...scored,...remaining];
  // 결과 카드에 관련도 배지 추가
  applyFilter();
  scored.forEach((law,i)=>{
    if(!law._nlScore) return;
    const card=document.getElementById('c'+i);
    if(!card) return;
    const head=card.querySelector('.card-head');
    if(!head) return;
    const badge=document.createElement('span');
    badge.className='nl-badge'; badge.title='자연어 매칭 스코어';
    badge.innerHTML=`✨ 관련도 ${Math.min(100,Math.round(law._nlScore/3))}%`;
    head.querySelector('.lorg').after(badge);
  });
}

// 검색창 입력 시 자연어 감지 힌트
document.addEventListener('DOMContentLoaded',()=>{
  const qi=document.getElementById('q');
  if(qi) qi.addEventListener('input',()=>{
    const q=qi.value.trim();
    const banner=document.getElementById('nlBanner');
    if(!banner) return;
    if(isNaturalLang(q) && mode==='law'){
      banner.style.display='flex';
    } else {
      banner.style.display='none';
    }
  });
});


</script>
</body>
</html>"""


# ── Flask 라우트 ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html; charset=utf-8")


@app.route("/api/search")
def search_laws():
    query   = request.args.get("query", "").strip()
    display = request.args.get("display", "20")
    if not query:
        return jsonify({"error": "검색어를 입력하세요"}), 400
    try:
        add_recent(query)
        data = _law_get_json({"target": "law", "query": query, "display": display})
        err  = data.get("LawSearch", {}).get("message", "")
        if err:
            return jsonify({"error": f"법제처 오류: {err}"}), 502
        laws = data.get("LawSearch", {}).get("law", []) or []
        if isinstance(laws, dict):
            laws = [laws]
        if laws:
            print(f"[DEBUG] 검색결과 필드: {list(laws[0].keys())}")
        return jsonify({"success": True, "count": len(laws), "laws": laws})
    except Exception as e:
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


@app.route("/api/search/article")
def search_by_article_keyword():
    """
    조문 내용 키워드 검색.
    법제처 API는 조문 내용 검색을 직접 지원하지 않으므로,
    농업·지식재산 관련 핵심 법령 목록에서 조문을 불러와 서버 측 필터링한다.
    """
    query = request.args.get("query", "").strip()
    if not query:
        return jsonify({"error": "검색어를 입력하세요"}), 400

    # 검색 대상 법령 목록 (농업·지식재산 분야 핵심 법령)
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

    kw = query.lower()
    matched_laws = []

    import concurrent.futures

    def fetch_and_filter(law_name: str):
        """법령 조문을 불러와 키워드 포함 여부 확인"""
        try:
            mst = _get_mst(law_name)
            if not mst:
                return None
            root = None
            for param in ("MST", "ID"):
                try:
                    r = _law_get_xml("lawService.do", {"target": "law", param: mst})
                    tags = {el.tag for el in r.iter()}
                    if len(tags) > 3:
                        txt = " ".join(el.text or "" for el in r.iter())
                        if "없습니다" not in txt:
                            root = r; break
                except Exception:
                    continue
            if root is None:
                return None

            # 조문 파싱 후 키워드 검색
            lname, law_date, articles = _parse_articles(root)
            matched = [a for a in articles
                       if a.get("type") == "article" and
                       kw in (a.get("조문내용","") + a.get("조문제목","")).lower()]
            if matched:
                # 법령 메타 정보도 JSON 검색으로 가져오기
                try:
                    meta_data = _law_get_json({"target": "law", "query": law_name, "display": "1"})
                    meta_laws = meta_data.get("LawSearch", {}).get("law", []) or []
                    if isinstance(meta_laws, dict): meta_laws = [meta_laws]
                    meta = meta_laws[0] if meta_laws else {}
                except Exception:
                    meta = {}
                return {
                    "법령명한글": lname or law_name,
                    "법령구분명": meta.get("법령구분명", "법률"),
                    "소관부처명": meta.get("소관부처명", ""),
                    "공포일자":   meta.get("공포일자", ""),
                    "법령일련번호": meta.get("법령일련번호", ""),
                    "_matched_count": len(matched),
                }
        except Exception as e:
            print(f"[article-search] {law_name} 오류: {e}")
        return None

    # 병렬 처리 (최대 5개 동시)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(fetch_and_filter, name): name for name in CANDIDATE_LAWS}
        for future in concurrent.futures.as_completed(futures, timeout=25):
            res = future.result()
            if res:
                results.append(res)

    # 매칭 조문 수 기준 정렬
    results.sort(key=lambda x: x.get("_matched_count", 0), reverse=True)
    for r in results:
        r.pop("_matched_count", None)

    print(f"[article-search] '{query}' → {len(results)}건 매칭")
    return jsonify({"success": True, "count": len(results), "laws": results})


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

    system_prompt = f"""당신은 대한민국 법률 전문 해석 AI입니다.
「{law_name}」 {art_no} {art_title} 조문을 아래 형식으로 해석하세요.

1. 핵심 요약 (1~2문장): 이 조문이 말하는 것을 가장 간결하게
2. 쉬운 해설 (3~5문장): 법률 비전문가가 이해하도록 쉬운 언어로
3. 실무 포인트 (1~3개): 농업인·기업이 알아야 할 실질적인 사항

한국어로 300자 이내로 간결하게 작성하세요.
※ 이 해석은 참고용이며 법적 효력이 없습니다."""

    user_msg = f"조문 내용:\n{art_content}"

    try:
        # ── Claude ─────────────────────────────────────────────────────────────
        if provider == "claude":
            mdl = model or "claude-sonnet-4-20250514"
            resp = req_lib.post(
                "https://api.anthropic.com/v1/messages",
                json={"model": mdl, "max_tokens": 800,
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
                json={"model": mdl, "max_tokens": 800,
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
            mdl = model or "gemini-2.0-flash"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={api_key}"
            resp = req_lib.post(
                url,
                json={"contents": [{"parts": [{"text": f"{system_prompt}\n\n{user_msg}"}]}],
                      "generationConfig": {"maxOutputTokens": 800}},
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
                tags = {el.tag for el in r.iter()}
                if len(tags) > 3:
                    txt = "".join(el.text or "" for el in r.iter())
                    if "없습니다" not in txt:
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
                        tags2 = {el.tag for el in pr.iter()}
                        if len(tags2) > 3:
                            txt2 = "".join(el.text or "" for el in pr.iter())
                            if "없습니다" not in txt2:
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
                    tags = {el.tag for el in r.iter()}
                    if len(tags) > 3:
                        txt = "".join(el.text or "" for el in r.iter())
                        if "없습니다" not in txt:
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



    return jsonify({"recent": recent_searches})


@app.route("/api/recent/add")
def add_recent_api():
    q = request.args.get("q", "").strip()
    if q:
        add_recent(q)
    return jsonify({"ok": True})


@app.route("/api/ping")
def ping():
    try:
        data = _law_get_json({"target": "law", "query": "농지", "display": "1"})
        ok   = bool(data.get("LawSearch", {}).get("law"))
        return jsonify({"server": True, "law_api": ok})
    except Exception as e:
        return jsonify({"server": True, "law_api": False, "message": str(e)})


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
    print("  🌾  농업 법령 검색 서비스")
    print(f"  🔗  {url}")
    print("  종료: Ctrl+C")
    print("=" * 50)
    # 로컬 실행 시에만 브라우저 자동 오픈
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
