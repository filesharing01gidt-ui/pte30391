from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# Load local environment variables from .env before reading the bot token.
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

DATABASE_PATH = Path(__file__).resolve().parent / "channel_balances.sqlite3"
BOT_PREFIX = "!"
ANSWER_PREFIX = "?answer-"
CORRECT_ANSWER = "CBAIJHGEFD"
ANSWER_LENGTH = len(CORRECT_ANSWER)
ALLOWED_CHARS = set(CORRECT_ANSWER)
MAX_INT64 = 9_223_372_036_854_775_807

MONEY_PREFIX_PATTERN = re.compile(r"^\s*\$\s*(-?\d+)(.*)$", re.DOTALL)

TRANSPORT_COLOR = 0x2563EB
ERROR_COLOR = 0xDC2626
ADMIN_COLOR = 0x0F766E
AUDIT_COLOR = 0x7C3AED
ANSWER_COLOR = 0x1D4ED8
TOPIC_SYNC_DEBOUNCE_SECONDS = 1.5
TOPIC_SYNC_COOLDOWN_SECONDS = 300.0

TRANSPORT_COSTS: dict[str, tuple[str, int]] = {
    "?taxi": ("Taxi", 50),
    "?bike": ("Bike", 3),
    "?bus": ("Bus", 5),
    "?train": ("Train", 5),
    "?car": ("Car", 10),
    "?walk": ("Walk", 0),
}

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)


def validate_token() -> None:
    """Ensure the bot token is configured via environment variable."""
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is not set. Configure it as an environment variable before running the bot."
        )


@dataclass(slots=True)
class BalanceUpdateResult:
    previous_balance: int
    new_balance: int
    topic_synced: bool
    topic_message: str


class BalanceStorage:
    """SQLite-backed storage for channel balances."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._db_lock = asyncio.Lock()
        self._channel_locks: dict[int, asyncio.Lock] = {}

    async def initialize(self) -> None:
        """Initialize the database schema."""
        async with self._db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS channel_balances (
                        channel_id INTEGER PRIMARY KEY,
                        balance INTEGER NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.commit()
                conn.close()
                logger.info("Storage initialized at %s", self.db_path.resolve())
            except sqlite3.Error:
                logger.exception("Failed to initialize storage")
                raise

    def get_channel_lock(self, channel_id: int) -> asyncio.Lock:
        """Get an async lock for a specific channel ID."""
        if channel_id not in self._channel_locks:
            self._channel_locks[channel_id] = asyncio.Lock()
        return self._channel_locks[channel_id]

    async def get_balance(self, channel_id: int) -> Optional[int]:
        """Retrieve stored balance for a channel."""
        async with self._db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                row = conn.execute(
                    "SELECT balance FROM channel_balances WHERE channel_id = ?",
                    (channel_id,),
                ).fetchone()
                conn.close()
                balance = int(row[0]) if row else None
                logger.debug("Fetched stored balance for channel %s: %s", channel_id, balance)
                return balance
            except (sqlite3.Error, TypeError, ValueError):
                logger.exception("Failed to fetch balance for channel %s", channel_id)
                return None

    async def set_balance(self, channel_id: int, balance: int) -> bool:
        """Persist balance for a channel."""
        async with self._db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                conn.execute(
                    """
                    INSERT INTO channel_balances (channel_id, balance, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(channel_id)
                    DO UPDATE SET balance = excluded.balance, updated_at = CURRENT_TIMESTAMP
                    """,
                    (channel_id, balance),
                )
                conn.commit()
                conn.close()
                logger.info("Persisted balance for channel %s: %s", channel_id, balance)
                return True
            except sqlite3.Error:
                logger.exception("Failed to persist balance for channel %s", channel_id)
                return False

    async def list_all_balances(self) -> list[tuple[int, int]]:
        """Return all tracked balances from the persistent store."""
        async with self._db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                rows = conn.execute(
                    "SELECT channel_id, balance FROM channel_balances"
                ).fetchall()
                conn.close()
                parsed_rows = [(int(channel_id), int(balance)) for channel_id, balance in rows]
                logger.info("Loaded %d balance rows from storage", len(parsed_rows))
                return parsed_rows
            except (sqlite3.Error, TypeError, ValueError):
                logger.exception("Failed to list all balances")
                return []


storage = BalanceStorage(DATABASE_PATH)
_tree_synced = False


class TopicSyncCoordinator:
    """Best-effort, coalesced topic synchronization with cooldown handling."""

    def __init__(self) -> None:
        self._pending_balance: dict[int, int] = {}
        self._channel_refs: dict[int, discord.TextChannel] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._cooldown_until: dict[int, float] = {}

    def _get_channel(self, channel_id: int) -> Optional[discord.TextChannel]:
        cached = bot.get_channel(channel_id)
        if isinstance(cached, discord.TextChannel):
            self._channel_refs[channel_id] = cached
            return cached

        fallback = self._channel_refs.get(channel_id)
        if isinstance(fallback, discord.TextChannel):
            return fallback

        return None

    def enqueue(self, channel: discord.TextChannel, balance: int) -> str:
        """Queue a coalesced topic sync request; never blocks caller."""
        channel_id = channel.id
        self._channel_refs[channel_id] = channel
        self._pending_balance[channel_id] = balance

        now = time.monotonic()
        cooldown_until = self._cooldown_until.get(channel_id, 0.0)
        if cooldown_until > now:
            wait_s = int(cooldown_until - now)
            logger.info("Topic sync queued for channel %s but cooldown is active for %ss", channel_id, wait_s)
            status = "Queued for topic sync after cooldown."
        else:
            status = "Queued for topic sync."

        task = self._tasks.get(channel_id)
        if task and not task.done():
            logger.info("Coalesced topic sync update for channel %s to latest balance %s", channel_id, balance)
            return status

        task = asyncio.create_task(self._worker(channel_id))
        self._tasks[channel_id] = task
        task.add_done_callback(lambda t, cid=channel_id: self._on_task_done(cid, t))
        logger.info("Started topic sync worker for channel %s", channel_id)
        return status

    def _on_task_done(self, channel_id: int, task: asyncio.Task[None]) -> None:
        self._tasks.pop(channel_id, None)
        try:
            task.result()
        except Exception:
            logger.exception("Topic sync worker crashed for channel %s", channel_id)

    async def _worker(self, channel_id: int) -> None:
        while channel_id in self._pending_balance:
            await asyncio.sleep(TOPIC_SYNC_DEBOUNCE_SECONDS)
            desired_balance = self._pending_balance.get(channel_id)
            if desired_balance is None:
                break

            now = time.monotonic()
            cooldown_until = self._cooldown_until.get(channel_id, 0.0)
            if cooldown_until > now:
                wait_s = cooldown_until - now
                logger.info("Skipping topic sync for channel %s due to cooldown (%0.1fs remaining)", channel_id, wait_s)
                await asyncio.sleep(min(wait_s, 30.0))
                continue

            channel = self._get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                logger.warning("Cannot sync topic for channel %s because channel is unavailable", channel_id)
                self._pending_balance.pop(channel_id, None)
                return

            desired_topic = build_synced_topic(channel.topic, desired_balance)
            if (channel.topic or "") == desired_topic:
                logger.info("Skipped topic sync no-op for channel %s; topic already current", channel_id)
                if self._pending_balance.get(channel_id) == desired_balance:
                    self._pending_balance.pop(channel_id, None)
                continue

            try:
                await channel.edit(topic=desired_topic, reason="Balance synchronization")
                logger.info("Topic sync succeeded for channel %s", channel_id)
                if self._pending_balance.get(channel_id) == desired_balance:
                    self._pending_balance.pop(channel_id, None)
            except discord.Forbidden:
                logger.warning("Missing permission to edit topic for channel %s; dropping pending sync", channel_id)
                self._pending_balance.pop(channel_id, None)
                return
            except discord.HTTPException as exc:
                retry_after = float(getattr(exc, "retry_after", 0.0) or 0.0)
                cooldown = max(TOPIC_SYNC_COOLDOWN_SECONDS, retry_after)
                self._cooldown_until[channel_id] = time.monotonic() + cooldown
                logger.warning(
                    "Topic sync failed for channel %s with HTTP %s. Cooldown set to %.1fs",
                    channel_id,
                    getattr(exc, "status", "unknown"),
                    cooldown,
                )
            except Exception:
                logger.exception("Unexpected error syncing topic for channel %s", channel_id)
                self._pending_balance.pop(channel_id, None)
                return


topic_sync = TopicSyncCoordinator()


def base_embed(title: str, description: str, color: int) -> discord.Embed:
    """Create a standardized embed."""
    return discord.Embed(title=title, description=description, color=color)


def invalid_guess_embed(description: str) -> discord.Embed:
    return base_embed("Invalid Guess", description, ERROR_COLOR)


def permission_error_embed() -> discord.Embed:
    return base_embed(
        "Permission Denied",
        "You do not have permission to use this command. Administrator access is required.",
        ERROR_COLOR,
    )


def admin_result_embed(
    *,
    action: str,
    amount: int,
    previous_balance: int,
    new_balance: int,
    channel: discord.TextChannel,
) -> discord.Embed:
    embed = base_embed(
        "Balance Updated",
        f"Action: {action}",
        ADMIN_COLOR,
    )
    embed.add_field(name="Amount", value=f"${amount}", inline=True)
    embed.add_field(name="Previous Balance", value=f"${previous_balance}", inline=True)
    embed.add_field(name="New Balance", value=f"${new_balance}", inline=True)
    embed.add_field(name="Target Channel", value=channel.mention, inline=False)
    return embed


def transport_result_embed(
    *,
    label: str,
    cost: int,
    previous_balance: int,
    new_balance: int,
) -> discord.Embed:
    embed = base_embed("Transport Recorded", "", TRANSPORT_COLOR)
    embed.add_field(name="Amount Deducted", value=f"${cost}", inline=True)
    embed.add_field(name="Previous Balance", value=f"${previous_balance}", inline=True)
    embed.add_field(name="New Balance", value=f"${new_balance}", inline=True)
    embed.add_field(name="Transport", value=label, inline=False)
    return embed


def parse_topic_balance(topic: Optional[str]) -> Optional[int]:
    """Parse leading dollar balance from topic."""
    if not topic:
        return None
    match = MONEY_PREFIX_PATTERN.match(topic)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def build_synced_topic(original_topic: Optional[str], new_balance: int) -> str:
    """Create a normalized topic with a leading $<balance> and preserved remainder."""
    topic = (original_topic or "").strip()
    match = MONEY_PREFIX_PATTERN.match(topic)

    if match:
        remainder = " ".join(match.group(2).strip().split())
    else:
        remainder = " ".join(topic.split())

    return f"${new_balance}" if not remainder else f"${new_balance} {remainder}"


def is_safe_integer(value: int) -> bool:
    return -MAX_INT64 <= value <= MAX_INT64


def queue_topic_sync(channel: discord.TextChannel, new_balance: int) -> tuple[bool, str]:
    """Queue a best-effort topic sync and return status in unified result shape."""
    status = topic_sync.enqueue(channel, new_balance)
    return False, status


async def get_or_initialize_balance(channel: discord.TextChannel) -> Optional[int]:
    """Get balance from DB, optionally initializing from topic if available."""
    stored = await storage.get_balance(channel.id)
    if stored is not None:
        return stored

    parsed = parse_topic_balance(channel.topic)
    if parsed is None:
        return None

    if not is_safe_integer(parsed):
        logger.warning("Parsed topic balance out of range for channel %s", channel.id)
        return None

    persisted = await storage.set_balance(channel.id, parsed)
    if persisted:
        logger.info("Initialized stored balance for channel %s from topic", channel.id)
        return parsed

    return None


async def update_balance_and_topic(
    *,
    channel: discord.TextChannel,
    compute_new_balance: callable,
) -> Optional[BalanceUpdateResult]:
    """Safely update channel balance in storage and sync topic."""
    lock = storage.get_channel_lock(channel.id)
    async with lock:
        previous_balance = await get_or_initialize_balance(channel)
        if previous_balance is None:
            return None

        try:
            new_balance = int(compute_new_balance(previous_balance))
        except Exception:
            logger.exception("Invalid balance calculation for channel %s", channel.id)
            return None

        if not is_safe_integer(new_balance):
            logger.warning("Computed balance out of range for channel %s", channel.id)
            return None

        saved = await storage.set_balance(channel.id, new_balance)
        if not saved:
            return None

        topic_synced, topic_message = queue_topic_sync(channel, new_balance)
        return BalanceUpdateResult(previous_balance, new_balance, topic_synced, topic_message)


async def set_balance_and_topic(channel: discord.TextChannel, new_balance: int) -> Optional[BalanceUpdateResult]:
    """Set balance explicitly while preserving previous value semantics."""
    lock = storage.get_channel_lock(channel.id)
    async with lock:
        previous_balance = await get_or_initialize_balance(channel)
        if previous_balance is None:
            previous_balance = 0

        if not is_safe_integer(new_balance):
            return None

        saved = await storage.set_balance(channel.id, new_balance)
        if not saved:
            return None

        topic_synced, topic_message = queue_topic_sync(channel, new_balance)
        return BalanceUpdateResult(previous_balance, new_balance, topic_synced, topic_message)


def user_is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.administrator


def validate_amount(amount: int) -> Optional[str]:
    if amount < 0:
        return "Amount must be a non-negative integer."
    if not is_safe_integer(amount):
        return "Amount is outside the supported numeric range."
    return None


def validate_text_channel(channel: discord.abc.GuildChannel) -> Optional[str]:
    if not isinstance(channel, discord.TextChannel):
        return "Target channel must be a text channel."
    return None


async def send_interaction_embed(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed,
    ephemeral: bool,
) -> None:
    """Safely send an interaction response or follow-up."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
    except discord.HTTPException:
        logger.exception("Failed to send interaction embed")


async def handle_transport_command(message: discord.Message) -> bool:
    content = message.content.strip()
    if content not in TRANSPORT_COSTS:
        return False

    if message.guild is None or not isinstance(message.channel, discord.TextChannel):
        return True

    channel = message.channel
    label, cost = TRANSPORT_COSTS[content]

    result = await update_balance_and_topic(
        channel=channel,
        compute_new_balance=lambda previous: previous - cost,
    )

    if result is None:
        logger.info(
            "Transport command ignored for channel %s because no stored/initializable balance was found",
            channel.id,
        )
        return True

    logger.info(
        "Transport command applied on channel %s: %s, %s -> %s",
        channel.id,
        label,
        result.previous_balance,
        result.new_balance,
    )

    embed = transport_result_embed(
        label=label,
        cost=cost,
        previous_balance=result.previous_balance,
        new_balance=result.new_balance,
    )
    try:
        await message.reply(embed=embed, mention_author=False)
    except discord.HTTPException:
        logger.exception("Failed to send transport reply")
    return True


async def handle_answer_command(message: discord.Message) -> bool:
    content = message.content
    if not content.startswith(ANSWER_PREFIX):
        return False

    raw_guess = content[len(ANSWER_PREFIX):]

    if not raw_guess:
        await message.reply(
            embed=invalid_guess_embed(
                f"Empty guess. Provide exactly {ANSWER_LENGTH} uppercase letters from: {', '.join(sorted(ALLOWED_CHARS))}."
            ),
            mention_author=False,
        )
        return True

    if raw_guess != raw_guess.upper():
        await message.reply(
            embed=invalid_guess_embed("Guess must be uppercase."),
            mention_author=False,
        )
        return True

    if not raw_guess.isascii() or not raw_guess.isalpha():
        await message.reply(
            embed=invalid_guess_embed("Invalid character detected. Only letters are allowed."),
            mention_author=False,
        )
        return True

    guess = raw_guess

    if len(guess) != ANSWER_LENGTH:
        await message.reply(
            embed=invalid_guess_embed(f"Invalid length. Guess must be exactly {ANSWER_LENGTH} letters."),
            mention_author=False,
        )
        return True

    if any(char not in ALLOWED_CHARS for char in guess):
        await message.reply(
            embed=invalid_guess_embed(
                f"Invalid character detected. Allowed letters: {', '.join(sorted(ALLOWED_CHARS))}."
            ),
            mention_author=False,
        )
        return True

    if len(set(guess)) != len(guess):
        await message.reply(
            embed=invalid_guess_embed("All letters must be unique."),
            mention_author=False,
        )
        return True

    score = sum(
        1 for guessed_char, correct_char in zip(guess, CORRECT_ANSWER) if guessed_char == correct_char
    )

    score_embed = base_embed("Guess Score", f"{score}/{ANSWER_LENGTH}", ANSWER_COLOR)
    await message.reply(embed=score_embed, mention_author=False)
    return True


@bot.tree.command(name="add", description="Add an amount to a channel balance.")
@app_commands.check(user_is_admin)
@app_commands.describe(amount="Non-negative amount to add", channel="Target text channel")
async def add_balance(
    interaction: discord.Interaction,
    amount: int,
    channel: discord.abc.GuildChannel,
) -> None:
    amount_error = validate_amount(amount)
    if amount_error:
        await send_interaction_embed(
            interaction,
            embed=base_embed("Validation Error", amount_error, ERROR_COLOR),
            ephemeral=True,
        )
        return

    channel_error = validate_text_channel(channel)
    if channel_error:
        await send_interaction_embed(
            interaction,
            embed=base_embed("Validation Error", channel_error, ERROR_COLOR),
            ephemeral=True,
        )
        return

    text_channel = channel
    await interaction.response.defer(ephemeral=True)
    lock = storage.get_channel_lock(text_channel.id)
    async with lock:
        previous = await get_or_initialize_balance(text_channel)
        if previous is None:
            previous = 0

        new_balance = previous + amount
        if not is_safe_integer(new_balance):
            await send_interaction_embed(
                interaction,
                embed=base_embed("Validation Error", "Resulting balance is outside the supported range.", ERROR_COLOR),
                ephemeral=True,
            )
            return

        saved = await storage.set_balance(text_channel.id, new_balance)
        if not saved:
            await send_interaction_embed(
                interaction,
                embed=base_embed("Storage Error", "Could not update persistent storage.", ERROR_COLOR),
                ephemeral=True,
            )
            return

        queue_topic_sync(text_channel, new_balance)

    logger.info("/add updated channel %s to %s", text_channel.id, new_balance)

    embed = admin_result_embed(
        action="Add",
        amount=amount,
        previous_balance=previous,
        new_balance=new_balance,
        channel=text_channel,
    )
    await send_interaction_embed(interaction, embed=embed, ephemeral=True)


@bot.tree.command(name="set", description="Set a channel balance.")
@app_commands.check(user_is_admin)
@app_commands.describe(amount="Non-negative amount to set", channel="Target text channel")
async def set_balance_command(
    interaction: discord.Interaction,
    amount: int,
    channel: discord.abc.GuildChannel,
) -> None:
    amount_error = validate_amount(amount)
    if amount_error:
        await send_interaction_embed(
            interaction,
            embed=base_embed("Validation Error", amount_error, ERROR_COLOR),
            ephemeral=True,
        )
        return

    channel_error = validate_text_channel(channel)
    if channel_error:
        await send_interaction_embed(
            interaction,
            embed=base_embed("Validation Error", channel_error, ERROR_COLOR),
            ephemeral=True,
        )
        return

    text_channel = channel
    await interaction.response.defer(ephemeral=True)
    result = await set_balance_and_topic(text_channel, amount)
    if result is None:
        await send_interaction_embed(
            interaction,
            embed=base_embed("Storage Error", "Could not update channel balance.", ERROR_COLOR),
            ephemeral=True,
        )
        return

    embed = admin_result_embed(
        action="Set",
        amount=amount,
        previous_balance=result.previous_balance,
        new_balance=result.new_balance,
        channel=text_channel,
    )
    logger.info("/set updated channel %s to %s", text_channel.id, result.new_balance)
    await send_interaction_embed(interaction, embed=embed, ephemeral=True)


@bot.tree.command(name="remove", description="Remove an amount from a channel balance.")
@app_commands.check(user_is_admin)
@app_commands.describe(amount="Non-negative amount to remove", channel="Target text channel")
async def remove_balance(
    interaction: discord.Interaction,
    amount: int,
    channel: discord.abc.GuildChannel,
) -> None:
    amount_error = validate_amount(amount)
    if amount_error:
        await send_interaction_embed(
            interaction,
            embed=base_embed("Validation Error", amount_error, ERROR_COLOR),
            ephemeral=True,
        )
        return

    channel_error = validate_text_channel(channel)
    if channel_error:
        await send_interaction_embed(
            interaction,
            embed=base_embed("Validation Error", channel_error, ERROR_COLOR),
            ephemeral=True,
        )
        return

    text_channel = channel
    await interaction.response.defer(ephemeral=True)
    lock = storage.get_channel_lock(text_channel.id)
    async with lock:
        previous = await get_or_initialize_balance(text_channel)
        if previous is None:
            previous = 0

        new_balance = previous - amount
        if not is_safe_integer(new_balance):
            await send_interaction_embed(
                interaction,
                embed=base_embed("Validation Error", "Resulting balance is outside the supported range.", ERROR_COLOR),
                ephemeral=True,
            )
            return

        saved = await storage.set_balance(text_channel.id, new_balance)
        if not saved:
            await send_interaction_embed(
                interaction,
                embed=base_embed("Storage Error", "Could not update persistent storage.", ERROR_COLOR),
                ephemeral=True,
            )
            return

        queue_topic_sync(text_channel, new_balance)

    logger.info("/remove updated channel %s to %s", text_channel.id, new_balance)

    embed = admin_result_embed(
        action="Remove",
        amount=amount,
        previous_balance=previous,
        new_balance=new_balance,
        channel=text_channel,
    )
    await send_interaction_embed(interaction, embed=embed, ephemeral=True)


@bot.tree.command(name="balances", description="Audit all tracked channel balances.")
@app_commands.check(user_is_admin)
async def balances_audit(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    rows = await storage.list_all_balances()
    logger.info("/balances requested. Returning %d rows", len(rows))
    if not rows:
        await send_interaction_embed(
            interaction,
            embed=base_embed(
                "Tracked Channel Balances",
                "No channel balances are currently stored.",
                AUDIT_COLOR,
            ),
            ephemeral=True,
        )
        return

    entries: list[tuple[str, str]] = []
    for channel_id, balance in rows:
        resolved_channel = bot.get_channel(channel_id)
        if isinstance(resolved_channel, discord.TextChannel):
            display_name = f"#{resolved_channel.name}"
            display_channel = resolved_channel.mention
        else:
            display_name = f"[Deleted Channel] (ID: {channel_id})"
            display_channel = display_name
        line = f"{display_channel} — ${balance}"
        entries.append((display_name.lower(), line))

    entries.sort(key=lambda item: item[0])

    lines = [entry_line for _, entry_line in entries]
    embeds: list[discord.Embed] = []
    current_chunk: list[str] = []
    current_len = 0
    max_len = 3500

    for line in lines:
        additional = len(line) + 1
        if current_len + additional > max_len and current_chunk:
            embed = base_embed(
                "Tracked Channel Balances",
                "\n".join(current_chunk),
                AUDIT_COLOR,
            )
            embeds.append(embed)
            current_chunk = [line]
            current_len = len(line)
        else:
            current_chunk.append(line)
            current_len += additional

    if current_chunk:
        embeds.append(base_embed("Tracked Channel Balances", "\n".join(current_chunk), AUDIT_COLOR))

    try:
        if not interaction.response.is_done() and embeds:
            await interaction.response.send_message(embed=embeds[0], ephemeral=True)
            for embed in embeds[1:]:
                await interaction.followup.send(embed=embed, ephemeral=True)
        elif embeds:
            for embed in embeds:
                await interaction.followup.send(embed=embed, ephemeral=True)
    except discord.HTTPException:
        logger.exception("Failed to send balances audit response")


def simple_topic_sync_embed(title: str, channel: discord.TextChannel, balance: Optional[int] = None) -> discord.Embed:
    embed = base_embed(title, "", ADMIN_COLOR if "Failed" not in title else ERROR_COLOR)
    embed.add_field(name="Channel", value=channel.mention, inline=False)
    if balance is not None:
        embed.add_field(name="Balance", value=f"${balance}", inline=False)
    return embed


async def try_single_topic_sync(channel: discord.TextChannel, balance: int) -> tuple[str, bool]:
    """Attempt exactly one immediate topic sync, respecting cooldown for safety."""
    now = time.monotonic()
    cooldown_until = topic_sync._cooldown_until.get(channel.id, 0.0)
    if cooldown_until > now:
        logger.info("Manual topic sync skipped for channel %s due to active cooldown", channel.id)
        return "cooldown", False

    desired_topic = build_synced_topic(channel.topic, balance)
    if (channel.topic or "") == desired_topic:
        logger.info("Manual topic sync no-op for channel %s; already synchronized", channel.id)
        return "already", True

    try:
        await asyncio.wait_for(
            channel.edit(topic=desired_topic, reason="Manual topic resync"),
            timeout=2.0,
        )
        logger.info("Manual topic sync succeeded for channel %s", channel.id)
        return "success", True
    except asyncio.TimeoutError:
        topic_sync._cooldown_until[channel.id] = time.monotonic() + TOPIC_SYNC_COOLDOWN_SECONDS
        logger.warning("Manual topic sync timed out for channel %s", channel.id)
        return "failed", False
    except discord.Forbidden:
        logger.warning("Manual topic sync forbidden for channel %s", channel.id)
        return "failed", False
    except discord.HTTPException as exc:
        retry_after = float(getattr(exc, "retry_after", 0.0) or 0.0)
        cooldown = max(TOPIC_SYNC_COOLDOWN_SECONDS, retry_after)
        topic_sync._cooldown_until[channel.id] = time.monotonic() + cooldown
        logger.warning("Manual topic sync HTTP failure for channel %s, status=%s", channel.id, getattr(exc, "status", "unknown"))
        return "failed", False
    except Exception:
        logger.exception("Manual topic sync unexpected failure for channel %s", channel.id)
        return "failed", False


@bot.tree.command(name="resynctopic", description="Manually resync one channel topic from stored balance.")
@app_commands.check(user_is_admin)
@app_commands.describe(channel="Target text channel")
async def resync_topic(interaction: discord.Interaction, channel: discord.abc.GuildChannel) -> None:
    channel_error = validate_text_channel(channel)
    if channel_error:
        await send_interaction_embed(
            interaction,
            embed=base_embed("Validation Error", channel_error, ERROR_COLOR),
            ephemeral=True,
        )
        return

    text_channel = channel
    await interaction.response.defer(ephemeral=True)

    stored_balance = await storage.get_balance(text_channel.id)
    if stored_balance is None:
        await send_interaction_embed(
            interaction,
            embed=simple_topic_sync_embed("No Stored Balance", text_channel),
            ephemeral=True,
        )
        return

    status, _ok = await try_single_topic_sync(text_channel, stored_balance)
    if status == "already":
        embed = simple_topic_sync_embed("Topic Already Synced", text_channel, stored_balance)
    elif status == "success":
        embed = simple_topic_sync_embed("Topic Resynced", text_channel, stored_balance)
    else:
        embed = simple_topic_sync_embed("Topic Sync Failed", text_channel, stored_balance)

    await send_interaction_embed(interaction, embed=embed, ephemeral=True)



@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.CheckFailure):
        await send_interaction_embed(interaction, embed=permission_error_embed(), ephemeral=True)
        return

    if isinstance(error, app_commands.CommandInvokeError):
        logger.exception("App command invoke error: %s", error)
        await send_interaction_embed(
            interaction,
            embed=base_embed("Command Error", "An internal error occurred while processing the command.", ERROR_COLOR),
            ephemeral=True,
        )
        return

    logger.exception("Unhandled app command error: %s", error)
    await send_interaction_embed(
        interaction,
        embed=base_embed("Command Error", "The command failed due to an unexpected error.", ERROR_COLOR),
        ephemeral=True,
    )


@bot.event
async def on_ready() -> None:
    global _tree_synced

    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "unknown")
    if not _tree_synced:
        try:
            synced = await bot.tree.sync()
            logger.info("Synced %d app commands", len(synced))
            _tree_synced = True
        except discord.HTTPException:
            logger.exception("Failed to sync app command tree")


@bot.event
async def setup_hook() -> None:
    await storage.initialize()


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    is_transport_command = message.content.strip() in TRANSPORT_COSTS

    if is_transport_command:
        try:
            await handle_transport_command(message)
        except Exception:
            logger.exception("Error while processing transport command")
        return

    try:
        if await handle_answer_command(message):
            return
    except Exception:
        logger.exception("Error while processing answer command")
        return

    await bot.process_commands(message)


validate_token()
bot.run(TOKEN)
