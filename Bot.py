import asyncio
import logging
import os
import sys
import threading
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ============ КОНФИГУРАЦИЯ ============
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_CHANNEL_NAME = os.getenv("TWITCH_CHANNEL_NAME")
PORT = int(os.getenv("PORT", 10000))  # Render даёт порт через env

def check_config():
    missing = []
    for var in ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_CHANNEL_NAME"]:
        if not os.getenv(var):
            missing.append(var)
    
    if missing:
        logger.error(f"❌ Отсутствуют переменные: {', '.join(missing)}")
        sys.exit(1)
    logger.info("✅ Конфигурация загружена")

# ============ FLASK WEB SERVER (для Render) ============
app = Flask(__name__)

@app.route('/')
def health():
    return {
        "status": "running",
        "bot": "twitch-notifier",
        "timestamp": datetime.now().isoformat()
    }, 200

@app.route('/health')
def health_check():
    return "OK", 200

def run_web_server():
    app.run(host='0.0.0.0', port=PORT)

# ============ TWITCH API ============
class TwitchAPI:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token: Optional[str] = None
        self.token_expires: Optional[datetime] = None
    
    async def _get_access_token(self) -> str:
        if self.access_token and self.token_expires and datetime.now() < self.token_expires:
            return self.access_token
        
        url = "https://id.twitch.tv/oauth2/token"
        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    self.access_token = data["access_token"]
                    self.token_expires = datetime.now() + timedelta(hours=1)
                    logger.info("🔑 Twitch токен обновлён")
                    return self.access_token
                raise Exception(f"Ошибка токена: {await response.text()}")
    
    async def get_stream_info(self, user_login: str) -> Optional[dict]:
        token = await self._get_access_token()
        url = "https://api.twitch.tv/helix/streams"
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {token}"
        }
        params = {"user_login": user_login}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    if data["data"]:
                        return data["data"][0]
                return None
    
    async def get_user_info(self, user_login: str) -> Optional[dict]:
        token = await self._get_access_token()
        url = "https://api.twitch.tv/helix/users"
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {token}"
        }
        params = {"login": user_login}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    if data["data"]:
                        return data["data"][0]
                return None

# ============ ФОРМАТТЕР ============
class NotificationFormatter:
    @staticmethod
    def format_notification(stream_data: dict, user_data: dict, channel_name: str) -> dict:
        LIVE = "🔴"
        GAME = "🎮"
        VIEWERS = "👥"
        TIME = "⏰"
        
        title = stream_data.get("title", "Без названия")
        game = stream_data.get("game_name", "Не указана")
        viewers = stream_data.get("viewer_count", 0)
        started = stream_data.get("started_at", "")
        thumbnail = stream_data.get("thumbnail_url", "").replace("{width}", "1280").replace("{height}", "720")
        
        display_name = user_data.get("display_name", channel_name)
        
        try:
            start_time = datetime.fromisoformat(started.replace("Z", "+00:00"))
            start_str = start_time.strftime("%H:%M")
        except:
            start_str = "Только что"
        
        text = f"""
<b>{LIVE} В ЭФИРЕ: {display_name}</b>

<i>{title}</i>

{GAME} <b>Игра:</b> {game}
{VIEWERS} <b>Зрителей:</b> {viewers:,}
{TIME} <b>Начало:</b> {start_str}

<a href="https://twitch.tv/{channel_name}">🔗 Смотреть на Twitch</a>
"""
        
        keyboard = [[InlineKeyboardButton("🎬 Смотреть стрим", url=f"https://twitch.tv/{channel_name}")]]
        
        return {
            "text": text.strip(),
            "parse_mode": ParseMode.HTML,
            "reply_markup": InlineKeyboardMarkup(keyboard),
            "thumbnail": thumbnail
        }

# ============ БОТ ============
class TwitchNotifierBot:
    def __init__(self):
        check_config()
        self.twitch = TwitchAPI(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)
        self.app: Optional[Application] = None
        self.is_streaming = False
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"👋 <b>Бот для уведомлений о стримах {TWITCH_CHANNEL_NAME}</b>\n\n"
            f"Команды:\n/start — помощь\n/status — статус стрима\n/test — тест",
            parse_mode=ParseMode.HTML
        )
    
    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🔍 Проверяю...")
        stream = await self.twitch.get_stream_info(TWITCH_CHANNEL_NAME)
        
        if stream:
            await update.message.reply_text(
                f"✅ <b>Стрим идёт!</b>\n"
                f"🎮 {stream.get('game_name')}\n"
                f"👥 {stream.get('viewer_count'):,} зрителей",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text("😴 Стрима нет")
    
    async def test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🧪 Тест...")
        test_data = {
            "title": "🔥 Тестовый стрим!",
            "game_name": "Just Chatting",
            "viewer_count": 1337,
            "started_at": datetime.utcnow().isoformat() + "Z",
            "thumbnail_url": ""
        }
        await self.send_notification(test_data, {"display_name": TWITCH_CHANNEL_NAME})
        await update.message.reply_text("✅ Отправлено!")
    
    async def send_notification(self, stream_data: dict, user_data: dict):
        try:
            notif = NotificationFormatter.format_notification(stream_data, user_data, TWITCH_CHANNEL_NAME)
            
            # Пробуем отправить с фото
            if notif["thumbnail"] and "{width}" not in notif["thumbnail"]:
                try:
                    await self.app.bot.send_photo(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        photo=notif["thumbnail"],
                        caption=notif["text"],
                        parse_mode=notif["parse_mode"],
                        reply_markup=notif["reply_markup"]
                    )
                    logger.info("✅ Уведомление с фото")
                    return
                except Exception as e:
                    logger.warning(f"⚠️ Фото не отправлено: {e}")
            
            # Текстовое сообщение
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=notif["text"],
                parse_mode=notif["parse_mode"],
                reply_markup=notif["reply_markup"]
            )
            logger.info("✅ Текстовое уведомление")
            
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
    
    async def check_stream(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            logger.info("🔍 Проверка стрима...")
            stream = await self.twitch.get_stream_info(TWITCH_CHANNEL_NAME)
            
            if stream and not self.is_streaming:
                logger.info("🎉 Новый стрим!")
                self.is_streaming = True
                user = await self.twitch.get_user_info(TWITCH_CHANNEL_NAME)
                await self.send_notification(stream, user or {"display_name": TWITCH_CHANNEL_NAME})
                
            elif not stream and self.is_streaming:
                logger.info("🏁 Стрим закончился")
                self.is_streaming = False
                await self.app.bot.send_message(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    text=f"🏁 <b>Стрим {TWITCH_CHANNEL_NAME} завершён</b>",
                    parse_mode=ParseMode.HTML
                )
        except Exception as e:
            logger.error(f"❌ Ошибка проверки: {e}")
    
    def run(self):
        logger.info("🚀 Запуск...")
        
        # Запускаем веб-сервер в отдельном потоке
        web_thread = threading.Thread(target=run_web_server, daemon=True)
        web_thread.start()
        logger.info(f"🌐 Web сервер на порту {PORT}")
        
        # Запускаем бота
        self.app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("status", self.status))
        self.app.add_handler(CommandHandler("test", self.test))
        
        self.app.job_queue.run_repeating(self.check_stream, interval=60, first=10)
        
        logger.info("✅ Бот работает!")
        self.app.run_polling()

if __name__ == "__main__":
    bot = TwitchNotifierBot()
    bot.run()
