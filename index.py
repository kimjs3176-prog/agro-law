"""
Vercel Python 서버리스 진입점
모든 /api/* 요청을 Flask 앱으로 라우팅합니다.
"""
import sys, os

# 프로젝트 루트를 import 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api_server import app  # noqa: F401  ← Vercel이 이 app을 WSGI로 실행
