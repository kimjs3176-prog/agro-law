# 아키텍처

## 구성요소
```
[브라우저]  index.html (SPA: 인라인 CSS/JS, 프레임워크 없음)
    │  fetch /api/*
    ▼
[Flask]     api_server.py  ──►  법제처 law.go.kr DRF OpenAPI (국가법령 본문·조문·개정이력)
    │                      ──►  내규 MCP 서버 (SAGYU_MCP_URL, 선택) — 내규 원문 검색
    │                      ──►  AI 프로바이더 (Gemini/Claude/GPT) — 실무 시나리오 답변
    ├─ regulations_manifest.json      내규 목록(메타)
    ├─ regulations/<규정명>/index.html 내규 본문 HTML(로컬 파일)
    └─ regulations_vectors.{bin,json}  의미 검색 임베딩(선택)

[배포]      Vercel (api/index.py = WSGI 진입점, index.html·regulations = 정적)
```

## 데이터 흐름
1. **내규 검색** — `/api/internal/search?query=…`
   - 로컬 `regulations/` 및/또는 MCP 서버에서 규정을 찾아 조문 단위로 반환.
   - 응답: `{ success, structured[]|text, name_matches[], semantic[], semantic_available }`.
   - 프론트 `renderInternalResults()` 가 규정 카드 + 매칭 조문 + 결과 요약 헤더로 렌더.
2. **국가법령 검색/조회** — `/api/search`, `/api/law/articles`, `/api/law/history` 등
   - 법제처 DRF API를 프록시(서버에서 OC 키로 호출, 인증서 이슈 회피).
3. **내규↔법령 상호참조**
   - 프론트 `applyXref()` 가 내규 본문의 「법령명」·「제N조」를 감지해 링크화.
   - 법령 화면에서는 관련 내규를 교차 패널로 표시.
4. **실무 시나리오** — `/api/scenario` (POST)
   - 업무 질문 + 관련 법령/내규 컨텍스트를 AI에 전달해 근거 조문 기반 답변 생성.

## 검색 관련도 모델(내규)
프론트 `renderInternalResults()` 의 정렬 기준:
- **티어(`_rank`)**: 0 규정명 완전일치 → 1 규정명 부분일치 → 2 본문 키워드 매칭 → 3 의미검색.
- **밀도(`_rel`)**: `출현횟수 / √(문서길이)`. 거대 문서(별표·직무표 등)가 우연한
  매칭으로 상위를 차지하지 않도록 정규화. 별표·별지·부칙은 밀도 계산에서 제외
  (`_stripApxForRel`).
- 동률이면 매칭 조문 수로 비교.

## 주요 API 라우트(발췌)
| 경로 | 용도 |
|---|---|
| `GET /api/search` | 국가법령 키워드 검색 |
| `GET /api/law/articles` | 법령 조문 본문 |
| `GET /api/law/history`, `/api/law/amendments`, `/api/law/art_history` | 개정이력 |
| `POST /api/scenario` | 실무 시나리오 AI 답변 |
| `GET /api/internal/search` | 내규 검색(조문 단위) |
| `GET /api/internal/list` | 내규 전체 목록(대시보드) |
| `GET /api/internal/doc`, `/api/internal/original` | 내규 본문·원문 |
| `GET /api/internal/semantic` | 의미 검색 |
| `POST /api/regs/upload` | 개정 내규 업로드 |
| `GET /api/ping`, `/api/ai/models` | 상태·모델 목록 |

전체 라우트는 `templates/api_server.py` 의 `@app.route` 를 참고하세요.
