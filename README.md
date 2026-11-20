# Bithumb Scalping Bot

RSI 기반 스캘핑 봇 (FastAPI + HTML)

## 기능
- 알고리즘 리스트 + 체크박스로 활성화/비활성화
- 체크한 알고리즘만 실제 적용
- 실시간 모니터링 화면
- 거래 히스토리
- Telegram 매수/매도 알림
- .env로 API 키 및 텔레그램 설정
- 페이퍼 트레이딩 모드 기본 활성화

## 설치 및 실행

```bash
cd trading_bot
pip install -r requirements.txt
cp .env.example .env
# .env 파일 수정 (API 키, Chat ID 등)
uvicorn main:app --reload --host 0.0.0.0 --port 8000