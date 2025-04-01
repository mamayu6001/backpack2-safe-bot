---

### ✅ 2️⃣ `config.py`

```python
# config.py

from decimal import Decimal

# ======== API ========
API_KEY = 'YOUR_API_KEY'
API_SECRET = 'YOUR_API_SECRET'

# ======== 參數設定 ========
SYMBOL = 'SOL_USDC'                    # 交易對
GRID_LOWER_PRICE = Decimal('122.68')   # 網格下限價格
GRID_UPPER_PRICE = Decimal('129.41')   # 網格上限價格
GRID_NUM = 20                          # 網格數量
GRID_INVESTMENT = 200                  # 總投入金額（USDC）
STOP_LOSS_PERCENT = 7                  # 止損百分比
MONITOR_INTERVAL_SECONDS = 60          # 監控間隔（秒）
