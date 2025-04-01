import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime
from functools import wraps
from typing import Dict, List, Set, Tuple

from bpx.bpx import *
from bpx.bpx_pub import *
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

        self.base_currency = symbol.split("_")[0]
        self.quote_currency = symbol.split("_")[1]

        self.bpx = BpxClient()
        self.bpx.init(api_key=api_key, api_secret=api_secret)

        self.grid_prices = self.create_grid()

        self.quantity_per_grid = total_investment / num_grids

        self.active_orders: Dict[str, Dict] = {}  # orderId -> order_info
        self.order_grid_mapping: Dict[str, int] = {}  # orderId -> grid_index

        self.history_file = f"trade_history_{symbol}.json"
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
            market_depth = Depth(self.symbol)
            current_price = round(
                (float(market_depth["asks"][0][0]) + float(market_depth["bids"][-1][0]))
                / 2,
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
        account_balance = self.bpx.balances()
        base_available = float(account_balance[self.base_currency]["available"])
        quote_available = float(account_balance[self.quote_currency]["available"])
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

        active_orders = self.bpx.ordersQuery(self.symbol)
        if active_orders:
            logger.info(f"成功放置 {len(active_orders)} 个订单")
        else:
            logger.warning("未检测到活跃订单，可能下单失败")

    async def place_buy_order(self, grid_index: int) -> None:
        """放置买单"""
        price = self.grid_prices[grid_index]
        try:
            qty = self.calculate_quantity(price)

            response = self.bpx.ExeOrder(
                symbol=self.symbol,
                side="Bid",
                orderType="Limit",
                timeInForce="",
                quantity=qty,
                price=price,
            )

            if response and isinstance(response, dict) and "id" in response:
                order_id = response["id"]
                self.active_orders[order_id] = {
                    "price": price,
                    "quantity": qty,
                    "side": "Bid",
                    "grid_index": grid_index,
                }
                self.order_grid_mapping[order_id] = grid_index
                logger.info(f"买单已放置: 价格 {price}, 数量 {qty}, 订单ID: {order_id}")
            else:
                logger.warning(f"买单放置可能失败: {response}")
        except Exception as e:
            logger.error(f"放置买单失败: {e}")

    async def place_sell_order(self, grid_index: int) -> None:
        """放置卖单"""
        price = self.grid_prices[grid_index]
        try:
            qty = self.calculate_quantity(price)

            response = self.bpx.ExeOrder(
                symbol=self.symbol,
                side="Ask",
                orderType="Limit",
                timeInForce="",
                quantity=qty,
                price=price,
            )

            if response and isinstance(response, dict) and "id" in response:
                order_id = response["id"]
                self.active_orders[order_id] = {
                    "price": price,
                    "quantity": qty,
                    "side": "Ask",
                    "grid_index": grid_index,
                }
                self.order_grid_mapping[order_id] = grid_index
                logger.info(f"卖单已放置: 价格 {price}, 数量 {qty}, 订单ID: {order_id}")
            else:
                logger.warning(f"卖单放置可能失败: {response}")
        except Exception as e:
            logger.error(f"放置卖单失败: {e}")

    def calculate_quantity(self, price: float) -> float:
        """计算订单数量，确保格式正确"""
        qty = int(self.quantity_per_grid / price * 100) / 100
        return qty

    async def cancel_all_orders(self) -> None:
        """取消所有订单"""
        try:
            self.bpx.ordersCancel(self.symbol)
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
