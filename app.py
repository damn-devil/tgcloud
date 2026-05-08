import os
import asyncio
import sqlite3
from datetime import datetime
from flask import Flask, jsonify, render_template, request
from telethon import TelegramClient, events
import threading
import hashlib

app = Flask(__name__)

API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
PHONE_NUMBER = os.environ.get('PHONE_NUMBER', '')

client = TelegramClient('archiver_session', API_ID, API_HASH)
client.set_online(False)  # Не показываем онлайн

def init_db():
    conn = sqlite3.connect('messages.db')
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
    conn.commit()
    conn.close()

init_db()

# Сохраняем все сообщения
@client.on(events.NewMessage)
async def save_message(event):
    message = event.message
    sender = await message.get_sender()
    sender_name = getattr(sender, 'first_name', getattr(sender, 'username', 'Unknown'))
    
    media_type = 'none'
    if message.media:
        if hasattr(message.media, 'photo'):
            media_type = 'photo'
        elif hasattr(message.media, 'document'):
            media_type = 'file'
    
    conn = sqlite3.connect('messages.db')
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO messages 
                 (chat_id, message_id, sender_id, sender_name, text, media_type, date, is_deleted)
                 VALUES (?, ?, ?, ?, ?, ?, ?, 0)''',
              (str(event.chat_id), message.id, str(message.sender_id), 
               sender_name, message.text or '', media_type, 
               datetime.now().isoformat()))
    conn.commit()
    conn.close()
    print(f"📝 Сохранено: {sender_name}: {message.text[:50] if message.text else '[медиа]'}")

# Обработка удаления — МАРКИРУЕМ, НЕ УДАЛЯЕМ
@client.on(events.MessageDeleted)
async def mark_deleted(event):
    conn = sqlite3.connect('messages.db')
    c = conn.cursor()
    
    # Получаем информацию о сообщениях до маркировки
    placeholders = ','.join('?' for _ in event.deleted_ids)
    c.execute(f'''SELECT message_id, chat_id, sender_name, text FROM messages 
                  WHERE message_id IN ({placeholders}) AND chat_id = ?''',
              (*event.deleted_ids, str(event.chat_id)))
    deleted_msgs = c.fetchall()
    
    # Маркируем как удалённые
    for msg_id in event.deleted_ids:
        c.execute('''UPDATE messages 
                     SET is_deleted = 1, 
                         deleted_at = ?,
                         deleted_by_sender = 1
                     WHERE message_id = ? AND chat_id = ?''',
                  (datetime.now().isoformat(), msg_id, str(event.chat_id)))
    
    conn.commit()
    conn.close()
    
    # Выводим в консоль (будет видно в логах Render)
    for msg in deleted_msgs:
        print(f"🗑️ УДАЛЕНО: {msg[2]}: {msg[3][:50] if msg[3] else '[медиа]'}")

# Отслеживаем редактирование сообщений (тоже полезно)
@client.on(events.MessageEdited)
async def mark_edited(event):
    conn = sqlite3.connect('messages.db')
    c = conn.cursor()
    c.execute('''SELECT edit_history FROM messages 
                 WHERE message_id = ? AND chat_id = ?''',
              (event.message.id, str(event.chat_id)))
    result = c.fetchone()
    
    old_history = result[0] if result else ''
    new_history = f"{old_history}\n[{datetime.now().isoformat()}] Было: {event.old_text[:100] if event.old_text else '[media]'}" if hasattr(event, 'old_text') else ''
    
    c.execute('''UPDATE messages 
                 SET text = ?, edit_history = ?
                 WHERE message_id = ? AND chat_id = ?''',
              (event.message.text or '', new_history, 
               event.message.id, str(event.chat_id)))
    conn.commit()
    conn.close()
    print(f"✏️ Отредактировано: message {event.message.id}")

async def start_client():
    await client.start(PHONE_NUMBER)
    print("✅ Клиент запущен, онлайн статус: СКРЫТ")
    me = await client.get_me()
    print(f"📱 Аккаунт: {me.first_name} (@{me.username})")
    await client.run_until_disconnected()

def run_telegram():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_client())

# ============== ВЕБ-ИНТЕРФЕЙС ==============

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/messages')
def get_messages():
    chat_id = request.args.get('chat', '')
    include_deleted = request.args.get('deleted', 'true') == 'true'
    limit = int(request.args.get('limit', 500))
    
    conn = sqlite3.connect('messages.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    if include_deleted:
        query = """SELECT *, 
                   CASE WHEN is_deleted = 1 THEN 1 ELSE 0 END as is_deleted_flag
                   FROM messages 
                   WHERE chat_id LIKE ? 
                   ORDER BY date DESC LIMIT ?"""
        c.execute(query, (f'%{chat_id}%', limit))
    else:
        query = """SELECT *, 0 as is_deleted_flag
                   FROM messages 
                   WHERE chat_id LIKE ? AND is_deleted = 0 
                   ORDER BY date DESC LIMIT ?"""
        c.execute(query, (f'%{chat_id}%', limit))
    
    messages = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(messages)

@app.route('/api/chats')
def get_chats():
    conn = sqlite3.connect('messages.db')
    c = conn.cursor()
    c.execute('''SELECT chat_id, 
                        COUNT(*) as total,
                        SUM(CASE WHEN is_deleted = 1 THEN 1 ELSE 0 END) as deleted_count
                 FROM messages 
                 GROUP BY chat_id 
                 ORDER BY total DESC''')
    chats = [{'chat_id': row[0], 'total': row[1], 'deleted': row[2]} for row in c.fetchall()]
    conn.close()
    return jsonify(chats)

@app.route('/api/deleted-only')
def get_deleted_only():
    limit = int(request.args.get('limit', 200))
    conn = sqlite3.connect('messages.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''SELECT * FROM messages 
                 WHERE is_deleted = 1 
                 ORDER BY deleted_at DESC LIMIT ?''', (limit,))
    messages = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(messages)

@app.route('/api/stats')
def get_stats():
    conn = sqlite3.connect('messages.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM messages')
    total = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM messages WHERE is_deleted = 1')
    deleted = c.fetchone()[0]
    conn.close()
    return jsonify({'total': total, 'deleted': deleted, 'percent_deleted': round(deleted/total*100, 2) if total else 0})

if __name__ == '__main__':
    thread = threading.Thread(target=run_telegram, daemon=True)
    thread.start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)