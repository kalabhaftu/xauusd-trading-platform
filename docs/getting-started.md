# Getting started

You need a Windows PC, MetaTrader 5 installed, and a broker account. Sign in to MT5 and leave it open while Aurum is
running.

1. Double-click **AurumLauncher.exe**. Your browser opens the dashboard automatically. A new installation opens
   **Getting Started**; choose it from the left menu later to edit your saved setup.
2. Choose **Demo** or **Live**, enter the account number and exact server shown in MT5, select your gold symbol and
   strategy, then save. The connection check only reads account and market information. Your password stays in MT5.
3. In **Backtesting Lab**, choose a starting balance and timeframe, then select **Compare all strategies**. This
   simulates trades on downloaded historical prices; it does not send orders to your account.
4. Choose **Start strategy monitor** from Overview when you want Aurum to watch current market candles. It checks
   your selected strategies and records signals and outcomes. Opening the dashboard alone does not start monitoring
   or send Telegram alerts. Telegram is optional and can be configured under Settings.

The separate **Check MT5 connection** launcher action is for after you save an account in Getting Started. If no
account is saved yet, Aurum opens the setup page and explains what to enter; it does not treat missing setup as a
broker connection failure.

**Automatic costs** are selected by default. Leave them on if you do not know the broker's spread, slippage, fees, or
overnight charges. Aurum fills in starter limits and reads supported swap values from MT5. It estimates commission as
zero until you confirm the broker's actual fees. Backtests show that assumption, and automatic-order approval requires
confirmed costs. You can enter known values under **More risk and cost settings**.

**Auto execute** is the default preference; it does not mean orders are already enabled. Before an order, the strategy
must pass research and account/risk checks, and you must authorize that account. **Live** means real money. Start on a
demo account.
