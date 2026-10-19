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

if fails:
    sys.exit(f"\n실패 {len(fails)}건")
print("\n모두 통과")
