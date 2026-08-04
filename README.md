# TimeGuessr Scorekeeper Bot

Watches a Discord channel for daily TimeGuessr result posts and keeps a
leaderboard, win counts, and per-user stats — automatically, from the results
people already paste in.

It's tuned to this exact format:

```
TimeGuessr #1161 — 41,255/50,000
1️⃣ 🏆9,179 · 📅 4y · 🌍 3.3mi
2️⃣ 🏆7,461 · 📅 8y · 🌍 1198m
3️⃣ 🏆6,270 · 📅 11y · 🌍 142.9mi
4️⃣ 🏆8,745 · 📅 3y · 🌍 220.3mi
5️⃣ 🏆9,600 · 📅 3y · 🌍 32m
https://timeguessr.com
```

Any message containing that pattern gets parsed — the bot reacts ✅ once it's
logged, or ♻️ if that person already logged that game number (first
submission wins, so no double-dipping by re-pasting).

## Setup

1. **Create the bot application**
   - Go to https://discord.com/developers/applications → New Application
   - Bot tab → Reset Token, copy it somewhere safe
   - Under **Privileged Gateway Intents**, enable **MESSAGE CONTENT INTENT**
     (required — without this the bot can't read message text)

2. **Invite it to your server**
   - OAuth2 → URL Generator → scope `bot`
   - Permissions: `Send Messages`, `Read Message History`, `View Channel`,
     `Add Reactions`
   - Open the generated URL, pick your server

3. **Install & run**
   ```bash
   pip install -r requirements.txt
   export DISCORD_TOKEN="your-token-here"
   python timeguessr_bot.py
   ```

   Optional environment variables:
   - `TIMEGUESSR_CHANNEL_ID` — restrict scraping to one channel (right-click
     the channel with Developer Mode on → Copy Channel ID). Commands still
     work everywhere; only result-parsing is restricted.
   - `TIMEGUESSR_PREFIX` — change the command prefix (default `!`)
   - `TIMEGUESSR_DB` — path to the SQLite file (default `timeguessr.db`,
     created automatically, safe to back up/copy)

The bot needs to keep running to log results — deploy it on a small VPS, a
Raspberry Pi, or something like Railway/Fly.io/a spare machine that's on daily.

## Deploying so it runs 24/7 (Railway)

You don't need your own machine on all the time — Railway rents you a slice
of a server and keeps the bot running. Rough steps:

1. Push this folder to a GitHub repo (Railway deploys from a repo).
2. Go to https://railway.app → New Project → Deploy from GitHub repo →
   pick this repo. It reads `Dockerfile`/`railway.json` automatically.
3. In the Railway dashboard, go to your service → **Variables** → add
   `DISCORD_TOKEN` with your bot token (never commit the token itself).
4. Add a **Volume**: service → Settings → Volumes → mount path `/data`.
   This is what keeps your SQLite database (and leaderboard history)
   intact across redeploys — without it, every deploy wipes the data.
5. Deploy. Check the **Logs** tab for `Logged in as <YourBot>` to confirm
   it's connected.

## Deploying on Fly.io instead

1. Install the CLI: https://fly.io/docs/flyctl/install/
2. `fly launch` from this folder (it'll detect the Dockerfile and offer to
   generate/overwrite `fly.toml` — the one included here is a working
   starting point if you want to skip that wizard)
3. `fly volumes create timeguessr_data --size 1` (persistent storage,
   matches the mount in `fly.toml`)
4. `fly secrets set DISCORD_TOKEN=your-token-here`
5. `fly deploy`

Either platform: once it's up, it just runs — you only touch it again to
push code changes or check logs.

## Commands

| Command | What it does |
|---|---|
| `!leaderboard [n]` | Ranked by average score (default top 10) |
| `!wins [n]` | Most daily wins — highest score that day, ties don't count |
| `!stats @user` | One person's games played / avg / best / worst / total |
| `!history @user [n]` | A user's last n games (default 10) |
| `!game 1161` | Everyone's score for a specific game number, ranked |

## How duplicates & edge cases are handled

- **Duplicate posts**: keyed on `(user, game_number)` — re-pasting the same
  result, or pasting an old one twice, won't inflate stats.
- **Ties for the win**: if two people post the same top score for a game,
  nobody gets credited with a win that day (rather than picking one
  arbitrarily).
- **Multiple channels**: by default the bot scrapes results posted anywhere
  it can read; set `TIMEGUESSR_CHANNEL_ID` to lock it to your results
  channel if others post there too.
- **Per-round data** (year/distance accuracy) is stored as raw text
  alongside each result (`rounds_json` column) even though the built-in
  commands only surface total-score stats — it's there if you want to add
  "best geography" / "best year-guessing" leaderboards later.

## Extending it

Everything reads from a plain SQLite file (`timeguessr.db`), so you can query
it directly (`sqlite3 timeguessr.db`) or add new bot commands that pull from
the `results` table without touching the parsing logic. A natural next step
given the stored per-round JSON: a `!geography` or `!yearguesser` leaderboard
that ranks by average distance/year-accuracy instead of total score.
