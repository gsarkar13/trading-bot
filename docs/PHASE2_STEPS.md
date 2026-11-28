# Phase 2: what you do (paper trading only)

Real money stays OFF in Phase 2: there is no execution-broker connection yet, and the
mode ceiling is PAPER. Everything below is paper.

## Once

1. **Update the code** on the PC: double-click `deploy\windows_update.bat`
   (it pulls the new code and installs the new packages).
2. **Dashboard password:** double-click `deploy\windows_set_password.bat` and choose a
   password of at least 10 characters. Only a scrambled form (a hash) is saved.
3. **Trading calendar:** create `config\calendar\nse_2026.yaml` (December is enough) and
   `nse_2027.yaml` from NSE's holiday circular, following `config\calendar\README.md`.
   Put your name and the date in `verified_by`. Without it the engine records data but
   takes no paper trades.
4. **Switch a strategy on:** on the dashboard, Strategies tab, tick "on" for
   PDH/PDL breakout and/or EMA200 hammer. (Phase 1 showed neither has an edge yet;
   paper trading is how we collect the 40 trades each needs for the Phase 3 gate.)
5. **Optional, Telegram** (alerts and commands on your phone):
   - In Telegram, message **@BotFather**, send `/newbot`, follow the steps, copy the token.
   - Put it in `.env` as `ALGO_TELEGRAM_BOT_TOKEN=...` (never in a chat).
   - In `config\host.yaml` (this PC's own settings) set `telegram: enabled: true`.
   - Start the day once (below), send `/start` to your bot: it replies with your chat id.
     Put it in `config\host.yaml` as `telegram: owner_chat_id: <number>` and restart.
6. **Optional, AI opinions** (advisory only, never places or blocks a trade): put
   `ALGO_GEMINI_API_KEY` and/or `ALGO_ANTHROPIC_API_KEY` in `.env` and set
   `ai: enabled: true` in `config\host.yaml`. With no keys, everything else works the same.

## Each trading day

1. Around **08:55 IST** double-click `deploy\windows_trade.bat`.
2. Type your vault passphrase. If today's Fyers login is missing it shows the link:
   log in, then paste the address you land on (in the window, or to your Telegram bot).
3. Open **http://127.0.0.1:8600** and log in. Leave the black window open until ~15:40.
4. That's all. Paper trades open and close by themselves. You can watch, pause new
   entries, move a stop (only toward the price; logged as a rule override), or press
   **EMERGENCY FLATTEN ALL**.

Optional: `deploy\windows_schedule.bat` opens the window at 08:50 on weekdays (it still
asks for your passphrase, so you need to be at the PC).

## If something goes wrong

- **The window crashed or the PC restarted:** start `windows_trade.bat` again. Open
  paper positions are found and resumed (anything without a stop gets one).
- **Need to close everything and the dashboard doesn't respond:** Telegram `/flatten`
  (reply `FLATTEN`), or close the window and double-click `deploy\windows_flatten.bat`.

## What to send me

Nothing daily. After about two weeks, or when you have questions, attach the dashboard
"Review" numbers or `reports\soak` files if I ask. Never paste keys, tokens or the
redirect address into this chat.
