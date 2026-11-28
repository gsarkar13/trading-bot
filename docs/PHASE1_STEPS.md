# Phase 1: what to run on your laptop

None of these steps places an order. Everything only reads market data.
After each step, attach the file named in **Attach** to the chat. Never paste your App
Secret, a token or your vault passphrase into the chat.

Commands are for **Windows Command Prompt**. Linux/macOS equivalents are shown where
they differ.

---

## 1. One-time setup (about 10 minutes)

1. Install **Python 3.12** (exactly 3.12: the official Fyers SDK only installs on 3.12 on
   Windows; it can sit alongside 3.13/3.14) and **Git for Windows**:
   ```bat
   winget install Python.Python.3.12
   ```
   or use the "Windows installer (64-bit)" from https://www.python.org/downloads/windows/.
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

## 2. The data run: one command, one file to attach (15–40 minutes)

Run this **after 15:45 IST on a weekday, or at a weekend**:
```bat
deploy\windows_phase1.bat
```
*Linux:* `python -m app.tools.phase1`

What happens:
1. It asks you to **choose a vault passphrase** the first time (at least 8 characters,
   typed twice). It protects your Fyers token on disk. If you lose it, delete
   `data\vault.json` and choose a new one.
2. It prints a Fyers login link. Open it, log in and complete 2FA. Fyers then shows a
   page: copy the **whole address** from the browser's address bar and paste it in.
3. It then runs on its own: one test call to each Fyers service, about a year of
   history, and the in-sample backtest with the sensitivity grid. If the download stops
   (network, sleep), just run the command again; finished parts are skipped.
4. At the end it prints the name of **one zip file**.

**Attach:** that zip file (`reports\phase1_bundle_<date>_<time>.zip`).

Before anything goes into the zip, every file is checked: anything containing your App
ID, secret, token or token-like text is left out, and the zip's `manifest.json` lists
what was left out.

## 3. Record one full trading day (unattended)

On a trading day, before 09:00 IST:
1. Stop the laptop sleeping: *Settings → System → Power → Screen and sleep*:
   "When plugged in, put my device to sleep after" = **Never**. Keep it plugged in.
2. Double-click `deploy\windows_daily.bat` (Linux: `python -m app.tools.daily`).
   It asks for your vault passphrase, logs in to Fyers if needed, records until 15:35,
   downloads the day's candles, replays the day from the recorded ticks, and packs it all.
   Leave the window open.
3. Around 15:45 it prints the name of a zip file.

**Attach:** that zip file.

## 4. After I've looked at the in-sample results: out-of-sample

This step is deliberately separate, so the parameters are fixed before anyone looks
at data the strategies have never seen:
```bat
python -m app.tools.backtest freeze
python -m app.tools.backtest run --from 2025-12-03 --to 2026-11-27 --period out-of-sample
python -m app.tools.phase1 --bundle-only
```
`freeze` prints one line with the config hashes; paste that line in the chat (it
contains no secrets). The out-of-sample run refuses to start if any parameter changed
after the freeze. **Attach:** the new zip.

## 5. Running the steps one by one (only if asked)
`python -m app.tools.login`, `python -m app.tools.fyers_probe`,
`python -m app.tools.backfill --from A --to B`, `python -m app.tools.backtest run ...`,
`python -m app.services.recorder`, `python -m app.tools.replay --date D`.

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
