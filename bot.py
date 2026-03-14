from __future__ import annotations

import logging
import os
import re
from typing import Optional

import discord
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

TRANSPORT_COSTS: dict[str, tuple[str, int]] = {
    "?taxi": ("Taxi", 50),
    "?bike": ("Bike", 3),
    "?bus": ("Bus", 5),
    "?train": ("Train", 5),
    "?car": ("Car", 10),
    "?walk": ("Walk", 0),
}

ADMIN_COMMANDS = {
    "?add": "Add",
    "?set": "Set",
    "?remove": "Remove",
}

TRANSPORT_COLOR = 0x2D7D46
ADMIN_COLOR = 0x1F6FEB
ERROR_COLOR = 0xDC2626

MONEY_PATTERN = re.compile(r"^\$(\d+)")
CHANNEL_MENTION_PATTERN = re.compile(r"^<#(\d+)>$")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)


def validate_token() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set. Configure it before running the bot.")


def error_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=ERROR_COLOR)


def transport_embed(label: str, cost: int, old_amount: int, new_amount: int) -> discord.Embed:
    embed = discord.Embed(title="Transport Recorded", color=TRANSPORT_COLOR)
    embed.add_field(name="Transport", value=label, inline=True)
    embed.add_field(name="Amount Deducted", value=f"${cost}", inline=True)
    embed.add_field(name="Previous Balance", value=f"${old_amount}", inline=True)
    embed.add_field(name="New Balance", value=f"${new_amount}", inline=True)
    return embed


def admin_result_embed(
    action: str,
    amount: int,
    old_amount: int,
    new_amount: int,
    channel: discord.TextChannel,
) -> discord.Embed:
    embed = discord.Embed(title="Balance Updated", color=ADMIN_COLOR)
    embed.add_field(name="Action", value=action, inline=True)
    embed.add_field(name="Amount", value=f"${amount}", inline=True)
    embed.add_field(name="Previous Balance", value=f"${old_amount}", inline=True)
    embed.add_field(name="New Balance", value=f"${new_amount}", inline=True)
    embed.add_field(name="Target Channel", value=channel.mention, inline=False)
    return embed


def parse_topic_balance(topic: Optional[str]) -> Optional[int]:
    if not topic:
        return None
    match = MONEY_PATTERN.match(topic)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def replace_topic_balance(topic: Optional[str], new_amount: int) -> str:
    original = topic or ""
    return MONEY_PATTERN.sub(f"${new_amount}", original, count=1)


async def handle_transport_command(message: discord.Message) -> bool:
    content = message.content.strip()
    if content not in TRANSPORT_COSTS:
        return False

    if message.guild is None or not isinstance(message.channel, discord.TextChannel):
        return True

    label, cost = TRANSPORT_COSTS[content]
    channel = message.channel

    old_amount = parse_topic_balance(channel.topic)
    if old_amount is None:
        return True

    new_amount = old_amount - cost
    new_topic = replace_topic_balance(channel.topic, new_amount)

    try:
        await channel.edit(topic=new_topic, reason=f"Transport update: {label}")
    except discord.Forbidden:
        logger.warning("Missing permission to edit topic in channel %s", channel.id)
        return True
    except discord.HTTPException:
        logger.warning("HTTP error while editing topic in channel %s", channel.id)
        return True
    except Exception:
        logger.exception("Unexpected transport update error in channel %s", channel.id)
        return True

    try:
        await message.reply(
            embed=transport_embed(label, cost, old_amount, new_amount),
            mention_author=False,
        )
    except discord.HTTPException:
        logger.exception("Failed to send transport response")

    return True


def resolve_target_channel(
    message: discord.Message,
    parts: list[str],
) -> tuple[Optional[discord.TextChannel], Optional[str]]:
    if not isinstance(message.channel, discord.TextChannel):
        return None, "This command must be used in a text channel."

    if len(parts) < 3:
        return message.channel, None

    mention_match = CHANNEL_MENTION_PATTERN.match(parts[2])
    if not mention_match:
        return None, "Target channel must be a valid text channel mention."

    channel_id = int(mention_match.group(1))
    resolved = message.guild.get_channel(channel_id) if message.guild else None
    if not isinstance(resolved, discord.TextChannel):
        return None, "Target channel must be a text channel."

    return resolved, None


async def handle_admin_balance_command(message: discord.Message) -> bool:
    content = message.content.strip()
    parts = content.split()
    if not parts:
        return False

    command = parts[0].lower()
    if command not in ADMIN_COMMANDS:
        return False

    if message.guild is None:
        return True

    if not isinstance(message.author, discord.Member) or not message.author.guild_permissions.administrator:
        await message.reply(embed=error_embed("Permission Denied", "Administrator access is required."), mention_author=False)
        return True

    if len(parts) < 2:
        await message.reply(
            embed=error_embed("Validation Error", "Usage: ?add/?set/?remove <amount> [#channel]"),
            mention_author=False,
        )
        return True

    try:
        amount = int(parts[1])
    except ValueError:
        await message.reply(
            embed=error_embed("Validation Error", "Amount must be a non-negative integer."),
            mention_author=False,
        )
        return True

    if amount < 0:
        await message.reply(
            embed=error_embed("Validation Error", "Amount must be a non-negative integer."),
            mention_author=False,
        )
        return True

    target_channel, channel_error = resolve_target_channel(message, parts)
    if channel_error:
        await message.reply(embed=error_embed("Validation Error", channel_error), mention_author=False)
        return True

    if target_channel is None:
        return True

    old_amount = parse_topic_balance(target_channel.topic)
    if old_amount is None:
        await message.reply(
            embed=error_embed("Validation Error", "Channel topic must start with $<number>."),
            mention_author=False,
        )
        return True

    if command == "?add":
        new_amount = old_amount + amount
    elif command == "?remove":
        new_amount = old_amount - amount
    else:
        new_amount = amount

    new_topic = replace_topic_balance(target_channel.topic, new_amount)

    try:
        await target_channel.edit(topic=new_topic, reason=f"Admin balance command {command}")
    except discord.Forbidden:
        logger.warning("Missing permission to edit topic in channel %s", target_channel.id)
        await message.reply(
            embed=error_embed("Permission Error", "Bot cannot edit this channel topic."),
            mention_author=False,
        )
        return True
    except discord.HTTPException:
        logger.warning("HTTP error while editing topic in channel %s", target_channel.id)
        await message.reply(
            embed=error_embed("Request Error", "Could not update the channel topic."),
            mention_author=False,
        )
        return True
    except Exception:
        logger.exception("Unexpected admin update error in channel %s", target_channel.id)
        await message.reply(
            embed=error_embed("Command Error", "Unexpected error while updating balance."),
            mention_author=False,
        )
        return True

    action = ADMIN_COMMANDS[command]
    try:
        await message.reply(
            embed=admin_result_embed(action, amount, old_amount, new_amount, target_channel),
            mention_author=False,
        )
    except discord.HTTPException:
        logger.exception("Failed to send admin command response")

    return True


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "unknown")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.guild is None:
        return

    if await handle_transport_command(message):
        return

    if await handle_admin_balance_command(message):
        return


validate_token()
bot.run(TOKEN)
