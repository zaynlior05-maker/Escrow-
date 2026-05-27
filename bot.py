#!/usr/bin/env python3
"""
SecureEscrow Bot — Telegram Crypto Escrow Service
Supports: SOL, USDC (Solana), BTC, LTC
Version: 2.0.0  (+ per-deal Forum Topic group chats)

HOW GROUP CHATS WORK
────────────────────
Telegram bots cannot create groups, but they CAN create Topics inside
a Forum-enabled Supergroup.  Flow:
  1. You create one Supergroup → enable "Topics" in group settings
  2. Add the bot as admin with all rights
  3. Set ESCROW_GROUP_ID env var to that group's numeric ID
  4. For every new deal the bot auto-creates a private topic thread,
     posts deal info, generates an invite link, and DMs both parties.
  5. All status updates (payment, lock, release, dispute) are posted
     in the topic so everyone can see in real-time.
"""

import os
import uuid
import sqlite3
import logging
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from telegram.error import TelegramError

# ──────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
# ENVIRONMENT VARIABLES  (set in Railway dashboard or .env)
# ──────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
ADMIN_ID        = int(os.getenv("ADMIN_ID", "0"))
DB_PATH         = os.getenv("DB_PATH", "escrow.db")

# Numeric ID of your Forum-enabled Supergroup  (e.g. -1001234567890)
# If not set, group-topic features are silently skipped.
ESCROW_GROUP_ID = int(os.getenv("ESCROW_GROUP_ID", "0")) or None

# ──────────────────────────────────────────────────────────
# CONVERSATION STATES
# ──────────────────────────────────────────────────────────
(
    SELECT_CRYPTO,
    ENTER_AMOUNT,
    ENTER_DESCRIPTION,
    ENTER_BUYER,
    CONFIRM_DEAL,
) = range(5)

# ──────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────
class Status:
    AWAITING_PAYMENT    = "awaiting_payment"
    PAYMENT_RECEIVED    = "payment_received"
    FUNDS_LOCKED        = "funds_locked"
    DELIVERY_CONFIRMED  = "delivery_confirmed"
    COMPLETED           = "completed"
    DISPUTED            = "disputed"
    REFUNDED            = "refunded"
    CANCELLED           = "cancelled"

CRYPTOS = {
    "SOL":  {"name": "Solana",        "emoji": "◎"},
    "USDC": {"name": "USDC (Solana)", "emoji": "💵"},
    "BTC":  {"name": "Bitcoin",       "emoji": "₿"},
    "LTC":  {"name": "Litecoin",      "emoji": "Ł"},
}

STATUS_EMOJI = {
    Status.AWAITING_PAYMENT:   "⏳",
    Status.PAYMENT_RECEIVED:   "💳",
    Status.FUNDS_LOCKED:       "🔒",
    Status.DELIVERY_CONFIRMED: "📦",
    Status.COMPLETED:          "✅",
    Status.DISPUTED:           "⚠️",
    Status.REFUNDED:           "↩️",
    Status.CANCELLED:          "❌",
}

# Topic icon colours (Telegram's built-in palette IDs)
TOPIC_COLOURS = {
    Status.AWAITING_PAYMENT: 7322096,   # blue
    Status.FUNDS_LOCKED:     16776960,  # yellow
    Status.COMPLETED:        8311585,   # green
    Status.DISPUTED:         16711680,  # red
}

# ──────────────────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id   INTEGER PRIMARY KEY,
            username      TEXT,
            first_name    TEXT,
            is_banned     INTEGER DEFAULT 0,
            joined_at     TEXT    DEFAULT CURRENT_TIMESTAMP,
            total_deals   INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS transactions (
            tx_id             TEXT PRIMARY KEY,
            seller_id         INTEGER NOT NULL,
            buyer_id          INTEGER,
            buyer_username    TEXT,
            crypto            TEXT    NOT NULL,
            amount            REAL    NOT NULL,
            description       TEXT,
            status            TEXT    NOT NULL DEFAULT 'awaiting_payment',
            deposit_address   TEXT,
            deposit_privkey   TEXT,
            seller_wallet     TEXT,
            group_thread_id   INTEGER,          -- Forum topic message_thread_id
            group_invite_link TEXT,             -- One-time invite link for parties
            created_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
            updated_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
            completed_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS disputes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_id      TEXT    NOT NULL,
            raised_by  INTEGER NOT NULL,
            reason     TEXT,
            status     TEXT    DEFAULT 'open',
            resolution TEXT,
            created_at TEXT    DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_id      TEXT,
            action     TEXT    NOT NULL,
            actor_id   INTEGER,
            details    TEXT,
            ts         TEXT    DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()
    logger.info("Database initialised at %s", DB_PATH)

def log_action(tx_id: str, action: str, actor_id: int, details: str = ""):
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log (tx_id, action, actor_id, details) VALUES (?,?,?,?)",
        (tx_id, action, actor_id, details),
    )
    conn.commit()
    conn.close()

def upsert_user(user):
    conn = get_db()
    conn.execute(
        """INSERT INTO users (telegram_id, username, first_name)
           VALUES (?,?,?)
           ON CONFLICT(telegram_id) DO UPDATE
           SET username=excluded.username, first_name=excluded.first_name""",
        (user.id, user.username or "", user.first_name or ""),
    )
    conn.commit()
    conn.close()

def is_banned(user_id: int) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT is_banned FROM users WHERE telegram_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return bool(row and row["is_banned"])

def tx_from_id(tx_id: str) -> Optional[sqlite3.Row]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM transactions WHERE tx_id=?", (tx_id,)
    ).fetchone()
    conn.close()
    return row

def update_tx_status(tx_id: str, status: str):
    conn = get_db()
    conn.execute(
        "UPDATE transactions SET status=?, updated_at=CURRENT_TIMESTAMP WHERE tx_id=?",
        (status, tx_id),
    )
    conn.commit()
    conn.close()

# ──────────────────────────────────────────────────────────
# GROUP / TOPIC HELPERS
# ──────────────────────────────────────────────────────────
async def create_deal_topic(bot, tx_id: str, crypto: str, amount: float,
                             description: str, seller_username: str,
                             buyer_username: str) -> tuple[int | None, str | None]:
    """
    Creates a Forum Topic for the deal inside ESCROW_GROUP_ID.
    Returns (thread_id, invite_link) or (None, None) if group not configured.
    """
    if not ESCROW_GROUP_ID:
        return None, None

    topic_name = f"🔒 {tx_id} · {CRYPTOS[crypto]['emoji']} {amount} {crypto}"
    try:
        topic = await bot.create_forum_topic(
            chat_id=ESCROW_GROUP_ID,
            name=topic_name,
            icon_color=TOPIC_COLOURS[Status.AWAITING_PAYMENT],
        )
        thread_id = topic.message_thread_id

        # Post deal brief inside the topic
        await bot.send_message(
            chat_id=ESCROW_GROUP_ID,
            message_thread_id=thread_id,
            text=(
                f"🔐 *SecureEscrow — Deal Room*\n\n"
                f"🔑 TX: `{tx_id}`\n"
                f"🪙 Coin:  {CRYPTOS[crypto]['emoji']} {crypto}\n"
                f"💰 Amount:  {amount} {crypto}\n"
                f"📝 Deal:  {description}\n"
                f"👤 Seller:  @{seller_username}\n"
                f"🛒 Buyer:  @{buyer_username}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"All status updates will appear here.\n"
                f"Both parties and admin can chat in this thread.\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⏳ *Status: Awaiting Payment*"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

        # Create a single-use invite link (expires in 7 days, no member limit)
        invite = await bot.create_chat_invite_link(
            chat_id=ESCROW_GROUP_ID,
            name=f"Deal {tx_id}",
            expire_date=int(__import__("time").time()) + 7 * 86400,
            member_limit=10,          # seller + buyer + a few admins
            creates_join_request=False,
        )
        return thread_id, invite.invite_link

    except TelegramError as e:
        logger.warning("Could not create forum topic: %s", e)
        return None, None


async def notify_topic(bot, tx_id: str, text: str, kb=None):
    """Post a status message to the deal's Forum Topic."""
    if not ESCROW_GROUP_ID:
        return
    tx = tx_from_id(tx_id)
    if not tx or not tx["group_thread_id"]:
        return
    try:
        await bot.send_message(
            chat_id=ESCROW_GROUP_ID,
            message_thread_id=tx["group_thread_id"],
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
    except TelegramError as e:
        logger.warning("notify_topic failed for %s: %s", tx_id, e)


async def rename_topic(bot, tx_id: str, new_status: str):
    """Update the topic name to reflect current status."""
    if not ESCROW_GROUP_ID:
        return
    tx = tx_from_id(tx_id)
    if not tx or not tx["group_thread_id"]:
        return
    emoji = STATUS_EMOJI.get(new_status, "•")
    c = tx["crypto"]
    a = tx["amount"]
    new_name = f"{emoji} {tx_id} · {CRYPTOS[c]['emoji']} {a} {c}"
    colour = TOPIC_COLOURS.get(new_status)
    try:
        kw = {"icon_color": colour} if colour else {}
        await bot.edit_forum_topic(
            chat_id=ESCROW_GROUP_ID,
            message_thread_id=tx["group_thread_id"],
            name=new_name,
            **kw,
        )
    except TelegramError:
        pass   # non-critical


async def close_topic(bot, tx_id: str):
    """Close (lock) the Forum Topic when deal is done."""
    if not ESCROW_GROUP_ID:
        return
    tx = tx_from_id(tx_id)
    if not tx or not tx["group_thread_id"]:
        return
    try:
        await bot.close_forum_topic(
            chat_id=ESCROW_GROUP_ID,
            message_thread_id=tx["group_thread_id"],
        )
    except TelegramError:
        pass

# ──────────────────────────────────────────────────────────
# CRYPTO ADDRESS GENERATION
# Replace stubs with real library calls for production.
# ──────────────────────────────────────────────────────────
def generate_deposit_address(crypto: str) -> tuple[str, str]:
    """
    Returns (deposit_address, private_key_or_wif).

    ── SOL / USDC ──────────────────────────────────────────
    pip install solders
        from solders.keypair import Keypair
        kp = Keypair()
        return str(kp.pubkey()), kp.to_base58_string()

    ── BTC ─────────────────────────────────────────────────
    pip install bit
        from bit import Key
        k = Key(); return k.address, k.to_wif()

    ── LTC ─────────────────────────────────────────────────
    pip install bit  (or use blockcypher API)
        from bit import PrivateKeyTestnet  # swap for mainnet
        k = PrivateKeyTestnet(); return k.address, k.to_wif()
    """
    if crypto in ("SOL", "USDC"):
        try:
            from solders.keypair import Keypair          # type: ignore
            kp = Keypair()
            return str(kp.pubkey()), kp.to_base58_string()
        except ImportError:
            pass
    if crypto == "BTC":
        try:
            from bit import Key                          # type: ignore
            k = Key()
            return k.address, k.to_wif()
        except ImportError:
            pass
    if crypto == "LTC":
        try:
            from bit import PrivateKeyTestnet            # type: ignore
            k = PrivateKeyTestnet()
            return k.address, k.to_wif()
        except ImportError:
            pass
    # Placeholder — visible in address so you know it's a stub
    ph = uuid.uuid4().hex
    return f"PLACEHOLDER_{crypto}_{ph[:24]}", f"PRIVKEY_{ph}"

# ──────────────────────────────────────────────────────────
# MISC HELPERS
# ──────────────────────────────────────────────────────────
def gen_tx_id() -> str:
    return f"ESC-{uuid.uuid4().hex[:10].upper()}"

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤝 Create Escrow Deal", callback_data="new_deal")],
        [InlineKeyboardButton("💳 Pay Into Escrow",    callback_data="pay_prompt")],
        [InlineKeyboardButton("📋 My Transactions",    callback_data="my_txs")],
        [InlineKeyboardButton("ℹ️ How It Works",       callback_data="how_it_works")],
    ])

# ──────────────────────────────────────────────────────────
# /start
# ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)
    if is_banned(user.id):
        await update.message.reply_text("⛔ Your account has been suspended.")
        return

    if context.args and context.args[0].startswith("pay_"):
        context.args = [context.args[0][4:]]
        await cmd_pay(update, context)
        return

    await update.message.reply_text(
        f"🔐 *SecureEscrow Bot*\n\n"
        f"Hello, {user.first_name}! Your trusted crypto escrow service.\n\n"
        f"*Supported coins:*  ◎ SOL  💵 USDC  ₿ BTC  Ł LTC\n\n"
        f"*Flow:*\n"
        f"1 · Seller creates deal\n"
        f"2 · Bot opens a private group chat for both parties\n"
        f"3 · Buyer pays → Admin verifies → Funds locked\n"
        f"4 · Buyer confirms delivery → Admin releases to seller",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard(),
    )

# ──────────────────────────────────────────────────────────
# CREATE DEAL — Conversation
# ──────────────────────────────────────────────────────────
async def start_new_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)
    if is_banned(user.id):
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("◎ SOL",  callback_data="c_SOL"),
         InlineKeyboardButton("💵 USDC", callback_data="c_USDC")],
        [InlineKeyboardButton("₿ BTC",  callback_data="c_BTC"),
         InlineKeyboardButton("Ł LTC",  callback_data="c_LTC")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    msg = update.message or update.callback_query.message
    await msg.reply_text(
        "🤝 *New Escrow Deal* — Step 1 / 4\n\nSelect the cryptocurrency:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    return SELECT_CRYPTO

async def step_select_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    crypto = q.data[2:]
    context.user_data["crypto"] = crypto
    await q.edit_message_text(
        f"✅ Coin: {CRYPTOS[crypto]['emoji']} *{crypto}*\n\n"
        f"Step 2 / 4 — Enter the *amount* to escrow:\n_(e.g. 0.25)_",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ENTER_AMOUNT

async def step_enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a valid positive number.")
        return ENTER_AMOUNT
    context.user_data["amount"] = amount
    crypto = context.user_data["crypto"]
    await update.message.reply_text(
        f"✅ Amount: *{amount} {crypto}*\n\n"
        f"Step 3 / 4 — Describe the *deal / item / service*:\n"
        f"_(Be specific — protects both parties)_",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ENTER_DESCRIPTION

async def step_enter_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    if len(desc) < 5:
        await update.message.reply_text("❌ Please be more descriptive.")
        return ENTER_DESCRIPTION
    context.user_data["description"] = desc
    await update.message.reply_text(
        "✅ Description saved.\n\n"
        "Step 4 / 4 — Enter the *buyer's Telegram username*:\n_(e.g. @username)_",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ENTER_BUYER

async def step_enter_buyer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buyer = update.message.text.strip().lstrip("@")
    context.user_data["buyer_username"] = buyer
    d = context.user_data
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Create Deal", callback_data="confirm_deal")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await update.message.reply_text(
        f"📋 *Deal Summary*\n\n"
        f"🪙 Coin:        {CRYPTOS[d['crypto']]['emoji']} {d['crypto']}\n"
        f"💰 Amount:     {d['amount']} {d['crypto']}\n"
        f"📝 Description: {d['description']}\n"
        f"👤 Buyer:       @{buyer}\n\n"
        f"Confirm?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    return CONFIRM_DEAL

async def step_confirm_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Creating deal & group chat… please wait")
    user = update.effective_user
    d = context.user_data

    tx_id    = gen_tx_id()
    addr, pk = generate_deposit_address(d["crypto"])

    # ── Create Forum Topic for this deal ───────────────────
    thread_id, invite_link = await create_deal_topic(
        context.bot,
        tx_id,
        d["crypto"],
        d["amount"],
        d["description"],
        user.username or str(user.id),
        d["buyer_username"],
    )

    # ── Save to DB ─────────────────────────────────────────
    conn = get_db()
    conn.execute(
        """INSERT INTO transactions
           (tx_id, seller_id, buyer_username, crypto, amount, description,
            status, deposit_address, deposit_privkey,
            group_thread_id, group_invite_link)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (tx_id, user.id, d["buyer_username"], d["crypto"], d["amount"],
         d["description"], Status.AWAITING_PAYMENT, addr, pk,
         thread_id, invite_link),
    )
    conn.commit()
    conn.close()
    log_action(tx_id, "DEAL_CREATED", user.id, f"buyer:@{d['buyer_username']}")

    bot_username = (await context.bot.get_me()).username
    payment_link = f"https://t.me/{bot_username}?start=pay_{tx_id}"

    # ── Build group-chat section for messages ──────────────
    group_section = ""
    if invite_link and thread_id:
        group_section = (
            f"\n\n💬 *Deal Group Chat:*\n"
            f"[Join the deal room]({invite_link})\n"
            f"_(Seller, buyer & admin can chat there)_"
        )

    # ── Notify admin ───────────────────────────────────────
    await context.bot.send_message(
        ADMIN_ID,
        f"🔔 *New Deal Created*\n\n"
        f"TX: `{tx_id}`\n"
        f"Seller: @{user.username} ({user.id})\n"
        f"Buyer:  @{d['buyer_username']}\n"
        f"Amount: {d['amount']} {d['crypto']}\n"
        f"Desc:   {d['description']}\n"
        f"Deposit: `{addr}`"
        + (f"\nGroup thread ID: `{thread_id}`" if thread_id else ""),
        parse_mode=ParseMode.MARKDOWN,
    )

    # ── Notify buyer (DM) ──────────────────────────────────
    buyer_msg = (
        f"🔐 *Escrow Deal Invitation*\n\n"
        f"TX: `{tx_id}`\n"
        f"Amount: {d['amount']} {d['crypto']}\n"
        f"Deal:   {d['description']}\n"
        f"From:   @{user.username}\n\n"
        f"💳 Pay here 👉 {payment_link}"
        + group_section
    )
    try:
        await context.bot.send_message(
            f"@{d['buyer_username']}", buyer_msg,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
    except TelegramError:
        pass   # Buyer hasn't started the bot yet — link is enough

    # ── Reply to seller ────────────────────────────────────
    await q.edit_message_text(
        f"✅ *Deal Created!*\n\n"
        f"🔑 TX: `{tx_id}`\n"
        f"💰 {d['amount']} {d['crypto']}\n"
        f"📝 {d['description']}\n"
        f"👤 Buyer: @{d['buyer_username']}\n\n"
        f"📤 *Payment link for buyer:*\n`{payment_link}`\n\n"
        f"📬 Deposit address:\n`{addr}`"
        + group_section
        + "\n\n⏳ Waiting for buyer payment…",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )
    context.user_data.clear()
    return ConversationHandler.END

async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()
        await q.edit_message_text("❌ Cancelled.")
    else:
        await update.message.reply_text("❌ Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ──────────────────────────────────────────────────────────
# /pay  — Buyer views deal & marks payment sent
# ──────────────────────────────────────────────────────────
async def cmd_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "💳 Usage: `/pay TRANSACTION_ID`\nor use the link from your seller.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    tx_id = args[0].upper()
    tx = tx_from_id(tx_id)
    if not tx:
        await update.message.reply_text("❌ Transaction not found.")
        return
    if tx["status"] != Status.AWAITING_PAYMENT:
        await update.message.reply_text(
            f"❌ This deal is not accepting payment.\nStatus: `{tx['status']}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    group_line = ""
    if tx["group_invite_link"]:
        group_line = (
            f"\n\n💬 [Open Deal Group Chat]({tx['group_invite_link']})"
        )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I've Sent the Payment", callback_data=f"paid_{tx_id}")],
        [InlineKeyboardButton("❓ Help",                  callback_data=f"help_{tx_id}")],
    ])
    await update.message.reply_text(
        f"💳 *Payment Details*\n\n"
        f"🔑 TX: `{tx_id}`\n"
        f"💰 Send exactly: *{tx['amount']} {tx['crypto']}*\n"
        f"📝 Deal: {tx['description']}\n\n"
        f"📬 *Deposit address:*\n`{tx['deposit_address']}`\n\n"
        f"⚠️ Exact amount · {tx['crypto']} only · wait for on-chain confirmation"
        f"{group_line}\n\n"
        f"Click below after sending:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
        disable_web_page_preview=True,
    )

# ──────────────────────────────────────────────────────────
# Callback: buyer marks payment sent
# ──────────────────────────────────────────────────────────
async def cb_payment_sent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = update.effective_user
    tx_id = q.data[5:]

    tx = tx_from_id(tx_id)
    if not tx:
        await q.edit_message_text("❌ Transaction not found.")
        return

    conn = get_db()
    conn.execute(
        "UPDATE transactions SET status=?, buyer_id=?, updated_at=CURRENT_TIMESTAMP WHERE tx_id=?",
        (Status.PAYMENT_RECEIVED, user.id, tx_id),
    )
    conn.commit()
    conn.close()
    log_action(tx_id, "PAYMENT_MARKED_SENT", user.id)

    # Post in group topic
    await notify_topic(
        context.bot, tx_id,
        f"💳 *Payment Marked Sent*\n\n"
        f"Buyer @{user.username} has notified payment.\n"
        f"Admin is verifying on-chain — please wait…",
    )
    await rename_topic(context.bot, tx_id, Status.PAYMENT_RECEIVED)

    # Alert admin
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Payment",  callback_data=f"adm_confirm_{tx_id}")],
        [InlineKeyboardButton("❌ Not Found",        callback_data=f"adm_notfound_{tx_id}")],
    ])
    await context.bot.send_message(
        ADMIN_ID,
        f"🔔 *Payment Marked Sent*\n\n"
        f"TX: `{tx_id}`\n"
        f"Buyer: @{user.username} ({user.id})\n"
        f"Amount: {tx['amount']} {tx['crypto']}\n"
        f"Deposit: `{tx['deposit_address']}`\n\n"
        f"Please verify on-chain:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    # Alert seller
    try:
        await context.bot.send_message(
            tx["seller_id"],
            f"🔔 *Payment Marked Sent — {tx_id}*\n\n"
            f"Buyer notified payment. Admin is verifying.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError:
        pass

    await q.edit_message_text(
        f"✅ *Admin Notified*\n\nTX: `{tx_id}`\n\nAdmin will verify on-chain shortly.\n⏳ Please wait…",
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────────────────
# /confirm  — Buyer confirms delivery
# ──────────────────────────────────────────────────────────
async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text(
            "Usage: `/confirm TRANSACTION_ID`", parse_mode=ParseMode.MARKDOWN
        )
        return
    tx_id = context.args[0].upper()
    tx = tx_from_id(tx_id)
    if not tx or tx["buyer_id"] != user.id:
        await update.message.reply_text("❌ Not found or you're not the buyer.")
        return
    if tx["status"] != Status.FUNDS_LOCKED:
        await update.message.reply_text(
            f"❌ Cannot confirm at this stage. Status: `{tx['status']}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    update_tx_status(tx_id, Status.DELIVERY_CONFIRMED)
    log_action(tx_id, "DELIVERY_CONFIRMED", user.id)

    # Post in group topic
    await notify_topic(
        context.bot, tx_id,
        f"📦 *Buyer Confirmed Delivery!*\n\n"
        f"@{user.username} has confirmed they received the goods/service.\n"
        f"Admin will now release funds to the seller.",
    )
    await rename_topic(context.bot, tx_id, Status.DELIVERY_CONFIRMED)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Release Funds to Seller", callback_data=f"adm_release_{tx_id}")],
    ])
    await context.bot.send_message(
        ADMIN_ID,
        f"✅ *Buyer Confirmed Delivery*\n\n"
        f"TX: `{tx_id}`  ·  {tx['amount']} {tx['crypto']}\n"
        f"Buyer: @{user.username}\n\nRelease funds?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    await update.message.reply_text(
        f"✅ *Delivery Confirmed!*\n\nTX: `{tx_id}`\nAdmin notified to release funds.",
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────────────────
# /dispute  — Raise a dispute
# ──────────────────────────────────────────────────────────
async def cmd_dispute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/dispute TX_ID Your reason here`", parse_mode=ParseMode.MARKDOWN
        )
        return
    tx_id  = args[0].upper()
    reason = " ".join(args[1:])
    tx = tx_from_id(tx_id)
    if not tx or user.id not in (tx["seller_id"], tx["buyer_id"]):
        await update.message.reply_text("❌ Not found or not your deal.")
        return

    conn = get_db()
    conn.execute(
        "INSERT INTO disputes (tx_id, raised_by, reason) VALUES (?,?,?)",
        (tx_id, user.id, reason),
    )
    conn.execute(
        "UPDATE transactions SET status=?, updated_at=CURRENT_TIMESTAMP WHERE tx_id=?",
        (Status.DISPUTED, tx_id),
    )
    conn.commit()
    conn.close()
    log_action(tx_id, "DISPUTE_RAISED", user.id, reason)

    # Post in group topic
    await notify_topic(
        context.bot, tx_id,
        f"⚠️ *DISPUTE RAISED*\n\n"
        f"By: @{user.username}\n"
        f"Reason: {reason}\n\n"
        f"🔒 Funds are frozen. Admin is reviewing.\n"
        f"Expected resolution within 24 hours.",
    )
    await rename_topic(context.bot, tx_id, Status.DISPUTED)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Release to Seller", callback_data=f"adm_release_{tx_id}")],
        [InlineKeyboardButton("↩️ Refund Buyer",      callback_data=f"adm_refund_{tx_id}")],
    ])
    await context.bot.send_message(
        ADMIN_ID,
        f"⚠️ *DISPUTE RAISED*\n\n"
        f"TX: `{tx_id}`\n"
        f"By: @{user.username} ({user.id})\n"
        f"Amount: {tx['amount']} {tx['crypto']}\n"
        f"Reason: {reason}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    await update.message.reply_text(
        f"⚠️ *Dispute Filed*\n\nTX: `{tx_id}`\nAdmin will review within 24 hours.\nFunds are frozen.",
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────────────────
# /mytxs  — List transactions
# ──────────────────────────────────────────────────────────
async def cmd_mytxs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db()
    rows = conn.execute(
        """SELECT tx_id, crypto, amount, status FROM transactions
           WHERE seller_id=? OR buyer_id=?
           ORDER BY created_at DESC LIMIT 15""",
        (user.id, user.id),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📋 No transactions yet.")
        return
    lines = ["📋 *Your Recent Transactions:*\n"]
    for r in rows:
        e = STATUS_EMOJI.get(r["status"], "•")
        lines.append(f"{e} `{r['tx_id']}` — {r['amount']} {r['crypto']} ({r['status']})")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ──────────────────────────────────────────────────────────
# /txinfo  — Transaction detail
# ──────────────────────────────────────────────────────────
async def cmd_txinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage: `/txinfo TX_ID`", parse_mode=ParseMode.MARKDOWN)
        return
    tx_id = context.args[0].upper()
    tx = tx_from_id(tx_id)
    if not tx:
        await update.message.reply_text("❌ Not found.")
        return
    if user.id != ADMIN_ID and user.id not in (tx["seller_id"], tx["buyer_id"] or -1):
        await update.message.reply_text("❌ Unauthorised.")
        return

    admin_line = f"\n🔑 Deposit: `{tx['deposit_address']}`" if user.id == ADMIN_ID else ""
    group_line = f"\n💬 Group invite: {tx['group_invite_link']}" if tx["group_invite_link"] else ""
    e = STATUS_EMOJI.get(tx["status"], "•")
    await update.message.reply_text(
        f"📋 *Transaction Info*\n\n"
        f"TX:      `{tx['tx_id']}`\n"
        f"Status:  {e} `{tx['status']}`\n"
        f"Coin:    {tx['crypto']}\n"
        f"Amount:  {tx['amount']}\n"
        f"Deal:    {tx['description']}\n"
        f"Buyer:   @{tx['buyer_username']}\n"
        f"Created: {tx['created_at']}"
        f"{admin_line}{group_line}",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )

# ──────────────────────────────────────────────────────────
# /help
# ──────────────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_section = ""
    if user.id == ADMIN_ID:
        admin_section = (
            "\n\n*Admin Commands:*\n"
            "`/stats` — Dashboard\n"
            "`/pending` — Active deals\n"
            "`/release TX_ID` — Release to seller\n"
            "`/refund TX_ID` — Refund buyer\n"
            "`/ban USER_ID` — Ban user\n"
        )
    await update.message.reply_text(
        "🔐 *SecureEscrow — Help*\n\n"
        "*User Commands:*\n"
        "`/newdeal` — Create escrow (seller)\n"
        "`/pay TX_ID` — Pay into escrow (buyer)\n"
        "`/confirm TX_ID` — Confirm delivery (buyer)\n"
        "`/dispute TX_ID reason` — Raise dispute\n"
        "`/mytxs` — My transactions\n"
        "`/txinfo TX_ID` — Deal details"
        + admin_section,
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────────────────
# ADMIN DECORATOR
# ──────────────────────────────────────────────────────────
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            msg = update.message or (
                update.callback_query.message if update.callback_query else None
            )
            if msg:
                await msg.reply_text("❌ Unauthorised.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper

# ──────────────────────────────────────────────────────────
# ADMIN — confirm payment
# ──────────────────────────────────────────────────────────
@admin_only
async def admin_confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tx_id = q.data.replace("adm_confirm_", "")
    tx = tx_from_id(tx_id)
    if not tx:
        await q.edit_message_text("❌ Not found.")
        return

    update_tx_status(tx_id, Status.FUNDS_LOCKED)
    log_action(tx_id, "PAYMENT_CONFIRMED_ADMIN", ADMIN_ID)

    # Post in group topic
    await notify_topic(
        context.bot, tx_id,
        f"🔒 *Payment Confirmed — Funds Locked!*\n\n"
        f"Admin has verified the payment on-chain.\n\n"
        f"💰 {tx['amount']} {tx['crypto']} is now locked in escrow.\n\n"
        f"*Seller:* please deliver the goods/service now.\n"
        f"*Buyer:* once you receive — use `/confirm {tx_id}` in the bot.",
    )
    await rename_topic(context.bot, tx_id, Status.FUNDS_LOCKED)

    for uid, msg in [
        (tx["buyer_id"],
         f"✅ *Payment Confirmed!*\nTX `{tx_id}` — funds locked 🔒\n\n"
         f"After receiving the goods:\n`/confirm {tx_id}`\n\n"
         f"Problem? `/dispute {tx_id} reason`"),
        (tx["seller_id"],
         f"🔒 *Funds Locked!*\nTX `{tx_id}` — {tx['amount']} {tx['crypto']} secured.\n\n"
         f"Please deliver the goods/service now.\n"
         f"Funds release after buyer confirmation."),
    ]:
        try:
            await context.bot.send_message(uid, msg, parse_mode=ParseMode.MARKDOWN)
        except TelegramError:
            pass
    await q.edit_message_text(
        f"✅ Payment confirmed for `{tx_id}`. Both parties notified.",
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────────────────
# ADMIN — payment not found
# ──────────────────────────────────────────────────────────
@admin_only
async def admin_payment_notfound(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tx_id = q.data.replace("adm_notfound_", "")
    tx = tx_from_id(tx_id)
    if not tx:
        return
    update_tx_status(tx_id, Status.AWAITING_PAYMENT)

    await notify_topic(
        context.bot, tx_id,
        f"⚠️ *Payment Not Found*\n\n"
        f"Admin could not verify the payment on-chain.\n"
        f"Buyer please double-check the deposit address and amount, then try again.",
    )

    if tx["buyer_id"]:
        try:
            await context.bot.send_message(
                tx["buyer_id"],
                f"⚠️ *Payment Not Found* — TX `{tx_id}`\n\n"
                f"Admin could not verify your payment.\n"
                f"Check you sent to the correct address & contact support.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass
    await q.edit_message_text(
        f"❌ Payment not found — `{tx_id}` reset to awaiting.",
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────────────────
# ADMIN — release funds
# ──────────────────────────────────────────────────────────
@admin_only
async def admin_release(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()
        tx_id = q.data.replace("adm_release_", "")
    else:
        if not context.args:
            await update.message.reply_text(
                "Usage: `/release TX_ID`", parse_mode=ParseMode.MARKDOWN
            )
            return
        tx_id = context.args[0].upper()

    tx = tx_from_id(tx_id)
    if not tx:
        dest = q.edit_message_text if q else update.message.reply_text
        await dest("❌ Not found.")
        return

    conn = get_db()
    conn.execute(
        "UPDATE transactions SET status=?, completed_at=CURRENT_TIMESTAMP, "
        "updated_at=CURRENT_TIMESTAMP WHERE tx_id=?",
        (Status.COMPLETED, tx_id),
    )
    conn.commit()
    conn.close()
    log_action(tx_id, "FUNDS_RELEASED_ADMIN", ADMIN_ID)

    # NOTE: In production, broadcast the actual on-chain transfer here.

    # Post in group topic then close it
    await notify_topic(
        context.bot, tx_id,
        f"✅ *Escrow Complete — Funds Released!*\n\n"
        f"💸 {tx['amount']} {tx['crypto']} has been released to the seller.\n\n"
        f"Thank you for using SecureEscrow!\n"
        f"This deal room will now be closed.",
    )
    await rename_topic(context.bot, tx_id, Status.COMPLETED)
    await close_topic(context.bot, tx_id)

    for uid, msg in [
        (tx["seller_id"],
         f"💸 *Funds Released!*\nTX `{tx_id}` — {tx['amount']} {tx['crypto']}\n\nEscrow complete ✅"),
        (tx["buyer_id"],
         f"✅ *Escrow Complete!*\nTX `{tx_id}` — deal concluded. Thank you!"),
    ]:
        if uid:
            try:
                await context.bot.send_message(uid, msg, parse_mode=ParseMode.MARKDOWN)
            except TelegramError:
                pass

    reply = f"💸 Funds released for `{tx_id}` — {tx['amount']} {tx['crypto']}"
    if q:
        await q.edit_message_text(reply, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

# ──────────────────────────────────────────────────────────
# ADMIN — refund buyer
# ──────────────────────────────────────────────────────────
@admin_only
async def admin_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()
        tx_id = q.data.replace("adm_refund_", "")
    else:
        if not context.args:
            await update.message.reply_text(
                "Usage: `/refund TX_ID`", parse_mode=ParseMode.MARKDOWN
            )
            return
        tx_id = context.args[0].upper()

    tx = tx_from_id(tx_id)
    if not tx:
        return

    update_tx_status(tx_id, Status.REFUNDED)
    log_action(tx_id, "REFUND_ISSUED_ADMIN", ADMIN_ID)

    await notify_topic(
        context.bot, tx_id,
        f"↩️ *Refund Approved by Admin*\n\n"
        f"{tx['amount']} {tx['crypto']} will be returned to the buyer.\n"
        f"This deal room will now be closed.",
    )
    await rename_topic(context.bot, tx_id, Status.REFUNDED)
    await close_topic(context.bot, tx_id)

    for uid, msg in [
        (tx["buyer_id"],
         f"↩️ *Refund Approved*\nTX `{tx_id}` — {tx['amount']} {tx['crypto']}\nFunds will be returned."),
        (tx["seller_id"],
         f"↩️ *Refund Issued*\nTX `{tx_id}` — admin decided to refund the buyer."),
    ]:
        if uid:
            try:
                await context.bot.send_message(uid, msg, parse_mode=ParseMode.MARKDOWN)
            except TelegramError:
                pass

    reply = f"↩️ Refund processed for `{tx_id}`"
    if q:
        await q.edit_message_text(reply, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

# ──────────────────────────────────────────────────────────
# ADMIN — stats dashboard
# ──────────────────────────────────────────────────────────
@admin_only
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    stats = {}
    for s in (Status.AWAITING_PAYMENT, Status.FUNDS_LOCKED, Status.COMPLETED,
              Status.DISPUTED, Status.REFUNDED):
        stats[s] = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE status=?", (s,)
        ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()

    group_line = f"\n🗂 Group ID: `{ESCROW_GROUP_ID}`" if ESCROW_GROUP_ID else "\n⚠️ Group not configured"
    await update.message.reply_text(
        f"📊 *Admin Dashboard*\n\n"
        f"👥 Users: {users}   📋 Total TXs: {total}"
        f"{group_line}\n\n"
        f"⏳ Awaiting payment: {stats[Status.AWAITING_PAYMENT]}\n"
        f"🔒 Funds locked:     {stats[Status.FUNDS_LOCKED]}\n"
        f"✅ Completed:        {stats[Status.COMPLETED]}\n"
        f"⚠️ Disputed:         {stats[Status.DISPUTED]}\n"
        f"↩️ Refunded:         {stats[Status.REFUNDED]}",
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────────────────
# ADMIN — pending deals
# ──────────────────────────────────────────────────────────
@admin_only
async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    rows = conn.execute(
        """SELECT tx_id, crypto, amount, status, buyer_username FROM transactions
           WHERE status NOT IN ('completed','refunded','cancelled')
           ORDER BY created_at DESC""",
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("✅ No active transactions.")
        return
    lines = [f"📋 *Active Transactions ({len(rows)}):*\n"]
    for r in rows:
        e = STATUS_EMOJI.get(r["status"], "•")
        lines.append(
            f"{e} `{r['tx_id']}` — {r['amount']} {r['crypto']}\n"
            f"   Status: {r['status']}  Buyer: @{r['buyer_username']}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ──────────────────────────────────────────────────────────
# ADMIN — ban user
# ──────────────────────────────────────────────────────────
@admin_only
async def admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: `/ban USER_ID`", parse_mode=ParseMode.MARKDOWN
        )
        return
    uid = int(context.args[0])
    conn = get_db()
    conn.execute("UPDATE users SET is_banned=1 WHERE telegram_id=?", (uid,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ User {uid} banned.")
    try:
        await context.bot.send_message(uid, "⛔ Your account has been suspended.")
    except TelegramError:
        pass

# ──────────────────────────────────────────────────────────
# GENERIC CALLBACK ROUTER
# ──────────────────────────────────────────────────────────
async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = q.data

    if d == "new_deal":
        await start_new_deal(update, context)
    elif d == "pay_prompt":
        await q.answer()
        await q.edit_message_text(
            "💳 To pay, use:\n`/pay TRANSACTION_ID`\nor open the link from your seller.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif d == "my_txs":
        await q.answer()
        await cmd_mytxs(update, context)
    elif d == "how_it_works":
        await q.answer()
        await q.edit_message_text(
            "ℹ️ *How SecureEscrow Works*\n\n"
            "1️⃣ Seller creates deal → bot generates deposit address\n"
            "2️⃣ Bot opens a private *group topic* for seller, buyer & admin\n"
            "3️⃣ Buyer pays → notifies bot → admin verifies on-chain\n"
            "4️⃣ Admin locks funds · all see update in group chat\n"
            "5️⃣ Seller delivers goods/service\n"
            "6️⃣ Buyer confirms → `/confirm TX_ID`\n"
            "7️⃣ Admin releases funds to seller — group topic closes\n\n"
            "Problem at any stage? `/dispute TX_ID reason`",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif d.startswith("paid_"):
        await cb_payment_sent(update, context)
    elif d.startswith("adm_confirm_"):
        await admin_confirm_payment(update, context)
    elif d.startswith("adm_notfound_"):
        await admin_payment_notfound(update, context)
    elif d.startswith("adm_release_"):
        await admin_release(update, context)
    elif d.startswith("adm_refund_"):
        await admin_refund(update, context)
    elif d == "cancel":
        await q.answer()
        await q.edit_message_text("❌ Cancelled.")
    else:
        await q.answer()

# ──────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set!")
    if not ADMIN_ID:
        raise RuntimeError("ADMIN_ID environment variable is not set!")
    if not ESCROW_GROUP_ID:
        logger.warning(
            "ESCROW_GROUP_ID not set — group topic chats are disabled. "
            "Create a Forum supergroup, add the bot as admin, and set this variable."
        )

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    deal_conv = ConversationHandler(
        entry_points=[
            CommandHandler("newdeal", start_new_deal),
            CallbackQueryHandler(start_new_deal, pattern="^new_deal$"),
        ],
        states={
            SELECT_CRYPTO:     [CallbackQueryHandler(step_select_crypto,    pattern="^c_")],
            ENTER_AMOUNT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_enter_amount)],
            ENTER_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_enter_description)],
            ENTER_BUYER:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_enter_buyer)],
            CONFIRM_DEAL:      [CallbackQueryHandler(step_confirm_deal,      pattern="^confirm_deal$")],
        },
        fallbacks=[
            CallbackQueryHandler(conv_cancel, pattern="^cancel$"),
            CommandHandler("cancel", conv_cancel),
        ],
    )

    app.add_handler(deal_conv)
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("pay",     cmd_pay))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("dispute", cmd_dispute))
    app.add_handler(CommandHandler("mytxs",   cmd_mytxs))
    app.add_handler(CommandHandler("txinfo",  cmd_txinfo))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("stats",   admin_stats))
    app.add_handler(CommandHandler("pending", admin_pending))
    app.add_handler(CommandHandler("release", admin_release))
    app.add_handler(CommandHandler("refund",  admin_refund))
    app.add_handler(CommandHandler("ban",     admin_ban))
    app.add_handler(CallbackQueryHandler(button_router))

    logger.info("🤖 SecureEscrow Bot v2.0 is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
