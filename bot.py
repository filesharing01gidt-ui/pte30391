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
ANSWER_PREFIX = "?answer-"
ADMIN_PREFIXES = ("?add", "?set", "?remove")

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
CHANNEL_MENTION_PATTERN = re.compile(r"^<#(\d+)>$")

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


def replace_topic_balance(topic: Optional[str], new_balance: int) -> str:
    original = topic or ""
    return MONEY_PATTERN.sub(f"${new_balance}", original, count=1)


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

    try:
        new_topic = replace_topic_balance(channel.topic, new_amount)
        await channel.edit(topic=new_topic, reason="Transport balance update")
    except discord.Forbidden:
        logger.warning("Missing permission to edit topic for channel %s", channel.id)
        return True
    except discord.HTTPException:
        logger.warning("HTTP error editing topic for channel %s", channel.id)
        return True
    except Exception:
        logger.exception("Unexpected transport update error for channel %s", channel.id)
        return True

    embed = transport_result_embed(
        label=label,
        cost=cost,
        previous_balance=old_amount,
        new_balance=new_amount,
    )
    try:
        await message.reply(embed=embed, mention_author=False)
    except discord.HTTPException:
        logger.exception("Failed to send transport reply")

    return True


async def handle_admin_prefix_command(message: discord.Message) -> bool:
    content = message.content.strip()
    parts = content.split()
    if not parts:
        return False

    command = parts[0].lower()
    if command not in ADMIN_PREFIXES:
        return False

    if message.guild is None or not isinstance(message.channel, discord.TextChannel):
        return True

    if not isinstance(message.author, discord.Member) or not message.author.guild_permissions.administrator:
        await message.reply(embed=permission_error_embed(), mention_author=False)
        return True

    if len(parts) < 2:
        await message.reply(
            embed=base_embed("Validation Error", "Usage: ?add/?set/?remove <amount> [#channel]", ERROR_COLOR),
            mention_author=False,
        )
        return True

    try:
        amount = int(parts[1])
    except ValueError:
        await message.reply(
            embed=base_embed("Validation Error", "Amount must be a non-negative integer.", ERROR_COLOR),
            mention_author=False,
        )
        return True

    if amount < 0:
        await message.reply(
            embed=base_embed("Validation Error", "Amount must be a non-negative integer.", ERROR_COLOR),
            mention_author=False,
        )
        return True

    target_channel: discord.TextChannel = message.channel
    if len(parts) >= 3:
        mention_match = CHANNEL_MENTION_PATTERN.match(parts[2])
        if not mention_match:
            await message.reply(
                embed=base_embed("Validation Error", "Target channel must be a valid text channel mention.", ERROR_COLOR),
                mention_author=False,
            )
            return True

        resolved = bot.get_channel(int(mention_match.group(1)))
        if not isinstance(resolved, discord.TextChannel):
            await message.reply(
                embed=base_embed("Validation Error", "Target channel must be a text channel.", ERROR_COLOR),
                mention_author=False,
            )
            return True
        target_channel = resolved

    old_amount = parse_topic_balance(target_channel.topic)
    if old_amount is None:
        await message.reply(
            embed=base_embed("Validation Error", "Channel topic must start with $<number>.", ERROR_COLOR),
            mention_author=False,
        )
        return True

    if command == "?add":
        new_amount = old_amount + amount
        action = "Add"
    elif command == "?remove":
        new_amount = old_amount - amount
        action = "Remove"
    else:
        new_amount = amount
        action = "Set"

    try:
        new_topic = replace_topic_balance(target_channel.topic, new_amount)
        await target_channel.edit(topic=new_topic, reason=f"Balance update via {command}")
    except discord.Forbidden:
        logger.warning("Missing permission to edit topic for channel %s", target_channel.id)
        await message.reply(
            embed=base_embed("Permission Error", "Bot cannot edit this channel topic.", ERROR_COLOR),
            mention_author=False,
        )
        return True
    except discord.HTTPException:
        logger.warning("HTTP error editing topic for channel %s", target_channel.id)
        await message.reply(
            embed=base_embed("Request Error", "Could not update the channel topic.", ERROR_COLOR),
            mention_author=False,
        )
        return True
    except Exception:
        logger.exception("Unexpected admin update error for channel %s", target_channel.id)
        await message.reply(
            embed=base_embed("Command Error", "Unexpected error while updating balance.", ERROR_COLOR),
            mention_author=False,
        )
        return True

    await message.reply(
        embed=admin_result_embed(
            action=action,
            amount=amount,
            previous_balance=old_amount,
            new_balance=new_amount,
            channel=target_channel,
        ),
        mention_author=False,
    )
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

    score = sum(1 for guessed_char, correct_char in zip(guess, CORRECT_ANSWER) if guessed_char == correct_char)
    await message.reply(
        embed=base_embed("Guess Score", f"{score}/{ANSWER_LENGTH}", ANSWER_COLOR),
        mention_author=False,
    )
    return True


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "unknown")


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

    if message.content.strip().startswith(ADMIN_PREFIXES):
        try:
            await handle_admin_prefix_command(message)
        except Exception:
            logger.exception("Error while processing admin prefix command")
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
