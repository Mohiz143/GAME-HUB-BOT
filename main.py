"""GAME HUB 🎮 — a lightweight Telegram gaming bot backed by SQLite."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Iterator

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    MessageEntity,
    Update,
)
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


BOT_NAME = "GAME HUB 🎮"
DAILY_COOLDOWN = 24 * 60 * 60
DAILY_REWARD = 100
SLOT_COST = 10
SLOT_SYMBOLS = ("🍒", "🍋", "🍇", "🔔", "⭐", "7️⃣")
MAX_BATTLE_TURNS = 30
ADMIN_CACHE_SECONDS = 30
LINK_PATTERN = re.compile(
    r"(?i)(?:https?://|www\.|t\.me/|telegram\.me/|"
    r"(?<![@\w])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}"
    r"(?::\d+)?(?:[/?#][^\s]*)?)"
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("game_hub")


class InsufficientCoins(Exception):
    """Raised when a player cannot afford an action."""


class GameDatabase:
    """Small synchronous SQLite store; update handling is single-threaded."""

    def __init__(self, path: str) -> None:
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 10000")
        self._initialize()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                display_name TEXT NOT NULL,
                coins INTEGER NOT NULL DEFAULT 100 CHECK (coins >= 0),
                xp INTEGER NOT NULL DEFAULT 0 CHECK (xp >= 0),
                wins INTEGER NOT NULL DEFAULT 0 CHECK (wins >= 0),
                losses INTEGER NOT NULL DEFAULT 0 CHECK (losses >= 0),
                games_played INTEGER NOT NULL DEFAULT 0 CHECK (games_played >= 0),
                banned INTEGER NOT NULL DEFAULT 0 CHECK (banned IN (0, 1)),
                daily_claimed_at REAL,
                created_at REAL NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS players_username_lower
                ON players(lower(username)) WHERE username IS NOT NULL;

            CREATE TABLE IF NOT EXISTS game_actions (
                action_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES players(user_id),
                game TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'resolved')),
                result TEXT,
                created_at REAL NOT NULL,
                resolved_at REAL
            );

            CREATE TABLE IF NOT EXISTS battles (
                battle_id TEXT PRIMARY KEY,
                challenger_id INTEGER NOT NULL REFERENCES players(user_id),
                opponent_id INTEGER NOT NULL REFERENCES players(user_id),
                status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'completed', 'declined')),
                turn_user_id INTEGER,
                challenger_hp INTEGER NOT NULL DEFAULT 100 CHECK (challenger_hp BETWEEN 0 AND 100),
                opponent_hp INTEGER NOT NULL DEFAULT 100 CHECK (opponent_hp BETWEEN 0 AND 100),
                challenger_shield INTEGER NOT NULL DEFAULT 0 CHECK (challenger_shield IN (0, 1)),
                opponent_shield INTEGER NOT NULL DEFAULT 0 CHECK (opponent_shield IN (0, 1)),
                turn_number INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                finished_at REAL
            );

            CREATE TABLE IF NOT EXISTS admin_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id INTEGER PRIMARY KEY,
                protection_enabled INTEGER NOT NULL DEFAULT 0
                    CHECK (protection_enabled IN (0, 1)),
                updated_by INTEGER,
                updated_at REAL NOT NULL
            );
            """
        )

    def close(self) -> None:
        self.connection.close()

    def upsert_player(self, user: Any) -> None:
        username = getattr(user, "username", None)
        display_name = getattr(user, "full_name", None) or str(user.id)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO players (user_id, username, display_name, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    display_name = excluded.display_name
                """,
                (user.id, username, display_name, time.time()),
            )

    def get_player(self, user_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM players WHERE user_id = ?", (user_id,)
        ).fetchone()

    def is_banned(self, user_id: int) -> bool:
        row = self.connection.execute(
            "SELECT banned FROM players WHERE user_id = ?", (user_id,)
        ).fetchone()
        return bool(row and row["banned"])

    def leaderboard(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT display_name, username, xp, wins
            FROM players
            WHERE banned = 0
            ORDER BY xp DESC, wins DESC, games_played ASC, user_id ASC
            LIMIT 10
            """
        ).fetchall()

    def create_action(self, user_id: int, game: str, charge: int = 0) -> str:
        action_id = uuid.uuid4().hex
        with self.transaction() as conn:
            player = conn.execute(
                "SELECT coins FROM players WHERE user_id = ?", (user_id,)
            ).fetchone()
            if player is None:
                raise ValueError("Player is not registered. Send /start first.")
            if player["coins"] < charge:
                raise InsufficientCoins
            if charge:
                conn.execute(
                    "UPDATE players SET coins = coins - ? WHERE user_id = ?",
                    (charge, user_id),
                )
            conn.execute(
                """
                INSERT INTO game_actions (action_id, user_id, game, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (action_id, user_id, game, time.time()),
            )
        return action_id

    def complete_action(
        self,
        action_id: str,
        user_id: int,
        result: str,
        outcome: str,
        coin_reward: int | None = None,
    ) -> bool:
        """Resolve one action exactly once and apply its rewards atomically."""
        if outcome not in ("win", "loss", "draw"):
            raise ValueError("Invalid game outcome.")
        rewards = {
            "win": (20, 15),
            "loss": (10, 5),
            "draw": (12, 8),
        }
        xp_reward, default_coins = rewards[outcome]
        coins = default_coins if coin_reward is None else coin_reward
        if coins < 0:
            raise ValueError("Game rewards cannot be negative.")
        with self.transaction() as conn:
            action = conn.execute(
                """
                SELECT status FROM game_actions
                WHERE action_id = ? AND user_id = ?
                """,
                (action_id, user_id),
            ).fetchone()
            if action is None or action["status"] != "pending":
                return False
            conn.execute(
                """
                UPDATE game_actions
                SET status = 'resolved', result = ?, resolved_at = ?
                WHERE action_id = ? AND status = 'pending'
                """,
                (result, time.time(), action_id),
            )
            conn.execute(
                """
                UPDATE players
                SET coins = coins + ?, xp = xp + ?,
                    wins = wins + ?, losses = losses + ?, games_played = games_played + 1
                WHERE user_id = ?
                """,
                (coins, xp_reward, int(outcome == "win"), int(outcome == "loss"), user_id),
            )
        return True

    def claim_daily(self, user_id: int) -> tuple[bool, int]:
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT daily_claimed_at FROM players WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Player is not registered. Send /start first.")
            previous = row["daily_claimed_at"]
            if previous is not None and now - previous < DAILY_COOLDOWN:
                return False, max(0, int(DAILY_COOLDOWN - (now - previous)))
            conn.execute(
                """
                UPDATE players
                SET coins = coins + ?, xp = xp + 5, daily_claimed_at = ?
                WHERE user_id = ?
                """,
                (DAILY_REWARD, now, user_id),
            )
        return True, DAILY_REWARD

    def create_battle(self, challenger_id: int, opponent_id: int) -> str:
        battle_id = uuid.uuid4().hex[:12]
        with self.transaction() as conn:
            opponent = conn.execute(
                "SELECT banned FROM players WHERE user_id = ?", (opponent_id,)
            ).fetchone()
            if opponent is None:
                raise ValueError("That player has not started the bot yet.")
            if opponent["banned"]:
                raise ValueError("That player cannot receive challenges.")
            conn.execute(
                """
                INSERT INTO battles (
                    battle_id, challenger_id, opponent_id, status, created_at
                ) VALUES (?, ?, ?, 'pending', ?)
                """,
                (battle_id, challenger_id, opponent_id, time.time()),
            )
        return battle_id

    def get_battle(self, battle_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
        ).fetchone()

    def respond_to_challenge(
        self, battle_id: str, user_id: int, accept: bool
    ) -> sqlite3.Row | None:
        with self.transaction() as conn:
            battle = conn.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
            if (
                battle is None
                or battle["status"] != "pending"
                or battle["opponent_id"] != user_id
            ):
                return None
            status = "active" if accept else "declined"
            conn.execute(
                """
                UPDATE battles
                SET status = ?, turn_user_id = ?, finished_at = ?
                WHERE battle_id = ? AND status = 'pending'
                """,
                (
                    status,
                    battle["challenger_id"] if accept else None,
                    None if accept else time.time(),
                    battle_id,
                ),
            )
            return conn.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()

    def play_battle_turn(
        self, battle_id: str, user_id: int, move: str, damage: int
    ) -> tuple[str, sqlite3.Row | None, str]:
        """Return (status, battle, event text) after validating and applying a turn."""
        if move not in ("attack", "defend"):
            return "invalid", None, ""
        with self.transaction() as conn:
            battle = conn.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
            if battle is None or battle["status"] != "active":
                return "finished", battle, ""
            if battle["turn_user_id"] != user_id:
                return "not_your_turn", battle, ""

            challenger_turn = user_id == battle["challenger_id"]
            actor_hp_key = "challenger_hp" if challenger_turn else "opponent_hp"
            target_hp_key = "opponent_hp" if challenger_turn else "challenger_hp"
            actor_shield_key = (
                "challenger_shield" if challenger_turn else "opponent_shield"
            )
            target_shield_key = (
                "opponent_shield" if challenger_turn else "challenger_shield"
            )
            actor_hp = battle[actor_hp_key]
            target_hp = battle[target_hp_key]
            actor_shield = battle[actor_shield_key]
            target_shield = battle[target_shield_key]
            actor_name = "challenger" if challenger_turn else "opponent"

            if move == "defend":
                new_actor_hp = min(100, actor_hp + 5)
                new_actor_shield = 1
                new_target_hp = target_hp
                new_target_shield = target_shield
                event = "Defense raised a shield and restored 5 HP."
            else:
                actual_damage = max(3, damage // 4) if target_shield else damage
                new_actor_hp = actor_hp
                new_actor_shield = actor_shield
                new_target_hp = max(0, target_hp - actual_damage)
                new_target_shield = 0
                event = f"Attack dealt {actual_damage} damage."

            new_turn = battle["turn_number"] + 1
            finished = new_target_hp == 0 or new_turn >= MAX_BATTLE_TURNS
            if new_target_hp == 0:
                winner_id = user_id
                outcome = "win"
            elif finished:
                if new_actor_hp > new_target_hp:
                    winner_id, outcome = user_id, "win"
                elif new_actor_hp < new_target_hp:
                    winner_id = (
                        battle["opponent_id"]
                        if challenger_turn
                        else battle["challenger_id"]
                    )
                    outcome = "loss"
                else:
                    winner_id, outcome = None, "draw"
            else:
                winner_id, outcome = None, ""

            conn.execute(
                f"""
                UPDATE battles SET
                    {actor_hp_key} = ?, {target_hp_key} = ?,
                    {actor_shield_key} = ?, {target_shield_key} = ?,
                    turn_number = ?, turn_user_id = ?, status = ?, finished_at = ?
                WHERE battle_id = ? AND status = 'active'
                """,
                (
                    new_actor_hp,
                    new_target_hp,
                    new_actor_shield if move == "defend" else actor_shield,
                    new_target_shield,
                    new_turn,
                    (
                        battle["opponent_id"]
                        if challenger_turn
                        else battle["challenger_id"]
                    ),
                    "completed" if finished else "active",
                    time.time() if finished else None,
                    battle_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
            if finished:
                self._award_battle_rewards(conn, updated, winner_id)
            return ("completed" if finished else "active"), updated, event

    @staticmethod
    def _award_battle_rewards(
        conn: sqlite3.Connection, battle: sqlite3.Row, winner_id: int | None
    ) -> None:
        ids = (battle["challenger_id"], battle["opponent_id"])
        if winner_id is None:
            for user_id in ids:
                conn.execute(
                    """
                    UPDATE players
                    SET coins = coins + 10, xp = xp + 12, games_played = games_played + 1
                    WHERE user_id = ?
                    """,
                    (user_id,),
                )
        else:
            loser_id = ids[1] if winner_id == ids[0] else ids[0]
            conn.execute(
                """
                UPDATE players SET coins = coins + 20, xp = xp + 25,
                    wins = wins + 1, games_played = games_played + 1
                WHERE user_id = ?
                """,
                (winner_id,),
            )
            conn.execute(
                """
                UPDATE players SET coins = coins + 5, xp = xp + 10,
                    losses = losses + 1, games_played = games_played + 1
                WHERE user_id = ?
                """,
                (loser_id,),
            )

    def cancel_battle(self, battle_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE battles SET status = 'declined', finished_at = ? "
                "WHERE battle_id = ? AND status = 'pending'",
                (time.time(), battle_id),
            )

    def admin_adjust_coins(
        self, admin_id: int, target_id: int, amount: int, set_balance: bool
    ) -> int:
        if amount < 0:
            raise ValueError("Coin amounts cannot be negative.")
        with self.transaction() as conn:
            target = conn.execute(
                "SELECT coins FROM players WHERE user_id = ?", (target_id,)
            ).fetchone()
            if target is None:
                raise ValueError("Player not found. They must send /start first.")
            new_balance = amount if set_balance else target["coins"] + amount
            conn.execute(
                "UPDATE players SET coins = ? WHERE user_id = ?",
                (new_balance, target_id),
            )
            action = "setcoins" if set_balance else "givecoins"
            conn.execute(
                """
                INSERT INTO admin_logs (admin_id, action, details, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (admin_id, action, f"target={target_id}; amount={amount}", time.time()),
            )
        return new_balance

    def admin_set_ban(self, admin_id: int, target_id: int, banned: bool) -> bool:
        with self.transaction() as conn:
            target = conn.execute(
                "SELECT user_id FROM players WHERE user_id = ?", (target_id,)
            ).fetchone()
            if target is None:
                raise ValueError("Player not found. They must send /start first.")
            conn.execute(
                "UPDATE players SET banned = ? WHERE user_id = ?",
                (int(banned), target_id),
            )
            conn.execute(
                """
                INSERT INTO admin_logs (admin_id, action, details, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    admin_id,
                    "ban" if banned else "unban",
                    f"target={target_id}",
                    time.time(),
                ),
            )
        return True

    def admin_log(self, admin_id: int, action: str, details: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO admin_logs (admin_id, action, details, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (admin_id, action, details[:3000], time.time()),
            )

    def set_all_broadcast_logs(
        self, admin_id: int, message: str, sent: int, failed: int
    ) -> None:
        self.admin_log(
            admin_id, "broadcast", f"sent={sent}; failed={failed}; message={message[:500]}"
        )

    def broadcast_user_ids(self) -> list[int]:
        rows = self.connection.execute(
            "SELECT user_id FROM players WHERE banned = 0 ORDER BY user_id"
        ).fetchall()
        return [row["user_id"] for row in rows]

    def global_stats(self) -> tuple[int, int]:
        users = self.connection.execute(
            "SELECT COUNT(*) AS count FROM players"
        ).fetchone()["count"]
        actions = self.connection.execute(
            "SELECT COUNT(*) AS count FROM game_actions WHERE status = 'resolved'"
        ).fetchone()["count"]
        battles = self.connection.execute(
            "SELECT COUNT(*) AS count FROM battles WHERE status = 'completed'"
        ).fetchone()["count"]
        return users, actions + battles

    def group_protection_enabled(self, chat_id: int) -> bool:
        row = self.connection.execute(
            "SELECT protection_enabled FROM group_settings WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return bool(row and row["protection_enabled"])

    def set_group_protection(
        self, chat_id: int, admin_id: int, enabled: bool
    ) -> bool:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT protection_enabled FROM group_settings WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            if bool(row and row["protection_enabled"]) == enabled:
                return False
            conn.execute(
                """
                INSERT INTO group_settings (
                    chat_id, protection_enabled, updated_by, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    protection_enabled = excluded.protection_enabled,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (chat_id, int(enabled), admin_id, time.time()),
            )
            conn.execute(
                """
                INSERT INTO admin_logs (admin_id, action, details, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    admin_id,
                    "group_protect_on" if enabled else "group_protect_off",
                    f"chat={chat_id}",
                    time.time(),
                ),
            )
        return True

    def username_to_id(self, username: str) -> int | None:
        row = self.connection.execute(
            "SELECT user_id FROM players WHERE lower(username) = lower(?)",
            (username.lstrip("@"),),
        ).fetchone()
        return int(row["user_id"]) if row else None


DATABASE = GameDatabase(os.getenv("DATABASE_PATH", "game_hub.sqlite3"))
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
_chat_admin_cache: dict[int, tuple[float, set[int]]] = {}


async def chat_admin_ids(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int
) -> set[int] | None:
    """Return cached group admins, or None if Telegram could not verify them."""
    now = time.monotonic()
    cached = _chat_admin_cache.get(chat_id)
    if cached and cached[0] > now:
        return cached[1]
    try:
        members = await context.bot.get_chat_administrators(chat_id)
    except TelegramError:
        logger.exception("Could not retrieve admins for group %s", chat_id)
        return None
    admin_ids = {
        member.user.id
        for member in members
        if member.status in ("administrator", "creator")
    }
    _chat_admin_cache[chat_id] = (now + ADMIN_CACHE_SECONDS, admin_ids)
    return admin_ids


async def is_group_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool | None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat is None:
        return False
    if message and message.sender_chat and message.sender_chat.id == chat.id:
        return True
    if user is None:
        return False
    admins = await chat_admin_ids(context, chat.id)
    if admins is None:
        return None
    return user.id in admins


async def require_group_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[int, int]:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        raise ValueError("This moderation command can only be used in a group.")
    if user is None:
        raise ValueError("Could not identify the command sender.")
    is_admin = await is_group_admin(update, context)
    if is_admin is None:
        raise ValueError("Could not verify group admin permissions. Please try again.")
    if not is_admin:
        raise ValueError("Only this group's admins can use moderation commands.")
    return chat.id, user.id


def group_command_target(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    arg_index: int = 0,
) -> tuple[int, list[str]]:
    message = update.effective_message
    if message and message.reply_to_message:
        target = message.reply_to_message.from_user
        if target is None:
            raise ValueError("Reply to a message sent by the person you want to moderate.")
        return target.id, context.args[arg_index:]
    if len(context.args) <= arg_index:
        raise ValueError("Reply to a user's message or provide their numeric Telegram user ID.")
    try:
        target_id = int(context.args[arg_index])
    except ValueError as exc:
        raise ValueError("Use a numeric Telegram user ID, or reply to their message.") from exc
    if target_id <= 0:
        raise ValueError("Telegram user ID must be positive.")
    return target_id, context.args[arg_index + 1 :]


async def ensure_target_is_not_group_admin(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, target_id: int
) -> None:
    try:
        member = await context.bot.get_chat_member(chat_id, target_id)
    except TelegramError as exc:
        raise ValueError("Could not look up that group member.") from exc
    if member.status in ("administrator", "creator"):
        raise ValueError("This command cannot be used on a group admin.")


def message_contains_link(message: Any) -> bool:
    text = message.text or message.caption or ""
    entities = [*(message.entities or []), *(message.caption_entities or [])]
    if any(
        entity.type in (MessageEntity.URL, MessageEntity.TEXT_LINK)
        for entity in entities
    ):
        return True
    return bool(LINK_PATTERN.search(text))


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎲 Dice Duel", callback_data="game:dice"),
                InlineKeyboardButton("✊ RPS", callback_data="game:rps"),
            ],
            [
                InlineKeyboardButton("🔢 Number Target", callback_data="game:number"),
                InlineKeyboardButton("🎰 Lucky Slots", callback_data="game:slots"),
            ],
            [InlineKeyboardButton("⚔️ Quick Battle", callback_data="game:battle")],
            [
                InlineKeyboardButton("👤 Profile", callback_data="menu:profile"),
                InlineKeyboardButton("🏆 Leaderboard", callback_data="menu:leaderboard"),
            ],
            [
                InlineKeyboardButton("🎁 Daily reward", callback_data="menu:daily"),
                InlineKeyboardButton("❔ Help", callback_data="menu:help"),
            ],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Main menu", callback_data="menu:home")]]
    )


def game_outcome_text(outcome: str, reward: int) -> str:
    title = {"win": "You win!", "loss": "You lose.", "draw": "It's a draw!"}[outcome]
    return f"{title}\nReward: {reward} coins and {20 if outcome == 'win' else 10 if outcome == 'loss' else 12} XP."


def guarded(handler: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(handler)
    async def wrapped(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args: Any, **kwargs: Any
    ) -> Any:
        user = update.effective_user
        if user is not None:
            try:
                DATABASE.upsert_player(user)
                if user.id not in ADMIN_IDS and DATABASE.is_banned(user.id):
                    if update.callback_query:
                        await update.callback_query.answer(
                            "Your access to the games is disabled.", show_alert=True
                        )
                    elif update.effective_message:
                        await update.effective_message.reply_text(
                            "Your access to the games is disabled."
                        )
                    return None
                return await handler(update, context, *args, **kwargs)
            except InsufficientCoins:
                await respond(update, "You need more coins for that game.")
            except (ValueError, sqlite3.Error) as exc:
                logger.warning("Request rejected or database error: %s", exc)
                await respond(update, f"Unable to complete that request: {exc}")
            except TelegramError:
                logger.exception("Telegram API request failed")
                await respond(update, "Telegram could not complete that action. Please try again.")
            except Exception:
                logger.exception("Unexpected update handler error")
                await respond(update, "Something went wrong. Please try again later.")
        return None

    return wrapped


async def respond(update: Update, text: str) -> None:
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except TelegramError:
            pass
        if update.effective_message:
            await update.effective_message.reply_text(text)
    elif update.effective_message:
        await update.effective_message.reply_text(text)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"Welcome to {BOT_NAME}!\n\n"
        "Play quick games, earn coins and XP, and challenge friends. "
        "All coins are virtual and have no cash value.",
        reply_markup=main_keyboard(),
    )


def format_profile(row: sqlite3.Row) -> str:
    level = row["xp"] // 100 + 1
    return (
        f"👤 {row['display_name']}\n"
        f"Level: {level}\nXP: {row['xp']}\nCoins: {row['coins']}\n"
        f"Wins: {row['wins']}\nLosses: {row['losses']}\n"
        f"Games played: {row['games_played']}"
    )


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = DATABASE.get_player(update.effective_user.id)
    if row is None:
        raise ValueError("Send /start to create your profile.")
    await update.effective_message.reply_text(
        format_profile(row), reply_markup=back_keyboard()
    )


async def leaderboard_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    rows = DATABASE.leaderboard()
    if not rows:
        text = "No players yet. Send /start and play the first game!"
    else:
        lines = ["🏆 Top 10 by XP"]
        for index, row in enumerate(rows, start=1):
            name = f"@{row['username']}" if row["username"] else row["display_name"]
            lines.append(f"{index}. {name} — {row['xp']} XP · {row['wins']} wins")
        text = "\n".join(lines)
    await update.effective_message.reply_text(text, reply_markup=back_keyboard())


async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    claimed, value = DATABASE.claim_daily(update.effective_user.id)
    if claimed:
        text = f"🎁 Daily reward claimed: +{value} coins and +5 XP."
    else:
        hours, remainder = divmod(value, 3600)
        minutes = remainder // 60
        text = f"You already claimed today's reward. Come back in {hours}h {minutes}m."
    await update.effective_message.reply_text(text, reply_markup=back_keyboard())


async def games_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Choose a game:\n"
        "🎲 Dice Duel — roll against the bot.\n"
        "✊ Rock Paper Scissors — choose with buttons.\n"
        "🔢 Number Target — pick 1–10 against a random target.\n"
        "🎰 Lucky Slots — spend 10 coins for a random payout.\n"
        "⚔️ Quick Battle — challenge a registered player with /challenge @username.",
        reply_markup=main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Commands\n"
        "/start — open the game menu\n"
        "/profile — view your stats and balance\n"
        "/leaderboard — top 10 players by XP\n"
        "/daily — claim 100 coins once every 24 hours\n"
        "/games — see the games\n"
        "/challenge @username — challenge a registered player\n"
        "/help — show this guide\n\n"
        "Group admins can use /protect to enable filters, /protect off to disable "
        "them, and /ban, /unban, /mute and /unmute for moderation. With protection "
        "enabled, non-admin links and Telegram-marked protected content are removed.\n\n"
        "Each completed match gives XP and virtual coins. "
        "Level increases for every 100 XP. No real money or prizes are used."
    )
    await update.effective_message.reply_text(text, reply_markup=back_keyboard())


async def start_dice(message: Any, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    action_id = DATABASE.create_action(user_id, "dice_duel")
    user_roll_msg = await message.reply_dice(emoji="🎲")
    await asyncio.sleep(0.7)
    bot_roll_msg = await message.reply_dice(emoji="🎲")
    # Use the bot's actual animation result, as displayed to the player.
    bot_roll = bot_roll_msg.dice.value
    user_roll = user_roll_msg.dice.value
    outcome = "win" if user_roll > bot_roll else "loss" if user_roll < bot_roll else "draw"
    DATABASE.complete_action(action_id, user_id, f"{user_roll}-{bot_roll}", outcome)
    reward = {"win": 15, "loss": 5, "draw": 8}[outcome]
    await message.reply_text(
        f"Your roll: {user_roll}\nBot's roll: {bot_roll}\n"
        f"{game_outcome_text(outcome, reward)}",
        reply_markup=back_keyboard(),
    )


async def start_rps(message: Any, user_id: int) -> None:
    action_id = DATABASE.create_action(user_id, "rock_paper_scissors")
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✊ Rock", callback_data=f"rps:{action_id}:rock"),
            InlineKeyboardButton("✋ Paper", callback_data=f"rps:{action_id}:paper"),
            InlineKeyboardButton("✌️ Scissors", callback_data=f"rps:{action_id}:scissors"),
        ]]
    )
    await message.reply_text("Choose your move:", reply_markup=keyboard)


async def start_number_target(message: Any, user_id: int) -> None:
    action_id = DATABASE.create_action(user_id, "number_target")
    rows = [
        [InlineKeyboardButton(str(number), callback_data=f"num:{action_id}:{number}")
         for number in range(start, start + 5)]
        for start in (1, 6)
    ]
    await message.reply_text(
        "Pick a number from 1 to 10. The higher number wins!",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def start_slots(message: Any, user_id: int) -> None:
    action_id = DATABASE.create_action(user_id, "lucky_slots", charge=SLOT_COST)
    await message.reply_text(
        f"Lucky Slots costs {SLOT_COST} coins. Spin once for a random payout.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎰 Spin", callback_data=f"slot:{action_id}:spin")]]
        ),
    )


async def dice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_dice(update.effective_message, update.effective_user.id, context)


async def rps_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_rps(update.effective_message, update.effective_user.id)


async def number_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_number_target(update.effective_message, update.effective_user.id)


async def slots_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_slots(update.effective_message, update.effective_user.id)


async def game_menu_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    game = query.data.partition(":")[2]
    if game == "dice":
        await start_dice(query.message, update.effective_user.id, context)
    elif game == "rps":
        await start_rps(query.message, update.effective_user.id)
    elif game == "number":
        await start_number_target(query.message, update.effective_user.id)
    elif game == "slots":
        await start_slots(query.message, update.effective_user.id)
    elif game == "battle":
        await query.message.reply_text(
            "Challenge a registered player with /challenge @username. "
            "They must have opened the bot with /start first."
        )


async def rps_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, action_id, player_move = query.data.split(":")
    bot_move = random.choice(("rock", "paper", "scissors"))
    if player_move == bot_move:
        outcome = "draw"
    elif (player_move, bot_move) in (
        ("rock", "scissors"),
        ("paper", "rock"),
        ("scissors", "paper"),
    ):
        outcome = "win"
    else:
        outcome = "loss"
    completed = DATABASE.complete_action(
        action_id, update.effective_user.id, f"{player_move}-{bot_move}", outcome
    )
    await query.answer()
    if not completed:
        await query.message.reply_text("This game has already been completed.")
        return
    icons = {"rock": "✊ Rock", "paper": "✋ Paper", "scissors": "✌️ Scissors"}
    reward = {"win": 15, "loss": 5, "draw": 8}[outcome]
    await query.message.reply_text(
        f"You chose {icons[player_move]}; the bot chose {icons[bot_move]}.\n"
        f"{game_outcome_text(outcome, reward)}",
        reply_markup=back_keyboard(),
    )


async def number_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, action_id, number_text = query.data.split(":")
    chosen = int(number_text)
    if not 1 <= chosen <= 10:
        raise ValueError("Choose a number from 1 to 10.")
    target = random.randint(1, 10)
    outcome = "win" if chosen > target else "loss" if chosen < target else "draw"
    completed = DATABASE.complete_action(
        action_id, update.effective_user.id, f"{chosen}-{target}", outcome
    )
    await query.answer()
    if not completed:
        await query.message.reply_text("This game has already been completed.")
        return
    reward = {"win": 15, "loss": 5, "draw": 8}[outcome]
    await query.message.reply_text(
        f"You picked {chosen}; the target was {target}.\n"
        f"{game_outcome_text(outcome, reward)}",
        reply_markup=back_keyboard(),
    )


async def slots_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, action_id, _ = query.data.split(":")
    symbols = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
    matches = len(set(symbols))
    payout = 50 if matches == 1 else 20 if matches == 2 else 5
    outcome = "win" if payout > SLOT_COST else "loss"
    completed = DATABASE.complete_action(
        action_id,
        update.effective_user.id,
        "".join(symbols),
        outcome,
        coin_reward=payout,
    )
    await query.answer()
    if not completed:
        await query.message.reply_text("This spin has already been completed.")
        return
    net = payout - SLOT_COST
    description = (
        f"Three matching: +{net} net coins!"
        if matches == 1
        else f"Two matching: +{net} net coins."
        if matches == 2
        else f"No match: you got {payout} coins back ({net} net)."
    )
    await query.message.reply_text(
        f"🎰  {'  '.join(symbols)}\n{description}\n"
        f"You earned {20 if payout > SLOT_COST else 10} XP.",
        reply_markup=back_keyboard(),
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    key = query.data.partition(":")[2]
    await query.answer()
    if key == "home":
        await query.message.reply_text(
            f"{BOT_NAME} — pick a game or open your profile.",
            reply_markup=main_keyboard(),
        )
    elif key == "profile":
        row = DATABASE.get_player(update.effective_user.id)
        if row is None:
            raise ValueError("Send /start to create your profile.")
        await query.message.reply_text(format_profile(row), reply_markup=back_keyboard())
    elif key == "leaderboard":
        await leaderboard_command(update, context)
    elif key == "daily":
        claimed, value = DATABASE.claim_daily(update.effective_user.id)
        if claimed:
            text = f"🎁 Daily reward claimed: +{value} coins and +5 XP."
        else:
            hours, remainder = divmod(value, 3600)
            text = f"Come back in {hours}h {remainder // 60}m for your next reward."
        await query.message.reply_text(text, reply_markup=back_keyboard())
    elif key == "help":
        await help_command(update, context)


def battle_keyboard(battle_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("⚔️ Attack", callback_data=f"battle:{battle_id}:attack"),
            InlineKeyboardButton("🛡 Defend", callback_data=f"battle:{battle_id}:defend"),
        ]]
    )


def battle_status_text(battle: sqlite3.Row, event: str = "") -> str:
    lines = ["⚔️ Quick Battle"]
    if event:
        lines.append(event)
    lines.extend(
        [
            f"Challenger: {battle['challenger_hp']} HP",
            f"Opponent: {battle['opponent_hp']} HP",
        ]
    )
    return "\n".join(lines)


async def send_battle_status(
    context: ContextTypes.DEFAULT_TYPE,
    battle: sqlite3.Row,
    event: str = "",
    completed: bool = False,
) -> None:
    ids = (battle["challenger_id"], battle["opponent_id"])
    if completed:
        if battle["challenger_hp"] == battle["opponent_hp"]:
            result_line = "Battle draw. Both players received rewards."
        elif battle["challenger_hp"] > battle["opponent_hp"]:
            result_line = f"Battle over: player {battle['challenger_id']} wins!"
        else:
            result_line = f"Battle over: player {battle['opponent_id']} wins!"
    else:
        turn = "Challenger" if battle["turn_user_id"] == battle["challenger_id"] else "Opponent"
        result_line = f"{turn}'s turn."

    for user_id in ids:
        keyboard = None
        if not completed and battle["turn_user_id"] == user_id:
            keyboard = battle_keyboard(battle["battle_id"])
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"{battle_status_text(battle, event)}\n{result_line}",
                reply_markup=keyboard,
            )
        except TelegramError:
            logger.info("Could not notify battle participant %s", user_id)


async def challenge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        raise ValueError("Use /challenge in a private chat with the bot.")
    if len(context.args) != 1:
        raise ValueError("Usage: /challenge @username")
    target_id = DATABASE.username_to_id(context.args[0])
    if target_id is None:
        raise ValueError("Player not found. They must send /start and have a username.")
    if target_id == update.effective_user.id:
        raise ValueError("You cannot challenge yourself.")
    battle_id = DATABASE.create_battle(update.effective_user.id, target_id)
    await update.effective_message.reply_text(
        f"Challenge sent to {context.args[0]}. Waiting for their response."
    )
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"{update.effective_user.full_name} challenged you to a Quick Battle!",
            reply_markup=InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "Accept", callback_data=f"battle:{battle_id}:accept"
                    ),
                    InlineKeyboardButton(
                        "Decline", callback_data=f"battle:{battle_id}:decline"
                    ),
                ]]
            ),
        )
    except TelegramError:
        DATABASE.cancel_battle(battle_id)
        raise ValueError("Could not message that player. Ask them to open the bot first.")


async def battle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, battle_id, action = query.data.split(":")
    user_id = update.effective_user.id
    battle = DATABASE.get_battle(battle_id)
    if battle is None:
        await query.answer("Battle not found.", show_alert=True)
        return

    if action in ("accept", "decline"):
        battle = DATABASE.respond_to_challenge(
            battle_id, user_id, accept=(action == "accept")
        )
        await query.answer()
        if battle is None:
            await query.message.reply_text("This challenge is no longer available.")
        elif action == "decline":
            await query.message.reply_text("Challenge declined.")
            await send_battle_status(
                context, battle, event="The challenge was declined.", completed=True
            )
        else:
            await query.message.reply_text("Battle accepted. The challenger goes first.")
            await send_battle_status(context, battle, "The battle has started.")
        return

    if action not in ("attack", "defend"):
        await query.answer("That move is not valid.", show_alert=True)
        return
    status, battle, event = DATABASE.play_battle_turn(
        battle_id, user_id, action, random.randint(12, 22)
    )
    await query.answer()
    if status == "not_your_turn":
        await query.message.reply_text("It's not your turn.")
        return
    if status == "finished":
        await query.message.reply_text("This battle has already ended.")
        return
    if battle is None:
        await query.message.reply_text("This battle could not be found.")
        return
    await send_battle_status(context, battle, event, completed=(status == "completed"))


def require_admin(user_id: int) -> None:
    if user_id not in ADMIN_IDS:
        raise ValueError("This command is for admins only.")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    require_admin(user_id)
    users, games = DATABASE.global_stats()
    DATABASE.admin_log(user_id, "admin_dashboard", "opened admin dashboard")
    await update.effective_message.reply_text(
        f"🛠 Admin dashboard\nRegistered users: {users}\nCompleted games: {games}\n\n"
        "Commands: /stats, /givecoins, /setcoins, /ban, /unban, /broadcast"
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    require_admin(user_id)
    users, games = DATABASE.global_stats()
    DATABASE.admin_log(user_id, "stats", "viewed platform stats")
    await update.effective_message.reply_text(
        f"Registered users: {users}\nCompleted games: {games}"
    )


async def coin_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, set_balance: bool
) -> None:
    user_id = update.effective_user.id
    require_admin(user_id)
    if len(context.args) != 2:
        command = "/setcoins" if set_balance else "/givecoins"
        raise ValueError(f"Usage: {command} USER_ID AMOUNT")
    try:
        target_id, amount = int(context.args[0]), int(context.args[1])
    except ValueError as exc:
        raise ValueError("USER_ID and AMOUNT must be whole numbers.") from exc
    if target_id <= 0 or amount < 0 or (amount == 0 and not set_balance):
        raise ValueError("USER_ID must be positive; amount must be positive for /givecoins.")
    balance = DATABASE.admin_adjust_coins(user_id, target_id, amount, set_balance)
    await update.effective_message.reply_text(
        f"Player {target_id} now has {balance} coins."
    )


async def givecoins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await coin_command(update, context, set_balance=False)


async def setcoins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await coin_command(update, context, set_balance=True)


async def group_ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, admin_id = await require_group_admin(update, context)
    target_id, _ = group_command_target(update, context)
    await ensure_target_is_not_group_admin(context, chat_id, target_id)
    await context.bot.ban_chat_member(chat_id, target_id)
    DATABASE.admin_log(admin_id, "group_ban", f"chat={chat_id}; target={target_id}")
    await update.effective_message.reply_text(f"User {target_id} has been banned.")


async def group_unban_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat_id, admin_id = await require_group_admin(update, context)
    target_id, _ = group_command_target(update, context)
    await context.bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
    DATABASE.admin_log(admin_id, "group_unban", f"chat={chat_id}; target={target_id}")
    await update.effective_message.reply_text(f"User {target_id} has been unbanned.")


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, admin_id = await require_group_admin(update, context)
    target_id, remaining_args = group_command_target(update, context)
    if remaining_args:
        raise ValueError(
            "Use /mute as a reply, or use /mute USER_ID. Use /unmute to restore access."
        )
    await ensure_target_is_not_group_admin(context, chat_id, target_id)
    await context.bot.restrict_chat_member(
        chat_id,
        target_id,
        permissions=ChatPermissions.no_permissions(),
    )
    DATABASE.admin_log(
        admin_id,
        "group_mute",
        f"chat={chat_id}; target={target_id}; until=unmute",
    )
    await update.effective_message.reply_text(
        f"User {target_id} has been muted until an admin uses /unmute."
    )


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, admin_id = await require_group_admin(update, context)
    target_id, _ = group_command_target(update, context)
    chat = await context.bot.get_chat(chat_id)
    permissions = chat.permissions or ChatPermissions.all_permissions()
    await context.bot.restrict_chat_member(
        chat_id, target_id, permissions=permissions
    )
    DATABASE.admin_log(admin_id, "group_unmute", f"chat={chat_id}; target={target_id}")
    await update.effective_message.reply_text(f"User {target_id} has been unmuted.")


async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, admin_id = await require_group_admin(update, context)
    if len(context.args) > 1:
        raise ValueError("Usage: /protect or /protect off")
    mode = context.args[0].lower() if context.args else "on"
    if mode not in ("on", "off"):
        raise ValueError("Usage: /protect or /protect off")
    enabled = mode == "on"

    if enabled:
        try:
            bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        except TelegramError as exc:
            raise ValueError("Could not verify the bot's group permissions.") from exc
        can_delete = (
            bot_member.status == "creator"
            or (
                bot_member.status == "administrator"
                and bool(getattr(bot_member, "can_delete_messages", False))
            )
        )
        if not can_delete:
            raise ValueError(
                "Give the bot Delete messages permission, then run /protect again."
            )

    changed = DATABASE.set_group_protection(chat_id, admin_id, enabled)
    if enabled:
        text = (
            "Group protection is now on. Non-admin links and messages Telegram marks "
            "as protected will be removed."
            if changed
            else "Group protection is already on."
        )
    else:
        text = (
            "Group protection is now off."
            if changed
            else "Group protection is already off."
        )
    await update.effective_message.reply_text(
        text
        + "\nTelegram cannot identify all copyrighted material, so this is not "
        "general copyright detection."
    )
    logger.info(
        "Group protection turned %s for %s by %s",
        "on" if enabled else "off",
        chat_id,
        admin_id,
    )


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await group_ban_command(update, context)
        return
    user_id = update.effective_user.id
    require_admin(user_id)
    if len(context.args) != 1:
        raise ValueError("Usage: /ban USER_ID")
    try:
        target_id = int(context.args[0])
    except ValueError as exc:
        raise ValueError("USER_ID must be a whole number.") from exc
    DATABASE.admin_set_ban(user_id, target_id, True)
    await update.effective_message.reply_text(f"Player {target_id} has been banned.")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await group_unban_command(update, context)
        return
    user_id = update.effective_user.id
    require_admin(user_id)
    if len(context.args) != 1:
        raise ValueError("Usage: /unban USER_ID")
    try:
        target_id = int(context.args[0])
    except ValueError as exc:
        raise ValueError("USER_ID must be a whole number.") from exc
    DATABASE.admin_set_ban(user_id, target_id, False)
    await update.effective_message.reply_text(f"Player {target_id} has been unbanned.")


async def broadcast_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_id = update.effective_user.id
    require_admin(user_id)
    message = " ".join(context.args).strip()
    if not message:
        raise ValueError("Usage: /broadcast MESSAGE")
    if len(message) > 4000:
        raise ValueError("Broadcast messages must be 4,000 characters or fewer.")
    sent = failed = 0
    for target_id in DATABASE.broadcast_user_ids():
        try:
            await context.bot.send_message(chat_id=target_id, text=message)
            sent += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.04)
    DATABASE.set_all_broadcast_logs(user_id, message, sent, failed)
    await update.effective_message.reply_text(
        f"Broadcast finished. Sent: {sent}. Failed: {failed}."
    )


async def text_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Use the buttons or /help to see available commands.",
        reply_markup=main_keyboard(),
    )


async def welcome_new_members(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    if message is None:
        return
    members = [
        member for member in message.new_chat_members if member.id != context.bot.id
    ]
    if not members:
        return
    try:
        member_count: int | str = await context.bot.get_chat_member_count(
            message.chat_id
        )
    except TelegramError:
        logger.exception("Could not retrieve the group member count")
        member_count = "unavailable"

    for member in members:
        username = f"@{member.username}" if member.username else "not set"
        welcome = (
            "✨ ╭── [ 🧚🏻‍♀ ᴡ є ʟ ᴄ σ ϻ є  ʙ ᴧ ʙ ʏ 👀]\n"
            "│\n"
            f"├── 👄 ⇛ η ᴧ ϻ є : {member.full_name}\n"
            f"├── 🫀⇛ υ s є ʀ : {username}\n"
            f"├── 👀 ⇛ ɢ ʀ σ υ ᴘ : {message.chat.title or 'Group'}\n"
            f"├── 🩷 ⇛ ϻ є ϻ ʙ є ʀ s : {member_count}\n"
            "│\n"
            "├── [ 🫡 ɢ ʀ σ υ ᴘ  ʀ υ ʟ є s ]\n"
            "├── 😽 ⇛ ʀєsᴘєᴄᴛ ᴧʟʟ ϻєϻʙєʀs .\n"
            "├── ❤️ ⇛ ησ sᴘᴧϻ σʀ 18+ ᴄσηᴛєηᴛ .\n"
            "├── 💋 ⇛ єηᴊσʏ!\n"
            "│\n"
            "🌸 ╰── BOT ᴘ σ ᴡ є ʀ є ᴅ  ʙ ʏ  (@MARI0_HERE)"
        )
        await message.reply_text(welcome)


async def group_message_guard(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not DATABASE.group_protection_enabled(chat.id):
        return

    has_protected_content = bool(getattr(message, "has_protected_content", False))
    has_link = message_contains_link(message)
    if not has_link and not has_protected_content:
        return

    sender_is_admin = await is_group_admin(update, context)
    # If admin status cannot be checked, fail open rather than delete an admin's post.
    if sender_is_admin is None or sender_is_admin:
        return
    try:
        await message.delete()
        logger.info(
            "Removed a non-admin link/protected-content message from group %s",
            chat.id,
        )
    except TelegramError:
        logger.exception(
            "Could not delete a non-admin link/protected-content message in group %s",
            chat.id,
        )


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger.error("Unhandled Telegram update error", exc_info=context.error)


def create_application(token: str) -> Application:
    app = Application.builder().token(token).post_init(setup_command_menu).build()

    command_handlers = [
        ("start", start_command),
        ("profile", profile_command),
        ("leaderboard", leaderboard_command),
        ("daily", daily_command),
        ("games", games_command),
        ("help", help_command),
        ("dice", dice_command),
        ("rps", rps_command),
        ("number", number_command),
        ("slots", slots_command),
        ("challenge", challenge_command),
        ("admin", admin_command),
        ("stats", stats_command),
        ("givecoins", givecoins_command),
        ("setcoins", setcoins_command),
        ("ban", ban_command),
        ("unban", unban_command),
        ("mute", mute_command),
        ("unmute", unmute_command),
        ("protect", protect_command),
        ("broadcast", broadcast_command),
    ]
    for command, callback in command_handlers:
        app.add_handler(CommandHandler(command, guarded(callback)))

    app.add_handler(
        CallbackQueryHandler(guarded(game_menu_callback), pattern=r"^game:")
    )
    app.add_handler(
        CallbackQueryHandler(guarded(menu_callback), pattern=r"^menu:")
    )
    app.add_handler(CallbackQueryHandler(guarded(rps_callback), pattern=r"^rps:"))
    app.add_handler(CallbackQueryHandler(guarded(number_callback), pattern=r"^num:"))
    app.add_handler(CallbackQueryHandler(guarded(slots_callback), pattern=r"^slot:"))
    app.add_handler(
        CallbackQueryHandler(guarded(battle_callback), pattern=r"^battle:")
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            guarded(text_fallback),
        )
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.StatusUpdate.NEW_CHAT_MEMBERS,
            welcome_new_members,
        )
    )
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS, group_message_guard),
        group=1,
    )
    app.add_error_handler(error_handler)
    return app


async def setup_command_menu(application: Application) -> None:
    private_commands = [
        BotCommand("start", "Open the game menu"),
        BotCommand("games", "See the available games"),
        BotCommand("dice", "Play Dice Duel"),
        BotCommand("rps", "Play Rock Paper Scissors"),
        BotCommand("number", "Play Number Target"),
        BotCommand("slots", "Play Lucky Slots"),
        BotCommand("profile", "View your player stats"),
        BotCommand("leaderboard", "See the top players"),
        BotCommand("daily", "Claim your daily coins"),
        BotCommand("challenge", "Challenge a registered player"),
        BotCommand("help", "Show commands and instructions"),
    ]
    group_commands = [
        BotCommand("start", "Open the game menu"),
        BotCommand("games", "See the available games"),
        BotCommand("dice", "Play Dice Duel"),
        BotCommand("rps", "Play Rock Paper Scissors"),
        BotCommand("number", "Play Number Target"),
        BotCommand("slots", "Play Lucky Slots"),
        BotCommand("profile", "View your player stats"),
        BotCommand("leaderboard", "See the top players"),
        BotCommand("daily", "Claim your daily coins"),
        BotCommand("help", "Show commands and instructions"),
        BotCommand("ban", "Ban a member (reply or user ID)"),
        BotCommand("unban", "Unban a member by user ID"),
        BotCommand("mute", "Mute a member until an admin unmutes them"),
        BotCommand("unmute", "Unmute a member"),
        BotCommand("protect", "Enable or disable group protection"),
    ]
    try:
        await application.bot.set_my_commands(
            private_commands, scope=BotCommandScopeAllPrivateChats()
        )
        await application.bot.set_my_commands(
            group_commands, scope=BotCommandScopeAllGroupChats()
        )
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("Telegram command menu configured.")
    except TelegramError:
        logger.exception("Could not configure the Telegram command menu")


def main() -> int:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        logger.error(
            "BOT_TOKEN is missing. Add it to Replit Secrets before starting GAME HUB."
        )
        return 1
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS is empty. Admin commands will be unavailable.")
    app = create_application(token)
    logger.info("%s is starting with long polling.", BOT_NAME)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        logger.info("Shutdown requested.")
    finally:
        DATABASE.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
