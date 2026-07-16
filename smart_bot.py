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
        time TEXT DEFAULT '',
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
    # Очищаем сообщения старше 30 дней
    conn.execute("DELETE FROM conversations WHERE created_at < datetime('now', '-30 days')")
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

def add_task(user_id, title, deadline=None, priority='medium', category='', note='', time=None):
    conn = get_db()
    # Проверяем есть ли колонка time
    try:
        conn.execute('SELECT time FROM tasks LIMIT 1')
    except:
        conn.execute('ALTER TABLE tasks ADD COLUMN time TEXT DEFAULT ''')
        conn.commit()

    cur = conn.execute(
        'INSERT INTO tasks (user_id, title, note, deadline, priority, category, time) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (user_id, title, note, deadline, priority, category, time)
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

_last_sync_time = 0
_SYNC_INTERVAL = 300  # 5 минут

def sync_to_github(force=False):
    """Отправляет задачи в GitHub tasks.json (debounce 5 мин)"""
    global _last_sync_time
    now = time.time()
    if not force and (now - _last_sync_time) < _SYNC_INTERVAL:
        return
    _last_sync_time = now

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

    match = re.match(r'(?:напомни )?через (\d+) (минут[уыа]?|секунд[уыа]?|час[аов]*) (.+)', t)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        task_text = match.group(3)
        if 'секунд' in unit:
            remind_at = datetime.now() + timedelta(seconds=amount)
        elif 'час' in unit:
            remind_at = datetime.now() + timedelta(hours=amount)
        else:
            remind_at = datetime.now() + timedelta(minutes=amount)
        return remind_at, task_text

    match = re.match(r'(?:напомни )?завтра (\d{1,2}[:\.]?\d{0,2}) (.+)', t)
    if match:
        time_str = match.group(1).replace('.', ':')
        task_text = match.group(2)
        if ':' not in time_str:
            time_str += ':00'
        h, m = map(int, time_str.split(':'))
        remind_at = datetime.now().replace(hour=h, minute=m, second=0) + timedelta(days=1)
        return remind_at, task_text

    match = re.match(r'(?:напомни )?сегодня (\d{1,2}[:\.]?\d{0,2}) (.+)', t)
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

    match = re.match(r'(?:напомни )?в (\d{1,2})[:\.]?(\d{0,2}) (.+)', t)
    if match:
        h = int(match.group(1))
        m = int(match.group(2)) if match.group(2) else 0
        task_text = match.group(3)
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

ТЫ УМЕЕШЬ (это важно, пользователь может попросить):

1. ДОБАВЛЯТЬ ЗАДАЧИ — ответь форматом: [ADD: название, дата, приоритет]
   - дата: сегодня, завтра, 2026-07-20, или пусто
   - приоритет: high (срочно), medium (важно), low (потом)
   - Пример: [ADD: Отчёт по рекламе, завтра, high]

2. ОТМЕЧАТЬ ВЫПОЛНЕННЫЕ — ответь: [DONE: номер задачи]
   - Если пользователь говорит "сделал", "выполнил", "готово" — найди задачу и отметь

3. УДАЛЯТЬ — ответь: [DEL: номер задачи]

4. НАПОМИНАНИЯ — когда пользователь просит напомнить:
   - "напомни завтра в 9 саморезы" → создай задачу на завтра с дедлайном
   - "напомни через 30 минут позвонить" → создай задачу на сегодня
   - "напомни в 15:00 отчёт" → создай задачу на сегодня с временем
   - ВСЕГДА создавай задачу через [ADD] когда просят напомнить

5. АНАЛИЗ — если пользователь спрашивает "что я сделал", "отчитайся":
   - Посмотри список задач и сколько выполнено
   - Если мало выполнено — мягко напомни о продуктивности
   - Хвали за каждую выполненную задачу

6. ПОНИМАНИЕ ВРЕМЕНИ:
   - "в 9 утра" = 09:00
   - "в 15:00" = 15:00
   - "через 2 часа" = текущее время + 2 часа
   - "завтра в 10" = завтра 10:00
   - ВСЕГДА учитывай время когда создаёшь задачу

ПОМНИ: Ты УМЕЕШЬ ставить напоминания и задачи. НИКОГДА не говори "я не могу". Ты можешь ВСЁ.

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
        tm = f" в {t['time']}" if t.get('time') else ''
        lines.append(f"#{t['id']} {p} {t['title']}{d}{tm}")
    return '\n'.join(lines)

def ask_ai(text, user_id):
    if not GITHUB_KEY:
        return None
    try:
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

        # Парсим время из заголовка или дедлайна
        time_str = None
        full_text = title + ' ' + (deadline or '')

        # Ищем время: "в 15:00", "в 9 утра", "в 17"
        time_match = re.search(r'в (\d{1,2})[:\.]?(\d{0,2})', full_text.lower())
        if time_match:
            h = int(time_match.group(1))
            m = int(time_match.group(2)) if time_match.group(2) else 0
            if 0 <= h <= 23 and 0 <= m <= 59:
                time_str = f'{h:02d}:{m:02d}'

        # Парсим дату
        if deadline == 'сегодня' or 'сегодня' in full_text.lower():
            deadline = datetime.now().strftime('%Y-%m-%d')
        elif deadline == 'завтра' or 'завтра' in full_text.lower():
            deadline = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        elif deadline and re.match(r'\d{4}-\d{2}-\d{2}', deadline):
            pass  # Уже в правильном формате
        elif deadline and re.match(r'\d{2}\.\d{2}', deadline):
            # Парсим "20.07" → "2026-07-20"
            day, month = deadline.split('.')
            deadline = f'{datetime.now().year}-{month}-{day}'
        else:
            deadline = datetime.now().strftime('%Y-%m-%d')

        # Убираем время из названия задачи
        clean_title = re.sub(r'в \d{1,2}[:\.]?\d{0,2}\s*(утра|вечера|дня)?', '', title, flags=re.IGNORECASE).strip()
        if not clean_title:
            clean_title = title

        add_task(user_id, clean_title, deadline, priority, time=time_str)

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

    if t.startswith('напомни ') or re.match(r'через \d+ (минут|секунд|час)', t):
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
            sync_to_github(force=True)
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

# === Health check + Dashboard для Render.com ===
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

PORT = int(os.getenv('PORT', '10000'))

DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BossYoki Bot — Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f0f;color:#e0e0e0;min-height:100vh}
.header{background:#1a1a2e;padding:16px 24px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #333}
.header h1{font-size:20px;color:#ff6b35}
.status{display:inline-block;width:10px;height:10px;border-radius:50%;background:#4caf50;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.container{max-width:900px;margin:0 auto;padding:16px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.stat{background:#1a1a2e;border-radius:8px;padding:16px;text-align:center}
.stat-num{font-size:28px;font-weight:700;color:#ff6b35}
.stat-label{font-size:12px;color:#888;margin-top:4px}
.section{background:#1a1a2e;border-radius:8px;padding:16px;margin-bottom:16px}
.section h2{font-size:14px;color:#888;text-transform:uppercase;margin-bottom:12px}
.msg{padding:8px 12px;border-radius:6px;margin-bottom:6px;font-size:13px;line-height:1.4;word-break:break-word}
.msg-user{background:#16213e;border-left:3px solid #ff6b35}
.msg-bot{background:#1a1a2e;border-left:3px solid #4caf50}
.msg-time{font-size:11px;color:#666}
.msg-text{margin-top:2px}
.task{display:flex;align-items:center;gap:8px;padding:8px 12px;background:#16213e;border-radius:6px;margin-bottom:6px;font-size:13px}
.task-done{opacity:.5;text-decoration:line-through}
.pri-high{color:#ff4444}.pri-medium{color:#ffaa00}.pri-low{color:#4caf50}
.refresh{position:fixed;bottom:20px;right:20px;background:#ff6b35;color:#fff;border:none;border-radius:50%;width:48px;height:48px;font-size:20px;cursor:pointer;box-shadow:0 4px 12px rgba(255,107,53,.4)}
.refresh:hover{transform:scale(1.1)}
.ts{color:#555;font-size:11px;text-align:center;padding:8px}
</style>
</head>
<body>
<div class="header">
<div class="status"></div>
<h1>BossYoki Bot</h1>
<span style="color:#888;font-size:13px">Dashboard</span>
</div>
<div class="container">
<div class="stats" id="stats"></div>
<div class="section">
<h2>Последние сообщения</h2>
<div id="messages"><div class="ts">Загрузка...</div></div>
</div>
<div class="section">
<h2>Задачи</h2>
<div id="tasks"><div class="ts">Загрузка...</div></div>
</div>
</div>
<button class="refresh" onclick="load()" title="Обновить">&#8635;</button>
<script>
function load(){
fetch('/api/data').then(r=>r.json()).then(d=>{
document.getElementById('stats').innerHTML=
'<div class="stat"><div class="stat-num">'+d.stats.active+'</div><div class="stat-label">Активных</div></div>'+
'<div class="stat"><div class="stat-num">'+d.stats.done+'</div><div class="stat-label">Выполнено</div></div>'+
'<div class="stat"><div class="stat-num">'+d.stats.today+'</div><div class="stat-label">Сегодня</div></div>'+
'<div class="stat"><div class="stat-num">'+d.stats.overdue+'</div><div class="stat-label">Просрочено</div></div>';
let mh='';
d.messages.forEach(m=>{
let cls=m.role=='user'?'msg-user':'msg-bot';
let icon=m.role=='user'?'&#128100;':'&#129302;';
mh+='<div class="msg '+cls+'"><div class="msg-time">'+icon+' '+m.date+' '+m.time+'</div><div class="msg-text">'+esc(m.message)+'</div></div>';
});
document.getElementById('messages').innerHTML=mh||'<div class="ts">Нет сообщений</div>';
let th='';
d.tasks.forEach(t=>{
let cls=t.status=='done'?'task-done':'';
let pri=t.priority=='high'?'&#128308;':t.priority=='low'?'&#128994;':'&#128992;';
th+='<div class="task '+cls+'"><span>'+pri+'</span><span>#'+t.id+'</span><span>'+esc(t.title)+'</span><span style="color:#666;margin-left:auto">'+(t.deadline||'')+'</span></div>';
});
document.getElementById('tasks').innerHTML=th||'<div class="ts">Нет задач</div>';
}).catch(()=>{document.getElementById('messages').innerHTML='<div class="ts">Бот не отвечает (возможно засыпает)</div>'});
}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
load();
setInterval(load,30000);
</script>
</body>
</html>'''

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == '/api/data':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                data = self._get_api_data()
                self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
            else:
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(DASHBOARD_HTML.encode('utf-8'))
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def _get_api_data(self):
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        try:
            msgs = []
            for r in conn.execute(
                "SELECT role, message, created_at FROM conversations ORDER BY id DESC LIMIT 30"
            ).fetchall():
                row = dict(r)
                t = row.get('created_at', '')
                # SQLite хранит UTC — конвертируем в местное время
                try:
                    from datetime import datetime as dt2
                    from zoneinfo import ZoneInfo
                    utc = dt2.strptime(t[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('UTC'))
                    local = utc.astimezone()
                    row['time'] = local.strftime('%H:%M')
                    row['date'] = local.strftime('%d.%m')
                except Exception:
                    # Fallback: просто берём как есть
                    row['time'] = t[11:16] if len(t) > 16 else t[:5]
                    row['date'] = t[:10]
                msgs.append(row)
            msgs.reverse()
            tasks = [dict(r) for r in conn.execute(
                "SELECT id, title, priority, deadline, status FROM tasks WHERE user_id = ? ORDER BY id DESC LIMIT 50",
                (VK_USER_ID,)
            ).fetchall()]
            stats = {
                'active': conn.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'active'", (VK_USER_ID,)).fetchone()[0],
                'done': conn.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'done'", (VK_USER_ID,)).fetchone()[0],
                'today': conn.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'active' AND deadline = date('now')", (VK_USER_ID,)).fetchone()[0],
                'overdue': conn.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'active' AND deadline < date('now')", (VK_USER_ID,)).fetchone()[0],
            }
        finally:
            conn.close()
        return {'messages': msgs, 'tasks': tasks, 'stats': stats}

    def log_message(self, format, *args):
        pass

def start_health_server():
    try:
        server = HTTPServer(('0.0.0.0', PORT), DashboardHandler)
        print(f'Dashboard started on port {PORT}', flush=True)
        server.serve_forever()
    except Exception as e:
        print(f'Dashboard server error: {e}', flush=True)


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
