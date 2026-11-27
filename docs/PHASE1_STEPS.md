# Phase 1: what to run on your laptop

None of these steps places an order. Everything only reads market data.
After each step, attach the file named in **Attach** to the chat. Never paste your App
Secret, a token or your vault passphrase into the chat.

Commands are for **Windows Command Prompt**. Linux/macOS equivalents are shown where
they differ.

---

## 1. One-time setup (about 10 minutes)

1. Install **Python 3.12** from https://www.python.org/downloads/ (tick *Add python.exe
   to PATH*), and **Git for Windows** from https://git-scm.com/download/win.
2. Open Command Prompt in the folder where you want the code, then run:
   ```bat
   git clone https://github.com/gsarkar13/trading-bot.git
   cd trading-bot
   deploy\windows_setup.bat
   ```
   Git opens a browser window to sign in to GitHub the first time.
   *Linux:* `python3.12 -m venv .venv && . .venv/bin/activate && pip install -e . && cp .env.example .env && cp config/host.example.yaml config/host.yaml && python -m app.tools.init_db`
3. Open `.env` in Notepad (`notepad .env`) and fill in:
   - `ALGO_FYERS_APP_ID=` the App ID of your **non-trading** app (it looks like `XXXXXXX-100`)
   - `ALGO_FYERS_APP_SECRET=` its secret
   - `ALGO_FYERS_REDIRECT_URI=` leave as is. It must be exactly the redirect URL you typed
     when creating the app: `https://trade.fyers.in/api-login/redirect-uri/index.html`
4. Every new Command Prompt window needs the environment switched on first:
   ```bat
   cd trading-bot
   .venv\Scripts\activate
   ```
   *Linux:* `. .venv/bin/activate`

## 2. First login and probe (5 minutes, any day after 06:30 IST)

```bat
python -m app.tools.login
```
- The first time, it asks you to **choose a vault passphrase** (at least 8 characters,
  typed twice). It protects your Fyers token on disk. Don't lose it; if you do, delete
  `data\vault.json` and choose a new one.
- It prints a link. Open it, log in to Fyers and complete 2FA. Fyers then shows a page.
  Copy the **whole address** from the browser's address bar and paste it into the window.

Then record one real response from each Fyers data call:
```bat
python -m app.tools.fyers_probe
```
It is best run during market hours, so it also captures 20 seconds of live ticks.

**Attach:** the folder `reports\probe\<today>` (right-click → *Send to → Compressed (zipped) folder*).

## 3. Download history (after 15:45 IST or at a weekend; 5–15 minutes)

```bat
python -m app.tools.backfill --from 2025-12-01 --to 2026-11-27
```
- The Fyers Standard plan keeps about one year of 1-minute data. If you're on Prime, you
  can use an earlier `--from`.
- If it stops (network, token expiry), just run it again. Finished parts are skipped.

**Attach:** `reports\backfill\summary_2025-12-01_2026-11-27.json`

## 4. Backtest, in two steps (5–20 minutes each)

The split date is already fixed in `config\default.yaml` as `2026-08-01`: data before it
is **in-sample**, data from it on is **out-of-sample**. Don't change it after seeing
results.

**Step A, in-sample plus the sensitivity grid:**
```bat
python -m app.tools.backtest run --from 2025-12-01 --to 2026-11-27 --period in-sample --grid
```
**Attach:** both files it prints (`reports\backtest_in-sample_....html` and `.json`).

**Step B, only after we have looked at A together.** Freeze the parameters, then run the
data the strategies have never seen:
```bat
python -m app.tools.backtest freeze
python -m app.tools.backtest run --from 2025-12-01 --to 2026-11-27 --period out-of-sample
```
`freeze` prints one line with the config hashes. Paste that line in the chat (it contains
no secrets) so it can be recorded in `docs/DECISIONS.md`. The out-of-sample run refuses
to start if any parameter changed since the freeze.

**Attach:** both out-of-sample report files.

## 5. Record one full trading day (unattended)

On a trading day, before 09:00 IST:
1. Set Windows so the laptop doesn't sleep: *Settings → System → Power → Screen and sleep*:
   "When plugged in, put my device to sleep after" = **Never**. Keep it plugged in.
2. Double-click `deploy\windows_daily.bat` (or run `python -m app.tools.daily`).
   It asks for your vault passphrase, logs in to Fyers if needed, then records until
   15:35 and downloads the day's candles. Leave the window open; don't close it.
3. Around 15:40 it says "Finished".

**Attach:** `reports\recorder\<date>.json`

Then rebuild that day from the recorded ticks:
```bat
python -m app.tools.replay --date 2026-12-02
```
(use the date you recorded). **Attach:** `reports\replay\<date>.json`

## 6. Keep a copy of the data off the laptop (weekly, 2 minutes)

The recorder keeps the newest 3 daily backups in `data\backups\`. If the laptop's disk
fails, those go with it, so once a week copy the newest one somewhere else:
- **Google Drive for desktop** (free): copy the newest `data\backups\platform-YYYYMMDD.db`
  into your Google Drive folder in File Explorer.
- or a USB drive.
To restore: stop the recorder, copy the backup over `data\platform.db`, and start again.
(The server in Phase 3 will do this automatically with `rclone`.)

## 7. Getting updates

When I tell you a new version is merged:
```bat
deploy\windows_update.bat
```

## If something goes wrong

| Message | What to do |
|---|---|
| `not logged in to Fyers today` / `token expired (06:30 IST)` | run `python -m app.tools.login` again |
| `wrong passphrase or damaged vault file` | retype it. If it's lost, delete `data\vault.json` and log in again |
| `Refusing to backfill during market hours` | run it after 15:45, or add `--force` |
| `Refused: no frozen configs` | run `python -m app.tools.backtest freeze` first (step 4B) |
| Anything else | attach the newest file in `logs\` |
