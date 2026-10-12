# Getting started

You need a Windows PC, MetaTrader 5 installed, and a broker account. Sign in to MT5 and leave it open while Aurum is
running.

1. Double-click **AurumLauncher.exe**, choose **Open dashboard**, then open **Getting Started**.
2. Choose **Demo** or **Live**, enter the account number and exact server shown in MT5, select your gold symbol and
   strategy, then save. The connection check only reads account and market information. Your password stays in MT5.
3. In **Backtesting Lab**, choose a starting balance and timeframe, then select **Compare all strategies**. This
   simulates trades on downloaded historical prices; it does not send orders to your account.
4. Choose **Start strategy monitor** when you want Aurum to watch current market candles. It checks your selected
   strategies and records signals and outcomes. Opening the dashboard alone does not start market monitoring.

**Automatic costs** are selected by default. Leave them on if you do not know the broker's spread, slippage, fees, or
overnight charges. Aurum fills in starter limits and reads supported swap values from MT5. It estimates commission as
zero until you confirm the broker's actual fees. Backtests show that assumption, and automatic-order approval requires
confirmed costs. You can enter known values under **More risk and cost settings**.

**Auto execute** is the default preference; it does not mean orders are already enabled. Before an order, the strategy
must pass research and account/risk checks, and you must authorize that account. **Live** means real money. Start on a
demo account.
