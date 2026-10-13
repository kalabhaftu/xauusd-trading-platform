# Getting started

You need a Windows PC, the MetaTrader 5 desktop app installed, and a broker account. Aurum asks the official MT5
connector to find and start the usual MT5 installation during its connection check; you do not need to enter a terminal
folder path. If Windows asks to open MT5, keep security protection on and verify the publisher is MetaQuotes Ltd.

1. Double-click **AurumLauncher.exe**. Your browser opens the dashboard automatically. A new installation opens
   **Getting Started**; choose it from the left menu later to edit your saved setup.
2. Choose **Demo** or **Live**, enter the account number and exact server shown in MT5, select your gold symbol and
   strategy, then save. A successful check refreshes the balance and opens Overview. The check only reads account and
   market information. Your password stays in MT5. If you add Telegram bot details, Aurum turns updates on and sends
   one test message; open the bot and press **Start** first so Telegram lets it message you.
3. In **Backtesting Lab**, choose a starting balance and timeframe, then select **Compare all strategies**. This
   simulates trades on downloaded historical prices; it does not send orders to your account.
4. Choose **Start strategy monitor** from Overview when you want Aurum to watch current market candles. It checks
   your selected strategies and records signals and outcomes. Opening the dashboard alone does not start monitoring
   or send Telegram alerts. Telegram is optional and can be configured in **Getting Started** or **Settings**.

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

Risk percentages and the number of simultaneous positions are adjustable in Getting Started. The app no longer forces
a 0.5% maximum; risk per trade, per-trade limit, combined exposure, daily/weekly loss stops, and drawdown can each be
set up to 100%. A stop can fill worse than planned during a fast market or price gap.
