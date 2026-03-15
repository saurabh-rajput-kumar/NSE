
NSE AI SCREENER

Features
- Golden Cross (50 EMA > 200 EMA crossover)
- Death Cross detection
- Strong Uptrend detection
- Scans major NSE stocks
- Runs directly in browser

How to Run

1. Extract the zip.
2. Open the folder.
3. Double click:
   nse_ai_screener.html

Then press:
Run Scan

Important Note

Yahoo Finance sometimes blocks direct browser requests (CORS).
If that happens run a local server:

Python method:

python -m http.server 8000

Then open:

http://localhost:8000/nse_ai_screener.html

Future upgrades possible:

- RSI breakout scanner
- Volume expansion detection
- AI ranking of trades
- Auto scanning all NSE stocks
