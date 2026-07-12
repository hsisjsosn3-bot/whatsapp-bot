# -*- coding: utf-8 -*-
"""
bot.py — Telegram <-> WhatsApp Web bridge (premium UI edition)

Features
--------
- /login       -> WhatsApp se connect: QR code YA "link with phone number" code
- /send <phone> <message> -> WhatsApp pe message bhejna
- Incoming WhatsApp messages -> automatically Telegram pe forward
- Session persistence (cookies + Chrome profile) -> baar baar login nahi karna padta
- Headless Chrome + Selenium + webdriver-manager
- Render.com Background Worker par deploy-ready
- Premium look: reply keyboard (keyword buttons) + inline buttons + styled "cards"

Run:
    python bot.py
"""

from __future__ import annotations

import asyncio
import os
import pickle
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from webdriver_manager.chrome import ChromeDriverManager

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SESSION_FILE = os.getenv("SESSION_FILE", "wa_session.pkl")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))
HEADLESS = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")
CHROME_BIN = os.getenv("CHROME_BIN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
BOT_NAME = os.getenv("BOT_NAME", "WaveLink")

WHATSAPP_URL = "https://web.whatsapp.com"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN set nahi hai. .env.example ko .env mein copy karke BOT_TOKEN daalein.")

logger.remove()
logger.add(lambda m: print(m, end=""), level="INFO", colorize=True)
logger.add("bot.log", rotation="5 MB", retention=3, level="DEBUG")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

START_TIME = datetime.now()
DIVIDER = "──────────────────"


def human_uptime() -> str:
    d = datetime.now() - START_TIME
    h, rem = divmod(int(d.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def card(title: str, body: str, footer: str = "") -> str:
    text = f"✨ <b>{title}</b>\n{DIVIDER}\n{body}"
    if footer:
        text += f"\n{DIVIDER}\n<i>{footer}</i>"
    return text


# --------------------------------------------------------------------------- #
# Reply (keyword) keyboard — "premium" bottom menu
# --------------------------------------------------------------------------- #

BTN_LOGIN = "🔑 Login"
BTN_SEND = "📤 Send Message"
BTN_STATUS = "📶 Status"
BTN_LOGOUT = "🚪 Logout"
BTN_HELP = "❓ Help"

main_reply_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_LOGIN), KeyboardButton(text=BTN_STATUS)],
        [KeyboardButton(text=BTN_SEND), KeyboardButton(text=BTN_LOGOUT)],
        [KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Ek option chunein ya /help likhein…",
)


def login_method_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📷 QR Code se", callback_data="login_qr")],
            [InlineKeyboardButton(text="🔢 Phone Number Code se", callback_data="login_code")],
        ]
    )


def status_emoji() -> str:
    if wa_holder.get("driver") is None:
        return "🔴"
    return "🟢" if wa_holder.get("logged_in") else "🟡"


HELP_TEXT = (
    "🔑 <code>/login</code> — WhatsApp connect karein (QR ya phone-code)\n"
    "📤 <code>/send &lt;phone&gt; &lt;message&gt;</code> — message bhejein\n"
    "   Example: <code>/send +919876543210 Hi there!</code>\n"
    "📶 <code>/status</code> — connection health dekhein\n"
    "🚪 <code>/logout</code> — session clear karein\n\n"
    "Neeche diye buttons se bhi same kaam ho sakta hai."
)

# --------------------------------------------------------------------------- #
# WhatsApp Web driver wrapper
# --------------------------------------------------------------------------- #

@dataclass
class WhatsAppSession:
    driver: Optional[webdriver.Chrome] = None
    logged_in: bool = False
    connected_since: Optional[datetime] = None
    seen_unread_keys: set = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ---- lifecycle ----------------------------------------------------

    def _resolve_chrome_binary(self) -> Optional[str]:
        """
        Find an actual Chrome/Chromium executable on disk. CHROME_BIN is
        tried first, then common install locations (including Render's
        render-build.sh path), then whatever is on PATH. Returns None if
        nothing is found — the caller decides what to do with that.
        """
        candidates = []
        if CHROME_BIN:
            candidates.append(CHROME_BIN)

        candidates += [
            # Render.com (render-build.sh) path
            str(Path.home() / "project" / ".render" / "chrome" / "opt" / "google" / "chrome" / "chrome"),
            "/opt/render/project/.render/chrome/opt/google/chrome/chrome",
            # common Linux install locations
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]

        for path in candidates:
            if path and Path(path).is_file() and os.access(path, os.X_OK):
                return path

        # last resort: search PATH
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                return found

        return None

    def _build_driver(self) -> webdriver.Chrome:
        options = Options()
        if HEADLESS:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1280,1024")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        profile_dir = Path("chrome_profile").resolve()
        profile_dir.mkdir(exist_ok=True)
        options.add_argument(f"--user-data-dir={profile_dir}")

        chrome_path = self._resolve_chrome_binary()
        if chrome_path is None:
            raise RuntimeError(
                "Chrome binary nahi mila. CHROME_BIN environment variable ko "
                "Chrome executable ke full path pe set karein (e.g. the path "
                "render-build.sh prints), ya server pe Chrome/Chromium install karein."
            )
        options.binary_location = chrome_path
        logger.info("Using Chrome binary at {}", chrome_path)

        # Prefer webdriver-manager (pins an exact chromedriver version), but
        # fall back to Selenium's own built-in driver manager (Selenium
        # Manager, bundled since 4.6) if version auto-detection fails —
        # that happens when Chrome isn't a standard "google-chrome" PATH
        # entry, which webdriver-manager's version sniffing depends on.
        try:
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
        except Exception as e:
            logger.warning("webdriver-manager failed ({}), falling back to Selenium Manager", e)
            driver = webdriver.Chrome(options=options)

        driver.set_page_load_timeout(60)
        return driver

    def start(self) -> None:
        if self.driver is not None:
            return
        logger.info("Launching Chrome (headless={})", HEADLESS)
        self.driver = self._build_driver()
        self.driver.get(WHATSAPP_URL)
        self._load_cookies()

    def stop(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            self.driver = None
        self.logged_in = False
        self.connected_since = None

    # ---- session persistence -------------------------------------------

    def _save_cookies(self) -> None:
        if self.driver is None:
            return
        try:
            with open(SESSION_FILE, "wb") as f:
                pickle.dump(self.driver.get_cookies(), f)
        except WebDriverException as e:
            logger.warning("cookie save failed: {}", e)

    def _load_cookies(self) -> None:
        path = Path(SESSION_FILE)
        if not path.exists() or self.driver is None:
            return
        try:
            with open(path, "rb") as f:
                cookies = pickle.load(f)
            for c in cookies:
                c.pop("sameSite", None)
                try:
                    self.driver.add_cookie(c)
                except WebDriverException:
                    continue
            self.driver.refresh()
        except (pickle.PickleError, EOFError, OSError) as e:
            logger.warning("cookie load failed: {}", e)

    def clear_session(self) -> None:
        Path(SESSION_FILE).unlink(missing_ok=True)
        self.stop()

    # ---- state ----------------------------------------------------------

    def is_logged_in(self) -> bool:
        if self.driver is None:
            return False
        try:
            self.driver.find_element(By.XPATH, '//div[@contenteditable="true"][@data-tab="3"]')
            if not self.logged_in:
                self.connected_since = datetime.now()
            self.logged_in = True
            return True
        except NoSuchElementException:
            self.logged_in = False
            return False

    def get_qr_screenshot(self) -> Optional[bytes]:
        if self.driver is None:
            return None
        try:
            canvas = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located(
                    (By.XPATH, '//canvas[@aria-label="Scan this QR code to link a device!"]')
                )
            )
            return canvas.screenshot_as_png
        except TimeoutException:
            return None

    def get_link_with_phone_code(self, phone_digits: str) -> Optional[str]:
        """
        Drives the "Link with phone number instead" flow and returns the
        8-character code WhatsApp displays (you type this into the phone,
        it is NOT an SMS OTP sent to you).
        """
        if self.driver is None:
            return None
        try:
            link_btn = WebDriverWait(self.driver, 15).until(
                EC.element_to_be_clickable(
                    (By.XPATH, '//a[contains(text(), "link with phone number")]')
                )
            )
            link_btn.click()

            phone_input = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.XPATH, '//input[@aria-label]'))
            )
            phone_input.clear()
            phone_input.send_keys(phone_digits)
            phone_input.send_keys(Keys.ENTER)

            code_spans = WebDriverWait(self.driver, 20).until(
                EC.presence_of_all_elements_located(
                    (By.XPATH, '//div[@data-link-code]//span')
                )
            )
            code = "".join(el.text for el in code_spans if el.text)
            return code or None
        except (TimeoutException, NoSuchElementException) as e:
            logger.warning("link-with-phone flow failed: {}", e)
            return None

    # ---- messaging --------------------------------------------------------

    def send_message(self, phone: str, message: str) -> bool:
        if self.driver is None:
            raise RuntimeError("WhatsApp session start nahi hua")
        digits = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
        url = f"{WHATSAPP_URL}/send?phone={digits}&text={message}"
        self.driver.get(url)
        try:
            box = WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located(
                    (By.XPATH, '//div[@contenteditable="true"][@data-tab="10"]')
                )
            )
            box.send_keys(Keys.ENTER)
            time.sleep(1)
            self._save_cookies()
            return True
        except TimeoutException:
            logger.error("send timeout for {}", phone)
            return False

    def poll_new_messages(self) -> list[str]:
        """Forward-worthy unread-chat summaries (title + unread count)."""
        if self.driver is None or not self.is_logged_in():
            return []
        out: list[str] = []
        try:
            badges = self.driver.find_elements(
                By.XPATH, '//span[contains(@aria-label, "unread message")]'
            )
            for badge in badges:
                try:
                    row = badge.find_element(By.XPATH, './ancestor::div[@role="listitem"]')
                    title = row.find_element(By.XPATH, './/span[@title]').get_attribute("title")
                    key = f"{title}:{badge.text}"
                    if key in self.seen_unread_keys:
                        continue
                    self.seen_unread_keys.add(key)
                    out.append(f"📩 <b>{title}</b>\n{badge.text} naya message aaya hai")
                except NoSuchElementException:
                    continue
        except WebDriverException as e:
            logger.warning("poll error: {}", e)
        return out


wa_holder: dict = {"driver": None, "logged_in": False}
wa = WhatsAppSession()


def _sync_holder() -> None:
    wa_holder["driver"] = wa.driver
    wa_holder["logged_in"] = wa.logged_in


# --------------------------------------------------------------------------- #
# FSM states (for the /send conversation via buttons)
# --------------------------------------------------------------------------- #

class SendFlow(StatesGroup):
    waiting_phone = State()
    waiting_message = State()


class PhoneCodeFlow(StatesGroup):
    waiting_phone = State()


# --------------------------------------------------------------------------- #
# Auth helper
# --------------------------------------------------------------------------- #

def authorized(chat_id: int | str) -> bool:
    if not ADMIN_CHAT_ID:
        return True
    return str(chat_id) == str(ADMIN_CHAT_ID)


async def deny(message: Message) -> None:
    await message.answer("⛔ <b>Not authorized.</b>", parse_mode="HTML")


# --------------------------------------------------------------------------- #
# Core login flow (shared by /login command, buttons, and callback choices)
# --------------------------------------------------------------------------- #

async def do_qr_login(message: Message) -> None:
    status_msg = await message.answer("⏳ <i>QR code fetch ho raha hai…</i>", parse_mode="HTML")
    async with wa.lock:
        await asyncio.to_thread(wa.start)
        _sync_holder()
        if await asyncio.to_thread(wa.is_logged_in):
            _sync_holder()
            await status_msg.edit_text(card("Pehle se Connected", "✅ Session already active hai."), parse_mode="HTML")
            return

        qr_bytes = await asyncio.to_thread(wa.get_qr_screenshot)
        if qr_bytes is None:
            await status_msg.edit_text(
                card("Login Issue", "⚠️ QR code nahi mila.", footer="/status try karein."),
                parse_mode="HTML",
            )
            return

        await status_msg.delete()
        photo = BufferedInputFile(qr_bytes, filename="wa_qr.png")
        await message.answer_photo(
            photo,
            caption=(
                "📷 <b>Yeh QR code scan karein</b>\n"
                "WhatsApp → Settings → Linked devices → Link a device\n"
                "⏱ ~2 minute mein expire ho jayega"
            ),
            parse_mode="HTML",
        )

        for _ in range(24):
            await asyncio.sleep(5)
            if await asyncio.to_thread(wa.is_logged_in):
                _sync_holder()
                await asyncio.to_thread(wa._save_cookies)
                await message.answer(card("Connected", "✅ Login ho gaya, session save ho gaya!"), parse_mode="HTML")
                return
        await message.answer(card("QR Expire Ho Gaya", "⌛ Dobara /login try karein."), parse_mode="HTML")


async def do_phone_code_login(message: Message, phone_digits: str) -> None:
    status_msg = await message.answer("⏳ <i>Link code generate ho raha hai…</i>", parse_mode="HTML")
    async with wa.lock:
        await asyncio.to_thread(wa.start)
        _sync_holder()
        if await asyncio.to_thread(wa.is_logged_in):
            _sync_holder()
            await status_msg.edit_text(card("Pehle se Connected", "✅ Session already active hai."), parse_mode="HTML")
            return

        code = await asyncio.to_thread(wa.get_link_with_phone_code, phone_digits)
        if not code:
            await status_msg.edit_text(
                card(
                    "Code Nahi Mila",
                    "⚠️ Phone-number linking flow fail hua — WhatsApp Web ka UI badal gaya ho sakta hai.",
                    footer="QR code method try karein.",
                ),
                parse_mode="HTML",
            )
            return

        await status_msg.edit_text(
            card(
                "🔢 Yeh Code Apne Phone Mein Daalein",
                f"<code>{code}</code>",
                footer="WhatsApp → Linked devices → Link a device → \"Link with phone number\"",
            ),
            parse_mode="HTML",
        )

        for _ in range(24):
            await asyncio.sleep(5)
            if await asyncio.to_thread(wa.is_logged_in):
                _sync_holder()
                await asyncio.to_thread(wa._save_cookies)
                await message.answer(card("Connected", "✅ Login ho gaya, session save ho gaya!"), parse_mode="HTML")
                return
        await message.answer(card("Code Expire Ho Gaya", "⌛ Dobara /login try karein."), parse_mode="HTML")


# --------------------------------------------------------------------------- #
# Handlers — commands
# --------------------------------------------------------------------------- #

@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    banner = (
        f"🌊 <b>{BOT_NAME}</b>\n"
        f"<i>WhatsApp ⇄ Telegram bridge</i>\n"
        f"{DIVIDER}\n"
        f"Status: {status_emoji()} {'Connected' if wa.logged_in else 'Not connected'}\n"
        f"Uptime: <code>{human_uptime()}</code>\n"
        f"{DIVIDER}\n"
        f"Neeche diye buttons se shuru karein 👇"
    )
    await message.answer(banner, parse_mode="HTML", reply_markup=main_reply_keyboard)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(card("Command Reference", HELP_TEXT), parse_mode="HTML")


@dp.message(Command("login"))
async def cmd_login(message: Message) -> None:
    if not authorized(message.chat.id):
        return await deny(message)
    await message.answer(
        card("Login Method Chunein", "QR code scan karna hai ya phone-number code use karna hai?"),
        parse_mode="HTML",
        reply_markup=login_method_inline_keyboard(),
    )


@dp.message(Command("send"))
async def cmd_send(message: Message, command: CommandObject) -> None:
    if not authorized(message.chat.id):
        return await deny(message)
    if not command.args:
        await message.answer(
            card("Usage", "<code>/send &lt;phone&gt; &lt;message&gt;</code>",
                 footer="Example: /send +919876543210 Hi there!"),
            parse_mode="HTML",
        )
        return
    parts = command.args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: <code>/send &lt;phone&gt; &lt;message&gt;</code>", parse_mode="HTML")
        return
    await _execute_send(message, parts[0], parts[1])


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if wa.driver is None:
        await message.answer(card("Status", "🔴 Session start nahi hui.", footer="/login se connect karein."), parse_mode="HTML")
        return
    logged_in = await asyncio.to_thread(wa.is_logged_in)
    _sync_holder()
    if logged_in:
        since = wa.connected_since or datetime.now()
        dur = str(timedelta(seconds=int((datetime.now() - since).total_seconds())))
        body = f"🟢 Connected & logged in.\nSession length: <code>{dur}</code>"
    else:
        body = "🟡 Browser chal raha hai, login nahi hua.\n/login se link karein."
    await message.answer(card("Status", body), parse_mode="HTML")


@dp.message(Command("logout"))
async def cmd_logout(message: Message) -> None:
    if not authorized(message.chat.id):
        return await deny(message)
    async with wa.lock:
        await asyncio.to_thread(wa.clear_session)
        _sync_holder()
    await message.answer(card("Logged Out", "🚪 Session clear ho gaya.", footer="/login se dobara connect karein."), parse_mode="HTML")


# --------------------------------------------------------------------------- #
# Handlers — keyword (reply keyboard) buttons
# --------------------------------------------------------------------------- #

@dp.message(F.text == BTN_LOGIN)
async def kb_login(message: Message) -> None:
    await cmd_login(message)


@dp.message(F.text == BTN_STATUS)
async def kb_status(message: Message) -> None:
    await cmd_status(message)


@dp.message(F.text == BTN_LOGOUT)
async def kb_logout(message: Message) -> None:
    await cmd_logout(message)


@dp.message(F.text == BTN_HELP)
async def kb_help(message: Message) -> None:
    await cmd_help(message)


@dp.message(F.text == BTN_SEND)
async def kb_send_start(message: Message, state: FSMContext) -> None:
    if not authorized(message.chat.id):
        return await deny(message)
    await state.set_state(SendFlow.waiting_phone)
    await message.answer(
        card("Send Message", "📱 Phone number bhejein (country code ke saath, jaise <code>+919876543210</code>)"),
        parse_mode="HTML",
    )


@dp.message(SendFlow.waiting_phone)
async def kb_send_phone(message: Message, state: FSMContext) -> None:
    phone = message.text.strip()
    await state.update_data(phone=phone)
    await state.set_state(SendFlow.waiting_message)
    await message.answer(card("Send Message", f"✍️ <code>{phone}</code> ko kya message bhejna hai?"), parse_mode="HTML")


@dp.message(SendFlow.waiting_message)
async def kb_send_message(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    phone = data.get("phone", "")
    await state.clear()
    await _execute_send(message, phone, message.text)


async def _execute_send(message: Message, phone: str, text: str) -> None:
    progress = await message.answer(f"⏳ <i>Bhej rahe hain</i> <code>{phone}</code> …", parse_mode="HTML")
    async with wa.lock:
        if wa.driver is None or not await asyncio.to_thread(wa.is_logged_in):
            _sync_holder()
            await progress.edit_text(
                card("Connected Nahi Hai", "🔴 Login nahi hua.", footer="/login pehle karein."),
                parse_mode="HTML",
            )
            return
        ok = await asyncio.to_thread(wa.send_message, phone, text)
    if ok:
        await progress.edit_text(card("Message Bhej Diya", f"✅ Delivered to <code>{phone}</code>"), parse_mode="HTML")
    else:
        await progress.edit_text(
            card("Send Fail Hua", f"❌ <code>{phone}</code> ko deliver nahi ho paya", footer="Dobara try karein."),
            parse_mode="HTML",
        )


# --------------------------------------------------------------------------- #
# Handlers — inline callback buttons (login method choice)
# --------------------------------------------------------------------------- #

@dp.callback_query(F.data == "login_qr")
async def cb_login_qr(callback: CallbackQuery) -> None:
    await callback.answer()
    if not authorized(callback.message.chat.id):
        return await deny(callback.message)
    await do_qr_login(callback.message)


@dp.callback_query(F.data == "login_code")
async def cb_login_code(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not authorized(callback.message.chat.id):
        return await deny(callback.message)
    await state.set_state(PhoneCodeFlow.waiting_phone)
    await callback.message.answer(
        card("Phone Number Bhejein", "📱 Country code ke saath number bhejein, jaise <code>+919876543210</code>"),
        parse_mode="HTML",
    )


@dp.message(PhoneCodeFlow.waiting_phone)
async def phone_code_received(message: Message, state: FSMContext) -> None:
    await state.clear()
    digits = "".join(ch for ch in message.text if ch.isdigit())
    if not digits:
        await message.answer("⚠️ Valid phone number bhejein.")
        return
    await do_phone_code_login(message, digits)


# --------------------------------------------------------------------------- #
# Global error handler — one bad update should never kill the bot silently
# --------------------------------------------------------------------------- #

@dp.error()
async def global_error_handler(event: ErrorEvent) -> None:
    logger.exception("Unhandled error while processing update: {}", event.exception)

    chat_id = None
    update = event.update
    if update.message:
        chat_id = update.message.chat.id
    elif update.callback_query and update.callback_query.message:
        chat_id = update.callback_query.message.chat.id

    friendly = card(
        "Kuch Gadbad Ho Gaya",
        f"⚠️ <code>{type(event.exception).__name__}</code>: {str(event.exception)[:200]}",
        footer="Agar yeh baar baar ho raha hai, /status check karein ya /login dobara try karein.",
    )
    if chat_id is not None:
        try:
            await bot.send_message(chat_id, friendly, parse_mode="HTML")
        except Exception:
            pass  # don't let error reporting itself crash the handler


# --------------------------------------------------------------------------- #
# Background polling loop — auto-forward incoming WhatsApp messages
# --------------------------------------------------------------------------- #

async def poll_loop() -> None:
    while True:
        try:
            if wa.driver is not None and ADMIN_CHAT_ID:
                async with wa.lock:
                    new_msgs = await asyncio.to_thread(wa.poll_new_messages)
                for text in new_msgs:
                    await bot.send_message(ADMIN_CHAT_ID, text, parse_mode="HTML")
        except Exception as e:
            logger.exception("poll_loop error: {}", e)
        await asyncio.sleep(POLL_INTERVAL)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

async def main() -> None:
    logger.info("Starting {} (headless={}, poll_interval={}s)", BOT_NAME, HEADLESS, POLL_INTERVAL)
    poller = asyncio.create_task(poll_loop())
    try:
        await dp.start_polling(bot)
    finally:
        poller.cancel()
        wa.stop()


if __name__ == "__main__":
    asyncio.run(main())
