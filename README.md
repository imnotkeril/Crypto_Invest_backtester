# Crypto Portfolio Backtester

A comprehensive cryptocurrency portfolio backtesting tool that simulates trading strategies with zone-based accumulation and take-profit levels.

## Features

- **Zone-based accumulation**: Define price zones with multiple levels for gradual position building
- **Take-profit strategies**: Support for percentage-based, ATH-based, and trailing stop take profits
- **Multiple data sources**: Integration with Binance API for historical price data
- **Portfolio management**: Configure multiple assets with different allocation strategies
- **Comprehensive reporting**: Detailed trade logs and performance statistics
- **Config-based execution**: Run backtests using JSON configuration files

## Installation

1. Install Python 3.8 or higher
2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Usage

### Interactive Mode

Run the script without arguments to use interactive mode:

```bash
python crypto_backtester.py
```

You will be prompted to:
1. Create a portfolio (name, deposit, max assets)
2. Add assets with zones and take-profit configurations
3. View backtest results

### Config-based Mode

Create a JSON configuration file (see `config.example.json`) and run:

```bash
python crypto_backtester.py --config config.json
```

### Configuration Format

```json
{
  "name": "portfolio_name",
  "deposit": 10000,
  "max_assets": 5,
  "assets": [
    {
      "ticker": "BTC",
      "rebalancing": true,
      "zones": [
        { "price_from": 30000, "price_to": 20000 },
        { "price_from": 20000, "price_to": 15000 }
      ],
      "take_profits": [
        { "tp_type": "percent", "value": 100 },
        { "tp_type": "percent", "value": 200 },
        { "tp_type": "percent", "value": 300 },
        { "tp_type": "ath" },
        { "tp_type": "trailing" }
      ]
    }
  ]
}
```

## Trading Strategy

### Zone Accumulation

- Each zone is divided into 5 equal price levels
- When price touches a level, 20% of zone allocation is invested
- Supports 1-2 zones per asset

### Take Profit Levels

1. **Percentage-based**: Sell at fixed percentage gains (e.g., 100%, 200%, 300%)
2. **ATH-based**: Sell at all-time high or minimum 400% gain
3. **Trailing stop**: Dynamic stop-loss based on structural low

### Risk Management

- Stop loss activates at breakeven after 3rd take-profit
- Trailing stop activates after 4th take-profit
- Automatic position closure on stop-loss trigger

## Output

Results are saved to `results/` directory as CSV files with:
- Trade date and time
- Asset ticker
- Action (buy/sell)
- Price and quantity
- USD amount
- Level information

## Data Sources

- **Primary**: Binance API (USDT pairs)
- **Fallback**: CoinGecko API (for historical data)
- **Demo mode**: Generates synthetic data if API fails

## Supported Assets

All major cryptocurrencies available on Binance with USDT pairs:
- BTC, ETH, BNB, SOL, ADA, XRP, DOGE, DOT, AVAX, MATIC
- And many more (see interactive mode for full list)

## Requirements

- pandas >= 2.0.0
- numpy >= 1.24.0
- requests >= 2.31.0

## License

This project is provided as-is for educational and research purposes.

## Notes

- Historical data availability depends on API limits
- Demo data is generated when API calls fail
- Results are saved with timestamps for easy tracking
- Free cash tracking for portfolio management
