#!/usr/bin/env python3
"""API 안전성 회귀 시험 — 네트워크 없이 Flask test_client 로 점검 항목을 재현·확인한다.

  1. 서버 AI 키 미사용: 서버에 키가 있어도 사용자 키 없는 AI 요청은 거부, 의미 검색도 꺼짐
  2. 업로드 인증: 토큰이 없으면 로컬 개발 서버(직접 접속)만 허용, 그 밖은 401
  3. 잘못된 입력: 배열·숫자·문자열 JSON, limit=abc 등에 500 대신 400/정상 응답
  4. 사용자 간 공유되던 서버 메모리 API(즐겨찾기·최근 검색) 제거
  5. 요청 본문 크기 제한(413)

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

print("5) 요청 크기 제한")
r = c.post("/api/regs/upload", data={"file": (__import__("io").BytesIO(b"0" * (7 * 1024 * 1024)), "big.hwpx")},
           content_type="multipart/form-data")
check(r.status_code == 413, f"제한(1+5MB) 초과 요청 413 ({r.status_code})")

if fails:
    sys.exit(f"\n실패 {len(fails)}건")
print("\n모두 통과")
