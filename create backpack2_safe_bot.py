# backpack2_safe_bot.py
# ===================================
# ✅ Backpack2 空投安全版 成交統計機器人
# ===================================

import ccxt
import time
from decimal import Decimal
from config import *

# ======== 成交統計 ========
total_trades = 0
total_buy_qty = Decimal('0')
total_sell_qty = Decimal('0')
total_fees = Decimal('0')
total_pnl = Decimal('0')

# ======== 初始化交易所 ========
exchange = ccxt.backpack({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True
})

print("✅ 已啟動 Backpack2 安全版成交統計機器人")

# ======== 主循環 ========
while True:
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        last_price = Decimal(str(ticker['last']))

        if GRID_LOWER_PRICE <= last_price <= GRID_UPPER_PRICE:
            qty = Decimal('0.05')  # 假設固定下單數量
            side = 'buy' if total_trades % 2 == 0 else 'sell'

            order = exchange.create_market_order(SYMBOL, side, float(qty))
            
            fee = Decimal(str(order['fee']['cost'])) if 'fee' in order else Decimal('0')
            cost = Decimal(str(order['cost']))
            
            if side == 'buy':
                total_buy_qty += qty
                total_pnl -= cost
            else:
                total_sell_qty += qty
                total_pnl += cost

            total_fees += fee
            total_trades += 1

            print(f"[成交] 第 {total_trades} 筆 | {side.upper()} | 價格: {last_price} | 數量: {qty} | 手續費: {fee} | 累計盈虧: {total_pnl}")

        else:
            print(f"⏳ 監控中 | 現價 {last_price} 已超出設定區間")

    except Exception as e:
        print(f"⚠️ 發生錯誤: {e}")

    time.sleep(MONITOR_INTERVAL_SECONDS)
