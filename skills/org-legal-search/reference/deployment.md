# 배포 · 실행

## 로컬 실행
```bash
cd templates
python -m venv .venv && source .venv/bin/activate   # 선택
pip install -r requirements.txt
cp .env.example .env        # 그리고 LAW_OC 등 값 채우기
python run_local.py         # http://localhost:5100
```
- `LAW_OC`(법제처 OC 키)가 없으면 국가법령 조회가 동작하지 않습니다.
- 시나리오 AI·의미 검색은 해당 키가 있을 때만 켜집니다(없어도 나머지는 동작).

## Vercel 배포
구조가 이미 Vercel에 맞춰져 있습니다(`vercel.json`, `api/index.py`).
1. `templates/` 내용을 배포용 리포 루트로 둡니다.
2. Vercel 프로젝트 생성 → 이 리포 연결.
3. **환경변수** 등록(Project Settings → Environment Variables): `LAW_OC`,
   `SCENARIO_AI_PROVIDER`, `GEMINI_API_KEY`(등), 필요시 `SAGYU_MCP_URL`,
   `ALLOWED_ORIGINS`, `REG_UPLOAD_TOKEN`, `GITHUB_TOKEN`/`GITHUB_REPO`.
4. 배포. 라우팅:
   - `/api/*` → `api/index.py`(Flask WSGI)
   - `/regulations/*`, `/upload`, `/` → 정적/HTML (CSP·보안 헤더 포함)

## 라우팅·보안 헤더 (`vercel.json`)
- `regulations/**` 정적 서빙에 엄격한 CSP(`script-src 'none'`)로 규정 HTML 내
  스크립트 실행 차단.
- 전 경로에 `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`,
  `Referrer-Policy: strict-origin-when-cross-origin`.

## 의미 검색 벡터 생성(선택)
```bash
cd templates
python scripts/build_embeddings.py     # regulations/ → regulations_vectors.{bin,json}
python scripts/eval_semantic.py        # (선택) 간단 품질 확인
```
Gemini 키(`GEMINI_API_KEY`)가 필요하며, 생성된 벡터 파일이 배포에 포함되면
내규 검색에 의미 결과가 함께 표시됩니다.

## 운영 주의
- 실제 `.env`(키 포함)는 **커밋 금지**. Vercel 환경변수로만 주입.
- 다른 기관 내규·임베딩을 재배포하지 말 것. 각 기관 자산으로 채웁니다.
- 업로드 기능을 열면 반드시 `REG_UPLOAD_TOKEN` 을 설정하세요.
