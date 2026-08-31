---
name: org-legal-search
description: >-
  기관용 "내규(사규) + 국가법령 종합 검색" 웹서비스를 새 기관에 맞게 세팅하는 스킬.
  내규 검색·국가법령(법제처 OpenAPI) 연계·의미(임베딩) 검색·실무 시나리오 AI 답변·
  내규↔법령 상호 참조 링크를 갖춘 단일 페이지 앱(SPA)의 검증된 구조를 템플릿으로 제공한다.
  다른 공공기관·기업이 자체 내규와 소관 법령으로 같은 서비스를 구축·배포하려 할 때 사용한다.
  트리거: "내규 검색 서비스 만들기", "규정 검색 사이트", "법령 검색 포털 구축",
  "org-legal-search", "사규 검색 웹앱", "우리 기관 규정 검색 서비스".
---

# 기관용 내규·국가법령 종합 검색 서비스 (org-legal-search)

한 기관의 **내규(사규)** 와 **국가법령**을 한 화면에서 검색하고, 둘을 상호 링크로
연결해 열람하는 웹서비스의 **범용 템플릿 + 세팅 가이드**입니다. 특정 기관에 종속된
데이터·브랜딩을 모두 제거하고 구조만 남겼습니다.

## 이 스킬을 쓰는 때
- 다른 기관이 자체 내규·소관 법령으로 동일한 검색 서비스를 구축하려는 경우
- 규정 검색 포털, 사규 검색 사이트, 법령 연계 열람 도구를 처음부터 세우려는 경우

원본 서비스 코드는 건드리지 않습니다. 이 폴더의 `templates/` 를 새 리포/프로젝트로
복사한 뒤 아래 순서로 기관에 맞게 채우면 됩니다.

## 핵심 기능(구조로 포함됨)
- **내규 검색**: `regulations/` 의 규정 HTML을 조문 단위로 검색·미리보기, 분류별
  대시보드, 결과 요약 헤더, 관련도 순위.
- **국가법령 연계**: 법제처(law.go.kr) OpenAPI로 법령 본문·조문·개정이력 조회.
- **내규↔법령 상호참조**: 내규 본문의 「법령명」·「제N조」를 자동 링크(`applyXref`),
  법령 화면에서 관련 내규 교차 표시.
- **의미(임베딩) 검색**: 선택. `regulations_vectors.*` 가 있으면 키워드가 달라도
  의미가 가까운 조문을 함께 노출.
- **실무 시나리오 AI**: 업무 상황을 질문하면 근거 법령·조문을 제안(Gemini/Claude/GPT).
- **개정 내규 업로드**(`/upload`): 개정본을 올려 배포에 반영(선택, 토큰 보호).

## templates/ 구성
```
templates/
  index.html            프론트엔드 SPA (검색 UI·대시보드·열람 작업공간) — 인라인 CSS/JS
  api_server.py         Flask 백엔드 (법제처 연계·내규 검색·시나리오 AI·MCP)
  api/index.py          Vercel 서버리스 진입점(WSGI)
  reg_chunks.py         규정 텍스트 청크 유틸
  upload.html           개정 내규 업로드 페이지
  vercel.json           배포 라우팅·CSP 헤더
  requirements.txt      파이썬 의존성
  run_local.py          로컬 실행 스크립트
  .env.example          환경변수 템플릿(키·MCP·업로드 설정)
  scripts/
    build_embeddings.py 의미 검색용 임베딩 생성
    eval_semantic.py    임베딩 품질 간단 평가
  regulations_manifest.json      내규 목록(메타) — 샘플 2건
  regulations/<규정명>/index.html  규정 본문 HTML — 샘플 2건(인사규정·정보보안규정)
```
> 원본의 실제 내규 코퍼스·임베딩 벡터(`regulations_vectors.*`)는 기관 자산이라 제외했습니다.
> 샘플 데이터로 구조만 보여주며, 실제 내규로 교체해서 씁니다.

## 세팅 순서(요약)
1. `templates/` 를 새 프로젝트로 복사.
2. **기관 정보 치환** — 코드 속 `[기관 설정]` 주석 지점을 우리 기관 값으로 교체
   (아래 "커스터마이즈 체크리스트" 또는 `reference/customization.md`).
3. **환경변수 설정** — `.env.example` → `.env` 복사 후 `LAW_OC`(법제처 OC 키) 등 채우기.
4. **내규 데이터 넣기** — `regulations/` 에 규정 HTML, `regulations_manifest.json` 갱신
   (형식: `reference/data-format.md`).
5. **로컬 실행** — `pip install -r requirements.txt && python run_local.py`.
6. (선택) **의미 검색** — `python scripts/build_embeddings.py` 로 벡터 생성.
7. **배포** — Vercel 등에 배포(`reference/deployment.md`).

## 커스터마이즈 체크리스트 (`[기관 설정]` 지점)
`index.html`
- `<title>` / 상단 로고(`ORG`) / 브랜드 문구 / 푸터 기관명
- `DEFAULT_AGENCIES` — 국가법령 빠른검색 소관기관 탭
- `DEFAULT_KEYWORDS` — 기관 탭별 추천 법령 키워드
- `AGENCY_PROFILES` — 기관 소개·대표 시나리오·소관 링크
- `INTERNAL_CATEGORIES` — 내규 검색 바로가기 칩
- `guessOrg()` — 법령명→소관부처 추정 규칙

`api_server.py`
- `LAW_OC` 기본값(비움 → 환경변수 필수)
- CORS 허용 도메인 정규식(`your-app…vercel.app`) 또는 `ALLOWED_ORIGINS`
- `DOMAIN_LAW_MAP` / `CANDIDATE_LAWS` / `CANDIDATE_ADMRUL` — 소관 법령·행정규칙
- `AVAILABLE_LAWS` — 시나리오 AI가 참조할 법령 목록
- 시나리오 AI 시스템 프롬프트의 기관명
- `SAGYU_MCP_URL` — 내규 MCP 서버(선택)
- `_STALE_ORG_TOKENS` — 기관 옛 명칭(통폐합 이력이 있을 때)

`.env.example`
- `LAW_OC`, AI 프로바이더 키, `SAGYU_MCP_URL`, `GITHUB_REPO`, 업로드 토큰 등

## 더 읽을거리
- `reference/architecture.md` — 데이터 흐름·API 계약·검색 관련도 모델
- `reference/data-format.md` — 내규 매니페스트·규정 HTML·임베딩 벡터 스키마
- `reference/customization.md` — 기관별 치환 지점 상세
- `reference/deployment.md` — 로컬 실행·Vercel 배포·환경변수

## 주의
- **비밀정보 금지**: `.env` 실제 키를 커밋하지 마세요(`.env.example` 만 공유).
- **저작권/자산**: 다른 기관의 내규·임베딩을 그대로 재배포하지 마세요. 각 기관은
  자기 내규로 채웁니다.
- 국가법령 원문은 법제처 OpenAPI를 통해 실시간 조회하며 별도 저장하지 않습니다.
