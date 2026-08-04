"""
TimeGuessr Scorekeeper Bot
==========================

Watches a Discord channel for daily TimeGuessr result posts like:

    TimeGuessr #1161 — 41,255/50,000
    1️⃣ 🏆9,179 · 📅 4y · 🌍 3.3mi
    2️⃣ 🏆7,461 · 📅 8y · 🌍 1198m
    3️⃣ 🏆6,270 · 📅 11y · 🌍 142.9mi
    4️⃣ 🏆8,745 · 📅 3y · 🌍 220.3mi
    5️⃣ 🏆9,600 · 📅 3y · 🌍 32m
    https://timeguessr.com

...and records a per-user, per-game score row. From that it can answer:
    !leaderboard          -> average score, games played, ranked
    !wins                 -> most daily "wins" (highest score for that game #)
    !stats @user          -> a single user's summary
    !history @user [n]    -> a user's last n games
    !game 1161            -> everyone's score for a specific game number
    !today                -> today's parsed submissions

Setup
-----
1. pip install -r requirements.txt
2. Create a bot at https://discord.com/developers/applications
   - Bot tab -> enable "MESSAGE CONTENT INTENT"
   - Copy the bot token
3. Invite it to your server with the "Send Messages" + "Read Message History" +
   "View Channel" permissions (bot scope + applications.commands not required
   since this uses classic prefix commands).
4. Set the token as an environment variable and run:
       export DISCORD_TOKEN="your-token-here"
       python timeguessr_bot.py
5. By default the bot listens in every channel it can see. To restrict it to
   one channel, set TIMEGUESSR_CHANNEL_ID (right-click the channel -> Copy ID,
   with Developer Mode on).
"""

import os
import re
import sqlite3
import logging
from datetime import datetime, timezone
from contextlib import closing

import discord
from discord.ext import commands

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DB_PATH = os.environ.get("TIMEGUESSR_DB", "timeguessr.db")
COMMAND_PREFIX = os.environ.get("TIMEGUESSR_PREFIX", "!")
CHANNEL_ID = os.environ.get("TIMEGUESSR_CHANNEL_ID")  # optional, str->int below
CHANNEL_ID = int(CHANNEL_ID) if CHANNEL_ID else None
MAX_SCORE = 50000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("timeguessr")

# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

# "TimeGuessr #1161 — 41,255/50,000"  (also tolerate a plain hyphen instead of em dash)
HEADER_RE = re.compile(
    r"TimeGuessr\s*#(?P<game>\d+)\s*[—-]\s*(?P<total>[\d,]+)\s*/\s*50,?000",
    re.IGNORECASE,
)

# "1️⃣ 🏆9,179 · 📅 4y · 🌍 3.3mi"
# Round score is required; date/distance are captured as raw text since their
# units vary (y/mo/d, mi/m/km, and "Perfect!"/"0" edge cases).
ROUND_RE = re.compile(
    r"(?P<round>[1-5])\uFE0F?\u20E3\s*"
    r"\U0001F3C6\s*(?P<score>[\d,]+)\s*"
    r"(?:\u00B7|-|,)\s*\U0001F4C5\s*(?P<year>[^\u00B7\n]+?)\s*"
    r"(?:\u00B7|-|,)\s*\U0001F30D\s*(?P<dist>[^\n]+)"
)


class ParsedResult:
    def __init__(self, game_number, total_score, rounds):
        self.game_number = game_number
        self.total_score = total_score
        self.rounds = rounds  # list of dicts: round, score, year, dist


def parse_timeguessr_message(content: str) -> ParsedResult | None:
    header = HEADER_RE.search(content)
    if not header:
        return None

    game_number = int(header.group("game"))
    total_score = int(header.group("total").replace(",", ""))

    rounds = []
    for m in ROUND_RE.finditer(content):
        rounds.append(
            {
                "round": int(m.group("round")),
                "score": int(m.group("score").replace(",", "")),
                "year": m.group("year").strip(),
                "dist": m.group("dist").strip(),
            }
        )

    return ParsedResult(game_number, total_score, rounds)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(get_db()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                game_number INTEGER NOT NULL,
                total_score INTEGER NOT NULL,
                rounds_json TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(guild_id, user_id, game_number)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_results_game ON results(guild_id, game_number)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_results_user ON results(guild_id, user_id)"
        )


def save_result(guild_id, channel_id, user_id, username, message_id, parsed: ParsedResult):
    """Returns 'inserted', 'duplicate', or raises on unexpected error."""
    import json

    with closing(get_db()) as conn, conn:
        try:
            conn.execute(
                """
                INSERT INTO results
                    (guild_id, channel_id, user_id, username, game_number,
                     total_score, rounds_json, message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    channel_id,
                    user_id,
                    username,
                    parsed.game_number,
                    parsed.total_score,
                    json.dumps(parsed.rounds),
                    message_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return "inserted"
        except sqlite3.IntegrityError:
            return "duplicate"


def fetch_leaderboard(guild_id, limit=20):
    with closing(get_db()) as conn:
        rows = conn.execute(
            """
            SELECT user_id, username,
                   COUNT(*) AS games_played,
                   AVG(total_score) AS avg_score,
                   MAX(total_score) AS best_score,
                   SUM(total_score) AS total_score
            FROM results
            WHERE guild_id = ?
            GROUP BY user_id
            ORDER BY avg_score DESC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
    return rows


def fetch_wins(guild_id, limit=20):
    """A 'win' = strictly highest score for a given game_number (ties = no winner)."""
    with closing(get_db()) as conn:
        game_rows = conn.execute(
            """
            SELECT game_number, user_id, username, total_score
            FROM results
            WHERE guild_id = ?
            ORDER BY game_number
            """,
            (guild_id,),
        ).fetchall()

    per_game = {}
    for r in game_rows:
        per_game.setdefault(r["game_number"], []).append(r)

    wins = {}
    for game_number, entries in per_game.items():
        top_score = max(e["total_score"] for e in entries)
        winners = [e for e in entries if e["total_score"] == top_score]
        if len(winners) == 1:
            w = winners[0]
            key = (w["user_id"], w["username"])
            wins[key] = wins.get(key, 0) + 1

    ranked = sorted(wins.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return ranked


def fetch_user_stats(guild_id, user_id):
    with closing(get_db()) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS games_played,
                   AVG(total_score) AS avg_score,
                   MAX(total_score) AS best_score,
                   MIN(total_score) AS worst_score,
                   SUM(total_score) AS total_score
            FROM results
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()
    return row


def fetch_user_history(guild_id, user_id, limit=10):
    with closing(get_db()) as conn:
        rows = conn.execute(
            """
            SELECT game_number, total_score, created_at
            FROM results
            WHERE guild_id = ? AND user_id = ?
            ORDER BY game_number DESC
            LIMIT ?
            """,
            (guild_id, user_id, limit),
        ).fetchall()
    return rows


def fetch_game(guild_id, game_number):
    with closing(get_db()) as conn:
        rows = conn.execute(
            """
            SELECT username, total_score
            FROM results
            WHERE guild_id = ? AND game_number = ?
            ORDER BY total_score DESC
            """,
            (guild_id, game_number),
        ).fetchall()
    return rows


# --------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=commands.DefaultHelpCommand())


@bot.event
async def on_ready():
    init_db()
    log.info(f"Logged in as {bot.user} (id={bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    # Never process the bot's own messages, and skip other bots by default.
    if message.author.bot:
        return

    if CHANNEL_ID and message.channel.id != CHANNEL_ID:
        await bot.process_commands(message)
        return

    parsed = parse_timeguessr_message(message.content)
    if parsed and parsed.rounds:
        guild_id = message.guild.id if message.guild else 0
        status = save_result(
            guild_id,
            message.channel.id,
            message.author.id,
            str(message.author.display_name),
            message.id,
            parsed,
        )
        if status == "inserted":
            await message.add_reaction("✅")
        elif status == "duplicate":
            await message.add_reaction("♻️")
            await message.reply(
                f"Looks like you already logged Game #{parsed.game_number} — keeping your first submission.",
                mention_author=False,
            )

    await bot.process_commands(message)


def _fmt_score(n):
    return f"{n:,.0f}" if isinstance(n, float) else f"{n:,}"


@bot.command(name="leaderboard", aliases=["lb"])
async def leaderboard_cmd(ctx, limit: int = 10):
    rows = fetch_leaderboard(ctx.guild.id, limit=max(1, min(limit, 25)))
    if not rows:
        await ctx.send("No results logged yet. Post a TimeGuessr result to get started!")
        return

    lines = ["**TimeGuessr Leaderboard — ranked by average score**", "```"]
    lines.append(f"{'#':<3}{'Player':<20}{'Avg':>9}{'Best':>9}{'Games':>7}")
    for i, r in enumerate(rows, start=1):
        lines.append(
            f"{i:<3}{r['username'][:19]:<20}{_fmt_score(r['avg_score']):>9}"
            f"{_fmt_score(r['best_score']):>9}{r['games_played']:>7}"
        )
    lines.append("```")
    await ctx.send("\n".join(lines))


@bot.command(name="wins")
async def wins_cmd(ctx, limit: int = 10):
    ranked = fetch_wins(ctx.guild.id, limit=max(1, min(limit, 25)))
    if not ranked:
        await ctx.send("No decisive daily wins recorded yet (or every game so far ended in a tie).")
        return

    lines = ["**Most Daily Wins** (highest score that day, ties don't count)", "```"]
    lines.append(f"{'#':<3}{'Player':<20}{'Wins':>6}")
    for i, ((user_id, username), count) in enumerate(ranked, start=1):
        lines.append(f"{i:<3}{username[:19]:<20}{count:>6}")
    lines.append("```")
    await ctx.send("\n".join(lines))


@bot.command(name="stats")
async def stats_cmd(ctx, member: discord.Member = None):
    member = member or ctx.author
    row = fetch_user_stats(ctx.guild.id, member.id)
    if not row or row["games_played"] == 0:
        await ctx.send(f"No results logged for {member.display_name} yet.")
        return

    embed = discord.Embed(title=f"TimeGuessr stats — {member.display_name}", color=discord.Color.blurple())
    embed.add_field(name="Games played", value=str(row["games_played"]))
    embed.add_field(name="Average score", value=f"{row['avg_score']:,.0f} / {MAX_SCORE:,}")
    embed.add_field(name="Best score", value=f"{row['best_score']:,}")
    embed.add_field(name="Worst score", value=f"{row['worst_score']:,}")
    embed.add_field(name="Total score (all games)", value=f"{row['total_score']:,}")
    await ctx.send(embed=embed)


@bot.command(name="history")
async def history_cmd(ctx, member: discord.Member = None, limit: int = 10):
    member = member or ctx.author
    rows = fetch_user_history(ctx.guild.id, member.id, limit=max(1, min(limit, 25)))
    if not rows:
        await ctx.send(f"No results logged for {member.display_name} yet.")
        return

    lines = [f"**Recent games — {member.display_name}**", "```"]
    lines.append(f"{'Game #':<10}{'Score':>10}")
    for r in rows:
        lines.append(f"{r['game_number']:<10}{r['total_score']:>10,}")
    lines.append("```")
    await ctx.send("\n".join(lines))


@bot.command(name="game")
async def game_cmd(ctx, game_number: int):
    rows = fetch_game(ctx.guild.id, game_number)
    if not rows:
        await ctx.send(f"No one has logged Game #{game_number} yet.")
        return

    lines = [f"**TimeGuessr #{game_number} — results**", "```"]
    for i, r in enumerate(rows, start=1):
        lines.append(f"{i}. {r['username']:<20}{r['total_score']:>10,}")
    lines.append("```")
    await ctx.send("\n".join(lines))


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit(
            "Set the DISCORD_TOKEN environment variable to your bot token before running."
        )
    init_db()
    bot.run(token)
