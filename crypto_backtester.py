from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from datetime import datetime
import argparse
import pandas as pd
import numpy as np
import requests
import time
import os
import json
import sys


@dataclass
class Zone:
    """Trading zone configuration"""
    price_from: float
    price_to: float
    levels: List[float] = field(default_factory=list)

    def __post_init__(self):
        """Create 5 equal levels in the zone"""
        if not self.levels:
            step = (self.price_from - self.price_to) / 4
            self.levels = [self.price_from - (i * step) for i in range(5)]


@dataclass
class TakeProfit:
    """Take profit configuration"""
    tp_type: str  # 'percent', 'ath', 'trailing'
    value: Optional[float] = None  # percentage value if tp_type is 'percent'


@dataclass
class Asset:
    """Asset configuration"""
    ticker: str
    zones: List[Zone]
    take_profits: List[TakeProfit]
    rebalancing: bool = True
    allocation_per_zone: float = 0.0


@dataclass
class Trade:
    """Single trade record"""
    date: datetime
    asset: str
    action: str  # 'buy' or 'sell'
    price: float
    amount_usd: float
    quantity: float
    level: Optional[int] = None  # zone level for buys, TP level for sells


class Portfolio:
    """Portfolio management"""

    def __init__(self, name: str, deposit: float, max_assets: int):
        self.name = name
        self.deposit = deposit
        self.max_assets = max_assets
        self.allocation_per_asset = deposit / max_assets
        self.assets: List[Asset] = []
        self.free_cash = deposit

    def add_asset(self, asset: Asset) -> bool:
        """Add asset to portfolio"""
        if len(self.assets) >= self.max_assets:
            return False

        # Set allocation per zone for the asset
        asset.allocation_per_zone = (
            self.allocation_per_asset / len(asset.zones)
        )

        self.assets.append(asset)
        self.free_cash -= self.allocation_per_asset
        return True

    def get_asset_by_ticker(self, ticker: str) -> Optional[Asset]:
        """Get asset by ticker"""
        for asset in self.assets:
            if asset.ticker == ticker:
                return asset
        return None


def ensure_utf8_output():
    """Ensure UTF-8 stdout/stderr on Windows to avoid emoji/locale crashes"""
    try:
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        # Best-effort; do not block execution on encoding fixes
        pass


class PortfolioFactory:
    """Create portfolio objects from configuration without interactive input"""

    @staticmethod
    def from_config(config: Dict) -> Portfolio:
        required_fields = ["name", "deposit", "max_assets", "assets"]
        for field_name in required_fields:
            if field_name not in config:
                raise ValueError(
                    f"Config missing required field '{field_name}'"
                )

        portfolio = Portfolio(
            name=config["name"],
            deposit=float(config["deposit"]),
            max_assets=int(config["max_assets"])
        )

        assets_cfg = config.get("assets", [])
        if not isinstance(assets_cfg, list):
            raise ValueError("Config field 'assets' must be a list")

        for asset_cfg in assets_cfg:
            ticker = asset_cfg.get("ticker")
            zones_cfg = asset_cfg.get("zones", [])
            tps_cfg = asset_cfg.get("take_profits", [])
            rebalancing = bool(asset_cfg.get("rebalancing", True))

            if not ticker or not zones_cfg or not tps_cfg:
                raise ValueError(f"Incomplete asset config: {asset_cfg}")

            zones: List[Zone] = []
            for zone_cfg in zones_cfg:
                zones.append(
                    Zone(
                        price_from=float(zone_cfg["price_from"]),
                        price_to=float(zone_cfg["price_to"]),
                        levels=zone_cfg.get("levels", [])
                    )
                )

            take_profits: List[TakeProfit] = []
            for tp_cfg in tps_cfg:
                take_profits.append(
                    TakeProfit(
                        tp_type=tp_cfg["tp_type"],
                        value=tp_cfg.get("value")
                    )
                )

            asset = Asset(
                ticker=ticker.upper(),
                zones=zones,
                take_profits=take_profits,
                rebalancing=rebalancing
            )

            if not portfolio.add_asset(asset):
                raise ValueError(
                    "Asset limit exceeded while building portfolio from config"
                )

        return portfolio


def load_config(path: str) -> Dict:
    """Load JSON config from file"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class Position:
    """Current position in asset"""

    def __init__(self, asset: Asset):
        self.asset = asset
        self.total_invested = 0.0
        self.total_quantity = 0.0
        # investments per zone per level
        self.zone_investments: List[List[float]] = []
        # quantities per zone per level
        self.zone_quantities: List[List[float]] = []
        # sold quantity for each TP level
        self.tp_sold_quantity = [0.0] * 5
        self.stop_loss_active = False
        self.trailing_stop_active = False
        self.stop_loss_price = 0.0

        # Initialize zone tracking
        for zone in asset.zones:
            self.zone_investments.append([0.0] * 5)  # 5 levels per zone
            self.zone_quantities.append([0.0] * 5)

    def get_average_price(self) -> float:
        """Calculate average purchase price"""
        if self.total_quantity == 0:
            return 0.0
        return self.total_invested / self.total_quantity

    def get_current_quantity(self) -> float:
        """Get current quantity (total - sold)"""
        sold_quantity = sum(self.tp_sold_quantity)
        return self.total_quantity - sold_quantity

    def can_buy_at_level(self, zone_idx: int, level_idx: int) -> bool:
        """Check if we can buy at specific zone level"""
        return self.zone_investments[zone_idx][level_idx] == 0.0

    def buy_at_level(
        self, zone_idx: int, level_idx: int, price: float,
        amount_usd: float
    ) -> float:
        """Buy at specific zone level, return quantity bought"""
        quantity = amount_usd / price
        self.zone_investments[zone_idx][level_idx] = amount_usd
        self.zone_quantities[zone_idx][level_idx] = quantity
        self.total_invested += amount_usd
        self.total_quantity += quantity
        return quantity

    def sell_tp_percentage(
        self, tp_level: int, percentage: float, price: float
    ) -> Tuple[float, float]:
        """Sell percentage of position at TP level.
        Return (quantity_sold, usd_received)
        """
        available_quantity = self.get_current_quantity()
        if available_quantity <= 0:
            return 0.0, 0.0

        quantity_to_sell = available_quantity * (percentage / 100.0)
        usd_received = quantity_to_sell * price

        self.tp_sold_quantity[tp_level] += quantity_to_sell

        return quantity_to_sell, usd_received


class CoinGeckoAPI:
    """CoinGecko API implementation for historical data"""

    def __init__(self):
        self.base_url = "https://api.coingecko.com/api/v3"
        self.session = requests.Session()

    def get_coin_id(self, ticker: str) -> str:
        """Get CoinGecko coin ID by ticker"""
        # Remove USDT suffix if present
        clean_ticker = ticker.replace('USDT', '').replace('USD', '').upper()

        # Basic mapping for most popular coins (for faster lookup)
        popular_mapping = {
            'BTC': 'bitcoin',
            'ETH': 'ethereum',
            'BNB': 'binancecoin',
            'ADA': 'cardano',
            'SOL': 'solana',
            'XRP': 'ripple',
            'DOGE': 'dogecoin',
            'DOT': 'polkadot',
            'AVAX': 'avalanche-2',
            'SHIB': 'shiba-inu',
            'MATIC': 'matic-network',
            'LTC': 'litecoin',
            'UNI': 'uniswap',
            'LINK': 'chainlink',
            'TRX': 'tron',
            'NEAR': 'near',
            'ATOM': 'cosmos',
            'XLM': 'stellar',
            'ALGO': 'algorand',
            'VET': 'vechain',
            'FTM': 'fantom',
            'SAND': 'the-sandbox',
            'MANA': 'decentraland'
        }

        if clean_ticker in popular_mapping:
            coin_id = popular_mapping[clean_ticker]
            print(f"🔍 {ticker} -> {clean_ticker} -> {coin_id}")
            return coin_id
        else:
            # For unknown tickers, try to search dynamically
            print(f"🔍 {ticker} -> {clean_ticker} -> searching via API...")

            # Try direct lowercase first
            potential_id = clean_ticker.lower()

            # If ticker looks like it could be a coin ID already, return as-is
            return potential_id

    def get_historical_data(
        self, ticker: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """Get historical data from CoinGecko"""
        coin_id = self.get_coin_id(ticker)

        try:
            # Convert dates to timestamps
            start_ts = int(
                datetime.strptime(start_date, "%Y-%m-%d").timestamp()
            )
            end_ts = int(
                datetime.strptime(end_date, "%Y-%m-%d").timestamp()
            )

            # Try with public API first
            url = f"{self.base_url}/coins/{coin_id}/market_chart/range"
            params = {
                'vs_currency': 'usd',
                'from': start_ts,
                'to': end_ts
            }

            headers = {
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36'
                )
            }

            print(f"📡 Loading data for {coin_id}...")
            response = self.session.get(
                url, params=params, headers=headers, timeout=15
            )

            if response.status_code == 401:
                print("⚠️  API limit reached, trying alternative method...")
                # Try simpler endpoint
                return self._get_simple_prices(coin_id, start_date, end_date)

            response.raise_for_status()
            data = response.json()

            if 'prices' not in data or not data['prices']:
                print(f"❌ No price data for {coin_id}")
                return pd.DataFrame(
                    columns=['date', 'open', 'high', 'low', 'close', 'volume']
                )

            prices = data['prices']

            # Convert to DataFrame
            df_data = []
            for price_point in prices:
                date = datetime.fromtimestamp(price_point[0] / 1000)
                price = price_point[1]

                # Create OHLC from single price point (approximation)
                volatility = 0.02  # 2% daily volatility assumption
                high = price * (1 + volatility)
                low = price * (1 - volatility)

                df_data.append({
                    'date': date,
                    'open': price,
                    'high': high,
                    'low': low,
                    'close': price,
                    'volume': 1000000  # Placeholder volume
                })

            df = pd.DataFrame(df_data)

            # Group by date to get daily data
            df['date'] = df['date'].dt.date
            daily_df = df.groupby('date').agg({
                'open': 'first',
                'high': 'max',
                'low': 'min',
                'close': 'last',
                'volume': 'sum'
            }).reset_index()

            daily_df['date'] = pd.to_datetime(daily_df['date'])
            daily_df = daily_df.sort_values('date')

            print(f"✅ Loaded {len(daily_df)} days of data for {ticker}")
            return daily_df

        except Exception as e:
            print(f"❌ Error loading data for {ticker}: {str(e)}")
            print("🔄 Trying to generate demo data...")
            return self._generate_demo_data(ticker, start_date, end_date)

        # Rate limiting
        time.sleep(0.2)

    def _get_simple_prices(
        self, coin_id: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """Alternative method using simple price endpoint"""
        try:
            url = f"{self.base_url}/simple/price"
            params = {
                'ids': coin_id,
                'vs_currencies': 'usd',
                'include_24hr_change': 'true'
            }

            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()

            data = response.json()
            if coin_id in data:
                current_price = data[coin_id]['usd']
                print(f"📊 Current price for {coin_id}: ${current_price}")

                # Generate historical data based on current price
                return self._generate_demo_data_from_price(
                    coin_id, current_price, start_date, end_date
                )

        except Exception as e:
            print(f"⚠️  Alternative method failed: {str(e)}")

        return pd.DataFrame(
            columns=['date', 'open', 'high', 'low', 'close', 'volume']
        )

    def _generate_demo_data(
        self, ticker: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """Generate demo data for testing"""
        print(f"🎲 Generating demo data for {ticker}...")

        # Historical price ranges for different assets (April 2022 levels)
        historical_ranges = {
            'ATOM': {'start': 12.0, 'low': 4.5, 'high': 40.0},
            'SOL': {'start': 90.0, 'low': 8.0, 'high': 260.0},
            'BTC': {'start': 45000.0, 'low': 15500.0, 'high': 69000.0},
            'ETH': {'start': 3000.0, 'low': 880.0, 'high': 4800.0},
            'DOGE': {'start': 0.14, 'low': 0.049, 'high': 0.74},
            'SHIB': {'start': 0.000024, 'low': 0.000006, 'high': 0.000088}
        }

        clean_ticker = ticker.replace('USDT', '').replace('USD', '').upper()

        if clean_ticker in historical_ranges:
            price_info = historical_ranges[clean_ticker]
            print(
                f"📊 Using historical price range for {clean_ticker}: "
                f"${price_info['low']:.4f} - ${price_info['high']:.4f}"
            )
            return self._generate_realistic_demo_data(
                ticker, price_info, start_date, end_date
            )
        else:
            # For unknown assets, use current price
            base_price = 10.0
            return self._generate_demo_data_from_price(
                ticker, base_price, start_date, end_date
            )

    def _generate_demo_data_from_price(
        self, ticker: str, base_price: float, start_date: str,
        end_date: str
    ) -> pd.DataFrame:
        """Generate demo data from base price"""
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        dates = pd.date_range(start=start, end=end, freq='D')

        # Generate price movement with trend and volatility
        np.random.seed(42)  # For reproducible results

        prices = []
        current_price = base_price

        for i, date in enumerate(dates):
            # Add some trend and volatility
            # 0.1% daily return, 3% volatility
            daily_return = np.random.normal(0.001, 0.03)
            current_price *= (1 + daily_return)

            # Create OHLC
            volatility = abs(np.random.normal(0, 0.02))
            high = current_price * (1 + volatility)
            low = current_price * (1 - volatility)
            open_price = current_price * (1 + np.random.normal(0, 0.01))

            prices.append({
                'date': date,
                'open': open_price,
                'high': high,
                'low': low,
                'close': current_price,
                'volume': np.random.randint(1000000, 10000000)
            })

        df = pd.DataFrame(prices)
        print(f"✅ Generated {len(df)} days of demo data for {ticker}")

        return df

    def _test_ticker_validity(self, ticker: str) -> pd.DataFrame:
        """Quick test to check if ticker is valid through API"""
        coin_id = self.get_coin_id(ticker)

        try:
            # Try simple price endpoint first
            url = f"{self.base_url}/simple/price"
            params = {
                'ids': coin_id,
                'vs_currencies': 'usd'
            }

            headers = {
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36'
                )
            }

            response = self.session.get(
                url, params=params, headers=headers, timeout=5
            )

            if response.status_code == 200:
                data = response.json()
                if coin_id in data and 'usd' in data[coin_id]:
                    current_price = data[coin_id]['usd']
                    print(f"💰 Current price {ticker}: ${current_price}")

                    # Return simple DataFrame to indicate success
                    return pd.DataFrame([{
                        'date': datetime.now(),
                        'close': current_price,
                        'high': current_price,
                        'low': current_price,
                        'open': current_price,
                        'volume': 1
                    }])
            elif response.status_code == 404:
                print(f"❌ Ticker {ticker} not found in CoinGecko")
                return pd.DataFrame()
            else:
                print(f"⚠️  API returned code {response.status_code}")
                return pd.DataFrame()

        except Exception as e:
            print(f"⚠️  Error checking via API: {str(e)}")

        return pd.DataFrame()


class BinanceAPI:
    """Real Binance API implementation"""

    def __init__(self):
        self.base_url = "https://api.binance.com/api/v3"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36'
            )
        })

    def test_ticker_validity(self, ticker: str) -> bool:
        """Test if ticker exists on Binance"""
        try:
            # Normalize ticker
            if not ticker.endswith('USDT'):
                ticker = ticker + 'USDT'

            url = f"{self.base_url}/ticker/24hr"
            params = {'symbol': ticker}

            response = self.session.get(url, params=params, timeout=10)

            if response.status_code == 200:
                data = response.json()
                price = float(data['lastPrice'])
                print(f"Current price {ticker}: ${price:.4f}")
                return True
            elif response.status_code == 400:
                # Try without USDT suffix
                base_ticker = ticker.replace('USDT', '')
                if len(base_ticker) != len(ticker):  # Had USDT suffix
                    return self.test_ticker_validity(base_ticker)
                return False
            else:
                return False

        except Exception as e:
            print(f"API Error: {str(e)}")
            return False

    def get_historical_data(
        self, ticker: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """Get historical OHLCV data from Binance"""
        try:
            # Normalize ticker
            if not ticker.endswith('USDT'):
                ticker = ticker + 'USDT'

            # Convert dates to timestamps
            start_ts = int(
                datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000
            )
            end_ts = int(
                datetime.strptime(end_date, "%Y-%m-%d").timestamp() * 1000
            )

            url = f"{self.base_url}/klines"

            all_data = []
            current_start = start_ts

            # Binance limits to 1000 candles per request
            while current_start < end_ts:
                params = {
                    'symbol': ticker,
                    'interval': '1d',
                    'startTime': current_start,
                    'endTime': end_ts,
                    'limit': 1000
                }

                print(f"Loading data for {ticker}...")
                response = self.session.get(url, params=params, timeout=15)

                if response.status_code != 200:
                    print(f"API Error {response.status_code}: {response.text}")
                    break

                data = response.json()

                if not data:
                    break

                all_data.extend(data)

                # Update start time for next request
                if len(data) == 1000:
                    # Next millisecond after last close time
                    current_start = data[-1][6] + 1
                else:
                    break

                time.sleep(0.1)  # Rate limiting

            if not all_data:
                print(f"No data received for {ticker}")
                return pd.DataFrame()

            # Convert to DataFrame
            df_data = []
            for candle in all_data:
                df_data.append({
                    'date': datetime.fromtimestamp(candle[0] / 1000),
                    'open': float(candle[1]),
                    'high': float(candle[2]),
                    'low': float(candle[3]),
                    'close': float(candle[4]),
                    'volume': float(candle[5])
                })

            df = pd.DataFrame(df_data)
            df = df.sort_values('date').reset_index(drop=True)

            print(f"Loaded {len(df)} days for {ticker}")
            return df

        except Exception as e:
            print(f"Error loading {ticker}: {str(e)}")
            return pd.DataFrame()

    def get_popular_tickers(self) -> list:
        """Get list of popular USDT pairs"""
        try:
            url = f"{self.base_url}/ticker/24hr"
            response = self.session.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()

                # Filter USDT pairs and sort by volume
                usdt_pairs = [
                    item for item in data if item['symbol'].endswith('USDT')
                ]
                usdt_pairs = sorted(
                    usdt_pairs,
                    key=lambda x: float(x['quoteVolume']),
                    reverse=True
                )

                # Return top 50 by volume
                return [pair['symbol'] for pair in usdt_pairs[:50]]
            else:
                # Fallback list
                return [
                    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'ADAUSDT',
                    'XRPUSDT', 'DOGEUSDT', 'DOTUSDT', 'AVAXUSDT', 'MATICUSDT'
                ]

        except Exception as e:
            print(f"Error getting tickers: {str(e)}")
            return ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'ATOMUSDT', 'DOGEUSDT']


class DataProvider:
    """Data provider for historical prices"""

    def __init__(self):
        self.api = BinanceAPI()
        self.cache = {}

    def get_historical_data(
        self, ticker: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """Get historical OHLCV data for ticker"""
        cache_key = f"{ticker}_{start_date}_{end_date}"

        if cache_key in self.cache:
            return self.cache[cache_key]

        df = self.api.get_historical_data(ticker, start_date, end_date)
        self.cache[cache_key] = df

        return df


class TechnicalAnalysis:
    """Technical analysis calculations"""

    @staticmethod
    def calculate_ma200(df: pd.DataFrame) -> pd.Series:
        """Calculate 200-period moving average"""
        return df['close'].rolling(window=200).mean()

    @staticmethod
    def get_structural_high(df: pd.DataFrame) -> float:
        """Get ATH (structural high)"""
        return df['high'].max()

    @staticmethod
    def get_structural_low_ma200(df: pd.DataFrame) -> float:
        """Get structural low based on MA200 close below"""
        ma200 = TechnicalAnalysis.calculate_ma200(df)
        closes_below_ma = df[df['close'] < ma200]
        if closes_below_ma.empty:
            return df['low'].min()
        return closes_below_ma['low'].min()

    @staticmethod
    def get_monthly_low(df: pd.DataFrame, date: datetime) -> float:
        """Get minimum low for the month"""
        month_start = date.replace(day=1)
        month_data = df[df['date'] >= month_start]
        if month_data.empty:
            return df['low'].min()
        return month_data['low'].min()


class TradeLogger:
    """Trade logging and statistics"""

    def __init__(self):
        self.trades: List[Trade] = []

    def log_trade(self, trade: Trade):
        """Log a trade"""
        self.trades.append(trade)

    def get_trades_df(self) -> pd.DataFrame:
        """Get trades as DataFrame"""
        if not self.trades:
            return pd.DataFrame()

        trades_data = []
        for trade in self.trades:
            trades_data.append({
                'date': trade.date,
                'asset': trade.asset,
                'action': trade.action,
                'price': trade.price,
                'amount_usd': trade.amount_usd,
                'quantity': trade.quantity,
                'level': trade.level
            })

        return pd.DataFrame(trades_data)

    def get_statistics(self, portfolio: Portfolio) -> Dict:
        """Calculate portfolio statistics"""
        stats = {
            'total_trades': len(self.trades),
            'buy_trades': len([t for t in self.trades if t.action == 'buy']),
            'sell_trades': len([t for t in self.trades if t.action == 'sell']),
            'total_invested': sum(
                t.amount_usd for t in self.trades if t.action == 'buy'
            ),
            'total_received': sum(
                t.amount_usd for t in self.trades if t.action == 'sell'
            ),
            'realized_profit': (
                sum(t.amount_usd for t in self.trades if t.action == 'sell') -
                sum(t.amount_usd for t in self.trades if t.action == 'buy')
            ),
            'free_cash': portfolio.free_cash
        }
        return stats


class BacktestEngine:
    """Main backtesting engine"""

    def __init__(self, portfolio: Portfolio, data_provider: DataProvider):
        self.portfolio = portfolio
        self.data_provider = data_provider
        self.positions: Dict[str, Position] = {}
        self.trade_logger = TradeLogger()
        self.technical_analysis = TechnicalAnalysis()

    def initialize_positions(self):
        """Initialize positions for all assets"""
        for asset in self.portfolio.assets:
            self.positions[asset.ticker] = Position(asset)

    def run_backtest(
        self, start_date: str = "2022-04-01", end_date: str = "2025-09-01"
    ):
        """Run the backtest"""
        print(f"Starting backtest from {start_date} to {end_date}")
        self.initialize_positions()

        for asset in self.portfolio.assets:
            print(f"Processing {asset.ticker}...")
            self._process_asset(asset, start_date, end_date)

        print("Backtest completed!")
        return self.trade_logger.get_statistics(self.portfolio)

    def _process_asset(self, asset: Asset, start_date: str, end_date: str):
        """Process single asset through backtest"""
        # Get historical data
        df = self.data_provider.get_historical_data(
            asset.ticker, start_date, end_date
        )
        if df.empty:
            print(f"No data for {asset.ticker}")
            return

        position = self.positions[asset.ticker]
        structural_high = self.technical_analysis.get_structural_high(df)

        # Process each day
        for idx, row in df.iterrows():
            current_date = row['date']
            current_price = row['close']
            high_price = row['high']
            low_price = row['low']

            # Check buy conditions
            self._check_buy_conditions(
                asset, position, current_date, current_price, low_price
            )

            # Check sell conditions
            self._check_sell_conditions(
                asset, position, current_date, current_price,
                high_price, structural_high, df
            )

    def _check_buy_conditions(
        self, asset: Asset, position: Position, date: datetime,
        current_price: float, low_price: float
    ):
        """Check if buy conditions are met"""
        for zone_idx, zone in enumerate(asset.zones):
            for level_idx, level_price in enumerate(zone.levels):
                # Check if price touched the level
                # and we haven't bought at this level yet
                if (low_price <= level_price and
                        position.can_buy_at_level(zone_idx, level_idx)):
                    # Calculate buy amount (20% of zone allocation)
                    buy_amount = asset.allocation_per_zone * 0.2

                    # Execute buy
                    quantity = position.buy_at_level(
                        zone_idx, level_idx, level_price, buy_amount
                    )

                    # Log trade
                    trade = Trade(
                        date=date,
                        asset=asset.ticker,
                        action='buy',
                        price=level_price,
                        amount_usd=buy_amount,
                        quantity=quantity,
                        level=level_idx + 1
                    )
                    self.trade_logger.log_trade(trade)

    def _check_sell_conditions(
        self, asset: Asset, position: Position, date: datetime,
        current_price: float, high_price: float, structural_high: float,
        df: pd.DataFrame
    ):
        """Check if sell conditions are met"""
        current_quantity = position.get_current_quantity()
        if current_quantity <= 0:
            return

        avg_price = position.get_average_price()

        # Check each take profit level
        for tp_idx, tp in enumerate(asset.take_profits):
            if position.tp_sold_quantity[tp_idx] > 0:
                continue  # Already sold at this TP level

            target_price = self._calculate_tp_price(
                tp, avg_price, structural_high
            )
            if target_price <= 0:
                continue

            # Check if price reached TP level
            if high_price >= target_price:
                quantity_sold, usd_received = (
                    position.sell_tp_percentage(tp_idx, 20.0, target_price)
                )

                if quantity_sold > 0:
                    trade = Trade(
                        date=date,
                        asset=asset.ticker,
                        action='sell',
                        price=target_price,
                        amount_usd=usd_received,
                        quantity=quantity_sold,
                        level=tp_idx + 1
                    )
                    self.trade_logger.log_trade(trade)

                    # Activate stop loss after 3rd TP
                    if tp_idx == 2:  # 3rd TP (index 2)
                        position.stop_loss_active = True
                        position.stop_loss_price = avg_price  # Breakeven stop

                    # Activate trailing stop after 4th TP
                    if tp_idx == 3:  # 4th TP (index 3)
                        position.trailing_stop_active = True
                        ta = self.technical_analysis
                        structural_low = ta.get_structural_low_ma200(df)
                        position.stop_loss_price = structural_low

        # Check stop loss conditions
        if (position.stop_loss_active and
                current_price <= position.stop_loss_price):
            # Close remaining position
            remaining_quantity = position.get_current_quantity()
            if remaining_quantity > 0:
                usd_received = remaining_quantity * current_price

                trade = Trade(
                    date=date,
                    asset=asset.ticker,
                    action='sell',
                    price=current_price,
                    amount_usd=usd_received,
                    quantity=remaining_quantity,
                    level=0  # Stop loss
                )
                self.trade_logger.log_trade(trade)

                # Mark all TP levels as sold
                for i in range(len(position.tp_sold_quantity)):
                    position.tp_sold_quantity[i] = position.total_quantity

    def _calculate_tp_price(
        self, tp: TakeProfit, avg_price: float, structural_high: float
    ) -> float:
        """Calculate target price for take profit"""
        if tp.tp_type == 'percent':
            return avg_price * (1 + tp.value / 100.0)
        elif tp.tp_type == 'ath':
            # If ATH is less than 300% from avg_price, use 400%
            min_target = avg_price * 4.0  # 400%
            return max(structural_high, min_target)
        elif tp.tp_type == 'trailing':
            # For trailing, we don't set a fixed price
            return float('inf')
        return 0.0


class UserInterface:
    """User interface for portfolio configuration"""

    @staticmethod
    def create_portfolio() -> Portfolio:
        """Create portfolio through user input"""
        print("=== Portfolio Creation ===")

        name = input("Portfolio name: ").strip()

        while True:
            try:
                deposit = float(input("Deposit: "))
                if deposit > 0:
                    break
                print("Deposit must be greater than 0")
            except ValueError:
                print("Enter a valid amount")

        while True:
            try:
                max_assets = int(input("Number of assets: "))
                if max_assets > 0:
                    break
                print("Number of assets must be greater than 0")
            except ValueError:
                print("Enter a valid number")

        portfolio = Portfolio(name, deposit, max_assets)
        print(
            f"Portfolio created! Allocation per asset: "
            f"${portfolio.allocation_per_asset:.2f}"
        )
        return portfolio

    @staticmethod
    def add_assets_to_portfolio(portfolio: Portfolio):
        """Add assets to portfolio through user input"""
        print(
            f"\n=== Adding Assets (max. {portfolio.max_assets}) ==="
        )

        while len(portfolio.assets) < portfolio.max_assets:
            print(f"\nAsset {len(portfolio.assets) + 1}:")
            asset = UserInterface._create_asset(portfolio.allocation_per_asset)

            if asset:
                if portfolio.add_asset(asset):
                    print(f"✅ {asset.ticker} added to portfolio")
                else:
                    print("❌ Error adding asset")

            add_more = input("\nAdd another asset? (y/n): ").lower().strip()
            if add_more != 'y':
                break

        print(f"\n📊 Total assets: {len(portfolio.assets)}")
        print(f"💰 Free cash: ${portfolio.free_cash:.2f}")

    @staticmethod
    def _create_asset(allocation: float) -> Optional[Asset]:
        """Create asset through user input"""
        # Ticker validation loop
        while True:
            print("\n💡 Available commands:")
            print("   • Enter any ticker (BTC, SOL, DOGE, SHIB, etc.)")
            print("   • 'list' - show popular tickers on Binance")
            print("   • 'skip' - skip this asset")

            user_input = input("Enter ticker: ").strip()

            if user_input.upper() == 'SKIP':
                return None
            elif user_input.lower() == 'list':
                UserInterface._show_popular_tickers()
                continue
            elif not user_input:
                print("❌ Ticker cannot be empty")
                continue

            ticker = user_input.upper()

            # Validate ticker through Binance API
            print(f"🔍 Checking ticker {ticker} on Binance...")
            data_provider = DataProvider()

            # Test ticker validity through Binance API
            if data_provider.api.test_ticker_validity(ticker):
                print(f"✅ Ticker {ticker} confirmed on Binance!")
                break
            else:
                print(f"❌ Ticker {ticker} not found on Binance!")
                print("💡 Try:")
                print("   • Check ticker spelling")
                print("   • Use 'list' for popular tickers")
                print("   • Only USDT pairs available on Binance")

                retry = input(
                    "Want to try another ticker? (y/n): "
                ).lower().strip()
                if retry != 'y':
                    return None

        # Number of zones
        while True:
            try:
                num_zones = int(input("Number of zones (1-2): "))
                if num_zones in [1, 2]:
                    break
                print("❌ Enter 1 or 2")
            except ValueError:
                print("❌ Enter a valid number")

        # Create zones
        zones = []
        for i in range(num_zones):
            print(f"Zone {i + 1}:")
            while True:
                try:
                    zone_input = input(
                        "Enter range (from-to, e.g. 25000-20000): "
                    )
                    price_from, price_to = map(float, zone_input.split('-'))
                    if price_from > price_to > 0:
                        zones.append(Zone(price_from, price_to))
                        print(
                            f"✅ Zone created: ${price_to:.2f} - "
                            f"${price_from:.2f}"
                        )
                        break
                    print(
                        "❌ 'From' price must be greater than 'to' price, "
                        "both > 0"
                    )
                except ValueError:
                    print("❌ Enter in format: price_from-price_to")

        # Take profits configuration
        tp_type = input("TP type (standard/custom): ").lower().strip()

        if tp_type == 'standard':
            take_profits = UserInterface._create_standard_tps()
            print("✅ Standard TPs: 100%, 200%, 300%, ATH, trailing")
        else:
            take_profits = UserInterface._create_custom_tps()

        # Rebalancing
        rebalancing = input(
            "Rebalancing on return (y/n): "
        ).lower().strip() == 'y'
        rebalancing_text = "enabled" if rebalancing else "disabled"
        print(f"✅ Rebalancing {rebalancing_text}")

        return Asset(ticker, zones, take_profits, rebalancing)

    @staticmethod
    def _create_standard_tps() -> List[TakeProfit]:
        """Create standard take profits"""
        return [
            TakeProfit('percent', 100.0),  # 100%
            TakeProfit('percent', 200.0),  # 200%
            TakeProfit('percent', 300.0),  # 300%
            TakeProfit('ath'),  # ATH (or 400% if ATH < 300%)
            TakeProfit('trailing')  # Trailing stop
        ]

    @staticmethod
    def _create_custom_tps() -> List[TakeProfit]:
        """Create custom take profits"""
        print("Enter 5 TPs:")
        take_profits = []

        # TP 1-3: only percentage
        for i in range(3):
            while True:
                try:
                    value = float(input(f"TP {i + 1} (%): "))
                    if value > 0:
                        take_profits.append(TakeProfit('percent', value))
                        break
                    print("Percentage must be greater than 0")
                except ValueError:
                    print("Enter a valid percentage")

        # TP 4: percentage or ATH
        tp4_input = input("TP 4 (% or ATH): ").strip()
        if tp4_input.lower() == 'ath':
            take_profits.append(TakeProfit('ath'))
        else:
            try:
                value = float(tp4_input)
                take_profits.append(TakeProfit('percent', value))
            except ValueError:
                print("Error in TP 4, using ATH")
                take_profits.append(TakeProfit('ath'))

        # TP 5: percentage or trailing
        tp5_input = input("TP 5 (% or trailing): ").strip()
        if tp5_input.lower() == 'trailing':
            take_profits.append(TakeProfit('trailing'))
        else:
            try:
                value = float(tp5_input)
                take_profits.append(TakeProfit('percent', value))
            except ValueError:
                print("Error in TP 5, using trailing")
                take_profits.append(TakeProfit('trailing'))

        return take_profits

    @staticmethod
    def _show_popular_tickers():
        """Show popular cryptocurrency tickers available on Binance"""
        print("\n📋 POPULAR TICKERS ON BINANCE:")
        print("=" * 60)

        # Group popular tickers by category
        major = [
            'BTC', 'ETH', 'BNB', 'ADA', 'SOL', 'XRP', 'DOGE', 'DOT',
            'AVAX', 'MATIC'
        ]
        defi = [
            'UNI', 'LINK', 'AAVE', 'MKR', 'COMP', 'SUSHI', 'YFI', 'SNX',
            'CRV', '1INCH'
        ]
        meme = [
            'SHIB', 'PEPE', 'FLOKI', 'WIF', 'BONK', 'MEME', 'DEGEN',
            'NEIRO', 'TURBO', 'MUMU'
        ]
        layer1 = [
            'ATOM', 'NEAR', 'FTM', 'ALGO', 'VET', 'HBAR', 'ICP', 'TRX',
            'XLM', 'EOS'
        ]

        print("🏆 TOP COINS:")
        for i in range(0, len(major), 5):
            row = "   " + "   ".join(f"{t:<6}" for t in major[i:i + 5])
            print(row)

        print("\n🔗 DEFI TOKENS:")
        for i in range(0, len(defi), 5):
            row = "   " + "   ".join(f"{t:<6}" for t in defi[i:i + 5])
            print(row)

        print("\n🐸 MEME COINS:")
        for i in range(0, len(meme), 5):
            row = "   " + "   ".join(f"{t:<6}" for t in meme[i:i + 5])
            print(row)

        print("\n⛓️  LAYER 1:")
        for i in range(0, len(layer1), 5):
            row = "   " + "   ".join(f"{t:<6}" for t in layer1[i:i + 5])
            print(row)

        print("\n💡 All tickers trade in USDT pairs")
        print("📝 Can use with or without USDT: BTC, BTCUSDT")
        print("=" * 60)


def main():
    """Main application entry point"""
    ensure_utf8_output()

    parser = argparse.ArgumentParser(description="Crypto portfolio backtester")
    parser.add_argument(
        "--config",
        type=str,
        help="Path to JSON config for non-interactive run"
    )
    args = parser.parse_args()

    print("🚀 Crypto Portfolio Backtester")
    print("=" * 50)

    # Create results directory if it doesn't exist
    results_dir = "results"
    if not os.path.exists(results_dir):
        os.makedirs(results_dir)
        print(f"📁 Created folder: {results_dir}/")

    try:
        # Create portfolio
        if args.config:
            print(f"📂 Loading config: {args.config}")
            config = load_config(args.config)
            portfolio = PortfolioFactory.from_config(config)
            print(f"✅ Portfolio from config: {portfolio.name}")
        else:
            if not sys.stdin.isatty():
                raise RuntimeError(
                    "Interactive input is unavailable. "
                    "Run with --config path/to/config.json parameter"
                )
            portfolio = UserInterface.create_portfolio()

        # Add assets
        if args.config:
            print(f"📦 Assets from config: {len(portfolio.assets)}")
        else:
            UserInterface.add_assets_to_portfolio(portfolio)

        if not portfolio.assets:
            print("❌ No assets for testing!")
            return

        print(f"\n📈 Starting backtest with {len(portfolio.assets)} assets...")

        # Initialize backtester
        data_provider = DataProvider()
        engine = BacktestEngine(portfolio, data_provider)

        # Run backtest
        stats = engine.run_backtest()

        # Display results
        print("\n" + "=" * 60)
        print("📊 BACKTEST RESULTS")
        print("=" * 60)

        print(f"💼 Portfolio: {portfolio.name}")
        print(f"💰 Initial deposit: ${portfolio.deposit:,.2f}")
        print(f"📈 Assets in portfolio: {len(portfolio.assets)}")
        print(f"💸 Free cash: ${stats['free_cash']:,.2f}")
        print("-" * 60)
        print(f"📈 Total trades: {stats['total_trades']}")
        print(f"🟢 Buys: {stats['buy_trades']}")
        print(f"🔴 Sells: {stats['sell_trades']}")
        print(f"💵 Total invested: ${stats['total_invested']:,.2f}")
        print(f"💰 Total received: ${stats['total_received']:,.2f}")
        print(f"📊 Realized profit: ${stats['realized_profit']:,.2f}")

        if stats['total_invested'] > 0:
            roi = (stats['realized_profit'] / stats['total_invested']) * 100
            print(f"🎯 ROI: {roi:.2f}%")
        else:
            print("🎯 ROI: 0.00% (no investments)")

        # Create filename with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{results_dir}/backtest_{portfolio.name}_{timestamp}.csv"

        # Get trades DataFrame
        trades_df = engine.trade_logger.get_trades_df()

        # Always create file, even if empty
        if not trades_df.empty:
            print(
                f"\n📋 TRADE LOG "
                f"(showing last 10 of {len(trades_df)}):"
            )
            print("-" * 80)

            # Format DataFrame for display
            display_df = trades_df.copy()
            display_df['date'] = display_df['date'].dt.strftime('%Y-%m-%d')
            display_df['price'] = display_df['price'].apply(
                lambda x: f"${x:.4f}"
            )
            display_df['amount_usd'] = display_df['amount_usd'].apply(
                lambda x: f"${x:.2f}"
            )
            display_df['quantity'] = display_df['quantity'].apply(
                lambda x: f"{x:.6f}"
            )

            print(display_df.tail(10).to_string(index=False, max_colwidth=12))

            # Save trades to CSV
            trades_df.to_csv(filename, index=False)
            print(f"\n💾 Full log saved: {filename}")

        else:
            print(
                "\n📋 TRADE LOG: Empty (no trades executed)"
            )

            # Create empty CSV with headers
            empty_df = pd.DataFrame(
                columns=[
                    'date', 'asset', 'action', 'price', 'amount_usd',
                    'quantity', 'level'
                ]
            )
            empty_df.to_csv(filename, index=False)
            print(f"💾 Empty file created: {filename}")

        # Show portfolio summary
        print("\n📈 ASSET SUMMARY:")
        print("-" * 50)
        for asset in portfolio.assets:
            position = engine.positions.get(asset.ticker)
            if position:
                current_qty = position.get_current_quantity()
                total_invested = position.total_invested
                avg_price = position.get_average_price()

                print(f"{asset.ticker:<8} | "
                      f"Invested: ${total_invested:>8,.2f} | "
                      f"Qty: {current_qty:>10.4f} | "
                      f"Avg: ${avg_price:>8.4f}")

        print("\n✅ Backtesting completed!")

    except KeyboardInterrupt:
        print("\n❌ Interrupted by user")
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
