import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime
from functools import wraps
from typing import Dict, List, Set, Tuple

import ccxt.async_support as ccxt  # Use the async version of ccxt
from dotenv import load_dotenv
from requests.exceptions import ConnectionError, RequestException, Timeout

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("grid_trading.log"), logging.StreamHandler()],
)
logger = logging.getLogger("grid_trading")

# 全局变量
running = True
trade_history = []
total_profit_loss = 0
total_fee = 0

# 网络错误重试装饰器
def retry_on_network_error(max_retries=5, backoff_factor=1.5, max_backoff=60):
    """网络错误重试装饰器"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            retries = 0
            backoff = 1
            while retries < max_retries:
                try:
                    return await func(*args, **kwargs)
                except (RequestException, ConnectionError, Timeout) as e:
                    retries += 1
                    if retries >= max_retries:
                        logger.error(f"达到最大重试次数 {max_retries}，操作失败: {e}")
                        raise
                    backoff = min(backoff * backoff_factor, max_backoff)
                    logger.warning(
                        f"网络错误: {e}. 将在 {backoff:.1f} 秒后重试 (尝试 {retries}/{max_retries})"
                    )
                    await asyncio.sleep(backoff)
            raise Exception("重试机制逻辑错误")
        return wrapper
    return decorator

class GridTradingBot:
    """网格交易机器人类"""
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        symbol: str,
        lower_price: float,
        upper_price: float,
        num_grids: int,
        total_investment: float,
        pair_accuracy: int = 2,
        fee_rate: float = 0.0008,
    ):
        """
        初始化网格交易机器人
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.symbol = symbol
        self.lower_price = lower_price
        self.upper_price = upper_price
        self.num_grids = num_grids
        self.total_investment = total_investment
        self.pair_accuracy = pair_accuracy
        self.fee_rate = fee_rate

        self.base_currency = symbol.split("/")[0]  # ccxt uses '/' (e.g., "SOL/USDC")
        self.quote_currency = symbol.split("/")[1]

        # Initialize ccxt.backpack exchange
        self.exchange = ccxt.backpack({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
        })

        self.grid_prices = self.create_grid()
        self.quantity_per_grid = total_investment / num_grids
        self.active_orders: Dict[str, Dict] = {}  # orderId -> order_info
        self.order_grid_mapping: Dict[str, int] = {}  # orderId -> grid_index
        self.history_file = f"trade_history_{symbol.replace('/', '_')}.json"
        self.load_trade_history()

        logger.info(f"网格交易机器人初始化完成: {symbol}, 价格范围: {lower_price}-{upper_price}, 网格数: {num_grids}")

    def create_grid(self) -> List[float]:
        """创建价格网格"""
        grid_size = (self.upper_price - self.lower_price) / self.num_grids
        return [
            round(self.lower_price + i * grid_size, self.pair_accuracy)
            for i in range(self.num_grids + 1)
        ]

    @retry_on_network_error(max_retries=5, backoff_factor=1.5)
    async def get_market_price(self) -> float:
        """获取当前市场价格"""
        try:
            ticker = await self.exchange.fetch_ticker(self.symbol)
            current_price = round(
                (ticker['ask'] + ticker['bid']) / 2,
                self.pair_accuracy,
            )
            return current_price
        except Exception as e:
            logger.error(f"获取市场价格失败: {e}")
            if hasattr(self, "last_price") and self.last_price:
                return self.last_price
            raise

    @retry_on_network_error(max_retries=5, backoff_factor=1.5)
    async def get_account_balance(self) -> Tuple[float, float]:
        """获取账户余额"""
        balance = await self.exchange.fetch_balance()
        base_available = float(balance[self.base_currency].get('free', 0))
        quote_available = float(balance[self.quote_currency].get('free', 0))
        return base_available, quote_available

    async def place_grid_orders(self) -> None:
        """在网格上放置买卖订单"""
        await self.cancel_all_orders()

        current_price = await self.get_market_price()
        self.last_price = current_price
        logger.info(f"当前市场价格: {current_price}")

        current_grid_index = 0
        for i, price in enumerate(self.grid_prices):
            if price > current_price:
                current_grid_index = i - 1
                break

        logger.info(f"当前价格所在网格索引: {current_grid_index}")

        self.active_orders = {}
        self.order_grid_mapping = {}

        buy_tasks = []
        for i in range(current_grid_index, -1, -1):
            buy_tasks.append(self.place_buy_order(i))

        sell_tasks = []
        for i in range(current_grid_index + 1, len(self.grid_prices)):
            sell_tasks.append(self.place_sell_order(i))

        await asyncio.gather(*buy_tasks, *sell_tasks)

        active_orders = await self.exchange.fetch_open_orders(self.symbol)
        if active_orders:
            logger.info(f"成功放置 {len(active_orders)} 个订单")
        else:
            logger.warning("未检测到活跃订单，可能下单失败")

    async def place_buy_order(self, grid_index: int) -> None:
        """放置买单"""
        price = self.grid_prices[grid_index]
        try:
            qty = self.calculate_quantity(price)
            order = await self.exchange.create_limit_buy_order(
                self.symbol, qty, price
            )
            order_id = order['id']
            self.active_orders[order_id] = {
                "price": price,
                "quantity": qty,
                "side": "buy",
                "grid_index": grid_index,
            }
            self.order_grid_mapping[order_id] = grid_index
            logger.info(f"买单已放置: 价格 {price}, 数量 {qty}, 订单ID: {order_id}")
        except Exception as e:
            logger.error(f"放置买单失败: {e}")

    async def place_sell_order(self, grid_index: int) -> None:
        """放置卖单"""
        price = self.grid_prices[grid_index]
        try:
            qty = self.calculate_quantity(price)
            order = await self.exchange.create_limit_sell_order(
                self.symbol, qty, price
            )
            order_id = order['id']
            self.active_orders[order_id] = {
                "price": price,
                "quantity": qty,
                "side": "sell",
                "grid_index": grid_index,
            }
            self.order_grid_mapping[order_id] = grid_index
            logger.info(f"卖单已放置: 价格 {price}, 数量 {qty}, 订单ID: {order_id}")
        except Exception as e:
            logger.error(f"放置卖单失败: {e}")

    def calculate_quantity(self, price: float) -> float:
        """计算订单数量，确保格式正确"""
        qty = int(self.quantity_per_grid / price * 100) / 100
        return qty

    async def cancel_all_orders(self) -> None:
        """取消所有订单"""
        try:
            await self.exchange.cancel_all_orders(self.symbol)
            logger.info(f"已取消所有 {self.symbol} 订单")
        except Exception as e:
            logger.error(f"取消订单失败: {e}")

    def load_trade_history(self) -> None:
        """加载历史交易记录"""
        global trade_history, total_profit_loss, total_fee
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r") as f:
                    data = json.load(f)
                    trade_history = data.get("trades", [])
                    total_profit_loss = data.get("total_profit_loss", 0)
                    total_fee = data.get("total_fee", 0)
                    logger.info(f"已加载历史交易记录: {len(trade_history)}笔交易")
                    logger.info(
                        f"累计盈亏: {total_profit_loss:.4f} USDC, 累计手续费: {total_fee:.4f} USDC"
                    )
            except Exception as e:
                logger.error(f"加载历史交易记录失败: {e}")

    # Add the rest of the methods (save_trade_history, process_new_trades, etc.) as needed...

async def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    load_dotenv()

    api_key = os.getenv("API_KEY")
    api_secret = os.getenv("API_SECRET")

    if not api_key or not api_secret:
        logger.error("错误: API密钥未正确加载，请检查.env文件")
        sys.exit(1)

    symbol = os.getenv("TRADING_PAIR", "SOL/USDC")  # ccxt uses '/' instead of '_'
    pair_accuracy = int(os.getenv("PAIR_ACCURACY", "2"))

    try:
        exchange = ccxt.backpack({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
        })
        ticker = await exchange.fetch_ticker(symbol)
        current_price = round(
            (ticker['ask'] + ticker['bid']) / 2,
            pair_accuracy,
        )
        logger.info(f"当前市场价格: {current_price}")

        grid_range_percent = float(os.getenv("GRID_RANGE_PERCENT", "5"))
        lower_price = round(
            current_price * (1 - grid_range_percent / 100), pair_accuracy
        )
        upper_price = round(
            current_price * (1 + grid_range_percent / 100), pair_accuracy
        )
    except Exception as e:
        logger.error(f"获取市场价格失败: {e}")
        lower_price = float(os.getenv("GRID_LOWER_PRICE", "60"))
        upper_price = float(os.getenv("GRID_UPPER_PRICE", "65"))

    num_grids = int(os.getenv("GRID_NUM", "5"))
    total_investment = float(os.getenv("GRID_INVESTMENT", "100"))
    fee_rate = float(os.getenv("FEE_RATE", "0.0008"))

    bot = GridTradingBot(
        api_key=api_key,
        api_secret=api_secret,
        symbol=symbol,
        lower_price=lower_price,
        upper_price=upper_price,
        num_grids=num_grids,
        total_investment=total_investment,
        pair_accuracy=pair_accuracy,
        fee_rate=fee_rate,
    )

    await bot.run()

if __name__ == "__main__":
    asyncio.run(main())
