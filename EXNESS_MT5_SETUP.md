# KETS + Exness MT5 Managed Trading

## What this build does

- A signed-in KETS user can create a private connector token from **Managed Trading**.
- The user installs the KETS MT5 Expert Advisor on their **desktop MT5** terminal.
- The EA polls KETS for an authorized order and reports account status back to KETS.
- KETS never receives the user's Exness website password and does not hold deposits or withdrawal funds.
- Automatic trading is **OFF by default** and must be explicitly enabled after the MT5 terminal reports connected.

## User setup

1. Open an Exness MT5 trading account (demo first).
2. Install MT5 desktop. Exness states that Expert Advisors run on MT4/MT5 desktop terminals, not mobile/web terminals.
3. Sign in to the user's MT5 account in MT5 desktop.
4. In KETS, open **Managed Trading** and press **Connect broker / exchange**.
5. Copy the private connector token shown by KETS.
6. In MetaTrader 5: `File -> Open Data Folder -> MQL5 -> Experts` and copy `KETS_MT5_Exness_Bridge.mq5` there. Compile it in MetaEditor.
7. In MT5: `Tools -> Options -> Expert Advisors` and enable algorithmic trading. Add the KETS URL shown on the KETS Managed Trading page to the allowed WebRequest URLs.
8. Attach the EA to an appropriate chart and paste the KETS connector token into the EA input.
9. Confirm KETS shows **CONNECTED** and the account balance/equity.
10. Keep **Automatic entries & exits OFF** until a demo test is successful.
11. When ready, explicitly enable automatic trading.

## Important

- The EA executes orders on the MT5 account to which it is attached.
- The default order volume is 0.01 lots. This is a conservative default, not a profit guarantee.
- The KETS server only queues the latest executable signal for a connected account when auto trading is enabled.
- This is automated CFD/forex/metal/crypto trading and can lose money. Test on demo first.
- Do not put an Exness website password, payment PIN, or withdrawal credentials into KETS.


## KETS precision managed-trading mode

The managed-trading layer keeps the existing KETS signal engine unchanged. Automatic execution adds a stricter execution gate: by default it prefers **STRONG REVERSAL ENTRY** signals, requires a high strategy/entry-quality score (85/100 by default), requires a recent timestamp (maximum 180 seconds), limits the number of open trades, and sends the signal's TP and SL with the order.

You can change the **Custom lot size** from the KETS Managed Trading page. The default is 0.01 lot. The $25 field is a **projected-profit target only**; KETS cannot guarantee that any trade will make $25 or more. Actual P/L depends on the broker's symbol contract size, spread, commission, slippage, entry price and the chosen lot size.

The EA also reports open MT5 positions and closes an opposite-direction position on the same symbol before opening a new KETS signal when `CloseOppositeOnNewSignal=true`. TP/SL remain the primary automatic exits. Test on a demo account before using real funds.
