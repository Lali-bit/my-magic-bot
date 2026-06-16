#!/usr/bin/env python3
"""
Telegram бот для нумерологического анализа
Версия: 5.0 с DeepSeek AI, FREE/PRO режимами, YooKassa оплатой и историей диалогов
Полностью переписан под python-telegram-bot v21+
"""

import os
import re
import sqlite3
import logging
import hashlib
import hmac
import json
from datetime import datetime, timedelta, time as dt_time
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, constants
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# ====
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ====

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("numerology_bot")

# ====
# ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# ====

env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.db")

# YooKassa (обязательно для оплаты)
YUKASSA_SHOP_ID = os.getenv("YUKASSA_SHOP_ID", "").strip()
YUKASSA_SECRET_KEY = os.getenv("YUKASSA_SECRET_KEY", "").strip()

# Webhook для YooKassa (должен быть настроен в личном кабинете YooKassa)
YUKASSA_WEBHOOK_URL = os.getenv("YUKASSA_WEBHOOK_URL", "").strip()

# Настройки подписок
SUBSCRIPTION_MONTH_PRICE = int(os.getenv("SUBSCRIPTION_MONTH_PRICE", "399"))
SUBSCRIPTION_YEAR_PRICE = int(os.getenv("SUBSCRIPTION_YEAR_PRICE", "3990"))
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "5"))

# Часовой пояс
TZ = ZoneInfo("Europe/Moscow")

# Валидация обязательных переменных
if not BOT_TOKEN:
    raise RuntimeError("❌ В .env не задан BOT_TOKEN")
if not DEEPSEEK_API_KEY:
    raise RuntimeError("❌ В .env не задан DEEPSEEK_API_KEY")

logger.info("✅ Переменные окружения загружены")

# ====
# БАЗА ДАННЫХ
# ====

class Database:
    """Менеджер базы данных SQLite с поддержкой истории диалогов"""
    
    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path
        self.init_database()
        logger.info(f"✅ База данных инициализирована: {db_path}")
    
    def get_connection(self):
        """Создаёт подключение к БД"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def init_database(self):
        """Инициализация таблиц БД"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Таблица пользователей
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    name TEXT,
                    birthdate TEXT,
                    registration_date TEXT,
                    state TEXT DEFAULT 'idle',
                    language TEXT DEFAULT 'ru',
                    daily_requests INTEGER DEFAULT 0,
                    last_request_date TEXT,
                    daily_forecast_enabled INTEGER DEFAULT 1
                )
            """)
            
            # Добавляем поле daily_forecast_enabled для существующих таблиц
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN daily_forecast_enabled INTEGER DEFAULT 1")
            except sqlite3.OperationalError:
                pass  # Поле уже существует
            
            # Таблица подписок
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    subscription_type TEXT,
                    start_date TEXT,
                    expiry_date TEXT,
                    payment_status TEXT,
                    payment_id TEXT,
                    auto_renew INTEGER DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            """)
            
            # Таблица статистики использования
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS usage_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action_type TEXT,
                    timestamp TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            """)
            
            # НОВАЯ ТАБЛИЦА: История диалогов для AI
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS conversation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    role TEXT,
                    content TEXT,
                    timestamp TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            """)
            
            # Индекс для быстрого поиска по user_id
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversation_user_id 
                ON conversation_history(user_id, timestamp DESC)
            """)
            
            conn.commit()
    
    def get_user(self, user_id: int) -> Optional[Dict]:
        """Получить данные пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def create_user(self, user_id: int, username: str = None):
        """Создать нового пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO users (user_id, username, registration_date, last_request_date)
                VALUES (?, ?, ?, ?)
            """, (user_id, username, datetime.now().isoformat(), datetime.now().date().isoformat()))
            conn.commit()
    
    def update_user(self, user_id: int, **kwargs):
        """Обновить данные пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
            values = list(kwargs.values()) + [user_id]
            cursor.execute(f"UPDATE users SET {fields} WHERE user_id = ?", values)
            conn.commit()
    
    def is_pro_user(self, user_id: int) -> bool:
        """Проверить наличие активной PRO подписки"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM subscriptions 
                WHERE user_id = ? 
                AND payment_status = 'succeeded'
                AND expiry_date > ?
            """, (user_id, datetime.now().isoformat()))
            return cursor.fetchone() is not None
    
    def check_daily_limit(self, user_id: int) -> bool:
        """Проверить лимит запросов для FREE пользователей"""
        if self.is_pro_user(user_id):
            return True
        
        user = self.get_user(user_id)
        if not user:
            return False
        
        today = datetime.now().date().isoformat()
        
        # Сброс счётчика если новый день
        if user['last_request_date'] != today:
            self.update_user(user_id, daily_requests=0, last_request_date=today)
            return True
        
        return user['daily_requests'] < FREE_DAILY_LIMIT
    
    def increment_daily_requests(self, user_id: int):
        """Увеличить счётчик запросов"""
        user = self.get_user(user_id)
        if user:
            self.update_user(user_id, daily_requests=user['daily_requests'] + 1)
    
    def add_subscription(self, user_id: int, subscription_type: str, months: int, payment_id: str = "ADMIN_GRANT"):
        """Добавить PRO подписку"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            start_date = datetime.now()
            expiry_date = start_date + timedelta(days=30 * months)
            
            cursor.execute("""
                INSERT INTO subscriptions 
                (user_id, subscription_type, start_date, expiry_date, payment_status, payment_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, subscription_type, start_date.isoformat(), 
                  expiry_date.isoformat(), "succeeded", payment_id))
            conn.commit()
    
    def log_action(self, user_id: int, action_type: str):
        """Записать действие в статистику"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO usage_stats (user_id, action_type, timestamp)
                VALUES (?, ?, ?)
            """, (user_id, action_type, datetime.now().isoformat()))
            conn.commit()
    
    def get_stats(self) -> Dict:
        """Получить общую статистику"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("SELECT COUNT(*) FROM users")
            total_users = cursor.fetchone()[0]
            
            cursor.execute("""
                SELECT COUNT(DISTINCT user_id) FROM subscriptions 
                WHERE payment_status = 'succeeded' AND expiry_date > ?
            """, (datetime.now().isoformat(),))
            pro_users = cursor.fetchone()[0]
            
            cursor.execute("""
                SELECT COUNT(*) FROM usage_stats 
                WHERE timestamp > ?
            """, ((datetime.now() - timedelta(days=7)).isoformat(),))
            actions_week = cursor.fetchone()[0]
            
            return {
                "total_users": total_users,
                "pro_users": pro_users,
                "free_users": total_users - pro_users,
                "actions_week": actions_week
            }
    
    def get_all_users_with_status(self) -> List[Dict]:
        """Получить список всех пользователей с их статусом подписки"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT u.user_id, u.username, u.name, u.registration_date,
                       CASE 
                           WHEN EXISTS (
                               SELECT 1 FROM subscriptions s 
                               WHERE s.user_id = u.user_id 
                               AND s.payment_status = 'succeeded' 
                               AND s.expiry_date > ?
                           ) THEN 'PRO'
                           ELSE 'FREE'
                       END as status
                FROM users u
                ORDER BY u.registration_date DESC
            """, (datetime.now().isoformat(),))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_popular_functions(self, limit: int = 10) -> List[Tuple[str, int]]:
        """Получить статистику популярности функций бота"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT action_type, COUNT(*) as count
                FROM usage_stats
                WHERE timestamp > ?
                GROUP BY action_type
                ORDER BY count DESC
                LIMIT ?
            """, ((datetime.now() - timedelta(days=30)).isoformat(), limit))
            return [(row['action_type'], row['count']) for row in cursor.fetchall()]
    
    # ===== МЕТОДЫ ДЛЯ ИСТОРИИ ДИАЛОГОВ =====
    
    def add_message_to_history(self, user_id: int, role: str, content: str):
        """
        Добавить сообщение в историю диалога
        role: 'user' или 'assistant'
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO conversation_history (user_id, role, content, timestamp)
                VALUES (?, ?, ?, ?)
            """, (user_id, role, content, datetime.now().isoformat()))
            conn.commit()
    
    def get_conversation_history(self, user_id: int, limit: int = 10) -> List[Dict]:
        """
        Получить последние N сообщений из истории диалога
        Возвращает список в формате [{role: 'user'/'assistant', content: '...'}]
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT role, content, timestamp
                FROM conversation_history
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (user_id, limit))
            
            # Возвращаем в обратном порядке (от старых к новым)
            messages = [{"role": row['role'], "content": row['content']} 
                       for row in reversed(cursor.fetchall())]
            return messages
    
    def clear_conversation_history(self, user_id: int):
        """Очистить историю диалога пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM conversation_history WHERE user_id = ?", (user_id,))
            conn.commit()
    
    def trim_conversation_history(self, user_id: int, keep_last: int = 15):
        """
        Оставить только последние N сообщений, удалить старые
        Это предотвращает неограниченный рост таблицы
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM conversation_history
                WHERE user_id = ? AND id NOT IN (
                    SELECT id FROM conversation_history
                    WHERE user_id = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
            """, (user_id, user_id, keep_last))
            conn.commit()

# Глобальный экземпляр БД
db = Database()

# ====
# НУМЕРОЛОГИЧЕСКИЕ РАСЧЁТЫ
# ====

def digit_sum(number: int) -> int:
    """Вычисляет сумму цифр числа"""
    return sum(int(d) for d in str(number))

def reduce_to_1_9(number: int, preserve_master: bool = False) -> int:
    """
    Редуцирует число до однозначного (1-9)
    preserve_master: если True, сохраняет мастер-числа 11, 22, 33
    """
    while number > 9:
        if preserve_master and number in [11, 22, 33]:
            return number
        number = digit_sum(number)
    return number if number != 0 else 9

def parse_date(text: str) -> Optional[datetime]:
    """Парсит дату в формате ДД.ММ.ГГГГ"""
    text = text.strip()
    pattern = r'^(\d{2})\.(\d{2})\.(\d{4})$'
    match = re.match(pattern, text)
    if not match:
        return None
    try:
        return datetime.strptime(text, "%d.%m.%Y")
    except ValueError:
        return None

def consciousness_number(day: int) -> int:
    """
    Число Сознания считается ТОЛЬКО по дню рождения
    Пример: 22 -> 2+2 = 4, 31 -> 3+1 = 4
    """
    return reduce_to_1_9(digit_sum(day))

def daily_number(date: datetime) -> int:
    """
    Число дня - сумма цифр дня и месяца (год игнорируется)
    Пример: 21.10.2025 → 2+1+1+0 = 4
    """
    day_sum = digit_sum(date.day)
    month_sum = digit_sum(date.month)
    total = day_sum + month_sum
    return reduce_to_1_9(total)

def mission_number(d: datetime) -> int:
    """Число Миссии - сумма всех цифр даты рождения"""
    total = digit_sum(d.day) + digit_sum(d.month) + digit_sum(d.year)
    return reduce_to_1_9(total, preserve_master=True)

def action_number(d: datetime) -> int:
    """Число Действия - сумма всех цифр даты"""
    return reduce_to_1_9(digit_sum(int(d.strftime("%d%m%Y"))))

def matrix_counts(d: datetime) -> Tuple[List[int], List[int]]:
    """
    Возвращает сильные числа (присутствующие) и зоны роста (отсутствующие)
    """
    date_str = d.strftime("%d%m%Y")
    counts = {str(i): 0 for i in range(1, 10)}
    
    for ch in date_str:
        if ch in counts:
            counts[ch] += 1
    
    strong = [int(k) for k, v in counts.items() if v > 0]
    missing = [int(k) for k, v in counts.items() if v == 0]
    
    return strong, missing

def finance_code(d: datetime) -> Tuple[str, int]:
    """Финансовый код и его корень"""
    date_str = d.strftime("%d%m%Y")
    root = reduce_to_1_9(digit_sum(int(date_str)))
    return date_str, root

# ====
# БАЗА ТЕКСТОВ ПО НУМЕРОЛОГИИ
# ====

MATRIX_MEANINGS = {
    1: "Воля, лидерство",
    2: "Коммуникация, партнёрство",
    3: "Креатив, самовыражение",
    4: "Система, дисциплина",
    5: "Гибкость, перемены",
    6: "Семья, забота",
    7: "Интуиция, анализ",
    8: "Амбиции, управление",
    9: "Миссия, гуманизм"
}

GROWTH_TIPS = {
    1: "Учиться мягкому лидерству и договариваться.",
    2: "Отстаивать свои границы и самостоятельность.",
    3: "Прокачивать регулярное самовыражение (короткие, но ежедневные практики).",
    4: "Выстраивать порядок и доводить до конца (маленькие шаги + чек-листы).",
    5: "Фокус и завершение: меньше задач — больше качества.",
    6: "Забота о себе наравне с заботой о других.",
    7: "Доверие к миру, дневник наблюдений, 10 минут тишины.",
    8: "Мягкая сила: ответственность без давления, деньги через ценность.",
    9: "Практичность: доводить миссию до результата."
}

CONSCIOUSNESS_DESC = {
    1: {"plus": "Лидерство, решительность, энергия", "minus": "Упрямство, эгоцентричность", "nuance": "Слышать других и не давить."},
    2: {"plus": "Дипломатия, партнёрство, мягкость", "minus": "Неуверенность, зависимость", "nuance": "Развивать самостоятельность."},
    3: {"plus": "Креатив, самовыражение, харизма", "minus": "Поверхностность, хаос", "nuance": "Дисциплина для идей."},
    4: {"plus": "Справедливость, система, новаторство", "minus": "Незавершённость, перегруз", "nuance": "Структура и финиш задач."},
    5: {"plus": "Свобода, коммуникация, гибкость", "minus": "Разбросанность, бунт", "nuance": "Ответственность и завершение."},
    6: {"plus": "Забота, ответственность, качество", "minus": "Жертвенность, контроль", "nuance": "Здоровые границы."},
    7: {"plus": "Глубина, интуиция, анализ", "minus": "Замкнутость, хаос", "nuance": "Доверие и осознанность."},
    8: {"plus": "Сила, управление, амбиции", "minus": "Жёсткость, давление", "nuance": "Мудрое лидерство."},
    9: {"plus": "Гуманизм, миссия, завершение", "minus": "Идеализм, выгорание", "nuance": "Практичность и мера."}
}

MISSION_DESC = {
    1: {"plus": "Воля, цельность", "minus": "Эго и жёсткость", "goal": "Учиться вести мягко."},
    2: {"plus": "Гармония, дипломатия", "minus": "Зависимость", "goal": "Баланс и самостоятельность."},
    3: {"plus": "Идеи, радость", "minus": "Расфокус", "goal": "Дисциплина для творчества."},
    4: {"plus": "Структура, фундамент", "minus": "Застревание", "goal": "Гибкость и финиш задач."},
    5: {"plus": "Перемены, свобода", "minus": "Хаос", "goal": "Свобода с ответственностью."},
    6: {"plus": "Ответственность, забота", "minus": "Перегруз", "goal": "Границы и баланс."},
    7: {"plus": "Смысл, мудрость", "minus": "Кризисы", "goal": "Доверие и осознанность."},
    8: {"plus": "Результат, масштаб", "minus": "Контроль", "goal": "Этика + эффективность."},
    9: {"plus": "Служение, завершение", "minus": "Выгорание", "goal": "Практичность."}
}

ACTION_DESC = {
    1: {"plus": "Решительность, напор", "minus": "Грубость", "title": "Действует прямо и быстро."},
    2: {"plus": "Согласование, мирность", "minus": "Колебания", "title": "Через сотрудничество и баланс."},
    3: {"plus": "Динамика, креатив", "minus": "Хаос", "title": "Через идеи и движение."},
    4: {"plus": "Система, шаги", "minus": "Застревание", "title": "Через порядок и дисциплину."},
    5: {"plus": "Гибкость, скорость", "minus": "Раздрай", "title": "Через перемены и общение."},
    6: {"plus": "Ответственность, забота", "minus": "Перегруз", "title": "Через качество и поддержку."},
    7: {"plus": "Аналитика, интуиция", "minus": "Изоляция", "title": "Через смысл и глубину."},
    8: {"plus": "Сила, управление", "minus": "Давление", "title": "Через цель и результат."},
    9: {"plus": "Миссия, гуманизм", "minus": "Идеализм", "title": "Через завершение и пользу."}
}

FINANCE_NOTES = {
    1: "Деньги через личную инициативу и лидерство. Риск — давить.",
    2: "Деньги через партнёрства и доверие. Риск — зависимость.",
    3: "Деньги через креатив и контент. Риск — хаос.",
    4: "Деньги через систему и процессы. Риск — незавершённость.",
    5: "Деньги через маркетинг и перемены. Риск — расфокус.",
    6: "Деньги через качество и заботу. Риск — перегруз.",
    7: "«Финансовый философ»: деньги через знания и аналитику. Риск — кризисы и хаос.",
    8: "Деньги через управление и масштаб. Риск — жёсткость.",
    9: "Деньги через миссию и пользу людям. Риск — выгорание."
}

# ====
# YOOKASSA ИНТЕГРАЦИЯ
# ====

class YooKassaPayment:
    """Класс для работы с YooKassa API"""
    
    def __init__(self, shop_id: str, secret_key: str):
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.api_url = "https://api.yookassa.ru/v3/payments"
    
    def create_payment(self, amount: float, description: str, user_id: int, 
                      return_url: str = None) -> Optional[Dict]:
        """
        Создать платёж в YooKassa
        Возвращает dict с payment_id и confirmation_url
        """
        import uuid
        
        idempotence_key = str(uuid.uuid4())
        
        headers = {
            "Content-Type": "application/json",
            "Idempotence-Key": idempotence_key
        }
        
        payload = {
    "amount": {
        "value": f"{amount:.2f}",
        "currency": "RUB"
    },
    "receipt": {
        "customer": {
            "email": f"user_{user_id}@example.com",
            "phone": "79000000000"
        },
        "items": [
            {
                "description": description[:128],
                "quantity": "1.00",
                "amount": {
                    "value": f"{amount:.2f}",
                    "currency": "RUB"
                },
                "vat_code": 1,
                "payment_subject": "service",
                "payment_mode": "full_payment"
            }
        ]
    },
    "confirmation": {
        "type": "redirect",
        "return_url": return_url or f"https://t.me/ваш_бот"
    },
    "capture": True,
    "description": description,
    "metadata": {
        "user_id": str(user_id)
    }
}
        
        try:
            response = requests.post(
                self.api_url,
                json=payload,
                headers=headers,
                auth=(self.shop_id, self.secret_key),
                timeout=30
            )
            response.raise_for_status()
            
            result = response.json()
            return {
                "payment_id": result.get("id"),
                "confirmation_url": result.get("confirmation", {}).get("confirmation_url"),
                "status": result.get("status")
            }
        
        except requests.exceptions.RequestException as e:
            logger.error(f"YooKassa API Error: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in YooKassa: {e}")
            return None
    
    def check_payment(self, payment_id: str) -> Optional[Dict]:
        """Проверить статус платежа"""
        try:
            response = requests.get(
                f"{self.api_url}/{payment_id}",
                auth=(self.shop_id, self.secret_key),
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error checking payment: {e}")
            return None
    
    @staticmethod
    def verify_webhook_signature(body: bytes, signature: str, secret_key: str) -> bool:
        """
        Проверить подпись webhook от YooKassa
        body: тело запроса в байтах
        signature: значение заголовка X-Signature
        secret_key: секретный ключ из личного кабинета
        """
        expected_signature = hmac.new(
            secret_key.encode(),
            body,
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(expected_signature, signature)

# Глобальный экземпляр YooKassa (если настроен)
yukassa = None
if YUKASSA_SHOP_ID and YUKASSA_SECRET_KEY:
    yukassa = YooKassaPayment(YUKASSA_SHOP_ID, YUKASSA_SECRET_KEY)
    logger.info("✅ YooKassa инициализирована")
else:
    logger.warning("⚠️ YooKassa не настроена (отсутствуют YUKASSA_SHOP_ID или YUKASSA_SECRET_KEY)")

# ====
# DEEPSEEK AI ИНТЕГРАЦИЯ С ИСТОРИЕЙ ДИАЛОГОВ
# ====

def ask_deepseek_ai(prompt: str, user_id: int = None, max_tokens: int = 1500, 
                   use_history: bool = True) -> str:
    """
    Запрос к DeepSeek AI с учётом истории диалога
    user_id: для загрузки истории диалога
    use_history: использовать ли историю (False для разовых запросов)
    """
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    system_prompt = (
        "Ты — цифровой психолог-нумеролог, работающий с числами сознания, миссии, матрицей (цифры 1–9), "
        "стилем действия и финансовым кодом. "
        "Никакой астрологии, гороскопов, знаков зодиака, таро, чакр и т.п. "
        "Форматируй ответ для Telegram-HTML: используй <b>жирный</b>, <i>курсив</i>, списки с эмодзи. "
        "НЕ используй *, #, кодовые блоки ``` или таблицы Markdown. "
        "НЕ используй теги заголовков (h1, h2, h3), br, hr, div, span. "
        "Будь тёплым, эмпатичным и конкретным. Давай практические советы."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    
    # Добавляем историю диалога если нужно
    if use_history and user_id:
        history = db.get_conversation_history(user_id, limit=10)
        messages.extend(history)
    
    # Добавляем текущий запрос
    messages.append({"role": "user", "content": prompt})
    
    data = {
        "model": "deepseek-chat",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        response.raise_for_status()
        result = response.json()
        answer = result["choices"][0]["message"]["content"].strip()
        
        # Чистка от Markdown артефактов
        answer = answer.replace("**", "")
        answer = answer.replace("```", "")
        answer = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', answer)
        answer = re.sub(r'\*([^*]+)\*', r'<i>\1</i>', answer)
        
        # Удаляем недопустимые HTML-теги (Telegram поддерживает только b, strong, i, em, u, ins, s, strike, del, a, code, pre, blockquote)
        # Заголовки h1-h6
        answer = re.sub(r'</?h[1-6][^>]*>', '', answer)
        # <br> и <br/> заменяем на перенос строки
        answer = re.sub(r'<br\s*/?>', '\n', answer)
        # <hr>
        answer = re.sub(r'<hr\s*/?>', '\n---\n', answer)
        # <div>, <span>
        answer = re.sub(r'</?div[^>]*>', '', answer)
        answer = re.sub(r'</?span[^>]*>', '', answer)
        # Удаляем любые другие теги, кроме разрешённых (b, strong, i, em, u, ins, s, strike, del, a, code, pre, blockquote)
        answer = re.sub(r'<(?!\/?(?:b|strong|i|em|u|ins|s|strike|del|a|code|pre|blockquote)\b)[^>]*>', '', answer, flags=re.IGNORECASE)
        # Убираем лишние переносы строк
        answer = re.sub(r'\n{3,}', '\n\n', answer)
        
        # Обрезаем сообщение, если оно слишком длинное (лимит Telegram 4096 символов)
        if len(answer) > 4000:
            answer = answer[:3997] + "..."
        
        # Сохраняем в историю если нужно
        if use_history and user_id:
            db.add_message_to_history(user_id, "user", prompt)
            db.add_message_to_history(user_id, "assistant", answer)
            # Подрезаем историю, оставляя последние 15 сообщений
            db.trim_conversation_history(user_id, keep_last=15)
        
        return answer
    
    except requests.exceptions.RequestException as e:
        logger.error(f"DeepSeek API Error: {e}")
        return f"⚠️ Ошибка соединения с AI: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error in DeepSeek AI: {e}")
        return f"⚠️ Произошла ошибка при обработке запроса: {str(e)}"

# ====
# ГЕНЕРАЦИЯ ОТЧЁТОВ
# ====

def build_user_profile_context(user_id: int) -> str:
    """Создаёт контекст профиля пользователя для AI"""
    user = db.get_user(user_id)
    if not user or not user['birthdate']:
        return ""
    
    d = datetime.strptime(user['birthdate'], "%d.%m.%Y")
    cn = consciousness_number(d.day)
    ms = mission_number(d)
    act = action_number(d)
    strong, missing = matrix_counts(d)
    fcode, froot = finance_code(d)
    
    context = (
        f"Контекст профиля пользователя:\n"
        f"- Имя: {user['name']}\n"
        f"- Дата рождения: {user['birthdate']}\n"
        f"- Число Сознания: {cn}\n"
        f"- Число Миссии: {ms}\n"
        f"- Число Действия: {act}\n"
        f"- Сильные числа (присутствуют в дате): {strong}\n"
        f"- Зоны роста (отсутствуют в дате): {missing}\n"
        f"- Финансовый код: {fcode} (корень: {froot})\n\n"
        f"Учитывай нумерологический профиль пользователя в своём ответе.\n"
    )
    
    return context

def build_full_report(name: str, d: datetime) -> str:
    """Создаёт полный нумерологический отчёт"""
    day_raw = d.day
    cn = consciousness_number(d.day)
    ms = mission_number(d)
    act = action_number(d)
    strong, missing = matrix_counts(d)
    fcode, froot = finance_code(d)
    
    # Нюанс мастер-числа
    master_note = ""
    if day_raw in (11, 22):
        if day_raw == 22:
            master_note = f"\n🧩 <b>Нюанс дня {day_raw}:</b> Мастер-строитель! Усиление вибрации четвёрки ×2. Креатив и трансформация."
        else:
            master_note = f"\n🧩 <b>Нюанс дня {day_raw}:</b> Мастер-число! Усиленная интуиция и контакт с идеями."
    
    c = CONSCIOUSNESS_DESC.get(cn, {})
    m = MISSION_DESC.get(ms, {})
    a = ACTION_DESC.get(act, {})
    
    # Сильные стороны
    strong_lines = []
    for x in strong:
        strong_lines.append(f"• <b>{x}</b> — {MATRIX_MEANINGS.get(x, '—')}: присутствует опора.")
    
    # Зоны роста
    growth_lines = []
    for x in missing:
        tip = GROWTH_TIPS.get(x, "Нарабатывай постепенно, маленькими шагами.")
        growth_lines.append(f"• <b>{x}</b> — {MATRIX_MEANINGS.get(x, '—')}: зона роста. {tip}")
    
    # Финансовый комментарий
    f_note = FINANCE_NOTES.get(froot, "")
    
    # Формирование отчёта в HTML
    text = (
        f"👋 <b>{name}</b>, вот твой персональный нумерологический отчёт\n"
        f"📅 Дата рождения: <b>{d.strftime('%d.%m.%Y')}</b>{master_note}\n\n"
        
        f"🔑 <b>Число Сознания: {day_raw} → {cn}</b>\n"
        f"• ✅ Радует: {c.get('plus', '—')}\n"
        f"• ❌ Разрушает: {c.get('minus', '—')}\n"
        f"• 💡 Нюанс: {c.get('nuance', '—')}\n\n"
        
        f"🌟 <b>Миссия: {ms}</b>\n"
        f"• ✅ В плюсе: {m.get('plus', '—')}\n"
        f"• ❌ В минусе: {m.get('minus', '—')}\n"
        f"• 🎯 Цель: {m.get('goal', '—')}\n\n"
        
        f"🧭 <b>Стиль действия: {act}</b> — {a.get('title', '—')}\n"
        f"• ✅ Плюс: {a.get('plus', '—')}\n"
        f"• ❌ Минус: {a.get('minus', '—')}\n\n"
        
        f"🗂 <b>Матрица судьбы</b>\n"
        f"✨ <b>Сильные стороны:</b>\n" + ("\n".join(strong_lines) if strong_lines else "—") + "\n\n"
        f"🎯 <b>Зоны роста:</b>\n" + ("\n".join(growth_lines) if growth_lines else "—") + "\n\n"
        
        f"💰 <b>Финансовый код: {fcode}</b> (корень: {froot})\n"
        + (f"• {f_note}" if f_note else "")
    )
    
    return text

def generate_daily_forecast(user_id: int, today: datetime) -> str:
    """
    Генерирует персонализированный ежедневный прогноз
    """
    user = db.get_user(user_id)
    if not user or not user['birthdate']:
        return None
    
    # Расчет числа дня
    day_num = daily_number(today)
    
    # Расчет числа сознания пользователя
    birthdate = datetime.strptime(user['birthdate'], "%d.%m.%Y")
    user_consciousness = consciousness_number(birthdate.day)
    user_mission = mission_number(birthdate)
    
    # Формируем промпт для AI (БЕЗ использования истории)
    prompt = (
        f"Сегодня {today.strftime('%d.%m.%Y')}, число дня: {day_num}\n\n"
        f"Пользователь:\n"
        f"- Имя: {user['name']}\n"
        f"- Дата рождения: {user['birthdate']}\n"
        f"- Число сознания: {user_consciousness}\n"
        f"- Число миссии: {user_mission}\n\n"
        f"Создай персонализированный прогноз на сегодня в формате Telegram-HTML:\n\n"
        f"1. <b>🌟 Энергия дня</b> (2-3 предложения о числе {day_num} и что оно несет)\n"
        f"2. <b>💫 Для тебя сегодня</b> (как энергия дня взаимодействует с числом сознания {user_consciousness} пользователя)\n"
        f"3. <b>✨ Сильные стороны дня</b> (3-4 пункта списком с эмодзи)\n"
        f"4. <b>⚠️ На что обратить внимание</b> (2-3 пункта списком с эмодзи)\n"
        f"5. <b>🎯 Совет дня</b> (конкретная рекомендация)\n\n"
        f"Будь кратким, позитивным и практичным. Ответ должен быть не более 400 слов."
    )
    
    forecast = ask_deepseek_ai(prompt, user_id=user_id, max_tokens=1000, use_history=False)
    
    # Формируем итоговое сообщение
    header = (
        f"🌅 <b>Доброе утро, {user['name']}!</b>\n\n"
        f"📅 Сегодня: {today.strftime('%d.%m.%Y')}\n"
        f"🔢 Число дня: <b>{day_num}</b>\n\n"
    )
    
    return header + forecast

# ====
# ЕЖЕДНЕВНЫЕ РАССЫЛКИ
# ====

async def send_daily_forecasts(context: ContextTypes.DEFAULT_TYPE):
    """
    Отправка ежедневных прогнозов всем PRO пользователям в 10:00 МСК
    """
    logger.info("🌅 Начинаем отправку ежедневных прогнозов...")
    
    today = datetime.now(TZ)
    
    # Получаем всех пользователей с включенной рассылкой
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, name, birthdate 
            FROM users 
            WHERE birthdate IS NOT NULL 
            AND daily_forecast_enabled = 1
        """)
        users = cursor.fetchall()
    
    sent_count = 0
    error_count = 0
    
    for user_row in users:
        user_id = user_row['user_id']
        
        # Проверяем, что пользователь PRO
        if not db.is_pro_user(user_id):
            continue
        
        try:
            forecast = generate_daily_forecast(user_id, today)
            
            if forecast:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=forecast,
                    parse_mode=constants.ParseMode.HTML
                )
                sent_count += 1
                logger.info(f"✅ Прогноз отправлен пользователю {user_id}")
            
            # Небольшая задержка чтобы не превысить лимиты Telegram
            import asyncio
            await asyncio.sleep(0.1)
        
        except Exception as e:
            error_count += 1
            logger.error(f"❌ Ошибка отправки прогноза пользователю {user_id}: {e}")
    
    logger.info(f"🌅 Рассылка завершена. Отправлено: {sent_count}, Ошибок: {error_count}")

# ====
# КЛАВИАТУРЫ
# ====

def main_menu(is_pro: bool = False) -> InlineKeyboardMarkup:
    """Главное меню бота с разделением FREE/PRO"""
    if is_pro:
        # PRO меню - все разделы доступны
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Моя карта", callback_data="card"),
             InlineKeyboardButton("❤️ Совместимость", callback_data="compat")],
            [InlineKeyboardButton("✨ Практики роста", callback_data="practices"),
             InlineKeyboardButton("📚 Личный гайд", callback_data="guide")],
            [InlineKeyboardButton("🎬 Книги и фильмы", callback_data="media"),
             InlineKeyboardButton("📝 Мини-тест", callback_data="test")],
            [InlineKeyboardButton("🤖 Спросить AI психолога", callback_data="ask_ai"),
             InlineKeyboardButton("📅 Календарь", callback_data="calendar")],
            [InlineKeyboardButton("🗑 Очистить историю AI", callback_data="clear_history"),
             InlineKeyboardButton("👤 Профиль", callback_data="profile")]
        ])
    else:
        # FREE меню - только базовые разделы
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Моя карта", callback_data="card")],
            [InlineKeyboardButton("❤️ Совместимость 🔒", callback_data="compat"),
             InlineKeyboardButton("✨ Практики роста 🔒", callback_data="practices")],
            [InlineKeyboardButton("📚 Личный гайд 🔒", callback_data="guide"),
             InlineKeyboardButton("🎬 Книги и фильмы 🔒", callback_data="media")],
            [InlineKeyboardButton("📝 Мини-тест 🔒", callback_data="test"),
             InlineKeyboardButton("🤖 AI психолог 🔒", callback_data="ask_ai")],
            [InlineKeyboardButton("📅 Календарь 🔒", callback_data="calendar"),
             InlineKeyboardButton("👤 Профиль", callback_data="profile")],
            [InlineKeyboardButton("⭐ Оформить PRO подписку", callback_data="subscription")]
        ])

def back_menu() -> InlineKeyboardMarkup:
    """Кнопка возврата в меню"""
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="menu")]])

# ====
# ОБРАБОТЧИКИ КОМАНД (ASYNC)
# ====

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start - начало работы с ботом"""
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    # Создаём пользователя если его нет
    db.create_user(user_id, username)
    user = db.get_user(user_id)
    
    # Если пользователь уже зарегистрирован
    if user and user['name'] and user['birthdate']:
        is_pro = db.is_pro_user(user_id)
        status = "⭐ PRO" if is_pro else "🆓 FREE"
        await update.message.reply_text(
            f"👋 С возвращением, <b>{user['name']}</b>!\n\n"
            f"Твой статус: {status}\n"
            f"Рад снова видеть тебя. Выбери нужный раздел:",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=main_menu(is_pro)
        )
        return
    
    # Новый пользователь
    db.update_user(user_id, state='awaiting_name')
    
    welcome_text = (
        "👋 <b>Привет! Я твой персональный бот-нумеролог</b>\n\n"
        "🔮 Я помогу тебе:\n"
        "• Узнать свои сильные стороны и зоны роста\n"
        "• Понять свою миссию и стиль действия\n"
        "• Получить рекомендации от AI-психолога\n"
        "• Проверить совместимость с партнёром\n"
        "• Найти свой финансовый код\n\n"
        "📝 Для начала, как к тебе обращаться? Напиши своё имя:"
    )
    
    await update.message.reply_text(
        welcome_text,
        parse_mode=constants.ParseMode.HTML
    )

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /menu - вызов главного меню"""
    user_id = update.effective_user.id
    
    # Сброс всех состояний ожидания
    db.update_user(user_id, state='idle')
    context.user_data.clear()
    
    is_pro = db.is_pro_user(user_id)
    
    await update.message.reply_text(
        "🏠 <b>Главное меню</b>\n\nВыбери нужный раздел:",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=main_menu(is_pro)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help - справка"""
    help_text = (
        "ℹ️ <b>Справка по боту</b>\n\n"
        "<b>Доступные команды:</b>\n"
        "/start - Начать работу с ботом\n"
        "/menu - Открыть главное меню\n"
        "/help - Показать эту справку\n"
        "/cancel - Отменить текущее действие\n\n"
        "<b>Что умеет бот:</b>\n"
        "📊 Персональный нумерологический анализ\n"
        "❤️ Совместимость с партнёром\n"
        "✨ Практики для развития\n"
        "🤖 AI-консультации с памятью диалога\n"
        "📚 Подбор книг и фильмов\n"
        "💰 Расчёт финансового кода\n\n"
        "<b>FREE версия:</b>\n"
        f"• До {FREE_DAILY_LIMIT} запросов в день\n"
        "• Базовый анализ личности\n\n"
        "<b>PRO версия:</b>\n"
        "• Безлимит запросов\n"
        "• Расширенный AI-анализ с историей\n"
        "• Детальные отчёты\n"
        "• Ежедневные прогнозы в 10:00\n"
        f"• От {SUBSCRIPTION_MONTH_PRICE}₽/месяц\n\n"
        "По всем вопросам: /help"
    )
    
    await update.message.reply_text(
        help_text,
        parse_mode=constants.ParseMode.HTML,
        reply_markup=back_menu()
    )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /cancel - отмена текущего действия"""
    user_id = update.effective_user.id
    
    db.update_user(user_id, state='idle')
    context.user_data.clear()
    
    is_pro = db.is_pro_user(user_id)
    
    await update.message.reply_text(
        "❌ Действие отменено.",
        reply_markup=main_menu(is_pro)
    )

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /admin - админ-панель"""
    user_id = update.effective_user.id
    
    if False and user_id == ADMIN_USER_ID:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    
    stats = db.get_stats()
    popular_functions = db.get_popular_functions(5)
    
    # Формируем список популярных функций
    functions_text = ""
    if popular_functions:
        function_names = {
            'registration_complete': '✅ Регистрация',
            'compatibility_check': '❤️ Совместимость',
            'ai_question': '🤖 Вопрос AI',
            'practices': '✨ Практики',
            'guide': '📚 Личный гайд',
            'media': '🎬 Книги/фильмы',
            'test': '📝 Мини-тест',
            'calendar': '📅 Календарь'
        }
        for func, count in popular_functions:
            func_name = function_names.get(func, func)
            functions_text += f"  • {func_name}: {count} раз\n"
    else:
        functions_text = "  Нет данных\n"
    
    admin_text = (
        "👑 <b>Админ-панель</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• Всего пользователей: {stats['total_users']}\n"
        f"• PRO пользователей: {stats['pro_users']}\n"
        f"• FREE пользователей: {stats['free_users']}\n"
        f"• Действий за неделю: {stats['actions_week']}\n\n"
        f"🔥 <b>Популярные функции (30 дней):</b>\n"
        f"{functions_text}\n"
        f"<b>📋 Команды администратора:</b>\n"
        f"/admin_users - Список всех пользователей\n"
        f"/admin_stats - Детальная статистика\n"
        f"/grant_pro user_id months - Выдать PRO подписку\n"
        f"  Пример: <code>/grant_pro 123456789 1</code>\n"
        f"/grant_pro @username months - Выдать PRO по username\n"
        f"  Пример: <code>/grant_pro @john 12</code>"
    )
    
    await update.message.reply_text(
        admin_text,
        parse_mode=constants.ParseMode.HTML
    )

async def admin_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /admin_users - список пользователей"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    
    users = db.get_all_users_with_status()
    
    if not users:
        await update.message.reply_text("📭 В базе нет пользователей")
        return
    
    # Формируем список пользователей (постраничный вывод)
    max_users_per_message = 20
    
    for i in range(0, len(users), max_users_per_message):
        chunk = users[i:i+max_users_per_message]
        
        users_text = f"👥 <b>Пользователи ({i+1}-{i+len(chunk)} из {len(users)}):</b>\n\n"
        
        for user in chunk:
            status_emoji = "⭐" if user['status'] == 'PRO' else "🆓"
            username = f"@{user['username']}" if user['username'] else "—"
            name = user['name'] if user['name'] else "Не указано"
            reg_date = user['registration_date'][:10] if user['registration_date'] else "—"
            
            users_text += (
                f"{status_emoji} <b>{user['status']}</b> | ID: <code>{user['user_id']}</code>\n"
                f"  👤 {name} ({username})\n"
                f"  📅 Регистрация: {reg_date}\n\n"
            )
        
        await update.message.reply_text(
            users_text,
            parse_mode=constants.ParseMode.HTML
        )

async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /admin_stats - детальная статистика"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    
    stats = db.get_stats()
    popular_functions = db.get_popular_functions(10)
    
    # Формируем детальную статистику функций
    functions_text = ""
    if popular_functions:
        function_names = {
            'registration_complete': '✅ Регистрация',
            'compatibility_check': '❤️ Совместимость',
            'ai_question': '🤖 Вопрос AI',
            'practices': '✨ Практики',
            'guide': '📚 Личный гайд',
            'media': '🎬 Книги/фильмы',
            'test': '📝 Мини-тест',
            'calendar': '📅 Календарь'
        }
        total_actions = sum(count for _, count in popular_functions)
        for func, count in popular_functions:
            func_name = function_names.get(func, func)
            percentage = (count / total_actions * 100) if total_actions > 0 else 0
            bar = "█" * int(percentage / 5)  # Шкала из 20 символов max
            functions_text += f"{func_name}\n  {bar} {percentage:.1f}% ({count})\n\n"
    else:
        functions_text = "Нет данных\n"
    
    stats_text = (
        "📊 <b>Детальная статистика бота</b>\n\n"
        f"👥 <b>Пользователи:</b>\n"
        f"• Всего зарегистрировано: {stats['total_users']}\n"
        f"• PRO пользователей: {stats['pro_users']}\n"
        f"• FREE пользователей: {stats['free_users']}\n"
        f"• Конверсия в PRO: {(stats['pro_users']/stats['total_users']*100 if stats['total_users'] > 0 else 0):.1f}%\n\n"
        f"📈 <b>Активность:</b>\n"
        f"• Действий за неделю: {stats['actions_week']}\n"
        f"• Среднее на пользователя: {(stats['actions_week']/stats['total_users'] if stats['total_users'] > 0 else 0):.1f}\n\n"
        f"🔥 <b>Популярные функции (за 30 дней):</b>\n\n"
        f"{functions_text}"
    )
    
    await update.message.reply_text(
        stats_text,
        parse_mode=constants.ParseMode.HTML
    )

async def grant_pro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /grant_pro - выдача PRO подписки администратором"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    
    # Проверка аргументов
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ <b>Неверный формат команды!</b>\n\n"
            "Используйте:\n"
            "<code>/grant_pro user_id месяцев</code>\n"
            "Пример: <code>/grant_pro 123456789 1</code>\n\n"
            "Или:\n"
            "<code>/grant_pro @username месяцев</code>\n"
            "Пример: <code>/grant_pro @john 12</code>",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    target_identifier = context.args[0]
    try:
        months = int(context.args[1])
        if months < 1 or months > 120:
            raise ValueError()
    except ValueError:
        await update.message.reply_text(
            "❌ Количество месяцев должно быть числом от 1 до 120"
        )
        return
    
    # Определяем целевого пользователя
    target_user_id = None
    
    if target_identifier.startswith('@'):
        # Поиск по username
        username = target_identifier[1:]  # Убираем @
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users WHERE username = ?", (username,))
            result = cursor.fetchone()
            if result:
                target_user_id = result['user_id']
    else:
        # Прямой user_id
        try:
            target_user_id = int(target_identifier)
        except ValueError:
            await update.message.reply_text("❌ Неверный формат user_id")
            return
    
    if not target_user_id:
        await update.message.reply_text(
            f"❌ Пользователь {target_identifier} не найден в базе.\n"
            f"Пользователь должен сначала запустить бота командой /start"
        )
        return
    
    # Проверка существования пользователя
    target_user = db.get_user(target_user_id)
    if not target_user:
        await update.message.reply_text(
            f"❌ Пользователь с ID {target_user_id} не найден в базе"
        )
        return
    
    # Выдача подписки
    db.add_subscription(
        target_user_id,
        subscription_type="PRO" if months >= 12 else "PRO_MONTH",
        months=months,
        payment_id=f"ADMIN_GRANT_{user_id}"
    )
    
    # Уведомление администратора
    await update.message.reply_text(
        f"✅ <b>PRO подписка успешно выдана!</b>\n\n"
        f"Пользователь: {target_identifier}\n"
        f"User ID: <code>{target_user_id}</code>\n"
        f"Период: {months} мес.\n"
        f"До: {(datetime.now() + timedelta(days=30*months)).strftime('%d.%m.%Y')}",
        parse_mode=constants.ParseMode.HTML
    )
    
    # Уведомление пользователя
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=(
                f"🎉 <b>Поздравляем!</b>\n\n"
                f"Вам выдана PRO подписка на {months} мес.!\n\n"
                f"⭐ Теперь вам доступны:\n"
                f"• Безлимит запросов\n"
                f"• Расширенный AI-анализ с памятью диалога\n"
                f"• Детальные отчёты\n"
                f"• Ежедневные прогнозы\n"
                f"• Все функции бота без ограничений\n\n"
                f"Наслаждайтесь! 🚀"
            ),
            parse_mode=constants.ParseMode.HTML,
            reply_markup=main_menu(True)
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя {target_user_id}: {e}")

# ====
# ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ (ASYNC)
# ====

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений"""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    
    user = db.get_user(user_id)
    if not user:
        db.create_user(user_id, update.effective_user.username)
        user = db.get_user(user_id)
    
    state = user.get('state', 'idle')
    
    # === Ожидание имени ===
    if state == 'awaiting_name':
        if len(text) > 50:
            await update.message.reply_text("❌ Имя слишком длинное. Попробуйте ещё раз:")
            return
        
        db.update_user(user_id, name=text, state='awaiting_birthdate')
        await update.message.reply_text(
            f"Отлично, <b>{text}</b>! 👍\n\n"
            f"Теперь введи дату рождения в формате <b>ДД.ММ.ГГГГ</b>\n"
            f"Например: 22.06.1995",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    # === Ожидание даты рождения ===
    if state == 'awaiting_birthdate':
        birthdate = parse_date(text)
        if not birthdate:
            await update.message.reply_text(
                "❌ Неверный формат даты.\n\n"
                "Введи дату в формате <b>ДД.ММ.ГГГГ</b>\n"
                "Например: 22.06.1995",
                parse_mode=constants.ParseMode.HTML
            )
            return
        
        # Проверка разумности даты
        if birthdate.year < 1900 or birthdate > datetime.now():
            await update.message.reply_text(
                "❌ Некорректная дата рождения. Попробуйте ещё раз:"
            )
            return
        
        db.update_user(user_id, birthdate=text, state='idle')
        db.log_action(user_id, 'registration_complete')
        
        # Генерируем отчёт
        user = db.get_user(user_id)
        report = build_full_report(user['name'], birthdate)
        
        await update.message.reply_text(
            report,
            parse_mode=constants.ParseMode.HTML
        )
        
        is_pro = db.is_pro_user(user_id)
        
        await update.message.reply_text(
            "✅ <b>Регистрация завершена!</b>\n\n"
            "Выбери нужный раздел в меню:",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=main_menu(is_pro)
        )
        return
    
    # === Ожидание даты партнёра для совместимости ===
    if state == 'awaiting_compat_date':
        partner_date = parse_date(text)
        if not partner_date:
            await update.message.reply_text(
                "❌ Неверный формат даты.\n\n"
                "Введи дату партнёра в формате <b>ДД.ММ.ГГГГ</b>\n"
                "Например: 14.02.1990",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=back_menu()
            )
            return
        
        db.update_user(user_id, state='idle')
        
        # Проверка лимитов
        if not db.check_daily_limit(user_id):
            await show_limit_message(update.message)
            return
        
        db.increment_daily_requests(user_id)
        db.log_action(user_id, 'compatibility_check')
        
        # Генерация отчёта совместимости через AI (БЕЗ истории)
        wait_msg = await update.message.reply_text("⏳ Анализирую совместимость...")
        
        profile_context = build_user_profile_context(user_id)
        prompt = (
            f"{profile_context}\n"
            f"Сделай анализ совместимости на основе нумерологического анализа с партнёром, "
            f"родившимся {partner_date.strftime('%d.%m.%Y')}.\n\n"
            f"Формат ответа в Telegram-HTML:\n"
            f"<b>💑 Совместимость</b>\n\n"
            f"<b>✅ Сильные стороны пары:</b>\n"
            f"(перечисли 3-4 пункта с эмодзи)\n\n"
            f"<b>⚠️ Возможные вызовы:</b>\n"
            f"(перечисли 2-3 пункта)\n\n"
            f"<b>💡 Рекомендации:</b>\n"
            f"(дай 3-4 практических совета)"
        )
        
        result = ask_deepseek_ai(prompt, user_id=user_id, max_tokens=1200, use_history=False)
        
        try:
            await wait_msg.delete()
        except:
            pass
        
        await update.message.reply_text(
            result,
            parse_mode=constants.ParseMode.HTML,
            reply_markup=back_menu()
        )
        return
    
    # === Ожидание вопроса для AI (С ИСТОРИЕЙ) ===
    if state == 'awaiting_ai_question':
        db.update_user(user_id, state='idle')
        
        # Проверка лимитов
        if not db.check_daily_limit(user_id):
            await show_limit_message(update.message)
            return
        
        db.increment_daily_requests(user_id)
        db.log_action(user_id, 'ai_question')
        
        wait_msg = await update.message.reply_text("⏳ Обрабатываю ваш вопрос...")
        
        profile_context = build_user_profile_context(user_id)
        prompt = (
            f"{profile_context}\n"
            f"Вопрос пользователя: {text}\n\n"
            f"Ответь на вопрос, опираясь на нумерологический профиль пользователя. "
            f"Дай конкретные практические рекомендации. Ответ форматируй в Telegram-HTML."
        )
        
        # ВАЖНО: use_history=True - AI будет помнить предыдущие сообщения
        result = ask_deepseek_ai(prompt, user_id=user_id, use_history=True)
        
        try:
            await wait_msg.delete()
        except:
            pass
        
        await update.message.reply_text(
            result,
            parse_mode=constants.ParseMode.HTML,
            reply_markup=back_menu()
        )
        return
    
    # === Прохождение теста ===
    if 'test_state' in context.user_data:
        test_state = context.user_data['test_state']
        test_state['answers'].append(text)
        test_state['idx'] += 1
        
        if test_state['idx'] < len(test_state['questions']):
            # Следующий вопрос
            await update.message.reply_text(
                f"📝 <b>Вопрос {test_state['idx'] + 1}/{len(test_state['questions'])}</b>\n\n"
                f"{test_state['questions'][test_state['idx']]}",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=back_menu()
            )
            return
        
        # Тест завершён - генерируем вывод через AI
        db.log_action(user_id, 'test_complete')
        
        wait_msg = await update.message.reply_text("⏳ Анализирую ваши ответы...")
        
        profile_context = build_user_profile_context(user_id)
        answers_text = "\n".join([
            f"{i+1}. {q}\nОтвет: {a}"
            for i, (q, a) in enumerate(zip(test_state['questions'], test_state['answers']))
        ])
        
        prompt = (
            f"{profile_context}\n"
            f"Проведена мини-диагностика на основе нумерологии.\n\n"
            f"Вопросы и ответы:\n{answers_text}\n\n"
            f"Сделай краткий вывод в Telegram-HTML:\n"
            f"<b>✨ Сильные стороны</b>\n"
            f"<b>🎯 Зоны роста</b>\n"
            f"<b>💡 Рекомендация недели</b>"
        )
        
        result = ask_deepseek_ai(prompt, user_id=user_id, use_history=False)
        
        try:
            await wait_msg.delete()
        except:
            pass
        
        context.user_data.pop('test_state', None)
        
        await update.message.reply_text(
            f"✅ <b>Тест завершён!</b>\n\n{result}",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=back_menu()
        )
        return
    
    # === Свободный текст - передаём AI с историей ===
    if user.get('birthdate'):
        # Проверка лимитов
        if not db.check_daily_limit(user_id):
            await show_limit_message(update.message)
            return
        
        db.increment_daily_requests(user_id)
        db.log_action(user_id, 'free_text_query')
        
        wait_msg = await update.message.reply_text("⏳ Обрабатываю...")
        
        profile_context = build_user_profile_context(user_id)
        prompt = f"{profile_context}\n{text}\n\nОтветь используя нумерологический профиль, форматируй в Telegram-HTML."
        
        # С историей для естественного диалога
        result = ask_deepseek_ai(prompt, user_id=user_id, use_history=True)
        
        try:
            await wait_msg.delete()
        except:
            pass
        
        await update.message.reply_text(
            result,
            parse_mode=constants.ParseMode.HTML,
            reply_markup=back_menu()
        )
    else:
        await update.message.reply_text(
            "Пожалуйста, сначала пройдите регистрацию: /start"
        )

# ====
# ОБРАБОТЧИК CALLBACK КНОПОК (ASYNC)
# ====

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на inline кнопки"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    callback_data = query.data
    
    user = db.get_user(user_id)
    if not user:
        db.create_user(user_id, query.from_user.username)
        user = db.get_user(user_id)
    
    # Проверка PRO статуса
    is_pro = db.is_pro_user(user_id)
    
    # === Возврат в меню ===
    if callback_data == "menu":
        db.update_user(user_id, state='idle')
        context.user_data.clear()
        
        await query.message.reply_text(
            "🏠 <b>Главное меню</b>\n\nВыбери нужный раздел:",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=main_menu(is_pro)
        )
        return
    
    # === Проверка регистрации ===
    if not user.get('birthdate'):
        await query.message.reply_text(
            "⚠️ Сначала пройди регистрацию: /start",
            reply_markup=back_menu()
        )
        return
    
    # === Проверка PRO для защищенных разделов ===
    pro_required_sections = ['compat', 'practices', 'guide', 'media', 'test', 'ask_ai', 'calendar']
    
    if callback_data in pro_required_sections and not is_pro:
        feature_names = {
            'compat': 'Анализ совместимости',
            'practices': 'Практики роста',
            'guide': 'Личный гайд',
            'media': 'Книги и фильмы',
            'test': 'Мини-тест',
            'ask_ai': 'AI психолог',
            'calendar': 'Персональный календарь'
        }
        await show_pro_required_message(query, feature_names.get(callback_data, "Этот раздел"))
        return
    
    birthdate = datetime.strptime(user['birthdate'], "%d.%m.%Y")
    
    # === Моя карта ===
    if callback_data == "card":
        db.log_action(user_id, 'view_card')
        report = build_full_report(user['name'], birthdate)
        
        await query.message.reply_text(
            report,
            parse_mode=constants.ParseMode.HTML,
            reply_markup=back_menu()
        )
        return
    
    # === Совместимость ===
    if callback_data == "compat":
        db.update_user(user_id, state='awaiting_compat_date')
        
        await query.message.reply_text(
            "❤️ <b>Совместимость</b>\n\n"
            "Введи дату рождения партнёра в формате <b>ДД.ММ.ГГГГ</b>\n"
            "Например: 14.02.1990",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=back_menu()
        )
        return
    
    # === Практики роста ===
    if callback_data == "practices":
        # Проверка лимитов
        if not db.check_daily_limit(user_id):
            await show_limit_message(query.message)
            return
        
        db.increment_daily_requests(user_id)
        db.log_action(user_id, 'view_practices')
        
        wait_msg = await query.message.reply_text("⏳ Подбираю практики...")
        
        profile_context = build_user_profile_context(user_id)
        prompt = (
            f"{profile_context}\n"
            f"Составь персональные практики на основе нумерологии для прокачки зон роста (пустых чисел матрицы). "
            f"Для каждого числа дай 2-3 простых конкретных шага.\n"
            f"Формат Telegram-HTML с эмодзи."
        )
        
        result = ask_deepseek_ai(prompt, user_id=user_id, use_history=False)
        
        try:
            await wait_msg.delete()
        except:
            pass
        
        await query.message.reply_text(
            result,
            parse_mode=constants.ParseMode.HTML,
            reply_markup=back_menu()
        )
        return
    
    # === Личный гайд ===
    if callback_data == "guide":
        # Проверка лимитов
        if not db.check_daily_limit(user_id):
            await show_limit_message(query.message)
            return
        
        db.increment_daily_requests(user_id)
        db.log_action(user_id, 'view_guide')
        
        wait_msg = await query.message.reply_text("⏳ Создаю твой личный гайд...")
        
        profile_context = build_user_profile_context(user_id)
        prompt = (
            f"{profile_context}\n"
            f"Составь персональный гайд на основе нумерологии. Формат Telegram-HTML:\n"
            f"<b>✨ Сильные стороны</b> (3-4 пункта)\n"
            f"<b>🎯 Зоны роста</b> (2-3 пункта)\n"
            f"<b>💪 Практика недели</b> (конкретное упражнение)\n"
            f"<b>💡 Ключевой совет</b>\n"
            f"Коротко, дружелюбно, без воды."
        )
        
        result = ask_deepseek_ai(prompt, user_id=user_id, use_history=False)
        
        try:
            await wait_msg.delete()
        except:
            pass
        
        await query.message.reply_text(
            result,
            parse_mode=constants.ParseMode.HTML,
            reply_markup=back_menu()
        )
        return
    
    # === Книги и фильмы ===
    if callback_data == "media":
        # Проверка лимитов
        if not db.check_daily_limit(user_id):
            await show_limit_message(query.message)
            return
        
        db.increment_daily_requests(user_id)
        db.log_action(user_id, 'view_media')
        
        wait_msg = await query.message.reply_text("⏳ Подбираю рекомендации...")
        
        profile_context = build_user_profile_context(user_id)
        prompt = (
            f"{profile_context}\n"
            f"Подбери 6-8 рекомендаций книг и фильмов под нумерологический профиль. "
            f"Для каждого укажи название и кратко (1 строка) — почему подходит.\n"
            f"Формат Telegram-HTML с эмодзи 📚 и 🎬."
        )
        
        result = ask_deepseek_ai(prompt, user_id=user_id, use_history=False)
        
        try:
            await wait_msg.delete()
        except:
            pass
        
        await query.message.reply_text(
            result,
            parse_mode=constants.ParseMode.HTML,
            reply_markup=back_menu()
        )
        return
    
    # === Мини-тест ===
    if callback_data == "test":
        questions = [
            "Что тебе легче: начать или закончить? Почему?",
            "Где чаще «буксуешь»: система (4) или творчество (3)?",
            "Как обычно принимаешь решения: через анализ (7) или импульс (5)?",
            "Что для тебя деньги: цель (8) или ресурс под миссию (9)?",
            "Какая привычка сильнее всего мешает завершать дела?",
            "Какая маленькая ежедневная практика тебя укрепит прямо сейчас?",
            "Какой 1 результат хочешь получить за неделю?"
        ]
        
        context.user_data['test_state'] = {
            'questions': questions,
            'idx': 0,
            'answers': []
        }
        
        await query.message.reply_text(
            f"📝 <b>Мини-тест на основе нумерологии</b>\n\n"
            f"Ответь на 7 вопросов коротко и честно.\n\n"
            f"<b>Вопрос 1/{len(questions)}</b>\n\n"
            f"{questions[0]}",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=back_menu()
        )
        return
    
    # === Спросить AI ===
    if callback_data == "ask_ai":
        db.update_user(user_id, state='awaiting_ai_question')
        
        user_info = db.get_user(user_id)
        remaining = ""
        
        if not is_pro:
            today = datetime.now().date().isoformat()
            if user_info['last_request_date'] == today:
                remaining = f"\n\n📊 Осталось запросов сегодня: {FREE_DAILY_LIMIT - user_info['daily_requests']}/{FREE_DAILY_LIMIT}"
        
        await query.message.reply_text(
            f"🤖 <b>AI-психолог на основе нумерологии</b>\n\n"
            f"Задай любой вопрос о своей личности, отношениях, карьере, финансах.\n"
            f"Я отвечу на основе твоего нумерологического профиля и запомню наш диалог.{remaining}\n\n"
            f"💡 <i>Чтобы начать новый диалог, используй кнопку «Очистить историю AI»</i>",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=back_menu()
        )
        return
    
    # === Очистить историю AI ===
    if callback_data == "clear_history":
        db.clear_conversation_history(user_id)
        await query.message.reply_text(
            "🗑 <b>История диалога очищена</b>\n\n"
            "Теперь AI начнёт новый диалог с чистого листа.",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=back_menu()
        )
        return
    
    # === Календарь ===
    if callback_data == "calendar":
        # Генерируем календарь на неделю вперед
        if not db.check_daily_limit(user_id):
            await show_limit_message(query.message)
            return
        
        db.increment_daily_requests(user_id)
        db.log_action(user_id, 'view_calendar')
        
        wait_msg = await query.message.reply_text("⏳ Формирую календарь...")
        
        profile_context = build_user_profile_context(user_id)
        
        # Формируем информацию о числах на неделю вперед
        today = datetime.now(TZ)
        week_info = []
        for i in range(7):
            day = today + timedelta(days=i)
            day_num = daily_number(day)
            week_info.append(f"{day.strftime('%d.%m (%A)')}: число дня {day_num}")
        
        prompt = (
            f"{profile_context}\n"
            f"Создай персональный календарь на неделю вперед:\n"
            f"{chr(10).join(week_info)}\n\n"
            f"Для каждого дня дай краткую рекомендацию (1-2 строки) с учётом числа дня и профиля пользователя.\n"
            f"Формат Telegram-HTML с эмодзи."
        )
        
        result = ask_deepseek_ai(prompt, user_id=user_id, use_history=False, max_tokens=1500)
        
        try:
            await wait_msg.delete()
        except:
            pass
        
        await query.message.reply_text(
            f"📅 <b>Твой персональный календарь</b>\n\n{result}",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=back_menu()
        )
        return
    
    # === Профиль ===
    if callback_data == "profile":
        status = "⭐ PRO" if is_pro else "🆓 FREE"
        
        profile_text = (
            f"👤 <b>Профиль</b>\n\n"
            f"Имя: <b>{user['name']}</b>\n"
            f"Дата рождения: <b>{user['birthdate']}</b>\n"
            f"Статус: {status}\n"
        )
        
        if is_pro:
            # Найти дату окончания подписки
            with db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT expiry_date FROM subscriptions 
                    WHERE user_id = ? AND payment_status = 'succeeded' AND expiry_date > ?
                    ORDER BY expiry_date DESC LIMIT 1
                """, (user_id, datetime.now().isoformat()))
                result = cursor.fetchone()
                if result:
                    expiry = datetime.fromisoformat(result['expiry_date'])
                    profile_text += f"Подписка до: <b>{expiry.strftime('%d.%m.%Y')}</b>\n"
        else:
            today = datetime.now().date().isoformat()
            if user['last_request_date'] == today:
                profile_text += f"\nЗапросов сегодня: {user['daily_requests']}/{FREE_DAILY_LIMIT}\n"
        
        profile_text += (
            f"\n💡 <i>Чтобы изменить дату рождения, просто отправь новую "
            f"в формате ДД.ММ.ГГГГ</i>"
        )
        
        await query.message.reply_text(
            profile_text,
            parse_mode=constants.ParseMode.HTML,
            reply_markup=back_menu()
        )
        return
    
    # === Подписка ===
    if callback_data == "subscription":
        if is_pro:
            await query.message.reply_text(
                "⭐ <b>У вас уже есть PRO подписка!</b>\n\n"
                "Вам доступны все функции бота без ограничений.",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=back_menu()
            )
            return
        
        subscription_text = (
            "⭐ <b>PRO подписка</b>\n\n"
            "<b>Что включено:</b>\n"
            "✅ Безлимит запросов к AI-психологу\n"
            "✅ AI с памятью диалога (запоминает контекст)\n"
            "✅ Расширенный анализ личности\n"
            "✅ Детальная совместимость\n"
            "✅ Персональные практики и рекомендации\n"
            "✅ Подбор книг и фильмов\n"
            "✅ Ежедневные прогнозы в 10:00 МСК\n"
            "✅ Персональный календарь\n"
            "✅ Приоритетная поддержка\n\n"
            f"<b>Тарифы:</b>\n"
            f"💳 1 месяц — {SUBSCRIPTION_MONTH_PRICE}₽\n"
            f"💳 1 год — {SUBSCRIPTION_YEAR_PRICE}₽ <i>(экономия 17%)</i>\n\n"
        )
        
        if yukassa:
            subscription_text += "Нажмите на кнопку для оплаты:"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💳 Оплатить {SUBSCRIPTION_MONTH_PRICE}₽ (1 мес)", callback_data="pay_month")],
                [InlineKeyboardButton(f"💳 Оплатить {SUBSCRIPTION_YEAR_PRICE}₽ (1 год)", callback_data="pay_year")],
                [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
            ])
        else:
            subscription_text += (
                "⚠️ <i>Платёжная система временно недоступна.</i>\n"
                "Свяжитесь с администратором для оформления подписки."
            )
            keyboard = back_menu()
        
        await query.message.reply_text(
            subscription_text,
            parse_mode=constants.ParseMode.HTML,
            reply_markup=keyboard
        )
        return
    
    # === Оплата месячной подписки ===
    if callback_data == "pay_month":
        if not yukassa:
            await query.message.reply_text(
                "⚠️ Платёжная система временно недоступна.",
                reply_markup=back_menu()
            )
            return
        
        # Создаём платёж через YooKassa
        payment_data = yukassa.create_payment(
            amount=SUBSCRIPTION_MONTH_PRICE,
            description="PRO подписка на 1 месяц - Нумеролог бот",
            user_id=user_id,
            return_url="https://t.me/digital_psychologia_bot"
        )
        
        if not payment_data or not payment_data.get('confirmation_url'):
            await query.message.reply_text(
                "❌ Ошибка создания платежа. Попробуйте позже или свяжитесь с поддержкой.",
                reply_markup=back_menu()
            )
            return
        
        # Сохраняем payment_id для отслеживания
        context.user_data['pending_payment_id'] = payment_data['payment_id']
        context.user_data['pending_subscription_months'] = 1
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оплатить", url=payment_data['confirmation_url'])],
            [InlineKeyboardButton("✅ Я оплатил", callback_data="check_payment")],
            [InlineKeyboardButton("⬅️ Отмена", callback_data="menu")]
        ])
        
        await query.message.reply_text(
            f"💳 <b>Оплата подписки (1 месяц)</b>\n\n"
            f"Сумма: <b>{SUBSCRIPTION_MONTH_PRICE}₽</b>\n\n"
            f"Нажмите кнопку «Оплатить» для перехода на страницу оплаты.\n"
            f"После успешной оплаты нажмите «Я оплатил».\n\n"
            f"💡 Платёж обрабатывается через защищенную систему YooKassa.",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=keyboard
        )
        return
    
    # === Оплата годовой подписки ===
    if callback_data == "pay_year":
        if not yukassa:
            await query.message.reply_text(
                "⚠️ Платёжная система временно недоступна.",
                reply_markup=back_menu()
            )
            return
        
        payment_data = yukassa.create_payment(
            amount=SUBSCRIPTION_YEAR_PRICE,
            description="PRO подписка на 1 год - Нумеролог бот",
            user_id=user_id,
            return_url="https://t.me/digital_psychologia_bot"
        )
        
        if not payment_data or not payment_data.get('confirmation_url'):
            await query.message.reply_text(
                "❌ Ошибка создания платежа. Попробуйте позже или свяжитесь с поддержкой.",
                reply_markup=back_menu()
            )
            return
        
        context.user_data['pending_payment_id'] = payment_data['payment_id']
        context.user_data['pending_subscription_months'] = 12
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оплатить", url=payment_data['confirmation_url'])],
            [InlineKeyboardButton("✅ Я оплатил", callback_data="check_payment")],
            [InlineKeyboardButton("⬅️ Отмена", callback_data="menu")]
        ])
        
        await query.message.reply_text(
            f"💳 <b>Оплата подписки (1 год)</b>\n\n"
            f"Сумма: <b>{SUBSCRIPTION_YEAR_PRICE}₽</b>\n"
            f"Экономия: <b>{SUBSCRIPTION_MONTH_PRICE * 12 - SUBSCRIPTION_YEAR_PRICE}₽</b>\n\n"
            f"Нажмите кнопку «Оплатить» для перехода на страницу оплаты.\n"
            f"После успешной оплаты нажмите «Я оплатил».\n\n"
            f"💡 Платёж обрабатывается через защищенную систему YooKassa.",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=keyboard
        )
        return
    
    # === Проверка платежа ===
    if callback_data == "check_payment":
        payment_id = context.user_data.get('pending_payment_id')
        months = context.user_data.get('pending_subscription_months', 1)
        
        if not payment_id:
            await query.message.reply_text(
                "❌ Платёж не найден. Начните процесс оплаты заново.",
                reply_markup=back_menu()
            )
            return
        
        # Проверяем статус платежа
        payment_info = yukassa.check_payment(payment_id)
        
        if not payment_info:
            await query.message.reply_text(
                "⚠️ Не удалось проверить статус платежа. Попробуйте позже.",
                reply_markup=back_menu()
            )
            return
        
        payment_status = payment_info.get('status')
        
        if payment_status == 'succeeded':
            # Платёж успешен - активируем подписку
            db.add_subscription(
                user_id,
                subscription_type="PRO_YEAR" if months >= 12 else "PRO_MONTH",
                months=months,
                payment_id=payment_id
            )
            
            # Очищаем временные данные
            context.user_data.pop('pending_payment_id', None)
            context.user_data.pop('pending_subscription_months', None)
            
            await query.message.reply_text(
                f"🎉 <b>Поздравляем!</b>\n\n"
                f"Оплата прошла успешно!\n"
                f"PRO подписка активирована на {months} мес.\n\n"
                f"⭐ Теперь вам доступны:\n"
                f"• Безлимит запросов\n"
                f"• AI с памятью диалога\n"
                f"• Расширенный анализ\n"
                f"• Ежедневные прогнозы\n"
                f"• Все функции без ограничений\n\n"
                f"Наслаждайтесь! 🚀",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=main_menu(True)
            )
            
            # Логируем успешную оплату
            logger.info(f"✅ Успешная оплата: user_id={user_id}, payment_id={payment_id}, months={months}")
            
        elif payment_status == 'pending':
            await query.message.reply_text(
                "⏳ <b>Платёж обрабатывается</b>\n\n"
                "Пожалуйста, подождите. Проверьте статус через минуту.",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Проверить снова", callback_data="check_payment")],
                    [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
                ])
            )
        elif payment_status == 'waiting_for_capture':
            await query.message.reply_text(
                "⏳ <b>Платёж ожидает подтверждения</b>\n\n"
                "Это займет несколько секунд.",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Проверить снова", callback_data="check_payment")],
                    [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
                ])
            )
        else:
            # cancelled, declined, etc.
            await query.message.reply_text(
                f"❌ <b>Платёж не выполнен</b>\n\n"
                f"Статус: {payment_status}\n\n"
                f"Попробуйте оплатить снова или свяжитесь с поддержкой.",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Попробовать снова", callback_data="subscription")],
                    [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
                ])
            )
        
        return

# ====
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ====

async def show_limit_message(message):
    """Показывает сообщение об исчерпании лимита"""
    limit_text = (
        f"⚠️ <b>Лимит исчерпан</b>\n\n"
        f"Вы достигли лимита {FREE_DAILY_LIMIT} запросов в день для FREE версии.\n\n"
        f"⭐ <b>Оформите PRO подписку</b> для безлимитного доступа:\n"
        f"• {SUBSCRIPTION_MONTH_PRICE}₽/месяц\n"
        f"• {SUBSCRIPTION_YEAR_PRICE}₽/год (экономия 17%)\n\n"
        f"Команда /menu"
    )
    await message.reply_text(
        limit_text,
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Оформить PRO", callback_data="subscription")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
        ])
    )

async def show_pro_required_message(query_or_message, feature_name: str = "этой функции"):
    """Показывает сообщение о необходимости PRO подписки"""
    pro_text = (
        f"🔒 <b>{feature_name} доступен только в PRO версии</b>\n\n"
        f"⭐ <b>Перейдите на PRO и пользуйтесь всем функционалом без ограничений!</b>\n\n"
        f"<b>Что включено в PRO:</b>\n"
        f"✅ Полный нумерологический анализ\n"
        f"✅ Анализ совместимости с партнёром\n"
        f"✅ Персональные практики для роста\n"
        f"✅ Личный гайд по развитию\n"
        f"✅ Подбор книг и фильмов\n"
        f"✅ Мини-тест для самоанализа\n"
        f"✅ Безлимитный AI-психолог с памятью\n"
        f"✅ Персональный календарь\n"
        f"✅ Ежедневные прогнозы в 10:00\n\n"
        f"<b>Стоимость:</b>\n"
        f"💳 {SUBSCRIPTION_MONTH_PRICE}₽/месяц\n"
        f"💳 {SUBSCRIPTION_YEAR_PRICE}₽/год <i>(экономия 17%)</i>"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Оформить PRO", callback_data="subscription")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
    ])
    
    # Проверяем тип объекта (CallbackQuery или Message)
    if hasattr(query_or_message, 'message'):
        # Это CallbackQuery
        await query_or_message.message.reply_text(
            pro_text,
            parse_mode=constants.ParseMode.HTML,
            reply_markup=keyboard
        )
    else:
        # Это Message
        await query_or_message.reply_text(
            pro_text,
            parse_mode=constants.ParseMode.HTML,
            reply_markup=keyboard
        )

# ====
# ОБРАБОТЧИК ОШИБОК
# ====

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ошибок"""
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
    
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ Произошла ошибка при обработке запроса. Попробуйте позже или используйте /menu",
                reply_markup=back_menu()
            )
        except:
            pass

# ====
# POST_INIT ДЛЯ JOBQUEUE
# ====

async def post_init(application: Application) -> None:
    """
    Выполняется после инициализации Application
    Здесь job_queue уже готов к использованию
    """
    jq = application.job_queue
    
    # Настройка ежедневной рассылки в 10:00 МСК
    jq.run_daily(
        send_daily_forecasts,
        time=dt_time(hour=10, minute=0, second=0, tzinfo=TZ),
        name='daily_forecasts'
    )
    
    logger.info("📅 Ежедневная рассылка настроена на 10:00 МСК")

# ====
# ГЛАВНАЯ ФУНКЦИЯ
# ====

def main():
    """Запуск бота"""
    logger.info("=" * 50)
    logger.info("🚀 Запуск Telegram бота для нумерологии v5.0")
    logger.info("=" * 50)
    
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не задан в .env")
        return
    
    if not DEEPSEEK_API_KEY:
        logger.error("❌ DEEPSEEK_API_KEY не задан в .env")
        return
    
    # Создание приложения с post_init
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)  # ВАЖНО: инициализация JobQueue
        .build()
    )
    
    # Регистрация обработчиков команд
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("admin_users", admin_users_command))
    application.add_handler(CommandHandler("admin_stats", admin_stats_command))
    application.add_handler(CommandHandler("grant_pro", grant_pro_command))
    
    # Регистрация обработчика текстовых сообщений
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )
    
    # Регистрация обработчика callback кнопок
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Регистрация обработчика ошибок
    application.add_error_handler(error_handler)
    
    # Запуск бота
    logger.info("✅ Бот успешно запущен!")
    logger.info("Нажмите Ctrl+C для остановки")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()