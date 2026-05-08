import os
import asyncio
import sqlite3
import base64
from datetime import datetime
from flask import Flask, jsonify, render_template, request
from telethon import TelegramClient, events
from telethon.tl.functions.account import UpdateStatusRequest
import threading

app = Flask(__name__)

# ========== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ==========
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
PHONE_NUMBER = os.environ.get('PHONE_NUMBER', '')
SESSION_BASE64 = os.environ.get('SESSION_FILE', '')

# ========== ЗАГРУЗКА СЕССИИ ИЗ BASE64 ==========
def get_session_path():
    """Создаёт файл сессии из переменной окружения SESSION_FILE"""
    if SESSION_BASE64:
        try:
            session_bytes = base64.b64decode(SESSION_BASE64)
            session_path = '/tmp/telegram_session'
            with open(session_path + '.session', 'wb') as f:
                f.write(session_bytes)
            print("✅ Сессия загружена из SESSION_FILE")
            return session_path
        except Exception as e:
            print(f"⚠️ Ошибка загрузки сессии: {e}")
            return '/tmp/archiver_session'
    else:
        print("⚠️ SESSION_FILE не найден, использую archiver_session")
        return '/tmp/archiver_session'

# Удаляем старые файлы сессии если есть
try:
    for f in ['/tmp/telegram_session.session', '/tmp/telegram_session.session.lock', 
              '/tmp/archiver_session.session', '/tmp/archiver_session.session.lock']:
        if os.path.exists(f):
            os.remove(f)
except:
    pass

SESSION_PATH = get_session_path()

# ========== СОЗДАНИЕ КЛИЕНТА ==========
client = TelegramClient(
    SESSION_PATH,
    API_ID,
    API_HASH,
    connection_retries=None,
    retry_delay=1,
    auto_reconnect=True
)

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect('/tmp/messages.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        message_id INTEGER,
        sender_id TEXT,
        sender_name TEXT,
        text TEXT,
        media_type TEXT,
        date TEXT,
        is_deleted BOOLEAN DEFAULT 0,
        deleted_at TEXT,
        deleted_by_sender BOOLEAN DEFAULT 0,
        edit_history TEXT
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_deleted ON messages(is_deleted)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_chat_date ON messages(chat_id, date DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_message_id ON messages(chat_id, message_id)')
    conn.commit()
    conn.close()
    print("✅ База данных инициализирована")

init_db()

# ========== ОБРАБОТЧИКИ СОБЫТИЙ TELEGRAM ==========

# Сохраняем все новые сообщения
@client.on(events.NewMessage)
async def save_message(event):
    media_url = None
if message.media:
    try:
        # Скачиваем медиа во временную папку
        media_path = await message.download_media(file='/tmp/media/')
        if media_path:
            media_url = f'/media/{os.path.basename(media_path)}'
            media_type = 'photo' if message.photo else 'video' if message.video else 'file'
    except Exception as e:
        print(f"Ошибка скачивания медиа: {e}")

    message = event.message
    try:
        sender = await message.get_sender()
        sender_name = getattr(sender, 'first_name', getattr(sender, 'username', 'Unknown'))
    except:
        sender_name = 'Unknown'
    
    # Определяем тип медиа
    media_type = 'none'
    if message.media:
        if hasattr(message.media, 'photo'):
            media_type = 'photo'
        elif hasattr(message.media, 'document'):
            media_type = 'file'
        elif hasattr(message.media, 'video'):
            media_type = 'video'
        else:
            media_type = 'media'
    
    conn = sqlite3.connect('/tmp/messages.db')
    c = conn.cursor()
    try:
        c.execute('''INSERT OR REPLACE INTO messages 
                     (chat_id, message_id, sender_id, sender_name, text, media_type, date, is_deleted)
                     VALUES (?, ?, ?, ?, ?, ?, ?, 0)''',
                  (str(event.chat_id), message.id, str(message.sender_id), 
                   sender_name, message.text or '', media_type, 
                   datetime.now().isoformat()))
        conn.commit()
        print(f"📝 Сохранено: {sender_name}: {message.text[:50] if message.text else '[медиа]'}")
    except Exception as e:
        print(f"❌ Ошибка сохранения: {e}")
    finally:
        conn.close()

# Маркируем удалённые сообщения (НЕ УДАЛЯЕМ!)
@client.on(events.MessageDeleted)
async def mark_deleted(event):
    conn = sqlite3.connect('/tmp/messages.db')
    c = conn.cursor()
    try:
        for msg_id in event.deleted_ids:
            c.execute('''UPDATE messages 
                         SET is_deleted = 1, deleted_at = ?
                         WHERE message_id = ? AND chat_id = ?''',
                      (datetime.now().isoformat(), msg_id, str(event.chat_id)))
        conn.commit()
        print(f"🗑️ Отмечено удалённых: {len(event.deleted_ids)}")
    except Exception as e:
        print(f"❌ Ошибка маркировки удаления: {e}")
    finally:
        conn.close()

# Отслеживаем редактирование сообщений
@client.on(events.MessageEdited)
async def mark_edited(event):
    conn = sqlite3.connect('/tmp/messages.db')
    c = conn.cursor()
    try:
        # Получаем текущий текст
        c.execute('SELECT text FROM messages WHERE message_id = ? AND chat_id = ?',
                  (event.message.id, str(event.chat_id)))
        result = c.fetchone()
        old_text = result[0] if result else ''
        
        # Обновляем с историей изменений
        edit_note = f"\n[{datetime.now().isoformat()}] Было: {old_text[:100]}"
        c.execute('''UPDATE messages 
                     SET text = ?, edit_history = COALESCE(edit_history || ?, ?)
                     WHERE message_id = ? AND chat_id = ?''',
                  (event.message.text or '', edit_note, edit_note,
                   event.message.id, str(event.chat_id)))
        conn.commit()
        print(f"✏️ Отредактировано: message {event.message.id}")
    except Exception as e:
        print(f"❌ Ошибка редактирования: {e}")
    finally:
        conn.close()

# ========== ЗАПУСК КЛИЕНТА ==========
async def set_offline():
    """Скрываем онлайн статус"""
    try:
        await client(UpdateStatusRequest(offline=True))
        print("✅ Онлайн статус скрыт")
    except Exception as e:
        print(f"⚠️ Не удалось скрыть онлайн: {e}")

async def start_client():
    try:
        await client.start()
        await set_offline()
        print("✅ Клиент запущен, онлайн статус: СКРЫТ")
        me = await client.get_me()
        print(f"📱 Аккаунт: {me.first_name} (@{me.username})")
        await client.run_until_disconnected()
    except Exception as e:
        print(f"❌ Ошибка клиента: {e}")
        await asyncio.sleep(30)

def run_telegram():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_client())

# ========== ВЕБ-ИНТЕРФЕЙС (FLASK) ==========

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/messages')
def get_messages():
    chat_id = request.args.get('chat', '')
    include_deleted = request.args.get('deleted', 'true') == 'true'
    limit = int(request.args.get('limit', 500))
    
    conn = sqlite3.connect('/tmp/messages.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    try:
        if include_deleted:
            c.execute("""
                SELECT *, CASE WHEN is_deleted = 1 THEN 1 ELSE 0 END as is_deleted_flag 
                FROM messages 
                WHERE chat_id LIKE ? 
                ORDER BY date DESC 
                LIMIT ?
            """, (f'%{chat_id}%', limit))
        else:
            c.execute("""
                SELECT *, 0 as is_deleted_flag 
                FROM messages 
                WHERE chat_id LIKE ? AND is_deleted = 0 
                ORDER BY date DESC 
                LIMIT ?
            """, (f'%{chat_id}%', limit))
        messages = [dict(row) for row in c.fetchall()]
    except Exception as e:
        print(f"❌ Ошибка запроса: {e}")
        messages = []
    finally:
        conn.close()
    
    return jsonify(messages)

@app.route('/api/chats')
def get_chats():
    conn = sqlite3.connect('/tmp/messages.db')
    c = conn.cursor()
    try:
        c.execute("""
            SELECT chat_id, COUNT(*) as total, 
                   SUM(CASE WHEN is_deleted = 1 THEN 1 ELSE 0 END) as deleted_count 
            FROM messages 
            GROUP BY chat_id 
            ORDER BY total DESC
        """)
        chats = [{'chat_id': row[0], 'total': row[1], 'deleted': row[2]} for row in c.fetchall()]
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        chats = []
    finally:
        conn.close()
    return jsonify(chats)

@app.route('/api/deleted-only')
def get_deleted_only():
    limit = int(request.args.get('limit', 200))
    conn = sqlite3.connect('/tmp/messages.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        c.execute("""
            SELECT * FROM messages 
            WHERE is_deleted = 1 
            ORDER BY deleted_at DESC 
            LIMIT ?
        """, (limit,))
        messages = [dict(row) for row in c.fetchall()]
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        messages = []
    finally:
        conn.close()
    return jsonify(messages)

@app.route('/api/stats')
def get_stats():
    conn = sqlite3.connect('/tmp/messages.db')
    c = conn.cursor()
    try:
        c.execute('SELECT COUNT(*) FROM messages')
        total = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM messages WHERE is_deleted = 1')
        deleted = c.fetchone()[0]
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        total = 0
        deleted = 0
    finally:
        conn.close()
    
    percent = round(deleted / total * 100, 2) if total > 0 else 0
    return jsonify({'total': total, 'deleted': deleted, 'percent_deleted': percent})

@app.route('/health')
def health():
    """Endpoint для health check на Render"""
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})

# ========== ЗАПУСК ==========
if __name__ == '__main__':
    # Запускаем Telegram клиент в фоновом потоке
    telegram_thread = threading.Thread(target=run_telegram, daemon=True)
    telegram_thread.start()
    
    # Запускаем Flask сервер
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Запуск веб-сервера на порту {port}")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
