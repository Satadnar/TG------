import asyncio
import logging
import os
import sys
import threading
import time
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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")
TWITCH_CHANNEL_NAME = os.getenv("TWITCH_CHANNEL_NAME", "")
PORT = int(os.getenv("PORT", "10000"))

def check_config():
    """Проверка конфигурации"""
    logger.info("🔍 Проверка конфигурации...")
    logger.info(f"PORT: {PORT}")
    logger.info(f"CHANNEL: {TWITCH_CHANNEL_NAME}")
    logger.info(f"TELEGRAM_CHANNEL: {TELEGRAM_CHANNEL_ID}")
    
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
        logger.error(f"❌ Отсутствуют переменные: {', '.join(missing)}")
        return False
    
    logger.info("✅ Конфигурация OK")
    return True

# ============ FLASK WEB SERVER ============
app = Flask(__name__)

@app.route('/')
def health():
    return {"status": "running", "service": "twitch-notifier"}, 200

@app.route('/health')
def health_check():
    return "OK", 200

def run_web_server():
    try:
        logger.info(f"🌐 Запуск Flask на порту {PORT}")
        app.run(host='0.0.0.0', port=PORT, threaded=True)
    except Exception as e:
        logger.error(f"❌ Ошибка Flask: {e}")

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
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        self.access_token = data["access_token"]
                        expires_in = data.get("expires_in", 3600)
                        self.token_expires = datetime.now() + timedelta(seconds=expires_in - 300)
                        logger.info("🔑 Twitch токен получен")
                        return self.access_token
                    else:
                        text = await response.text()
                        logger.error(f"❌ Ошибка Twitch API: {response.status} - {text}")
                        raise Exception(f"Twitch API error: {response.status}")
        except Exception as e:
            logger.error(f"❌ Ошибка получения токена: {e}")
            raise
    
    async def get_stream_info(self, user_login: str) -> Optional[dict]:
        try:
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
                        if data.get("data"):
                            return data["data"][0]
                    else:
                        logger.warning(f"⚠️ Twitch API вернул {response.status}")
                    return None
        except Exception as e:
            logger.error(f"❌ Ошибка получения стрима: {e}")
            return None
    
    async def get_user_info(self, user_login: str) -> Optional[dict]:
        try:
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
                        if data.get("data"):
                            return data["data"][0]
                    return None
        except Exception as e:
            logger.error(f"❌ Ошибка получения пользователя: {e}")
            return None

# ============ БОТ ============
class TwitchNotifierBot:
    def __init__(self):
        self.twitch = None
        self.application = None
        self.is_streaming = False
    
    def initialize(self):
        """Инициализация после проверки конфига"""
        if not check_config():
            logger.error("❌ Неверная конфигурация, выход...")
            return False
        
        self.twitch = TwitchAPI(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)
        return True
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"👋 <b>Бот для уведомлений о стримах {TWITCH_CHANNEL_NAME}</b>\n\n"
            f"Команды:\n/start — помощь\n/status — статус стрима\n/test — тест",
            parse_mode=ParseMode.HTML
        )
    
    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🔍 Проверяю статус...")
        
        if not self.twitch:
            await update.message.reply_text("❌ Ошибка: Twitch API не инициализирован")
            return
        
        try:
            stream = await self.twitch.get_stream_info(TWITCH_CHANNEL_NAME)
            
            if stream:
                game = stream.get("game_name", "Не указана")
                viewers = stream.get("viewer_count", 0)
                title = stream.get("title", "Без названия")
                
                await update.message.reply_text(
                    f"✅ <b>Стрим идёт!</b>\n\n"
                    f"📝 <i>{title}</i>\n"
                    f"🎮 {game}\n"
                    f"👥 {viewers:,} зрителей",
                    parse_mode=ParseMode.HTML
                )
            else:
                await update.message.reply_text(
                    f"😴 <b>Сейчас стрима нет</b>\n\n"
                    f"Загляни позже!"
                )
        except Exception as e:
            logger.error(f"Ошибка status: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🧪 Отправляю тестовое уведомление...")
        
        test_data = {
            "title": "🔥 Тестовый стрим! Проверка оформления",
            "game_name": "Just Chatting",
            "viewer_count": 1337,
            "started_at": datetime.utcnow().isoformat() + "Z",
            "thumbnail_url": ""
        }
        
        await self.send_notification(test_data, {"display_name": TWITCH_CHANNEL_NAME})
        await update.message.reply_text("✅ Тестовое уведомление отправлено!")
    
    async def send_notification(self, stream_data: dict, user_data: dict):
        """Отправка уведомления в канал"""
        try:
            LIVE = "🔴"
            GAME = "🎮"
            VIEWERS = "👥"
            TIME = "⏰"
            
            title = stream_data.get("title", "Без названия")
            game_name = stream_data.get("game_name", "Не указана")
            viewer_count = stream_data.get("viewer_count", 0)
            started_at = stream_data.get("started_at", "")
            thumbnail_url = stream_data.get("thumbnail_url", "").replace("{width}", "1280").replace("{height}", "720")
            
            display_name = user_data.get("display_name", TWITCH_CHANNEL_NAME)
            
            # Форматирование времени
            try:
                start_time = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                start_time_str = start_time.strftime("%H:%M")
            except:
                start_time_str = "Только что"
            
            message_text = f"""
<b>{LIVE} В ЭФИРЕ: {display_name}</b>

<i>{title}</i>

{GAME} <b>Игра:</b> {game_name}
{VIEWERS} <b>Зрителей:</b> {viewer_count:,}
{TIME} <b>Начало:</b> {start_time_str}

<a href="https://twitch.tv/{TWITCH_CHANNEL_NAME}">🔗 Смотреть на Twitch</a>
"""
            
            keyboard = [[InlineKeyboardButton("🎬 Смотреть стрим", url=f"https://twitch.tv/{TWITCH_CHANNEL_NAME}")]]
            
            # Пробуем с фото
            if thumbnail_url and "http" in thumbnail_url:
                try:
                    await self.application.bot.send_photo(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        photo=thumbnail_url,
                        caption=message_text.strip(),
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                    logger.info("✅ Уведомление с фото отправлено")
                    return
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось отправить фото: {e}")
            
            # Текстовое сообщение
            await self.application.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=message_text.strip(),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard),
                disable_web_page_preview=False
            )
            logger.info("✅ Текстовое уведомление отправлено")
            
        except Exception as e:
            logger.error(f"❌ Ошибка отправки уведомления: {e}")
    
    async def check_stream(self, context: ContextTypes.DEFAULT_TYPE):
        """Проверка статуса стрима"""
        try:
            logger.info("🔍 Проверка статуса стрима...")
            
            if not self.twitch:
                logger.error("❌ Twitch API не инициализирован")
                return
            
            stream = await self.twitch.get_stream_info(TWITCH_CHANNEL_NAME)
            
            if stream and not self.is_streaming:
                logger.info("🎉 Обнаружен новый стрим!")
                self.is_streaming = True
                
                user_data = await self.twitch.get_user_info(TWITCH_CHANNEL_NAME)
                if not user_data:
                    user_data = {"display_name": TWITCH_CHANNEL_NAME}
                
                await self.send_notification(stream, user_data)
                
            elif not stream and self.is_streaming:
                logger.info("🏁 Стрим закончился")
                self.is_streaming = False
                
                try:
                    await self.application.bot.send_message(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        text=f"🏁 <b>Стрим {TWITCH_CHANNEL_NAME} завершён</b>\n\nСпасибо за просмотр! 🙏",
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки сообщения о завершении: {e}")
            else:
                status = "в эфире" if self.is_streaming else "не в эфире"
                logger.info(f"ℹ️ Статус без изменений: {status}")
                
        except Exception as e:
            logger.error(f"❌ Ошибка в check_stream: {e}")
    
    def run(self):
        """Запуск бота"""
        logger.info("🚀 Инициализация бота...")
        
        if not self.initialize():
            logger.error("❌ Инициализация не удалась")
            time.sleep(5)
            return
        
        # Запускаем Flask в отдельном потоке
        logger.info("🌐 Запуск веб-сервера...")
        web_thread = threading.Thread(target=run_web_server, daemon=True)
        web_thread.start()
        
        # Даём время на старт Flask
        time.sleep(2)
        
        # Запускаем бота
        logger.info("🤖 Запуск Telegram бота...")
        self.application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("status", self.status))
        self.application.add_handler(CommandHandler("test", self.test))
        
        # Планировщик проверки стрима
        self.application.job_queue.run_repeating(
            self.check_stream,
            interval=60,
            first=10
        )
        
        logger.info("✅ Бот полностью запущен!")
        
        try:
            self.application.run_polling(allowed_updates=Update.ALL_TYPES)
        except Exception as e:
            logger.error(f"❌ Критическая ошибка бота: {e}")
            raise

if __name__ == "__main__":
    try:
        bot = TwitchNotifierBot()
        bot.run()
    except Exception as e:
        logger.error(f"❌ Фатальная ошибка: {e}")
        sys.exit(1)
