from __future__ import annotations

import logging
import os
import re

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

BOT_PREFIX = "!"
ANSWER_PREFIX = "?answer-"
CORRECT_ANSWER = "CBAIJHGEFD"
ANSWER_LENGTH = len(CORRECT_ANSWER)
ALLOWED_CHARS = set(CORRECT_ANSWER)

TRANSPORT_COLOR = 0x2563EB
ERROR_COLOR = 0xDC2626
ADMIN_COLOR = 0x0F766E
ANSWER_COLOR = 0x1D4ED8

TRANSPORT_COSTS: dict[str, tuple[str, int]] = {
    "?taxi": ("Taxi", 50),
    "?bike": ("Bike", 3),
    "?bus": ("Bus", 5),
    "?train": ("Train", 5),
    "?car": ("Car", 10),
    "?walk": ("Walk", 0),
}

MONEY_PATTERN = re.compile(r"^\$(\d+)")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)


def validate_token() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set. Configure it before running the bot.")


def base_embed(title: str, description: str, color: int) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def invalid_guess_embed(description: str) -> discord.Embed:
    return base_embed("Invalid Guess", description, ERROR_COLOR)


def permission_error_embed() -> discord.Embed:
    return base_embed(
        "Permission Denied",
        "You do not have permission to use this command. Administrator access is required.",
        ERROR_COLOR,
    )


def transport_result_embed(
    *,
    label: str,
    cost: int,
    previous_balance: int,
    new_balance: int,
) -> discord.Embed:
    embed = base_embed("Transport Recorded", "", TRANSPORT_COLOR)
    embed.add_field(name="Transport", value=label, inline=False)
    embed.add_field(name="Amount Deducted", value=f"${cost}", inline=True)
    embed.add_field(name="Previous Balance", value=f"${previous_balance}", inline=True)
    embed.add_field(name="New Balance", value=f"${new_balance}", inline=True)
    return embed


def admin_result_embed(
    *,
    action: str,
    amount: int,
    previous_balance: int,
    new_balance: int,
    channel: discord.TextChannel,
) -> discord.Embed:
    embed = base_embed("Balance Updated", f"Action: {action}", ADMIN_COLOR)
    embed.add_field(name="Amount", value=f"${amount}", inline=True)
    embed.add_field(name="Previous Balance", value=f"${previous_balance}", inline=True)
    embed.add_field(name="New Balance", value=f"${new_balance}", inline=True)
    embed.add_field(name="Target Channel", value=channel.mention, inline=False)
    return embed


def parse_topic_balance(topic: str | None) -> int | None:
    if not topic:
        return None
    match = MONEY_PATTERN.match(topic)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def replace_topic_balance(topic: str | None, new_balance: int) -> str:
    original = topic or ""
    return MONEY_PATTERN.sub(f"${new_balance}", original, count=1)


def user_is_admin(interaction: discord.Interaction) -> bool:
    return (
        interaction.guild is not None
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


def validate_amount(amount: int) -> str | None:
    if amount < 0:
        return "Amount must be a non-negative integer."
    return None


async def send_interaction_embed(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed,
    ephemeral: bool,
) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
    except discord.HTTPException:
        logger.exception("Failed to send interaction response")


async def apply_topic_balance_update(
    channel: discord.TextChannel,
    transform: callable,
) -> tuple[int, int] | None:
    old_amount = parse_topic_balance(channel.topic)
    if old_amount is None:
        return None

    new_amount = int(transform(old_amount))
    new_topic = replace_topic_balance(channel.topic, new_amount)
    await channel.edit(topic=new_topic, reason="Balance update")
    return old_amount, new_amount


async def handle_transport_command(message: discord.Message) -> bool:
    content = message.content.strip()
    if content not in TRANSPORT_COSTS:
        return False

    if message.guild is None or not isinstance(message.channel, discord.TextChannel):
        return True

    label, cost = TRANSPORT_COSTS[content]
    channel = message.channel

    try:
        result = await apply_topic_balance_update(channel, lambda amount: amount - cost)
    except discord.Forbidden:
        logger.warning("Missing permission to edit topic for channel %s", channel.id)
        return True
    except discord.HTTPException:
        logger.warning("HTTP error updating topic for channel %s", channel.id)
        return True
    except Exception:
        logger.exception("Unexpected transport update error for channel %s", channel.id)
        return True

    if result is None:
        return True

    previous_balance, new_balance = result
    embed = transport_result_embed(
        label=label,
        cost=cost,
        previous_balance=previous_balance,
        new_balance=new_balance,
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
        await message.reply(embed=invalid_guess_embed("Guess must be uppercase."), mention_author=False)
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
        await message.reply(embed=invalid_guess_embed("All letters must be unique."), mention_author=False)
        return True

    score = sum(1 for g, c in zip(guess, CORRECT_ANSWER) if g == c)
    await message.reply(
        embed=base_embed("Guess Score", f"{score}/{ANSWER_LENGTH}", ANSWER_COLOR),
        mention_author=False,
    )
    return True


async def handle_admin_balance_command(
    interaction: discord.Interaction,
    *,
    action: str,
    amount: int,
    channel: discord.TextChannel,
    transform: callable,
) -> None:
    amount_error = validate_amount(amount)
    if amount_error:
        await send_interaction_embed(
            interaction,
            embed=base_embed("Validation Error", amount_error, ERROR_COLOR),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    old_amount = parse_topic_balance(channel.topic)
    if old_amount is None:
        await send_interaction_embed(
            interaction,
            embed=base_embed("Validation Error", "Channel topic must start with $<number>.", ERROR_COLOR),
            ephemeral=True,
        )
        return

    try:
        new_amount = int(transform(old_amount))
        new_topic = replace_topic_balance(channel.topic, new_amount)
        await channel.edit(topic=new_topic, reason=f"Balance update via /{action.lower()}")
    except discord.Forbidden:
        logger.warning("Missing permission to edit topic for channel %s", channel.id)
        await send_interaction_embed(
            interaction,
            embed=base_embed("Permission Error", "Bot cannot edit this channel topic.", ERROR_COLOR),
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.warning("HTTP error editing topic for channel %s", channel.id)
        await send_interaction_embed(
            interaction,
            embed=base_embed("Request Error", "Could not update the channel topic.", ERROR_COLOR),
            ephemeral=True,
        )
        return
    except Exception:
        logger.exception("Unexpected admin balance update error for channel %s", channel.id)
        await send_interaction_embed(
            interaction,
            embed=base_embed("Command Error", "Unexpected error while updating balance.", ERROR_COLOR),
            ephemeral=True,
        )
        return

    embed = admin_result_embed(
        action=action,
        amount=amount,
        previous_balance=old_amount,
        new_balance=new_amount,
        channel=channel,
    )
    await send_interaction_embed(interaction, embed=embed, ephemeral=True)


@bot.tree.command(name="add", description="Add an amount to a channel topic balance.")
@app_commands.check(user_is_admin)
@app_commands.describe(amount="Non-negative amount to add", channel="Target text channel")
async def add_balance(interaction: discord.Interaction, amount: int, channel: discord.TextChannel) -> None:
    await handle_admin_balance_command(
        interaction,
        action="Add",
        amount=amount,
        channel=channel,
        transform=lambda old: old + amount,
    )


@bot.tree.command(name="set", description="Set a channel topic balance.")
@app_commands.check(user_is_admin)
@app_commands.describe(amount="Non-negative amount to set", channel="Target text channel")
async def set_balance(interaction: discord.Interaction, amount: int, channel: discord.TextChannel) -> None:
    await handle_admin_balance_command(
        interaction,
        action="Set",
        amount=amount,
        channel=channel,
        transform=lambda _old: amount,
    )


@bot.tree.command(name="remove", description="Remove an amount from a channel topic balance.")
@app_commands.check(user_is_admin)
@app_commands.describe(amount="Non-negative amount to remove", channel="Target text channel")
async def remove_balance(interaction: discord.Interaction, amount: int, channel: discord.TextChannel) -> None:
    await handle_admin_balance_command(
        interaction,
        action="Remove",
        amount=amount,
        channel=channel,
        transform=lambda old: old - amount,
    )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CheckFailure):
        await send_interaction_embed(interaction, embed=permission_error_embed(), ephemeral=True)
        return

    logger.exception("Unhandled app command error: %s", error)
    await send_interaction_embed(
        interaction,
        embed=base_embed("Command Error", "An internal error occurred.", ERROR_COLOR),
        ephemeral=True,
    )


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "unknown")
    try:
        synced = await bot.tree.sync()
        logger.info("Synced %d app commands", len(synced))
    except discord.HTTPException:
        logger.exception("Failed to sync app command tree")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    if message.content.strip() in TRANSPORT_COSTS:
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
