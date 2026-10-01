# KETS precision cTrader managed trading

This build uses cTrader Open API OAuth for user-authorized trading. Exness/MT5 autotrading files and controls are removed.

The existing KETS market schedule and signal engine remain in place: Monday-Friday GOLD only during the saved 06:00-11:00 and 15:00-18:00 EAT windows; Saturday-Sunday offline; 1-minute scanner. Managed trading waits for a complete low-risk KETS entry and uses the configured lot size and signal TP/SL.
