import os
import json
import asyncio
import uuid
import hashlib
import jwt
from datetime import datetime
from typing import List, Dict
from collections import deque
from urllib.parse import urlencode
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
import requests
import websockets

load_dotenv()

app = FastAPI(title="Bithumb Scalping Bot")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Config
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BITHUMB_ACCESS_KEY = os.getenv("BITHUMB_API_KEY")
BITHUMB_SECRET_KEY = os.getenv("BITHUMB_SECRET_KEY")
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"
MARKET = os.getenv("MARKET", "KRW-SOL")

# Algorithms
ALGORITHMS = {
    "rsi_extreme": {
        "name": "RSI Extreme Long",
        "description": "RSI(14) < 32 + 거래량 비율 > 1.3. 손절 0.25% / 익절 0.60%.",
        "enabled": True,
        "backtest": {
            "return": "0.25~0.38%",
            "win_rate": "71~83%",
            "trades": "6~7회",
            "note": "가장 안정적"
        }
    },
    "rsi_tight": {
        "name": "RSI Tight",
        "description": "RSI < 32 + 거래량 > 1.4",
        "enabled": False,
        "backtest": {
            "return": "0.50%",
            "win_rate": "100%",
            "trades": "4회",
            "note": "샘플 적음"
        }
    },
    "rsi_aggressive": {
        "name": "RSI Aggressive",
        "description": "RSI < 28 + 거래량 > 1.3",
        "enabled": False,
        "backtest": {
            "return": "0.27%",
            "win_rate": "80%",
            "trades": "5회",
            "note": "SOL 양호"
        }
    }
}

trade_history: List[Dict] = []
active_positions: Dict = {}
enabled_algos: List[str] = ["rsi_extreme"]

price_history = deque(maxlen=50)
volume_history = deque(maxlen=50)
last_message_time = datetime.now()
ws_connected = False

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or "your_chat_id" in str(TELEGRAM_CHAT_ID):
        print(f"[Telegram] {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")

def create_jwt(query: dict = None):
    """빗썸 JWT 생성 (수정 버전)"""
    if not BITHUMB_ACCESS_KEY or not BITHUMB_SECRET_KEY:
        return None
    
    payload = {
        "access_key": BITHUMB_ACCESS_KEY,
        "nonce": str(uuid.uuid4()),
        "timestamp": int(datetime.now().timestamp() * 1000)
    }
    
    if query:
        query_string = urlencode(query, doseq=True).encode()
        m = hashlib.sha512()
        m.update(query_string)
        payload["query_hash"] = m.hexdigest()
        payload["query_hash_alg"] = "SHA512"
    
    return jwt.encode(payload, BITHUMB_SECRET_KEY, algorithm="HS256")

def place_order(side: str, price: float, volume: float = 0.001):
    """실제 주문 (PAPER_TRADING=false일 때만)"""
    if PAPER_TRADING:
        print(f"[PAPER] {side} {MARKET} @ {price}")
        return {"status": "paper"}
    
    # 실제 주문 예시 (시장가)
    query = {
        "market": MARKET,
        "side": side.lower(),
        "ord_type": "price" if side == "BUY" else "market",
        "price": str(int(price * volume)) if side == "BUY" else None,
        "volume": str(volume) if side == "SELL" else None,
    }
    query = {k: v for k, v in query.items() if v is not None}
    
    token = create_jwt(query)
    if not token:
        return {"error": "API 키 없음"}
    
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.post("https://api.bithumb.com/v1/orders", json=query, headers=headers, timeout=10)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}

def get_balance():
    """KRW 사용 가능 잔고 조회"""
    if PAPER_TRADING or not BITHUMB_ACCESS_KEY or not BITHUMB_SECRET_KEY:
        return {"krw": 0, "available": 0, "mode": "paper"}
    
    token = create_jwt()
    if not token:
        return {"error": "JWT 생성 실패", "mode": "error"}
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    try:
        resp = requests.get(
            "https://api.bithumb.com/v1/accounts",
            headers=headers,
            timeout=10
        )
        
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:300]}", "mode": "error"}
        
        data = resp.json()
        
        if isinstance(data, dict) and "error" in data:
            return {"error": str(data["error"]), "mode": "error"}
        
        if not isinstance(data, list):
            return {"error": f"응답 형식 오류: {str(data)[:200]}", "mode": "error"}
        
        krw_item = next((item for item in data if isinstance(item, dict) and item.get("currency") == "KRW"), None)
        
        if not krw_item:
            return {"krw": 0, "available": 0, "mode": "live", "note": "KRW 잔고 없음"}
        
        balance = float(krw_item.get("balance", 0))
        locked = float(krw_item.get("locked", 0))
        
        return {
            "krw": balance,
            "available": max(balance - locked, 0),
            "mode": "live"
        }
        
    except Exception as e:
        return {"error": str(e), "mode": "error"}
    
def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

async def process_trade(data):
    global last_message_time
    last_message_time = datetime.now()
    
    price = float(data["trade_price"])
    volume = float(data["trade_volume"])
    price_history.append(price)
    volume_history.append(volume)
    
    if len(price_history) < 20:
        return
    
    rsi = calculate_rsi(list(price_history))
    avg_vol = sum(volume_history) / len(volume_history)
    vol_ratio = volume / avg_vol if avg_vol > 0 else 1.0
    
    if "rsi_extreme" in enabled_algos and rsi < 32 and vol_ratio > 1.3:
        await execute_signal("BUY", price, "RSI Extreme Long")

async def execute_signal(side: str, price: float, algo: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 주문 실행
    order_result = place_order(side, price)
    
    trade = {
        "time": now,
        "market": MARKET,
        "side": side,
        "price": price,
        "algo": algo,
        "paper": PAPER_TRADING,
        "order_result": str(order_result)
    }
    trade_history.append(trade)
    active_positions[MARKET] = trade
    
    mode = "페이퍼" if PAPER_TRADING else "실거래"
    msg = f"🟢 <b>{side} 시그널</b>\n마켓: {MARKET}\n알고리즘: {algo}\n가격: {price:,.0f}원\n모드: {mode}"
    send_telegram(msg)
    print(msg)

async def bithumb_websocket():
    global ws_connected
    uri = "wss://ws-api.bithumb.com/websocket/v1"
    subscribe = json.dumps([
        {"ticket": "scalping_bot"},
        {"type": "trade", "codes": [MARKET]},
        {"format": "DEFAULT"}
    ])
    retry_delay = 5
    
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10, close_timeout=5) as ws:
                await ws.send(subscribe)
                ws_connected = True
                print(f"WebSocket 연결됨: {MARKET}")
                send_telegram(f"🟢 WebSocket 연결됨 ({MARKET})")
                retry_delay = 5
                
                while True:
                    raw = await ws.recv()
                    data = json.loads(raw)
                    if data.get("type") == "trade":
                        await process_trade(data)
                        
        except Exception as e:
            ws_connected = False
            print(f"WebSocket 오류: {e}")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 60)

async def watchdog():
    while True:
        await asyncio.sleep(30)
        if ws_connected and (datetime.now() - last_message_time).seconds > 90:
            print("90초간 데이터 없음")
            send_telegram("⚠️ 90초간 체결 데이터 없음")

@app.on_event("startup")
async def startup():
    asyncio.create_task(bithumb_websocket())
    asyncio.create_task(watchdog())
    send_telegram("🤖 스캘핑 봇 시작됨")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    balance = get_balance()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "algorithms": ALGORITHMS,
        "enabled": enabled_algos,
        "market": MARKET,
        "paper": PAPER_TRADING,
        "positions": active_positions,
        "history": trade_history[-20:][::-1],
        "ws_connected": ws_connected,
        "balance": balance
    })

@app.post("/toggle_algo")
async def toggle_algo(algo_id: str = Form(...), enabled: bool = Form(...)):
    if enabled and algo_id not in enabled_algos:
        enabled_algos.append(algo_id)
    elif not enabled and algo_id in enabled_algos:
        enabled_algos.remove(algo_id)
    ALGORITHMS[algo_id]["enabled"] = enabled
    return {"status": "ok", "enabled": enabled_algos}

@app.get("/api/status")
async def status():
    return {
        "market": MARKET,
        "paper_trading": PAPER_TRADING,
        "ws_connected": ws_connected,
        "enabled_algorithms": enabled_algos,
        "active_positions": active_positions
    }

@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    return templates.TemplateResponse("history.html", {
        "request": request,
        "history": trade_history[::-1]
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)