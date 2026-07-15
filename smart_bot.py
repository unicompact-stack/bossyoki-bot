"""
smart_bot.py — Умный VK-бот Задачник+
- AI с контекстом текущих задач
- Синхронизация с GitHub (tasks.json)
- Напоминания по расписанию
- Жёсткий наставник
"""
import os
import sys
import json
import re
import signal
import logging
import threading
import time
import sqlite3
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.utils import get_random_id

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

TOKEN = os.getenv('VK_TOKEN_MESSAGES')
GROUP_ID = int(os.getenv('VK_GROUP_ID', '73303964'))
GITHUB_KEY = os.getenv('GITHUB_TOKEN', os.getenv('GROQ_API_KEY'))
VK_USER_ID = int(os.getenv('VK_USER_ID', '114439622'))
DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(DIR, 'smart_bot.log')
PID_FILE = os.path.join(DIR, 'smart_bot.pid')
DB_FILE = os.path.join(DIR, 'tasks.db')
GITHUB_REPO = 'unicompact-stack/bossyoki'
GITHUB_FILE = 'tasks.json'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger('smart_bot')


# === БД ===

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        note TEXT DEFAULT '',
        deadline TEXT,
        priority TEXT DEFAULT 'medium',
        category TEXT DEFAULT '',
        status TEXT DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        remind_at TIMESTAMP NOT NULL,
        sent INTEGER DEFAULT 0,
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()


# === История переписки ===

def save_message(user_id, role, message):
    conn = get_db()
    conn.execute('INSERT INTO conversations (user_id, role, message) VALUES (?, ?, ?)',
                 (user_id, role, message))
    conn.commit()
    conn.close()

def get_conversation_history(user_id, limit=20):
    conn = get_db()
    rows = conn.execute(
        'SELECT role, message FROM conversations WHERE user_id = ? ORDER BY id DESC LIMIT ?',
        (user_id, limit)
    ).fetchall()
    conn.close()
    return [{'role': r['role'], 'content': r['message']} for r in reversed(rows)]

def clear_conversation(user_id):
    conn = get_db()
    conn.execute('DELETE FROM conversations WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()


# === Задачи ===

def add_task(user_id, title, deadline=None, priority='medium', category='', note=''):
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO tasks (user_id, title, note, deadline, priority, category) VALUES (?, ?, ?, ?, ?, ?)',
        (user_id, title, note, deadline, priority, category)
    )
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    # Синхронизируем с GitHub
    sync_to_github()
    return task_id

def get_tasks(user_id, status='active'):
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM tasks WHERE user_id = ? AND status = ? ORDER BY '
        "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, deadline",
        (user_id, status)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def complete_task(user_id, task_id):
    conn = get_db()
    now = datetime.now().isoformat()
    cur = conn.execute(
        'UPDATE tasks SET status = ?, completed_at = ? WHERE id = ? AND user_id = ?',
        ('done', now, task_id, user_id)
    )
    conn.commit()
    changed = cur.rowcount
    conn.close()
    if changed:
        sync_to_github()
    return changed > 0

def delete_task(user_id, task_id):
    conn = get_db()
    cur = conn.execute('DELETE FROM tasks WHERE id = ? AND user_id = ?', (task_id, user_id))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    if changed:
        sync_to_github()
    return changed > 0

def get_stats(user_id):
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) FROM tasks WHERE user_id = ?', (user_id,)).fetchone()[0]
    done = conn.execute('SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = ?', (user_id, 'done')).fetchone()[0]
    active = conn.execute('SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = ?', (user_id, 'active')).fetchone()[0]
    overdue = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'active' AND deadline < date('now')",
        (user_id,)
    ).fetchone()[0]
    today_count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'active' AND deadline = date('now')",
        (user_id,)
    ).fetchone()[0]
    conn.close()
    return {'total': total, 'done': done, 'active': active, 'overdue': overdue, 'today': today_count}


# === GitHub Sync ===

def sync_to_github():
    """Отправляет задачи в GitHub tasks.json"""
    if not GITHUB_KEY or not GITHUB_KEY.startswith('ghp_'):
        return
    try:
        conn = get_db()
        rows = conn.execute('SELECT * FROM tasks WHERE user_id = ? ORDER BY id', (VK_USER_ID,)).fetchall()
        conn.close()

        tasks_list = []
        for r in rows:
            tasks_list.append({
                'id': str(r['id']),
                'title': r['title'],
                'note': r['note'] or '',
                'date': r['deadline'] or '',
                'time': '',
                'priority': r['priority'],
                'category': r['category'] or '',
                'done': r['status'] == 'done',
                'created': r['created_at'] or ''
            })

        data = json.dumps({
            'version': '2.0',
            'source': 'vk_bot',
            'updatedAt': datetime.now().isoformat(),
            'tasks': tasks_list
        }, ensure_ascii=False, indent=2)

        # Получаем SHA текущего файла
        sha = ''
        try:
            r = requests.get(
                f'https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}',
                headers={'Authorization': f'token {GITHUB_KEY}'},
                timeout=10
            )
            if r.status_code == 200:
                sha = r.json().get('sha', '')
        except:
            pass

        # Записываем
        body = {
            'message': f'🔄 Бот обновил задачи ({datetime.now().strftime("%H:%M")})',
            'content': __import__('base64').b64encode(data.encode()).decode(),
        }
        if sha:
            body['sha'] = sha

        r = requests.put(
            f'https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}',
            headers={'Authorization': f'token {GITHUB_KEY}', 'Content-Type': 'application/json'},
            json=body,
            timeout=10
        )
        if r.status_code in (200, 201):
            log.info('✅ Задачи синхронизированы с GitHub')
        else:
            log.warning(f'GitHub sync: {r.status_code}')
    except Exception as e:
        log.error(f'GitHub sync error: {e}')


def load_from_github():
    """Загружает задачи из GitHub tasks.json"""
    try:
        r = requests.get(
            f'https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}',
            timeout=10
        )
        if r.status_code == 200:
            content = __import__('base64').b64decode(r.json()['content']).decode()
            data = json.loads(content)
            tasks = data.get('tasks', [])

            conn = get_db()
            existing = {r['title'].lower() for r in conn.execute(
                'SELECT title FROM tasks WHERE user_id = ?', (VK_USER_ID,)
            ).fetchall()}

            added = 0
            for t in tasks:
                if t.get('done'):
                    continue
                if t['title'].lower() not in existing:
                    conn.execute(
                        'INSERT INTO tasks (user_id, title, note, deadline, priority, category) VALUES (?, ?, ?, ?, ?, ?)',
                        (VK_USER_ID, t['title'], t.get('note', ''), t.get('date', ''), t.get('priority', 'medium'), t.get('category', ''))
                    )
                    added += 1
            conn.commit()
            conn.close()

            if added:
                log.info(f'📥 Загружено {added} задач с GitHub')
            return added
    except Exception as e:
        log.error(f'GitHub load error: {e}')
    return 0


# === Напоминания ===

def add_reminder(task_id, remind_at):
    conn = get_db()
    conn.execute('INSERT INTO reminders (task_id, remind_at) VALUES (?, ?)', (task_id, remind_at))
    conn.commit()
    conn.close()

def get_pending_reminders():
    conn = get_db()
    now = datetime.now().isoformat()
    rows = conn.execute(
        'SELECT r.id, r.task_id, r.remind_at, t.title, t.user_id FROM reminders r '
        'JOIN tasks t ON r.task_id = t.id WHERE r.sent = 0 AND r.remind_at <= ?',
        (now,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def mark_reminder_sent(reminder_id):
    conn = get_db()
    conn.execute('UPDATE reminders SET sent = 1 WHERE id = ?', (reminder_id,))
    conn.commit()
    conn.close()

def parse_reminder_time(text):
    t = text.lower().strip()

    match = re.match(r'напомни через (\d+) (минут[уыа]?|секунд[уыа]?) (.+)', t)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        task_text = match.group(3)
        if 'секунд' in unit:
            remind_at = datetime.now() + timedelta(seconds=amount)
        else:
            remind_at = datetime.now() + timedelta(minutes=amount)
        return remind_at, task_text

    match = re.match(r'напомни завтра (\d{1,2}[:\.]?\d{0,2}) (.+)', t)
    if match:
        time_str = match.group(1).replace('.', ':')
        task_text = match.group(2)
        if ':' not in time_str:
            time_str += ':00'
        h, m = map(int, time_str.split(':'))
        remind_at = datetime.now().replace(hour=h, minute=m, second=0) + timedelta(days=1)
        return remind_at, task_text

    match = re.match(r'напомни сегодня (\d{1,2}[:\.]?\d{0,2}) (.+)', t)
    if match:
        time_str = match.group(1).replace('.', ':')
        task_text = match.group(2)
        if ':' not in time_str:
            time_str += ':00'
        h, m = map(int, time_str.split(':'))
        remind_at = datetime.now().replace(hour=h, minute=m, second=0)
        if remind_at < datetime.now():
            remind_at += timedelta(days=1)
        return remind_at, task_text

    return None, None


# === AI ===

SYSTEM_PROMPT = """Ты — Босс Йоки, жёсткий и требовательный наставник по тайм-менеджменту.

Твоя задача:
- Помогать пользователю быть продуктивным
- Хвалить за успехи, особенно за срочные задачи
- Если откладывает дела — мягко, но настойчиво напоминать
- Говорить на русском, коротко и по делу
- Использовать эмодзи умеренно

Ты можешь:
1. Добавлять задачи — ответь форматом: [ADD: название, дата, приоритет]
2. Отмечать выполненные — ответь: [DONE: номер]
3. Удалять — ответь: [DEL: номер]
4. Показывать список задач
5. Давать советы по планированию

ВАЖНО: Всегда отвечай на русском. Будь живым, общайся на ты."""

def get_tasks_context(user_id):
    tasks = get_tasks(user_id)
    if not tasks:
        return 'Нет активных задач.'
    lines = []
    pri = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}
    for t in tasks:
        p = pri.get(t['priority'], '🟡')
        d = f" ({t['deadline']})" if t.get('deadline') else ''
        lines.append(f"#{t['id']} {p} {t['title']}{d}")
    return '\n'.join(lines)

def ask_ai(text, user_id):
    if not GITHUB_KEY:
        return None
    try:
        # Сохраняем сообщение пользователя
        save_message(user_id, 'user', text)

        context = get_tasks_context(user_id)
        stats = get_stats(user_id)
        history = get_conversation_history(user_id, limit=20)

        system = SYSTEM_PROMPT + f"""

Текущие задачи пользователя:
{context}

Статистика:
- Активных: {stats['active']}
- Выполнено: {stats['done']}
- Сегодня: {stats['today']}
- Просрочено: {stats['overdue']}

Сегодня: {datetime.now().strftime('%d.%m.%Y')}"""

        messages = [{'role': 'system', 'content': system}]
        messages.extend(history)

        r = requests.post(
            'https://models.inference.ai.azure.com/chat/completions',
            headers={
                'Authorization': f'Bearer {GITHUB_KEY}',
                'Content-Type': 'application/json'
            },
            json={
                'model': 'gpt-4o-mini',
                'messages': messages,
                'max_tokens': 500,
                'temperature': 0.7
            },
            timeout=15
        )
        data = r.json()
        reply = data['choices'][0]['message']['content']

        # Сохраняем ответ бота
        save_message(user_id, 'assistant', reply)

        # Парсим команды AI
        parse_ai_actions(reply, user_id)

        return reply
    except Exception as e:
        log.error(f'AI error: {e}')
        return None

def parse_ai_actions(reply, user_id):
    """Парсит команды [ADD: ...], [DONE: ...], [DEL: ...] из ответа AI"""
    # ADD
    add_match = re.search(r'\[ADD:\s*(.+?)\]', reply)
    if add_match:
        parts = add_match.group(1).split(',')
        title = parts[0].strip()
        deadline = parts[1].strip() if len(parts) > 1 else None
        priority = parts[2].strip() if len(parts) > 2 else 'medium'

        if deadline == 'сегодня':
            deadline = datetime.now().strftime('%Y-%m-%d')
        elif deadline == 'завтра':
            deadline = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        elif deadline and re.match(r'\d{2}\.\d{2}', deadline):
            deadline = datetime.now().strftime('%Y') + '-' + deadline[::-1][:4][::-1]

        add_task(user_id, title, deadline, priority)

    # DONE
    done_match = re.search(r'\[DONE:\s*(\d+)\]', reply)
    if done_match:
        complete_task(user_id, int(done_match.group(1)))

    # DEL
    del_match = re.search(r'\[DEL:\s*(\d+)\]', reply)
    if del_match:
        delete_task(user_id, int(del_match.group(1)))


# === Команды ===

def handle_tasks(text, user_id):
    t = text.lower().strip()

    if t in ['задачи', 'список задач', 'мои задачи', '!список', 'список']:
        tasks = get_tasks(user_id)
        if not tasks:
            return '📋 Задач пока нет.\nНапиши "добавить [текст]" чтобы создать.'
        pri_icons = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}
        lines = ['📋 Твои задачи:\n']
        for task in tasks:
            pri = pri_icons.get(task['priority'], '🟡')
            deadline = f" ({task['deadline']})" if task.get('deadline') else ''
            lines.append(f"#{task['id']} {pri} {task['title']}{deadline}")
        return '\n'.join(lines)

    if t.startswith('добавить ') or t.startswith('новая ') or t.startswith('добавь '):
        task_text = text.split(' ', 1)[1] if ' ' in text else ''
        if not task_text:
            return 'Укажи текст задачи.\nПример: добавить отчёт по рекламе'

        lower = task_text.lower()
        deadline = None
        priority = 'medium'

        if 'срочно' in lower or 'важно' in lower or 'горит' in lower:
            priority = 'high'
        elif 'потом' in lower or 'не срочно' in lower:
            priority = 'low'

        if 'сегодня' in lower:
            deadline = datetime.now().strftime('%Y-%m-%d')
        elif 'завтра' in lower:
            deadline = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

        clean_title = re.sub(
            r'сегодня|завтра|срочно|важно|горит|потом|не срочно',
            '', task_text, flags=re.IGNORECASE
        ).strip()
        if not clean_title:
            clean_title = task_text

        task_id = add_task(user_id, clean_title, deadline, priority)
        pri_label = '🔴 Срочно' if priority == 'high' else '🟢 Потом' if priority == 'low' else '🟡 Важно'
        date_label = f" 📅 {deadline}" if deadline else ''
        return f'✅ Задача #{task_id}: {clean_title}\n{pri_label}{date_label}'

    if t.startswith('выполнено ') or t.startswith('сделано ') or t.startswith('выполни '):
        try:
            task_id = int(t.split()[1])
            if complete_task(user_id, task_id):
                return f'✅ Задача #{task_id} выполнена! Молодец!'
            return f'Задача #{task_id} не найдена.'
        except (ValueError, IndexError):
            return 'Укажи номер задачи.\nПример: выполнено 1'

    if t.startswith('удалить ') or t.startswith('убрать ') or t.startswith('!удалить '):
        try:
            task_id = int(t.split()[-1])
            if delete_task(user_id, task_id):
                return f'🗑 Задача #{task_id} удалена.'
            return f'Задача #{task_id} не найдена.'
        except (ValueError, IndexError):
            return 'Укажи номер задачи.'

    if t == '!отчёт' or t == '!отчет' or t == 'отчёт':
        stats = get_stats(user_id)
        return (
            f'📊 Отчёт:\n'
            f'✅ Выполнено: {stats["done"]}\n'
            f'📋 Активных: {stats["active"]}\n'
            f'📌 Сегодня: {stats["today"]}\n'
            f'⚠️ Просрочено: {stats["overdue"]}\n'
            f'📁 Всего: {stats["total"]}'
        )

    return None

def handle_reminder(text, user_id):
    t = text.lower().strip()

    if t.startswith('напомни '):
        remind_at, task_text = parse_reminder_time(t)
        if remind_at and task_text:
            task_id = add_task(user_id, task_text, remind_at.strftime('%Y-%m-%d'), 'medium')
            add_reminder(task_id, remind_at.strftime('%Y-%m-%d %H:%M:%S'))
            return f'⏰ Напоминание на {remind_at.strftime("%d.%m %H:%M")}: {task_text}'
        return 'Не понял время.\nПример: напомни через 30 минут позвонить'

    if t in ['напоминания', 'активные напоминания']:
        conn = get_db()
        rows = conn.execute(
            'SELECT r.remind_at, t.title FROM reminders r '
            'JOIN tasks t ON r.task_id = t.id '
            'WHERE t.user_id = ? AND r.sent = 0 ORDER BY r.remind_at',
            (user_id,)
        ).fetchall()
        conn.close()
        if not rows:
            return 'Активных напоминаний нет.'
        lines = ['⏰ Напоминания:\n']
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['remind_at'][:16]} — {row['title']}")
        return '\n'.join(lines)

    return None

def handle_help():
    return (
        '📌 Команды Задачника+:\n\n'
        '📋 Задачи:\n'
        '• добавить [текст] — новая задача\n'
        '• задачи — список задач\n'
        '• выполнено [номер] — отметить\n'
        '• удалить [номер] — удалить\n'
        '• отчёт — статистика\n\n'
        '⏰ Напоминания:\n'
        '• напомни через 30 минут [текст]\n'
        '• напомни завтра 10:00 [текст]\n'
        '• напоминания — список\n\n'
        '🤖 AI:\n'
        '• Любой вопрос — ответ от AI\n'
        '• Помощь — этот список'
    )


# === Фоновые задачи ===

vk_session_ref = None

def send_vk_message(user_id, text):
    try:
        if vk_session_ref:
            vk_session_ref.messages.send(
                user_id=user_id,
                message=text,
                random_id=get_random_id()
            )
    except Exception as e:
        log.error(f'VK send error: {e}')

def check_reminders():
    while True:
        try:
            pending = get_pending_reminders()
            for r in pending:
                send_vk_message(r['user_id'], f'⏰ Напоминание: {r["title"]}')
                mark_reminder_sent(r['id'])
                log.info(f'Напоминание: {r["user_id"]}: {r["title"]}')
        except Exception as e:
            log.error(f'Reminder check error: {e}')
        time.sleep(60)

def periodic_sync():
    """Синхронизация с GitHub каждые 5 минут"""
    while True:
        try:
            sync_to_github()
        except Exception as e:
            log.error(f'Periodic sync error: {e}')
        time.sleep(300)


# === Обработка сообщений ===

def handle_message(event, api):
    text = event.obj.message['text'].strip()
    user_id = event.obj.message['from_id']

    if not text:
        return

    # Сохраняем сообщение пользователя
    save_message(user_id, 'user', text)

    reply = None

    # Команды
    if text.lower() in ['помощь', 'помоги', '!помощь', 'команды', 'help']:
        reply = handle_help()
    if not reply:
        reply = handle_tasks(text, user_id)
    if not reply:
        reply = handle_reminder(text, user_id)

    # AI (если не распознана команда)
    if not reply:
        reply = ask_ai(text, user_id)

    if not reply:
        reply = 'Не понял. Напиши "помощь" для списка команд.'

    # Сохраняем ответ бота
    save_message(user_id, 'assistant', reply)

    api.messages.send(
        user_id=user_id,
        message=reply,
        random_id=get_random_id()
    )
    log.info(f'→ {user_id}: {reply[:100]}')


# === Запуск ===

# === Health check для Render.com ===
from http.server import HTTPServer, BaseHTTPRequestHandler
import os

PORT = int(os.getenv('PORT', '10000'))

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, format, *args):
        pass

def start_health_server():
    try:
        server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
        print(f'Health server started on port {PORT}', flush=True)
        server.serve_forever()
    except Exception as e:
        print(f'Health server error: {e}', flush=True)


def main():
    global vk_session_ref

    def stop(sig, frame):
        log.info('Остановлен.')
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    init_db()

    # Загружаем задачи из GitHub при старте
    load_from_github()

    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))

    vk_session = vk_api.VkApi(token=TOKEN)
    vk_session_ref = vk_session
    longpoll = VkBotLongPoll(vk_session, GROUP_ID)

    log.info(f'🚀 Бот запущен! Группа: {GROUP_ID}')

    # Health check для Render — ЗАПУСКАЕМ ПЕРВЫМ
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()
    log.info(f'✅ Health check на порту {PORT}')

    # Фоновые потоки
    threading.Thread(target=check_reminders, daemon=True).start()
    threading.Thread(target=periodic_sync, daemon=True).start()

    for event in longpoll.listen():
        if event.type == VkBotEventType.MESSAGE_NEW:
            text = event.obj.message.get('text', '').strip()
            user_id = event.obj.message['from_id']

            if not text:
                continue

            log.info(f'← {user_id}: {text}')
            handle_message(event, vk_session.get_api())


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'stop':
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            print(f'Остановлен (PID {pid})')
        else:
            print('Бот не запущен')
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == 'log':
        if os.path.exists(LOG_FILE):
            os.system(f'tail -30 "{LOG_FILE}"')
        else:
            print('Логов нет')
        sys.exit(0)

    main()
