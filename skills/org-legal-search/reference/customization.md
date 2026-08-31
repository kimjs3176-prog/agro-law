# 커스터마이즈 체크리스트

코드 안의 `[기관 설정]` 주석이 붙은 지점을 우리 기관 값으로 교체하면 됩니다.
`grep -rn "\[기관 설정\]" templates/` 로 전부 찾을 수 있습니다.

## index.html (프론트엔드)
| 항목 | 위치(검색어) | 바꿀 것 |
|---|---|---|
| 페이지 제목 | `<title>` | 기관명 포함 서비스명 |
| 상단 로고 | `class="logo"` 의 `ORG` | 기관 약칭/로고 텍스트 |
| 브랜드 문구 | `class="brand-full"` / `brand-short` | 서비스 명칭 |
| 푸터 | `Powered by` 위 `우리 기관` | 기관명·연도 |
| 소관기관 탭 | `DEFAULT_AGENCIES` | 국가법령 빠른검색 기관 목록(`id`/`name`) |
| 추천 키워드 | `DEFAULT_KEYWORDS` | 기관 탭별 추천 법령명 |
| 기관 프로필 | `AGENCY_PROFILES` | 소개·대표 시나리오·홈페이지 링크 |
| 내규 바로가기 | `INTERNAL_CATEGORIES` | 자주 찾는 내규 주제 칩 |
| 소관부처 추정 | `function guessOrg` | 법령명→소관부처 규칙 |

## api_server.py (백엔드)
| 항목 | 위치(검색어) | 바꿀 것 |
|---|---|---|
| 법제처 OC 키 | `LAW_OC` | 환경변수로 주입(코드 기본값은 비움) |
| CORS 도메인 | `your-app` 정규식 | 배포 도메인 또는 `ALLOWED_ORIGINS` 환경변수 |
| 도메인→법령 | `DOMAIN_LAW_MAP` | 업무 키워드→소관 법령 매핑 |
| 후보 법령 | `CANDIDATE_LAWS` | 조문 검색 대상 핵심 법령 |
| 후보 행정규칙 | `CANDIDATE_ADMRUL` | 소관 훈령·예규(없으면 빈 목록) |
| 시나리오 법령목록 | `AVAILABLE_LAWS` | AI가 참조할 법령 목록 |
| AI 시스템 프롬프트 | `법무·규정 담당 전문가` | 기관명 |
| 내규 MCP | `SAGYU_MCP_URL` | 내규 검색 MCP 엔드포인트(선택) |
| 옛 기관명 | `_STALE_ORG_TOKENS` | 통폐합·개명 이력이 있으면 옛 명칭 토큰 |

## .env / .env.example (환경변수)
- `LAW_OC` — 법제처 OpenAPI OC 키(**필수**). https://open.law.go.kr 에서 신청.
- `SCENARIO_AI_PROVIDER` + `GEMINI_API_KEY`/`CLAUDE_API_KEY`/`OPENAI_API_KEY` — 시나리오 AI(선택).
- `SAGYU_MCP_URL` — 내규 MCP 서버(선택). 비우면 로컬 `regulations/` 만 사용.
- `GITHUB_TOKEN`/`GITHUB_REPO`/`GITHUB_BRANCH` — 업로드분 자동 커밋(선택).
- `REG_UPLOAD_TOKEN` — 업로드 페이지 암호(선택).
- `ALLOWED_ORIGINS`, `DEBUG_ENDPOINTS` — 운영/보안(선택).

## 데이터
- `regulations_manifest.json` — 우리 기관 내규 목록으로 교체(형식: `data-format.md`).
- `regulations/<규정명>/index.html` — 규정 본문 HTML 배치.
- (선택) `python scripts/build_embeddings.py` 로 `regulations_vectors.*` 생성.
