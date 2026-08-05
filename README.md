# TimeGuessr + MapTap Scorekeeper Bot

Watches a Discord channel for daily result posts and keeps a leaderboard,
win counts, and per-user stats — automatically, from the results people
already paste. Supports two games:

**TimeGuessr** — keyed by game number:
```
TimeGuessr #1161 — 41,255/50,000
1️⃣ 🏆9,179 · 📅 4y · 🌍 3.3mi
2️⃣ 🏆7,461 · 📅 8y · 🌍 1198m
3️⃣ 🏆6,270 · 📅 11y · 🌍 142.9mi
4️⃣ 🏆8,745 · 📅 3y · 🌍 220.3mi
5️⃣ 🏆9,600 · 📅 3y · 🌍 32m
https://timeguessr.com
```

**MapTap** — keyed by date instead of a number; which emoji tags each guess
depends on some scoring threshold we don't know, so the bot stores and
reproduces whichever emoji was actually posted rather than guessing at the
goalposts. The final score includes bonus/streak math and is **not** the
sum of the guesses, so it's tracked separately:
```
[www.maptap.gg](https://www.maptap.gg) August 4
99🎯 100🎯 91👑 97🔥 89👑
Final score: 939
```

Any message matching either pattern gets parsed — the bot reacts ✅ once it's
logged, or ♻️ if that person already logged that game/day (first submission
wins, so no double-dipping by re-pasting).

## Setup

1. **Create the bot application**
   - Go to https://discord.com/developers/applications → New Application
   - Bot tab → Reset Token, copy it somewhere safe
   - Under **Privileged Gateway Intents**, enable **MESSAGE CONTENT INTENT**
     and **SERVER MEMBERS INTENT** (both required — without them Discord
     refuses the connection outright with a `PrivilegedIntentsRequired`
     error. Message Content lets the bot read the posts; Server Members
     lets `@user` arguments in commands like `!stats @user` resolve.)

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

### TimeGuessr
| Command | What it does |
|---|---|
| `!leaderboard [n]` | Ranked by average score (default top 10) |
| `!wins [n]` | Most daily wins — highest score that day, ties don't count |
| `!stats @user` | Games played / avg / best / worst / total |
| `!history @user [n]` | A user's last n games (default 10) |
| `!game 1161` | Everyone's score for a specific game number, ranked |

### MapTap
| Command | What it does |
|---|---|
| `!mtleaderboard [n]` | Ranked by average final score |
| `!mtwins [n]` | Most daily wins — highest final score that day, ties don't count |
| `!mtstats @user` | Games played / avg / best / worst / total, plus average per guess position |
| `!mthistory @user [n]` | A user's last n days, with per-guess breakdown and original emoji |
| `!mtday August 4` | Everyone's results for a specific date (also accepts `2026-08-04`) |
| `!mtguess 1` | Average score for guess #1, across everyone, all days |
| `!mtguess 1 August 4` | Average score for guess #1, just that day |

### Both games
| Command | What it does |
|---|---|
| `!backfill [#channel] [limit]` | Retroactively scan a channel's history and log any results found (requires "Manage Server" permission) |

## How duplicates & edge cases are handled

- **Duplicate posts**: TimeGuessr is keyed on `(user, game_number)`, MapTap
  on `(user, date)` — re-pasting the same result won't inflate stats.
- **Ties for the win**: if two people post the same top score, nobody gets
  credited with a win that day (rather than picking one arbitrarily).
- **MapTap dates without a year**: MapTap's posts just say "August 4" with
  no year, so the bot infers the year from the message's own timestamp
  (correcting for the rare case a post lands right at a Dec/Jan boundary).
- **MapTap emoji**: the emoji per guess isn't assumed to mean anything
  specific — whatever glyph was posted next to a score is stored and
  reproduced as-is in `!mthistory` / `!mtday`. Averages just show the
  number, since averaging emoji isn't meaningful.
- **MapTap final score ≠ sum of guesses**: it's a *weighted* sum — each
  guess position carries a multiplier of `[1, 1, 2, 3, 3]` for guesses 1
  through 5 (confirmed against a real post: `99×1 + 100×1 + 91×2 + 97×3 +
  89×3 = 939`). The bot still trusts the final score as posted rather than
  computing it, but logs a warning if the weighted sum doesn't match — a
  cheap sanity check that catches parsing bugs (wrong numbers picked up)
  without silently trusting or discarding anything.
- **Perfect score (1000)**: max-per-guess (100) × sum of the multipliers
  (10) = 1000, so a perfect run is a known, valid target score. If a post
  claims 1000 but the guess-breakdown line doesn't parse into 5 numbers
  (e.g. MapTap renders an all-perfect result differently), the bot still
  accepts it and records the final score — everywhere else, a post with
  no parseable guesses is rejected outright, but 1000 gets the benefit of
  the doubt. Per-guess stats just won't have data for that day in that
  case, since there was nothing to parse.
- **Score collisions**: nothing is keyed on score at all — the uniqueness
  constraint is `(user, date)`. Two different people (or even two
  completely different guess combinations) landing on the same final
  score are stored as fully separate rows; the only place scores get
  compared is win detection, where a tie means nobody's credited with
  that day's win, on purpose.
- **Multiple channels**: by default the bot scrapes results posted anywhere
  it can read; set `TIMEGUESSR_CHANNEL_ID` to lock it to your results
  channel if others post there too.
- **Retroactive backfill**: if the bot joined your server (or was turned on)
  after people had already been posting results, run `!backfill` in the
  channel with the history — it walks every past message once and logs
  anything it recognizes, for both games at once.

## Extending it

Everything reads from a plain SQLite file (`timeguessr.db`), so you can query
it directly (`sqlite3 timeguessr.db`) or add new bot commands that pull from
the `results` / `maptap_results` tables without touching the parsing logic.

