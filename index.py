"""
Vercel 서버리스 진입점
@vercel/python 런타임이 이 파일의 'app' 변수를 WSGI 앱으로 사용합니다.
"""
import sys, os

# law_search.py가 있는 루트 경로를 import 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from law_search import app   # noqa: F401  ← Vercel이 이 app을 서빙합니다
