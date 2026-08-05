"""
TimeGuessr + MapTap Scorekeeper Bot
====================================

Watches a Discord channel for daily result posts and keeps a leaderboard,
win counts, and per-user/per-guess stats for two games:

TimeGuessr — keyed by game number:
    TimeGuessr #1161 — 41,255/50,000
    1️⃣ 🏆9,179 · 📅 4y · 🌍 3.3mi
    2️⃣ 🏆7,461 · 📅 8y · 🌍 1198m
    3️⃣ 🏆6,270 · 📅 11y · 🌍 142.9mi
    4️⃣ 🏆8,745 · 📅 3y · 🌍 220.3mi
    5️⃣ 🏆9,600 · 📅 3y · 🌍 32m
    https://timeguessr.com

MapTap — keyed by date, final score isn't just the sum of guesses:
    [www.maptap.gg](https://www.maptap.gg) August 4
    99🎯 100🎯 91👑 97🔥 89👑
    Final score: 939

Commands:
    TimeGuessr: !leaderboard  !wins  !stats @user  !history @user  !game 1161
    MapTap:     !mtleaderboard  !mtwins  !mtstats @user  !mthistory @user
                !mtday August 4   !mtguess 1 [August 4]
    Both:       !backfill [#channel] [limit]   (retroactively scan history)

Setup
-----
1. pip install -r requirements.txt
2. Create a bot at https://discord.com/developers/applications
   - Bot tab -> enable "MESSAGE CONTENT INTENT" and "SERVER MEMBERS INTENT"
   - Copy the bot token
3. Invite it to your server with "Send Messages" + "Read Message History" +
   "View Channel" + "Add Reactions" permissions.
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
# MapTap parsing
#
# Example post:
#   [www.maptap.gg](https://www.maptap.gg) August 4
#   99🎯 100🎯 91👑 97🔥 89👑
#   Final score: 939
#
# Unlike TimeGuessr, MapTap is keyed by date (not a game number), guesses
# are on one line each tagged with an emoji reflecting some scoring
# threshold — the scale itself is presumably fixed on MapTap's end, we
# just don't know where the cutoffs are, so we store and reproduce
# whichever emoji was posted rather than guessing at the goalposts.
#
# The final score isn't a plain sum of the 5 guesses — it's a weighted
# sum, with each guess position carrying a multiplier: [1, 1, 2, 3, 3]
# for guesses 1-5. Confirmed against the sample above:
# 99×1 + 100×1 + 91×2 + 97×3 + 89×3 = 939.
#
# We still parse final_score directly off the post (it's right there,
# no need to trust our own arithmetic over what MapTap itself reported),
# but we compute the expected value from MAPTAP_MULTIPLIERS as a sanity
# check — a mismatch usually means the parse went wrong somewhere (wrong
# guesses picked up, wrong final score matched, etc.) rather than that
# MapTap's formula changed, so it's logged as a warning rather than
# silently trusted or silently discarded.
# --------------------------------------------------------------------------

MAPTAP_MULTIPLIERS = [1, 1, 2, 3, 3]  # index 0 = guess #1, etc.

MAPTAP_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")  # markdown [text](url) -> text

_MONTHS = [
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
]

MAPTAP_DATE_RE = re.compile(
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?",
    re.IGNORECASE,
)

# score immediately followed by an emoji glyph, e.g. "99🎯"
MAPTAP_GUESS_RE = re.compile(
    r"(?P<score>\d{1,4})\s*(?P<emoji>[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]+)"
)

MAPTAP_FINAL_RE = re.compile(r"final\s*score:?\s*(?P<final>[\d,]+)", re.IGNORECASE)


class ParsedMaptapResult:
    def __init__(self, game_date, final_score, guesses, expected_score=None):
        self.game_date = game_date  # date object
        self.final_score = final_score
        self.guesses = guesses  # list of dicts: index, score, emoji
        self.expected_score = expected_score  # weighted-sum sanity check, or None if guess count != multiplier count


def _resolve_maptap_year(month_num, day, explicit_year, reference_dt):
    """MapTap posts usually omit the year. Use the message's own timestamp
    to infer it, correcting for the rare case a post lands right at a
    year boundary (e.g. bot posts Jan 1 about a Dec 31 game)."""
    import datetime as _dt

    if explicit_year:
        return int(explicit_year)

    year = reference_dt.year
    try:
        candidate = _dt.date(year, month_num, day)
    except ValueError:
        return year  # shouldn't happen with valid month/day, fall back safely

    if (reference_dt.date() - candidate).days > 300:
        year += 1
    elif (candidate - reference_dt.date()).days > 300:
        year -= 1
    return year


def parse_maptap_message(content: str, reference_dt) -> ParsedMaptapResult | None:
    if "maptap.gg" not in content.lower():
        return None

    stripped = MAPTAP_LINK_RE.sub(r"\1", content)

    date_match = MAPTAP_DATE_RE.search(stripped)
    final_match = MAPTAP_FINAL_RE.search(stripped)
    if not (date_match and final_match):
        return None

    month_name = date_match.group(1).lower()
    month_num = next(
        (i + 1 for i, full in enumerate(_MONTHS) if full.startswith(month_name[:3])),
        None,
    )
    if month_num is None:
        return None

    day = int(date_match.group(2))
    year = _resolve_maptap_year(month_num, day, date_match.group(3), reference_dt)

    import datetime as _dt

    try:
        game_date = _dt.date(year, month_num, day)
    except ValueError:
        return None

    final_score = int(final_match.group("final").replace(",", ""))

    # Guesses line: exclude the "Final score: N" chunk so its number can't
    # accidentally be picked up (it never has a trailing emoji, but this
    # keeps intent explicit rather than relying on that alone).
    guesses_source = stripped[: final_match.start()]
    guesses = []
    for i, m in enumerate(MAPTAP_GUESS_RE.finditer(guesses_source), start=1):
        guesses.append(
            {
                "index": i,
                "score": int(m.group("score")),
                "emoji": m.group("emoji"),
            }
        )

    if not guesses:
        return None

    expected_score = None
    if len(guesses) == len(MAPTAP_MULTIPLIERS):
        expected_score = sum(g["score"] * MAPTAP_MULTIPLIERS[i] for i, g in enumerate(guesses))
        if expected_score != final_score:
            log.warning(
                "MapTap score mismatch for %s: posted final=%s, weighted-sum expected=%s "
                "(guesses=%s) — parse may have picked up the wrong numbers.",
                game_date.isoformat(), final_score, expected_score, guesses,
            )

    return ParsedMaptapResult(game_date, final_score, guesses, expected_score)


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS maptap_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                game_date TEXT NOT NULL,
                final_score INTEGER NOT NULL,
                guesses_json TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(guild_id, user_id, game_date)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_maptap_date ON maptap_results(guild_id, game_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_maptap_user ON maptap_results(guild_id, user_id)"
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
# MapTap storage
# --------------------------------------------------------------------------

def save_maptap_result(guild_id, channel_id, user_id, username, message_id, parsed: ParsedMaptapResult):
    """Returns 'inserted' or 'duplicate'."""
    import json

    with closing(get_db()) as conn, conn:
        try:
            conn.execute(
                """
                INSERT INTO maptap_results
                    (guild_id, channel_id, user_id, username, game_date,
                     final_score, guesses_json, message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    channel_id,
                    user_id,
                    username,
                    parsed.game_date.isoformat(),
                    parsed.final_score,
                    json.dumps(parsed.guesses),
                    message_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return "inserted"
        except sqlite3.IntegrityError:
            return "duplicate"


def fetch_maptap_leaderboard(guild_id, limit=20):
    with closing(get_db()) as conn:
        rows = conn.execute(
            """
            SELECT user_id, username,
                   COUNT(*) AS games_played,
                   AVG(final_score) AS avg_score,
                   MAX(final_score) AS best_score,
                   SUM(final_score) AS total_score
            FROM maptap_results
            WHERE guild_id = ?
            GROUP BY user_id
            ORDER BY avg_score DESC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
    return rows


def fetch_maptap_wins(guild_id, limit=20):
    """A 'win' = strictly highest final_score for a given date (ties = no winner)."""
    with closing(get_db()) as conn:
        day_rows = conn.execute(
            """
            SELECT game_date, user_id, username, final_score
            FROM maptap_results
            WHERE guild_id = ?
            ORDER BY game_date
            """,
            (guild_id,),
        ).fetchall()

    per_day = {}
    for r in day_rows:
        per_day.setdefault(r["game_date"], []).append(r)

    wins = {}
    for game_date, entries in per_day.items():
        top_score = max(e["final_score"] for e in entries)
        winners = [e for e in entries if e["final_score"] == top_score]
        if len(winners) == 1:
            w = winners[0]
            key = (w["user_id"], w["username"])
            wins[key] = wins.get(key, 0) + 1

    ranked = sorted(wins.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return ranked


def fetch_maptap_user_stats(guild_id, user_id):
    with closing(get_db()) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS games_played,
                   AVG(final_score) AS avg_score,
                   MAX(final_score) AS best_score,
                   MIN(final_score) AS worst_score,
                   SUM(final_score) AS total_score
            FROM maptap_results
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()
    return row


def fetch_maptap_user_history(guild_id, user_id, limit=10):
    with closing(get_db()) as conn:
        rows = conn.execute(
            """
            SELECT game_date, final_score, guesses_json
            FROM maptap_results
            WHERE guild_id = ? AND user_id = ?
            ORDER BY game_date DESC
            LIMIT ?
            """,
            (guild_id, user_id, limit),
        ).fetchall()
    return rows


def fetch_maptap_day(guild_id, game_date_iso):
    with closing(get_db()) as conn:
        rows = conn.execute(
            """
            SELECT username, final_score, guesses_json
            FROM maptap_results
            WHERE guild_id = ? AND game_date = ?
            ORDER BY final_score DESC
            """,
            (guild_id, game_date_iso),
        ).fetchall()
    return rows


def fetch_maptap_guess_position_avg(guild_id, position, game_date_iso=None, user_id=None):
    """Average score for a specific guess position (1-indexed).

    Optionally scoped to one date (average for that guess on that day) and/or
    one user (that user's average for that guess position across all days).
    Reads guesses_json in Python since guess position lives inside the JSON
    blob rather than its own column — fine at the data volumes this bot
    deals with (one row per user per day).
    """
    import json

    query = "SELECT guesses_json FROM maptap_results WHERE guild_id = ?"
    params = [guild_id]
    if game_date_iso:
        query += " AND game_date = ?"
        params.append(game_date_iso)
    if user_id:
        query += " AND user_id = ?"
        params.append(user_id)

    with closing(get_db()) as conn:
        rows = conn.execute(query, params).fetchall()

    scores = []
    for r in rows:
        guesses = json.loads(r["guesses_json"])
        match = next((g for g in guesses if g["index"] == position), None)
        if match:
            scores.append(match["score"])

    if not scores:
        return None, 0
    return sum(scores) / len(scores), len(scores)


def fetch_maptap_user_guess_averages(guild_id, user_id):
    """Returns {position: (avg, n)} across all of a user's games, for however
    many guess positions actually appear in their history."""
    import json

    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT guesses_json FROM maptap_results WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchall()

    by_position = {}
    for r in rows:
        for g in json.loads(r["guesses_json"]):
            by_position.setdefault(g["index"], []).append(g["score"])

    return {pos: (sum(vals) / len(vals), len(vals)) for pos, vals in sorted(by_position.items())}


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


def try_log_message(message: discord.Message):
    """Parse + save a single message if it's a TimeGuessr result.

    Returns (status, parsed) where status is 'inserted', 'duplicate', or
    None if the message wasn't a TimeGuessr result at all (parsed is None
    in that case). Shared by the live on_message handler and the
    !backfill command so both use identical logic.
    """
    if message.author.bot:
        return None, None

    parsed = parse_timeguessr_message(message.content)
    if not (parsed and parsed.rounds):
        return None, None

    guild_id = message.guild.id if message.guild else 0
    status = save_result(
        guild_id,
        message.channel.id,
        message.author.id,
        str(message.author.display_name),
        message.id,
        parsed,
    )
    return status, parsed


def try_log_maptap_message(message: discord.Message):
    """Same idea as try_log_message but for MapTap posts."""
    if message.author.bot:
        return None, None

    parsed = parse_maptap_message(message.content, message.created_at)
    if not parsed:
        return None, None

    guild_id = message.guild.id if message.guild else 0
    status = save_maptap_result(
        guild_id,
        message.channel.id,
        message.author.id,
        str(message.author.display_name),
        message.id,
        parsed,
    )
    return status, parsed


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if CHANNEL_ID and message.channel.id != CHANNEL_ID:
        await bot.process_commands(message)
        return

    status, parsed = try_log_message(message)
    if status == "inserted":
        await message.add_reaction("✅")
    elif status == "duplicate":
        await message.add_reaction("♻️")
        await message.reply(
            f"Looks like you already logged Game #{parsed.game_number} — keeping your first submission.",
            mention_author=False,
        )

    mt_status, mt_parsed = try_log_maptap_message(message)
    if mt_status == "inserted":
        await message.add_reaction("✅")
        if mt_parsed.expected_score is not None and mt_parsed.expected_score != mt_parsed.final_score:
            await message.add_reaction("⚠️")
            await message.reply(
                f"{message.author.mention} heads up — your posted final score "
                f"(**{mt_parsed.final_score:,}**) doesn't match what your guesses add up to "
                f"with the standard weighting (**{mt_parsed.expected_score:,}**). "
                f"Still logged as posted — just flagging it as non-compliant.",
                mention_author=True,
            )
    elif mt_status == "duplicate":
        await message.add_reaction("♻️")
        await message.reply(
            f"Looks like you already logged MapTap for {mt_parsed.game_date.isoformat()} — "
            f"keeping your first submission.",
            mention_author=False,
        )

    await bot.process_commands(message)


@bot.command(name="backfill")
@commands.has_permissions(manage_guild=True)
async def backfill_cmd(ctx, channel: discord.TextChannel = None, limit: int = None):
    """Scan a channel's history and retroactively log any TimeGuessr posts found.

    Usage:
        !backfill                -> scans the current channel, entire history
        !backfill #results       -> scans #results, entire history
        !backfill #results 5000  -> scans only the most recent 5000 messages

    Requires "Manage Server" permission (backfill can take a while and
    hits Discord's API a lot, so it's gated to avoid accidental re-runs).
    """
    channel = channel or ctx.channel

    perms = channel.permissions_for(ctx.guild.me)
    if not (perms.read_messages and perms.read_message_history):
        await ctx.send(
            f"I don't have permission to read message history in {channel.mention}. "
            f"Grant me 'View Channel' and 'Read Message History' there and try again."
        )
        return

    status_msg = await ctx.send(
        f"Scanning {channel.mention} for past TimeGuessr and MapTap results"
        + (f" (last {limit} messages)" if limit else " (entire history)")
        + "… this can take a while for large channels."
    )

    scanned = 0
    inserted = 0
    duplicates = 0
    mt_inserted = 0
    mt_duplicates = 0

    async for message in channel.history(limit=limit, oldest_first=True):
        scanned += 1
        result, _parsed = try_log_message(message)
        if result == "inserted":
            inserted += 1
        elif result == "duplicate":
            duplicates += 1

        mt_result, _mt_parsed = try_log_maptap_message(message)
        if mt_result == "inserted":
            mt_inserted += 1
        elif mt_result == "duplicate":
            mt_duplicates += 1

        if scanned % 500 == 0:
            await status_msg.edit(
                content=(
                    f"Still scanning {channel.mention}… {scanned:,} messages checked, "
                    f"{inserted:,} TimeGuessr + {mt_inserted:,} MapTap results logged so far."
                )
            )

    await status_msg.edit(
        content=(
            f"✅ Backfill of {channel.mention} complete.\n"
            f"Scanned **{scanned:,}** messages\n"
            f"TimeGuessr: logged **{inserted:,}** new · skipped **{duplicates:,}** duplicates\n"
            f"MapTap: logged **{mt_inserted:,}** new · skipped **{mt_duplicates:,}** duplicates"
        )
    )


@backfill_cmd.error
async def backfill_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need the 'Manage Server' permission to run a backfill.")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("Couldn't find that channel — mention it like `!backfill #results`.")
    else:
        raise error


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


# --------------------------------------------------------------------------
# MapTap commands
# --------------------------------------------------------------------------

def _parse_date_arg(date_str: str):
    """Accepts 'August 4', 'Aug 4 2026', or '2026-08-04'. Returns a date or None."""
    import datetime as _dt

    try:
        return _dt.date.fromisoformat(date_str)
    except ValueError:
        pass

    m = MAPTAP_DATE_RE.search(date_str)
    if not m:
        return None
    month_name = m.group(1).lower()
    month_num = next(
        (i + 1 for i, full in enumerate(_MONTHS) if full.startswith(month_name[:3])),
        None,
    )
    if month_num is None:
        return None
    day = int(m.group(2))
    year = int(m.group(3)) if m.group(3) else _dt.date.today().year
    try:
        return _dt.date(year, month_num, day)
    except ValueError:
        return None


@bot.command(name="mtleaderboard", aliases=["mtlb"])
async def maptap_leaderboard_cmd(ctx, limit: int = 10):
    rows = fetch_maptap_leaderboard(ctx.guild.id, limit=max(1, min(limit, 25)))
    if not rows:
        await ctx.send("No MapTap results logged yet. Post a MapTap result to get started!")
        return

    lines = ["**MapTap Leaderboard — ranked by average final score**", "```"]
    lines.append(f"{'#':<3}{'Player':<20}{'Avg':>9}{'Best':>9}{'Games':>7}")
    for i, r in enumerate(rows, start=1):
        lines.append(
            f"{i:<3}{r['username'][:19]:<20}{_fmt_score(r['avg_score']):>9}"
            f"{_fmt_score(r['best_score']):>9}{r['games_played']:>7}"
        )
    lines.append("```")
    await ctx.send("\n".join(lines))


@bot.command(name="mtwins")
async def maptap_wins_cmd(ctx, limit: int = 10):
    ranked = fetch_maptap_wins(ctx.guild.id, limit=max(1, min(limit, 25)))
    if not ranked:
        await ctx.send("No decisive MapTap wins recorded yet (or every day so far ended in a tie).")
        return

    lines = ["**MapTap — Most Daily Wins** (highest final score that day, ties don't count)", "```"]
    lines.append(f"{'#':<3}{'Player':<20}{'Wins':>6}")
    for i, ((user_id, username), count) in enumerate(ranked, start=1):
        lines.append(f"{i:<3}{username[:19]:<20}{count:>6}")
    lines.append("```")
    await ctx.send("\n".join(lines))


@bot.command(name="mtstats")
async def maptap_stats_cmd(ctx, member: discord.Member = None):
    member = member or ctx.author
    row = fetch_maptap_user_stats(ctx.guild.id, member.id)
    if not row or row["games_played"] == 0:
        await ctx.send(f"No MapTap results logged for {member.display_name} yet.")
        return

    embed = discord.Embed(title=f"MapTap stats — {member.display_name}", color=discord.Color.green())
    embed.add_field(name="Games played", value=str(row["games_played"]))
    embed.add_field(name="Average final score", value=f"{row['avg_score']:,.0f}")
    embed.add_field(name="Best final score", value=f"{row['best_score']:,}")
    embed.add_field(name="Worst final score", value=f"{row['worst_score']:,}")
    embed.add_field(name="Total (all games)", value=f"{row['total_score']:,}")

    guess_avgs = fetch_maptap_user_guess_averages(ctx.guild.id, member.id)
    if guess_avgs:
        breakdown = " · ".join(f"#{pos}: {avg:,.0f}" for pos, (avg, n) in guess_avgs.items())
        embed.add_field(name="Average per guess position", value=breakdown, inline=False)

    await ctx.send(embed=embed)


@bot.command(name="mthistory")
async def maptap_history_cmd(ctx, member: discord.Member = None, limit: int = 10):
    member = member or ctx.author
    rows = fetch_maptap_user_history(ctx.guild.id, member.id, limit=max(1, min(limit, 25)))
    if not rows:
        await ctx.send(f"No MapTap results logged for {member.display_name} yet.")
        return

    import json

    lines = [f"**Recent MapTap games — {member.display_name}**", "```"]
    lines.append(f"{'Date':<12}{'Final':>8}  Guesses")
    for r in rows:
        guesses = json.loads(r["guesses_json"])
        guess_str = " ".join(f"{g['score']}{g['emoji']}" for g in guesses)
        lines.append(f"{r['game_date']:<12}{r['final_score']:>8,}  {guess_str}")
    lines.append("```")
    await ctx.send("\n".join(lines))


@bot.command(name="mtday")
async def maptap_day_cmd(ctx, *, date_str: str):
    game_date = _parse_date_arg(date_str)
    if not game_date:
        await ctx.send("Couldn't parse that date — try `!mtday August 4` or `!mtday 2026-08-04`.")
        return

    rows = fetch_maptap_day(ctx.guild.id, game_date.isoformat())
    if not rows:
        await ctx.send(f"No one has logged MapTap for {game_date.isoformat()} yet.")
        return

    import json

    lines = [f"**MapTap — {game_date.isoformat()}**", "```"]
    for i, r in enumerate(rows, start=1):
        guesses = json.loads(r["guesses_json"])
        guess_str = " ".join(f"{g['score']}{g['emoji']}" for g in guesses)
        lines.append(f"{i}. {r['username']:<18}{r['final_score']:>7,}  {guess_str}")
    lines.append("```")
    await ctx.send("\n".join(lines))


@bot.command(name="mtguess")
async def maptap_guess_cmd(ctx, position: int, *, date_str: str = None):
    """Average score for a specific guess position.

    !mtguess 1              -> average 1st guess, across all days & players
    !mtguess 1 August 4     -> average 1st guess, just that day
    """
    game_date_iso = None
    if date_str:
        game_date = _parse_date_arg(date_str)
        if not game_date:
            await ctx.send("Couldn't parse that date — try `!mtguess 1 August 4` or `!mtguess 1 2026-08-04`.")
            return
        game_date_iso = game_date.isoformat()

    avg, n = fetch_maptap_guess_position_avg(ctx.guild.id, position, game_date_iso=game_date_iso)
    if avg is None:
        scope = f" on {game_date_iso}" if game_date_iso else ""
        await ctx.send(f"No data for guess #{position}{scope} yet.")
        return

    scope = f" on {game_date_iso}" if game_date_iso else " across all days"
    await ctx.send(f"**Average for guess #{position}{scope}:** {avg:,.1f}  (n={n})")


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit(
            "Set the DISCORD_TOKEN environment variable to your bot token before running."
        )
    init_db()
    bot.run(token)
