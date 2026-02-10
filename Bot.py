import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    stream=sys.stdout  # Важно для Render: логи в stdout
)
logger = logging.getLogger(__name__)

# ============ КОНФИГУРАЦИЯ ============
# Читаем из переменных окружения (Render)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_CHANNEL_NAME = os.getenv("TWITCH_CHANNEL_NAME")

# Проверка обязательных переменных
def check_config():
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHANNEL_ID:
        missing.append("TELEGRAM_CHANNEL_ID")
    if not TWITCH_CLIENT_ID:
        missing.append("TWITCH_CLIENT_ID")
    if not TWITCH_CLIENT_SECRET:
        missing.append("TWITCH_CLIENT_SECRET")
    if not TWITCH_CHANNEL_NAME:
        missing.append("TWITCH_CHANNEL_NAME")
    
    if missing:
        logger.error(f"❌ Отсутствуют переменные окружения: {', '.join(missing)}")
        sys.exit(1)
    
    logger.info("✅ Конфигурация загружена успешно")

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
                else:
                    raise Exception(f"Ошибка получения токена: {await response.text()}")
    
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

# ============ ФОРМАТТЕР СООБЩЕНИЙ ============
class NotificationFormatter:
    @staticmethod
    def format_stream_notification(stream_data: dict, user_data: dict, channel_name: str) -> dict:
        LIVE_EMOJI = "🔴"
        GAME_EMOJI = "🎮"
        VIEWERS_EMOJI = "👥"
        TIME_EMOJI = "⏰"
        
        title = stream_data.get("title", "Без названия")
        game_name = stream_data.get("game_name", "Не указана")
        viewer_count = stream_data.get("viewer_count", 0)
        started_at = stream_data.get("started_at", "")
        thumbnail_url = stream_data.get("thumbnail_url", "").replace("{width}", "1280").replace("{height}", "720")
        
        display_name = user_data.get("display_name", "Стример")
        
        try:
            start_time = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            start_time_str = start_time.strftime("%H:%M")
        except:
            start_time_str = "Только что"
        
        message_text = f"""
<b>{LIVE_EMOJI} В ЭФИРЕ: {display_name}</b>

<i>{title}</i>

{GAME_EMOJI} <b>Игра:</b> {game_name}
{VIEWERS_EMOJI} <b>Зрителей:</b> {viewer_count:,}
{TIME_EMOJI} <b>Начало:</b> {start_time_str}

<a href="https://twitch.tv/{channel_name}">🔗 Смотреть на Twitch</a>
"""
        
        keyboard = [
            [
                InlineKeyboardButton(
                    text="🎬 Смотреть стрим", 
                    url=f"https://twitch.tv/{channel_name}"
                )
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        return {
            "text": message_text.strip(),
            "parse_mode": ParseMode.HTML,
            "reply_markup": reply_markup,
            "thumbnail_url": thumbnail_url
        }

# ============ ОСНОВНОЙ БОТ ============
class TwitchNotifierBot:
    def __init__(self):
        check_config()
        self.twitch_api = TwitchAPI(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)
        self.application: Optional[Application] = None
        self.is_streaming = False
        self.last_notification_message_id: Optional[int] = None
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        welcome_text = f"""
👋 <b>Привет! Я бот для уведомлений о стримах {TWITCH_CHANNEL_NAME}</b>

Я отправляю уведомления в канал {TELEGRAM_CHANNEL_ID} когда начинается трансляция.

<b>Команды:</b>
/start — Это сообщение
/status — Проверить статус стрима
/test — Тестовое уведомление
"""
        await update.message.reply_text(welcome_text, parse_mode=ParseMode.HTML)
    
    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🔍 Проверяю статус стрима...")
        
        stream_data = await self.twitch_api.get_stream_info(TWITCH_CHANNEL_NAME)
        
        if stream_data:
            game = stream_data.get("game_name", "Не указана")
            viewers = stream_data.get("viewer_count", 0)
            title = stream_data.get("title", "Без названия")
            
            await update.message.reply_text(
                f"✅ <b>Стрим идёт!</b>\n\n"
                f"🎮 <b>Игра:</b> {game}\n"
                f"👥 <b>Зрителей:</b> {viewers:,}\n"
                f"📝 <b>Название:</b> {title}\n\n"
                f"<a href='https://twitch.tv/{TWITCH_CHANNEL_NAME}'>Смотреть на Twitch</a>",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text(
                f"😴 <b>Сейчас стрима нет</b>\n\n"
                f"Загляни позже или подпишись на уведомления!",
                parse_mode=ParseMode.HTML
            )
    
    async def test_notification(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🧪 Отправляю тестовое уведомление в канал...")
        
        test_stream_data = {
            "title": "🔥 Тестовый стрим! Проверка оформления уведомлений",
            "game_name": "Just Chatting",
            "viewer_count": 1337,
            "started_at": datetime.utcnow().isoformat() + "Z",
            "thumbnail_url": "https://static-cdn.jtvnw.net/previews-ttv/live_user_test-{width}x{height}.jpg"
        }
        
        test_user_data = {
            "display_name": TWITCH_CHANNEL_NAME.capitalize()
        }
        
        await self.send_stream_notification(test_stream_data, test_user_data)
        await update.message.reply_text("✅ Тестовое уведомление отправлено!")
    
    async def send_stream_notification(self, stream_data: dict, user_data: dict):
        try:
            notification = NotificationFormatter.format_stream_notification(
                stream_data, user_data, TWITCH_CHANNEL_NAME
            )
            
            thumbnail_url = notification.get("thumbnail_url")
            
            if thumbnail_url and "{width}" not in thumbnail_url:
                try:
                    message = await self.application.bot.send_photo(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        photo=thumbnail_url,
                        caption=notification["text"],
                        parse_mode=notification["parse_mode"],
                        reply_markup=notification["reply_markup"]
                    )
                    self.last_notification_message_id = message.message_id
                    logger.info(f"✅ Уведомление с фото отправлено: {message.message_id}")
                    return
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось отправить фото: {e}")
            
            message = await self.application.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=notification["text"],
                parse_mode=notification["parse_mode"],
                reply_markup=notification["reply_markup"],
                disable_web_page_preview=False
            )
            self.last_notification_message_id = message.message_id
            logger.info(f"✅ Текстовое уведомление отправлено: {message.message_id}")
            
        except Exception as e:
            logger.error(f"❌ Ошибка отправки уведомления: {e}")
    
    async def check_stream_status(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            logger.info("🔍 Проверка статуса стрима...")
            stream_data = await self.twitch_api.get_stream_info(TWITCH_CHANNEL_NAME)
            
            if stream_data and not self.is_streaming:
                logger.info("🎉 Обнаружен новый стрим!")
                self.is_streaming = True
                
                user_data = await self.twitch_api.get_user_info(TWITCH_CHANNEL_NAME)
                await self.send_stream_notification(stream_data, user_data)
                
            elif not stream_data and self.is_streaming:
                logger.info("🏁 Стрим закончился")
                self.is_streaming = False
                
                await self.application.bot.send_message(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    text=f"🏁 <b>Стрим {TWITCH_CHANNEL_NAME} завершён</b>\n\nСпасибо за просмотр! 🙏",
                    parse_mode=ParseMode.HTML
                )
            else:
                status = "в эфире" if self.is_streaming else "не в эфире"
                logger.info(f"ℹ️ Статус без изменений: {status}")
                
        except Exception as e:
            logger.error(f"❌ Ошибка проверки статуса: {e}")
    
    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"⚠️ Ошибка при обработке {update}: {context.error}")
    
    def run(self):
        logger.info("🚀 Запуск бота...")
        
        self.application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("status", self.status))
        self.application.add_handler(CommandHandler("test", self.test_notification))
        self.application.add_error_handler(self.error_handler)
        
        # Планировщик: проверка каждые 60 секунд
        job_queue = self.application.job_queue
        job_queue.run_repeating(
            self.check_stream_status,
            interval=60,
            first=10
        )
        
        logger.info("✅ Бот запущен и работает!")
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    bot = TwitchNotifierBot()
    bot.run()
