#!/usr/bin/env python3
"""API 안전성 회귀 시험 — 네트워크 없이 Flask test_client 로 점검 항목을 재현·확인한다.

  1. 서버 AI 키 미사용: 서버에 키가 있어도 사용자 키 없는 AI 요청은 거부, 의미 검색도 꺼짐
  2. 업로드 인증: 토큰이 없으면 로컬 개발 서버(직접 접속)만 허용, 그 밖은 401
  3. 잘못된 입력: 배열·숫자·문자열 JSON, limit=abc 등에 500 대신 400/정상 응답
  4. 사용자 간 공유되던 서버 메모리 API(즐겨찾기·최근 검색) 제거
  5. 요청 본문 크기 제한(413)
  6. 인기 검색어: 개인정보 형태·사람 이름 미기록, 2명 이상만 노출, 방문자 ID 미저장
  7. 조문 판별 정규식 과부하 방지, 오류 응답의 서비스키 가림·502, ?debug=1 관리자 전용
  8. 동시 조회 시간 제한, 캐시 상한, 무거운 검색 호출 제한, Ollama 미설정 차단, KIPRIS https
  9. Gemini 키는 URL 대신 헤더, AI 가 배열 JSON 을 돌려줘도 500 없음, 원본 파일 보안 헤더, 쓰지 않는 API 정리
 10. 방문자 수·내규 랭킹: 운영 저장소(Upstash Redis) 경로를 메모리 흉내로 재현해 집계·저장 내용 확인
 11. 내규 체계도: 시행세칙→모규정 연결, 순환 없음, 규정끼리는 상하로 묶지 않음
 12. 내규 검색 보강: '규정명 제N조' 직접 조회·없는 조문 안내·문장형 질문 낱말 검색, 법령명 확인 묶음 요청
 13. 법령 검색: 긴 행정규칙 이름 전체를 넣으면 그 규정(문장 검색으로 오인하지 않음)

사용: python scripts/test_api_safety.py   (실패 시 종료 코드 1)
"""
import json
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# 서버 키가 '있는' 상황을 만들어 둔다 — 그래도 쓰이지 않아야 한다
os.environ["GEMINI_API_KEY"] = "server-key-should-not-be-used"
os.environ.pop("REG_UPLOAD_TOKEN", None)
os.environ.pop("GITHUB_TOKEN", None)
os.environ["REG_UPLOAD_MAX_MB"] = "1"
socket.setdefaulttimeout(0.3)

import requests  # noqa: E402
import requests.sessions  # noqa: E402

_calls = []


def _blocked(*a, **k):
    _calls.append(a[2] if len(a) > 2 else k.get("url", ""))
    raise requests.exceptions.ConnectionError("blocked(test)")


requests.sessions.Session.request = _blocked
import api_server as A  # noqa: E402

c = A.app.test_client()
fails = []


def check(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        fails.append(msg)


print("1) 서버 AI 키 미사용")
r = c.post("/api/scenario", json={"query": "농지를 전용하려면 어떤 절차가 필요한가요"})
check(r.status_code == 503 and "AI 설정" in r.get_json()["error"], f"키 없는 AI 질문 거부 ({r.status_code})")
check(not any("generativelanguage" in str(u) for u in _calls), "서버 키로 Gemini 를 호출하지 않음")
check(A._user_gemini_key.__doc__ and "서버 키 미사용" in A._user_gemini_key.__doc__, "의미 검색 키는 헤더만")
with A.app.test_request_context("/api/internal/search"):
    check(A._user_gemini_key() == "", "헤더 없으면 의미 검색 키 없음(서버 키 무시)")
with A.app.test_request_context("/api/internal/search", headers={"X-Gemini-Key": "user-k"}):
    check(A._user_gemini_key() == "user-k", "사용자 헤더 키는 사용")

print("2) 업로드 인증")
os.environ["VERCEL"] = "1"
r = c.post("/api/regs/upload/delete", json={"slug": "감사규정"})
check(r.status_code == 401, f"배포 환경·토큰 없음 → 삭제 거부 ({r.status_code})")
r = c.post("/api/regs/upload", data={})
check(r.status_code == 401, f"배포 환경·토큰 없음 → 업로드 거부 ({r.status_code})")
os.environ.pop("VERCEL")
r = c.post("/api/regs/upload/delete", json={"slug": "x"}, headers={"X-Forwarded-For": "8.8.8.8"})
check(r.status_code == 401, f"프록시 경유(외부) 접속·토큰 없음 → 거부 ({r.status_code})")
A.REG_UPLOAD_TOKEN = "secret-token"
r = c.post("/api/regs/upload/delete", json={"slug": "x"}, headers={"X-Upload-Token": "wrong"})
check(r.status_code == 401, f"틀린 토큰 거부 ({r.status_code})")
r = c.post("/api/regs/upload/delete", json={"slug": "없는규정"}, headers={"X-Upload-Token": "secret-token"})
check(r.status_code != 401, f"맞는 토큰은 인증 통과 ({r.status_code})")
A.REG_UPLOAD_TOKEN = ""

print("3) 잘못된 입력 → 500 없음")
posts = ["/api/scenario", "/api/ai/interpret", "/api/regs/upload/delete", "/api/expense/hwpx",
         "/api/stats/visit", "/api/stats/reg"]
bad_bodies = ["[1, 2]", "123", '"str"', "{not json",
              json.dumps({"query": 12345, "name": [1], "vid": 5, "slug": ["a"], "rows": "x", "art_content": 5})]
for u in posts:
    worst = max(c.post(u, data=b, content_type="application/json").status_code for b in bad_bodies)
    check(worst < 500, f"POST {u} 최대 상태 {worst}")
for u, qs in [("/api/stats/ranking", "limit=abc"), ("/api/stats/ranking", "limit=-5&period=zzz"),
              ("/api/internal/semantic", "query=여비&top=zz")]:
    st = c.get(f"{u}?{qs}").status_code
    check(st < 500, f"GET {u}?{qs} → {st}")

print("4) 공유 서버 메모리 API 제거")
for u in ["/api/favorites", "/api/favorites/toggle?name=x", "/api/favorites/remove?name=x",
          "/api/recent", "/api/recent/add?q=x"]:
    r = c.get(u)
    ok = r.status_code == 404 or "favorites" not in r.get_data(as_text=True) and "recent" not in r.get_data(as_text=True)
    check(ok, f"{u} 없음 ({r.status_code})")

print("6) 인기 검색어 — 개인정보 보호")
import tempfile
A._STATS_FILE = os.path.join(tempfile.mkdtemp(), "st.json")
for q in ["010-1234-5678", "900101-1234567", "abc@koat.or.kr", "김철수", "홍길동 징계", "최민수 퇴직",
          "https://x.com", "12345678", "어제 팀장이 저한테 화를 내서 너무 힘들어요 어떻게 해요"]:
    check(A._query_safe(q) == "", f"기록 안 함: {q}")
for q in ["여비", "출장 여비", "농지법 전용허가", "감사규정 제15조", "육아휴직 기간"]:
    check(A._query_safe(q) == q, f"기록함: {q}")
c.post("/api/stats/query", json={"vid": "aaaaaaaaaaaa1", "q": "여비"})
c.post("/api/stats/query", json={"vid": "aaaaaaaaaaaa1", "q": "연차휴가"})
c.post("/api/stats/query", json={"vid": "aaaaaaaaaaaa1", "q": "연차휴가"})
c.post("/api/stats/query", json={"vid": "bbbbbbbbbbbb2", "q": "여비"})
A._stats_cache.clear()
got = [x["q"] for x in c.get("/api/stats/queries").get_json()["items"]]
check(got == ["여비"], f"서로 다른 2명 이상이 찾은 말만 노출 {got}")
raw = open(A._STATS_FILE, encoding="utf-8").read()
check("aaaaaaaaaaaa1" not in raw and "bbbbbbbbbbbb2" not in raw, "방문자 ID 원문을 검색어와 함께 저장하지 않음")
check(c.post("/api/stats/query", data="[1]", content_type="application/json").status_code == 400, "잘못된 본문 400")

print("5) 요청 크기 제한")
r = c.post("/api/regs/upload", data={"file": (__import__("io").BytesIO(b"0" * (7 * 1024 * 1024)), "big.hwpx")},
           content_type="multipart/form-data")
check(r.status_code == 413, f"제한(1+5MB) 초과 요청 413 ({r.status_code})")

print("7) 정규식 과부하(ReDoS)·오류 응답 키 노출·디버그 출력")
import time as _t
q = "법 " + "1조 " * 200 + "x"
t0 = _t.time(); A._ART_REF_RE.match(q); dt = _t.time() - t0
check(dt < 0.5, f"'법 1조×200 x' 조문 판별 {dt:.3f}s (예전에는 18회만으로 1초, 25회 이상은 수 분)")
m = A._ART_REF_RE.match("감사규정 제15조 제2항, 제16조")
check(m and m.group("law") == "감사규정", "조문 참조 판별 결과는 그대로")
err = requests.exceptions.ConnectionError(
    "HTTPSConnectionPool(host='apis.data.go.kr'): Max retries exceeded with url: /x?serviceKey=SECRET123&a=1")
check("SECRET123" not in A._safe_err(err) and A._err_status(err) == 502, f"연결 오류 문구에 키 없음·502 ({A._safe_err(err)})")
check("SECRET" not in A._redact("...?ServiceKey=SECRET&crtfc_key=SECRET2&OC=SECRET3"), "로그용 키 가림")
with A.app.test_request_context("/api/assist/company?debug=1"):
    check(A._debug_allowed() is False, "?debug=1 은 토큰 없으면 무시")

print("8) 시간 제한·캐시 상한·호출 제한·Ollama·KIPRIS https")
import time as _t
_orig = A._fetch_matching_articles if hasattr(A, "_fetch_matching_articles") else None
def _slow_pool_job():
    with A._pool(2) as ex:
        futs = [ex.submit(_t.sleep, 3)]
        try:
            for _ in A._cf.as_completed(futs, timeout=0.3):
                pass
        except A._cf.TimeoutError:
            pass
t0 = _t.time(); _slow_pool_job(); dt = _t.time() - t0
check(dt < 1.0, f"동시 조회 시간 제한이 실제로 걸림(남은 작업을 기다리지 않음) {dt:.2f}s")
lru = A._LruDict(3)
for i in range(10):
    lru[i] = i
check(len(lru) == 3 and lru.get(9) == 9 and lru.get(0) is None, "캐시 개수 상한(LRU)")
check(all(isinstance(x, A._LruDict) for x in (A._LAW_ARTICLE_CACHE, A._LAW_SUGGEST_CACHE, A._LAW_FAMILY_CACHE)),
      "법령 조문·자동완성·연계 캐시는 상한 있음")
A._FANOUT_PER_MIN = 3
codes = [c.get("/api/search/article?query=x", headers={"X-Forwarded-For": "203.0.113.9"}).status_code for _ in range(5)]
check(codes[-1] == 429, f"무거운 검색 IP별 분당 제한 → 429 ({codes})")
A._FANOUT_PER_MIN = 120
os.environ.pop("OLLAMA_BASE_URL", None)
txt, err = A._ai_generate("ollama", "", "", "s", "u")
check(not txt and "설정" in err, f"Ollama 미설정이면 호출하지 않음 ({err})")
src = open(os.path.join(ROOT, "api_server.py"), encoding="utf-8").read()
check("http://plus.kipris.or.kr" not in src and 'f"http://apis.data.go.kr' not in src, "KIPRIS·나라장터 서비스키를 http(평문)로 보내지 않음")

print("9) 키 전달 방식·AI 응답 형식·원본 파일 헤더·정리")
check("?key={api_key}" not in src and "x-goog-api-key" in src, "Gemini 키를 URL 이 아닌 헤더로 보냄")
_orig_gen = A._ai_generate
A._ai_generate = lambda *a, **k: ('["농지법", 3]', "")
r = c.post("/api/scenario", json={"query": "농지 전용 절차", "api_key": "user-k", "provider": "gemini"})
check(r.status_code < 500, f"AI 가 배열 JSON 을 돌려줘도 500 없음 ({r.status_code})")
A._ai_generate = _orig_gen
reg = next((d for d in os.listdir(A.REG_DIR) if not d.startswith(".") and os.path.isdir(os.path.join(A.REG_DIR, d))), None)
f = reg and next((x for x in os.listdir(os.path.join(A.REG_DIR, reg)) if not x.startswith(".")), None)
if f:
    r = c.get(f"/regulations/{reg}/{f}")
    check(r.headers.get("X-Content-Type-Options") == "nosniff" and "script-src 'none'" in r.headers.get("Content-Security-Policy", ""),
          f"원본 파일 nosniff·CSP ({r.status_code})")
check(c.get("/api/ai/models").status_code == 404, "쓰지 않던 /api/ai/models 제거")

print("10) 방문자 수·내규 랭킹(Redis 경로)·저장 내용")
c.post("/api/stats/visit", json={"vid": "ffffffffffff9", "new": True})
check("ffffffffffff9" not in open(A._STATS_FILE, encoding="utf-8").read(), "로컬 파일에도 방문자 ID 원문 없음")
DB = {}


def _fake_redis(cmds):
    out = []
    for op, *a in cmds:
        if op == "INCR":
            DB[a[0]] = int(DB.get(a[0], 0)) + 1; out.append(DB[a[0]])
        elif op == "GET":
            out.append(DB.get(a[0]))
        elif op == "EXPIRE":
            out.append(1)
        elif op == "PFADD":
            DB.setdefault(a[0], set()).update(a[1:]); out.append(1)
        elif op == "PFCOUNT":
            out.append(len(set().union(*[DB.get(k, set()) for k in a])))
        elif op == "ZADD":
            DB.setdefault(a[0], {})[a[2]] = float(a[1]); out.append(1)
        elif op == "ZINCRBY":
            z = DB.setdefault(a[0], {}); z[a[2]] = z.get(a[2], 0) + float(a[1]); out.append(z[a[2]])
        elif op == "ZREMRANGEBYSCORE":
            z = DB.get(a[0], {})
            for m in [m for m, v in z.items() if float(a[1]) <= v <= float(a[2])]:
                z.pop(m)
            out.append(0)
        elif op == "ZCOUNT":
            out.append(sum(1 for v in DB.get(a[0], {}).values() if v >= float(a[1])))
        elif op in ("ZRANGE", "ZREVRANGE"):
            z = sorted(DB.get(a[0], {}).items(), key=lambda x: x[1], reverse=op == "ZREVRANGE")
            z = z[int(a[1]):(None if a[2] == "-1" else int(a[2]) + 1)]
            out.append([v for m, sc in z for v in (m, str(sc))])
        else:
            raise RuntimeError(op)
    return out


_orig_redis, _orig_url = A._redis, (A._STATS_URL, A._STATS_TOKEN)
A._redis, A._STATS_URL, A._STATS_TOKEN = _fake_redis, "https://x.upstash.io", "t"
A._stats_cache.clear()
for vid, new in [("aaaaaaaaaaaa1", True), ("aaaaaaaaaaaa1", False), ("aaaaaaaaaaaa1", True), ("bbbbbbbbbbbb2", True)]:
    c.post("/api/stats/visit", json={"vid": vid, "new": new}, headers={"X-Forwarded-For": "203.0.113.7"})
sm = c.get("/api/stats/summary").get_json()
check(sm["today"]["pv"] == 3 and sm["today"]["uv"] == 2 and sm["online"] == 2 and sm["total"] == {"pv": 3, "uv": 2},
      f"방문 3·방문자 2·현재 접속 2 ({sm['today']}, {sm['online']})")
t1, t2 = (m["title"] for m in A._load_reg_manifest()[:2])
for n in (t1, t1, t2, "임의 문자열"):
    c.post("/api/stats/reg", json={"name": n})
rk = c.get("/api/stats/ranking?period=week&limit=5").get_json()["items"]
check(rk[:2] == [{"title": t1, "views": 2}, {"title": t2, "views": 1}] and len(rk) == 2, f"주간 랭킹 {rk}")
check(all(c.get(f"/api/stats/ranking?period={p}").get_json()["items"][0]["views"] == 2 for p in ("month", "all")), "월간·누적 랭킹")
dump = repr(DB)
vid_keys = sorted(k for k, v in DB.items() if isinstance(v, (set, dict)) and "aaaaaaaaaaaa1" in v)
check("203.0.113.7" not in dump, "IP 저장 안 함")
check(vid_keys == ["st:online", f"st:uv:{A._stats_day()}", "st:uv:all"], f"방문자 ID는 현재 접속(5분)·방문자 수(HyperLogLog)에만 {vid_keys}")
A._redis, (A._STATS_URL, A._STATS_TOKEN) = _orig_redis, _orig_url
A._stats_cache.clear()

print("11) 내규 체계도")
hd = c.get("/api/internal/hierarchy").get_json()
by = {n["title"]: n for n in hd["nodes"]}
check(hd["success"] and hd["count"] == len(A._load_reg_manifest()), f"전체 {hd['count']}건, 연결 {hd['linked']}건")
check(all(by[t]["parent"] == t.replace(" 시행세칙", "") for t in by if t.endswith("시행세칙") and t.replace(" 시행세칙", "") in by),
      "시행세칙은 모두 해당 규정 아래")
def _cyc(t):
    seen = set()
    while t:
        if t in seen:
            return True
        seen.add(t); t = by.get(t, {}).get("parent", "")
    return False
check(not any(_cyc(t) for t in by), "상하관계 순환 없음")
check(not any(n["category"] == "규정" and by.get(n["parent"], {}).get("category") == "규정" for n in by.values()), "규정끼리는 대등(상하로 묶지 않음)")

print("12) 내규 검색 보강·법령명 확인 묶음")
d = c.get("/api/internal/search", query_string={"query": "여비규정 제5조"}).get_json()
check(d.get("article_ref") == {"title": "여비규정", "no": "5"} and "제5조" in d["structured"][0]["content"][:12],
      "'여비규정 제5조' → 그 조문")
d = c.get("/api/internal/search", query_string={"query": "보수규정 제38조의2"}).get_json()
check((d.get("article_ref") or {}).get("no") == "38의2", "'제38조의2' 가지 조문")
d = c.get("/api/internal/search", query_string={"query": "감사규정 제999조"}).get_json()
check(d.get("article_missing", {}).get("title") == "감사규정" and not d.get("structured"), "없는 조문은 원인 안내")
d = c.get("/api/internal/search", query_string={"query": "출장을 가서 숙박비를 초과로 쓴 경우 어떻게 정산하나요"}).get_json()
check(d.get("fallback") == "terms" and {"출장", "숙박비"} <= set(d.get("terms") or []) and
      "여비규정" in [x["title"] for x in d["structured"][:3]], f"문장형 질문 → 낱말 검색 {d.get('terms')}")
check(A._int_terms("연차휴가 사용 방법")[0] == "연차휴가", "규정 낱말은 조사로 자르지 않음(연차휴가)")
d = c.get("/api/internal/search", query_string={"query": "여비"}).get_json()
check(not d.get("fallback") and d.get("structured"), "일반 검색은 그대로")
v = c.get("/api/validate", query_string={"qs": "농지법|특허법|" + "|".join(f"x{i}" for i in range(60))}).get_json()
check(isinstance(v.get("valid"), list) and len(v["valid"]) + len(v["failed"]) <= 40, "법령명 확인 묶음 요청(최대 40개)")
check("농지법" in v["failed"], "조회 실패는 '없음'으로 단정하지 않음")

print("13) 긴 행정규칙 이름 검색")
import xml.etree.ElementTree as _ET
_NM = "지식재산성과 관리 및 사용에 관한 업무처리 규정"
_orig_xml, _orig_arts = A._law_get_xml, A._law_articles_cached
def _fake_xml(ep, params, *a, **k):
    if params.get("target") == "admrul":
        return _ET.fromstring(f"<AdmRulSearch><admrul><행정규칙명>농림축산검역본부 {_NM}</행정규칙명><행정규칙일련번호>1</행정규칙일련번호></admrul>"
                              f"<admrul><행정규칙명>{_NM}</행정규칙명><행정규칙일련번호>2</행정규칙일련번호><행정규칙종류명>훈령</행정규칙종류명>"
                              f"<소관부처명>농촌진흥청</소관부처명></admrul></AdmRulSearch>")
    return _ET.fromstring("<LawSearch><totalCnt>0</totalCnt></LawSearch>")
A._law_get_xml = _fake_xml
A._law_articles_cached = lambda n, dt, info=None: (n, [{"type": "article", "조문번호": "1", "조문제목": "목적", "조문내용": "제1조(목적) …"}])
A._FANOUT_PER_MIN = 1000
for q in (_NM, _NM.replace(" ", "")):
    d = c.get("/api/search/article", query_string={"query": q}).get_json()
    check(d.get("mode") == "law_name" and d["laws"][0]["법령명한글"] == _NM and d["laws"][0]["법령구분명"] == "훈령",
          f"'{q[:12]}…' → 그 행정규칙(농촌진흥청 훈령), 비슷한 이름(농림축산검역본부)과 혼동 없음")
check(A._search_law_name("농지법") is None, "짧은 법령명은 기존 관련 조문 검색 유지")
A._law_get_xml, A._law_articles_cached = _orig_xml, _orig_arts
A._FANOUT_PER_MIN = 120

print("14) 내규 용어 사전(온톨로지)")
import json as _json
_on = _json.load(open(os.path.join(os.path.dirname(A.__file__), "ontology.json"), encoding="utf-8"))["concepts"]
_ids = {x["id"] for x in _on}
check(all(r in _ids for x in _on for r in [x.get("broader")] + x.get("related", []) if r), "사전의 상위·관련 개념 id 모두 존재")
_terms = [A._onto_key(t) for x in _on for t in [x["label"]] + x["synonyms"]]
check(len(_terms) == len(set(_terms)), "한 낱말이 두 개념에 겹치지 않음")
d = c.get("/api/internal/search", query_string={"query": "출장비"}).get_json()
check((d.get("concept") or {}).get("label") == "여비" and "여비" in (d.get("expanded") or [])
      and any(x.get("via") == "여비" and x["title"] == "여비규정" for x in d["structured"]),
      "'출장비' → 같은 말 '여비'로 여비규정까지 찾음")
_direct = [x for x in d["structured"] if not x.get("via")]
check(_direct and d["structured"][:len(_direct)] == _direct, "기존 결과가 앞, 같은 말로 찾은 규정은 뒤")
d = c.get("/api/internal/search", query_string={"query": "인사고과"}).get_json()
check(d["structured"] and all(x.get("via") for x in d["structured"]),
      f"규정에 없는 말(인사고과)도 대표어로 찾음 {d.get('expanded')}")
d = c.get("/api/internal/search", query_string={"query": "휴가"}).get_json()
check({"연차휴가", "병가"} <= set(d["concept"]["narrower"]) and not d.get("expanded"),
      "상위 개념은 하위 개념을 바로가기로만(검색 결과는 넓히지 않음)")
d = c.get("/api/internal/search", query_string={"query": "연차"}).get_json()
check("연차휴가" not in (d.get("expand_terms") or []), "검색어를 품은 말(연차휴가)은 다시 찾지 않음")
check(A._int_term_group("야근")[0] == "야근" and "시간외근무" in A._int_term_group("야근"), "문장형 낱말도 같은 말 포함")
check(A._onto_concept("농지법") is None and not c.get("/api/internal/search", query_string={"query": "여비규정"}).get_json().get("concept"),
      "사전에 없는 말은 그대로")

if fails:
    sys.exit(f"\n실패 {len(fails)}건")
print("\n모두 통과")
