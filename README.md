# 농업 법령 검색 서비스

법제처 국가법령정보 OpenAPI 연동 · 농업 관련 법령 검색 및 AI 해석

## 파일 구조

```
├── index.html          ← 프론트엔드 (Vercel이 정적 파일로 서빙)
├── api_server.py       ← Flask 백엔드 (모든 /api/* 라우트)
├── api/
│   └── index.py       ← Vercel 서버리스 진입점
├── vercel.json         ← Vercel 배포 설정
├── requirements.txt    ← Python 의존성
├── run_local.py        ← 로컬 실행용
└── .gitignore
```

## 로컬 실행

```bash
pip install -r requirements.txt
python run_local.py
# → http://localhost:5100 자동 오픈
```

## GitHub + Vercel 배포

### 1단계 — GitHub push

```bash
git init
git add .
git commit -m "농업 법령 검색 서비스"
git branch -M main
git remote add origin https://github.com/계정/저장소.git
git push -u origin main
```

### 2단계 — Vercel 설정

1. [vercel.com](https://vercel.com) → **Add New Project**
2. GitHub 저장소 선택 → **Import**
3. **Framework Preset**: `Other`
4. **Environment Variables** 추가:

| 변수명 | 값 |
|---|---|
| `LAW_OC` | `wlghdkgus1234` |
| `ANTHROPIC_API_KEY` | `sk-ant-...` *(AI 해석 선택)* |

5. **Deploy** → 완료 후 URL 발급

### 이후 업데이트

```bash
git add . && git commit -m "수정" && git push
# Vercel 자동 재배포
```

## 환경변수

| 변수 | 설명 | 기본값 |
|---|---|---|
| `LAW_OC` | 법제처 OpenAPI OC 키 | `wlghdkgus1234` |
| `ANTHROPIC_API_KEY` | Claude AI 키 (AI 해석) | 없음 |

## 주의사항

- Vercel Hobby(무료) 플랜: 함수 실행시간 최대 60초
- `index.html`은 Vercel CDN에서 정적으로 서빙됨 → 빠른 초기 로딩
- 즐겨찾기·메모·북마크는 브라우저 `localStorage` 저장 → 배포 환경에서도 정상 동작
