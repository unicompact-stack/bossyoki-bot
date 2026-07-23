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
import psycopg2
import requests
import pytz
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.utils import get_random_id

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

TOKEN = os.getenv('VK_TOKEN_MESSAGES')
GROUP_ID = int(os.getenv('VK_GROUP_ID', '73303964'))
GITHUB_KEY = os.getenv('GITHUB_TOKEN', os.getenv('GROQ_API_KEY'))
VK_USER_ID = int(os.getenv('VK_USER_ID', '114439622'))
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(DIR, 'smart_bot.log')
PID_FILE = os.path.join(DIR, 'smart_bot.pid')
GITHUB_REPO = 'unicompact-stack/bossyoki'
GITHUB_FILE = 'tasks.json'

MOSCOW_TZ = pytz.timezone('Europe/Moscow')

def get_moscow_now():
    return datetime.now(MOSCOW_TZ)

def to_utc(local_dt):
    if local_dt.tzinfo is None:
        local_dt = MOSCOW_TZ.localize(local_dt)
    return local_dt.astimezone(pytz.UTC)

def from_utc(utc_dt):
    if utc_dt.tzinfo is None:
        utc_dt = pytz.UTC.localize(utc_dt)
    return utc_dt.astimezone(MOSCOW_TZ)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger('smart_bot')

VERSION = '2.2.0'
BUILD_TIME = '21.07.2026 18:40'


# === БД ===

def get_db():
    conn = psycopg2.connect(SUPABASE_KEY)
    conn.autocommit = False
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id BIGINT NOT NULL,
        title TEXT NOT NULL,
        note TEXT DEFAULT '',
        deadline TEXT,
        time TEXT DEFAULT '',
        priority TEXT DEFAULT 'medium',
        category TEXT DEFAULT '',
        status TEXT DEFAULT 'active',
        created_at TIMESTAMP DEFAULT NOW(),
        completed_at TIMESTAMP
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        task_id BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        remind_at TIMESTAMP NOT NULL,
        sent INTEGER DEFAULT 0
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id BIGINT NOT NULL,
        role TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW()
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS mimo_tasks (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id BIGINT NOT NULL,
        text TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        result TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT NOW(),
        completed_at TIMESTAMP
    )''')
    conn.commit()
    conn.close()


# === История переписки ===

def save_message(user_id, role, message):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT INTO conversations (user_id, role, message) VALUES (%s, %s, %s)',
                (user_id, role, message))
    conn.commit()
    conn.close()

def get_conversation_history(user_id, limit=20):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'SELECT role, message FROM conversations WHERE user_id = %s ORDER BY id DESC LIMIT %s',
        (user_id, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return [{'role': r[0], 'content': r[1]} for r in reversed(rows)]

def clear_conversation(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM conversations WHERE user_id = %s', (user_id,))
    conn.commit()
    conn.close()


# === MiMo Tasks (задачи для MiMo Code — через GitHub) ===

import base64

MIMO_GITHUB_REPO = 'unicompact-stack/bossyoki'
MIMO_GITHUB_FILE = 'mimo_tasks.json'
GITHUB_API = 'https://api.github.com'

def load_mimo_tasks():
    """Читает задачи из GitHub"""
    if not GITHUB_KEY:
        return []
    url = f"{GITHUB_API}/repos/{MIMO_GITHUB_REPO}/contents/{MIMO_GITHUB_FILE}"
    headers = {"Authorization": f"token {GITHUB_KEY}", "Accept": "application/vnd.github.v3+json"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            content = base64.b64decode(data['content']).decode('utf-8')
            return json.loads(content), data.get('sha', '')
        elif r.status_code == 404:
            return [], None
        else:
            log.error(f'GitHub read error: {r.status_code}')
            return [], None
    except Exception as e:
        log.error(f'GitHub read error: {e}')
        return [], None

def save_mimo_tasks(tasks, sha=None):
    """Сохраняет задачи на GitHub"""
    if not GITHUB_KEY:
        return False
    url = f"{GITHUB_API}/repos/{MIMO_GITHUB_REPO}/contents/{MIMO_GITHUB_FILE}"
    headers = {"Authorization": f"token {GITHUB_KEY}", "Accept": "application/vnd.github.v3+json"}
    content = json.dumps(tasks, ensure_ascii=False, indent=2)
    encoded = base64.b64encode(content.encode('utf-8')).decode('utf-8')
    data = {"message": f"Update mimo_tasks ({get_moscow_now().strftime('%H:%M')})", "content": encoded}
    if sha:
        data["sha"] = sha
    try:
        r = requests.put(url, headers=headers, json=data, timeout=10)
        return r.status_code in (200, 201)
    except Exception as e:
        log.error(f'GitHub save error: {e}')
        return False

def add_mimo_task(user_id, text):
    tasks, sha = load_mimo_tasks()
    if not isinstance(tasks, list):
        tasks = []
    task_id = max([t.get('id', 0) for t in tasks], default=0) + 1
    tasks.append({
        "id": task_id,
        "text": text,
        "user_id": user_id,
        "status": "pending",
        "created": get_moscow_now().isoformat()
    })
    save_mimo_tasks(tasks, sha)
    return task_id

def get_pending_mimo_tasks():
    tasks, _ = load_mimo_tasks()
    if not isinstance(tasks, list):
        return []
    return [t for t in tasks if t.get('status') == 'pending']

def get_all_mimo_tasks(user_id=None):
    tasks, _ = load_mimo_tasks()
    if not isinstance(tasks, list):
        return []
    if user_id:
        tasks = [t for t in tasks if t.get('user_id') == user_id]
    return sorted(tasks, key=lambda x: x.get('id', 0), reverse=True)[:10]

def complete_mimo_task(task_id, result=''):
    tasks, sha = load_mimo_tasks()
    if not isinstance(tasks, list):
        return False
    for t in tasks:
        if t.get('id') == task_id:
            t['status'] = 'done'
            t['result'] = result
            t['completed_at'] = get_moscow_now().isoformat()
            break
    return save_mimo_tasks(tasks, sha)

def error_mimo_task(task_id, result=''):
    tasks, sha = load_mimo_tasks()
    if not isinstance(tasks, list):
        return False
    for t in tasks:
        if t.get('id') == task_id:
            t['status'] = 'error'
            t['result'] = result
            break
    return save_mimo_tasks(tasks, sha)

def clear_done_mimo_tasks():
    tasks, sha = load_mimo_tasks()
    if not isinstance(tasks, list):
        return 0
    before = len(tasks)
    tasks = [t for t in tasks if t.get('status') not in ('done', 'error')]
    save_mimo_tasks(tasks, sha)
    return before - len(tasks)

MIMO_CONFIG_FILE = os.path.join(DIR, 'mimo_config.json')

def load_mimo_config():
    if os.path.exists(MIMO_CONFIG_FILE):
        with open(MIMO_CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {"enabled": False}

def save_mimo_config(config):
    with open(MIMO_CONFIG_FILE, 'w') as f:
        json.dump(config, f)

def handle_mimo(text, user_id):
    t = text.lower().strip()
    config = load_mimo_config()

    # Включение/выключение MiMo режима — ВСЕГДА работают
    if t in ['мимо вкл', 'mimo on', 'мимо включить']:
        config['enabled'] = True
        save_mimo_config(config)
        return '🟢 MiMo Code включён!\n\nТеперь пиши задачи через "мимо [задача]".\nБосс Йоки молчит — работает только MiMo.'

    if t in ['мимо выкл', 'mimo off', 'мимо отключить']:
        config['enabled'] = False
        save_mimo_config(config)
        return '🔴 MiMo Code выключен.\n\nБосс Йоки снова работает! Пиши как обычно.'

    if t in ['мимо режим', 'mimo режим']:
        status = '🟢 Включён' if config.get('enabled', True) else '🔴 Выключен'
        return f'Режим MiMo Code: {status}\n\nКоманды:\n• мимо вкл — включить (Босс Йоки молчит)\n• мимо выкл — выключить (Босс Йоки работает)'

    # Если MiMo выключен — пропускаем обработку задач
    if not config.get('enabled', True):
        return None

    # Отправить задачу в MiMo Code
    if t.startswith('мимо ') or t.startswith('mimo '):
        task_text = text.split(' ', 1)[1] if ' ' in text else ''
        if not task_text:
            return 'Укажи текст задачи.\nПример: мимо сделай лендинг'
        task_id = add_mimo_task(user_id, task_text)
        return f'✅ Задача #{task_id} отправлена в MiMo Code:\n«{task_text}»\n\nСтатус: в очереди'

    # Показать очередь
    if t in ['мимо задачи', 'mimo задачи', 'очередь мимо']:
        tasks = get_all_mimo_tasks(user_id)
        if not tasks:
            return 'Очередь задач MiMo Code пуста.'
        status_icons = {'pending': '⏳', 'done': '✅', 'error': '❌', 'processing': '⚙️'}
        lines = ['🤖 Очередь MiMo Code:\n']
        for task in tasks:
            icon = status_icons.get(task['status'], '❓')
            lines.append(f"#{task['id']} {icon} {task['text'][:50]}")
        return '\n'.join(lines)

    # Очистить выполненные
    if t in ['мимо очистить', 'mimo очистить', 'мимо clear']:
        deleted = clear_done_mimo_tasks()
        return f'🗑 Удалено {deleted} завершённых задач.'

    # Статус
    if t in ['мимо статус', 'mimo статус']:
        pending = len(get_pending_mimo_tasks())
        return f'📊 В очереди: {pending} задач(а)'

    return None


# === Задачи ===

def add_task(user_id, title, deadline=None, priority='medium', category='', note='', time=None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO tasks (user_id, title, note, deadline, priority, category, time) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id',
        (user_id, title, note, deadline, priority, category, time)
    )
    task_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    sync_to_github()
    return task_id

def get_tasks(user_id, status='active'):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM tasks WHERE user_id = %s AND status = %s ORDER BY "
        "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, deadline",
        (user_id, status)
    )
    columns = [desc[0] for desc in cur.description]
    rows = cur.fetchall()
    conn.close()
    return [dict(zip(columns, row)) for row in rows]

def complete_task(user_id, task_id):
    conn = get_db()
    cur = conn.cursor()
    now = get_moscow_now().isoformat()
    cur.execute(
        "UPDATE tasks SET status = 'done', completed_at = %s WHERE id = %s AND user_id = %s",
        (now, task_id, user_id)
    )
    conn.commit()
    changed = cur.rowcount
    conn.close()
    if changed:
        sync_to_github()
    return changed > 0

def delete_task(user_id, task_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM tasks WHERE id = %s AND user_id = %s', (task_id, user_id))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    if changed:
        sync_to_github()
    return changed > 0

def get_stats(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM tasks WHERE user_id = %s', (user_id,))
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tasks WHERE user_id = %s AND status = 'done'", (user_id,))
    done = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tasks WHERE user_id = %s AND status = 'active'", (user_id,))
    active = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM tasks WHERE user_id = %s AND status = 'active' AND deadline < CURRENT_DATE::text",
        (user_id,)
    )
    overdue = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM tasks WHERE user_id = %s AND status = 'active' AND deadline = CURRENT_DATE::text",
        (user_id,)
    )
    today_count = cur.fetchone()[0]
    conn.close()
    return {'total': total, 'done': done, 'active': active, 'overdue': overdue, 'today': today_count}


# === GitHub Sync ===

_last_sync_time = 0
_SYNC_INTERVAL = 30  # 30 секунд минимум
_github_loaded = False  # Блокировка до загрузки из GitHub

def sync_to_github(force=False):
    """Бот пишет в GitHub tasks.json — главный источник"""
    global _last_sync_time
    if not _github_loaded:
        log.info('⏳ GitHub ещё не загружен, пропускаю sync')
        return
    now = time.time()
    if not force and (now - _last_sync_time) < _SYNC_INTERVAL:
        return
    _last_sync_time = now

    if not GITHUB_KEY or not GITHUB_KEY.startswith('ghp_'):
        log.warning('⚠️ GitHub токен не настроен')
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT * FROM tasks WHERE user_id = %s ORDER BY id', (VK_USER_ID,))
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        conn.close()

        # Если в БД нет задач — не перезаписываем GitHub (загружаем оттуда)
        if len(rows) == 0:
            log.info('⚠️ БД пуста, пропускаю sync (загружаю из GitHub)')
            load_from_github()
            return

        tasks_list = []
        for row in rows:
            r = dict(zip(columns, row))
            tasks_list.append({
                'id': str(r['id']),
                'title': r['title'],
                'note': r['note'] or '',
                'date': r['deadline'] or '',
                'time': r['time'] or '',
                'priority': r['priority'],
                'category': r['category'] or '',
                'done': r['status'] == 'done',
                'created': str(r['created_at']) if r['created_at'] else ''
            })

        data = json.dumps({
            'version': '2.0',
            'source': 'vk_bot',
            'updatedAt': get_moscow_now().isoformat(),
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
            'message': f'🔄 Бот обновил задачи ({get_moscow_now().strftime("%H:%M")})',
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
            log.info(f'✅ GitHub sync OK: {len(tasks_list)} задач')
        else:
            log.error(f'❌ GitHub sync failed: {r.status_code} — {r.text[:200]}')
    except Exception as e:
        log.error(f'❌ GitHub sync error: {e}')


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
            cur = conn.cursor()
            cur.execute('SELECT title FROM tasks WHERE user_id = %s', (VK_USER_ID,))
            existing = {row[0].lower() for row in cur.fetchall()}

            added = 0
            for t in tasks:
                if t.get('done'):
                    continue
                if t['title'].lower() not in existing:
                    cur.execute(
                        'INSERT INTO tasks (user_id, title, note, deadline, priority, category, time) VALUES (%s, %s, %s, %s, %s, %s, %s)',
                        (VK_USER_ID, t['title'], t.get('note', ''), t.get('date', ''), t.get('priority', 'medium'), t.get('category', ''), t.get('time', ''))
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
    if isinstance(remind_at, str):
        remind_at = datetime.strptime(remind_at, '%Y-%m-%d %H:%M:%S')
        remind_at = MOSCOW_TZ.localize(remind_at)
    if remind_at.tzinfo is None:
        remind_at = MOSCOW_TZ.localize(remind_at)
    remind_at_utc = remind_at.astimezone(pytz.UTC)
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT INTO reminders (task_id, remind_at) VALUES (%s, %s)', (task_id, remind_at_utc.strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()

def get_pending_reminders():
    conn = get_db()
    cur = conn.cursor()
    now = datetime.now(pytz.UTC).isoformat()
    cur.execute(
        'SELECT r.id, r.task_id, r.remind_at, t.title, t.user_id FROM reminders r '
        'JOIN tasks t ON r.task_id = t.id WHERE r.sent = 0 AND r.remind_at <= %s',
        (now,)
    )
    columns = [desc[0] for desc in cur.description]
    rows = cur.fetchall()
    conn.close()
    return [dict(zip(columns, row)) for row in rows]

def mark_reminder_sent(reminder_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE reminders SET sent = 1 WHERE id = %s', (reminder_id,))
    conn.commit()
    conn.close()

def parse_reminder_time(text):
    t = text.lower().strip()
    moscow_now = get_moscow_now()

    match = re.match(r'(?:напомни )?через (\d+) (минут[уыа]?|секунд[уыа]?|час[аов]*) (.+)', t)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        task_text = match.group(3)
        if 'секунд' in unit:
            remind_at = moscow_now + timedelta(seconds=amount)
        elif 'час' in unit:
            remind_at = moscow_now + timedelta(hours=amount)
        else:
            remind_at = moscow_now + timedelta(minutes=amount)
        return remind_at, task_text

    match = re.match(r'(?:напомни )?завтра (\d{1,2}[:\.]?\d{0,2}) (.+)', t)
    if match:
        time_str = match.group(1).replace('.', ':')
        task_text = match.group(2)
        if ':' not in time_str:
            time_str += ':00'
        h, m = map(int, time_str.split(':'))
        remind_at = moscow_now.replace(hour=h, minute=m, second=0) + timedelta(days=1)
        return remind_at, task_text

    match = re.match(r'(?:напомни )?сегодня (\d{1,2}[:\.]?\d{0,2}) (.+)', t)
    if match:
        time_str = match.group(1).replace('.', ':')
        task_text = match.group(2)
        if ':' not in time_str:
            time_str += ':00'
        h, m = map(int, time_str.split(':'))
        remind_at = moscow_now.replace(hour=h, minute=m, second=0)
        if remind_at < moscow_now:
            remind_at += timedelta(days=1)
        return remind_at, task_text

    match = re.match(r'(?:напомни )?в (\d{1,2})[:\.]?(\d{0,2}) (.+)', t)
    if match:
        h = int(match.group(1))
        m = int(match.group(2)) if match.group(2) else 0
        task_text = match.group(3)
        remind_at = moscow_now.replace(hour=h, minute=m, second=0)
        if remind_at < moscow_now:
            remind_at += timedelta(days=1)
        return remind_at, task_text

    return None, None


# === AI ===

SYSTEM_PROMPT = """Ты — «Старший Партнёр» (Managing Partner) венчурного фонда, который лично отвечает за портфельные компании. У тебя репутация жёсткого, но справедливого наставника. Ты ненавидишь пустую болтовню, но обожаешь, когда кто-то начинает действовать. Твоя главная цель — запустить двигатель пользователя и не давать ему глохнуть.

СТИЛЬ ОБЩЕНИЯ:
- Деловой, короткий, без эмодзи и смайлов.
- Похвала = сухое «Принято», «Годится» или «Ты жив, и это уже прогресс».
- Критика = холодный факт. Цифры и время: «Ты потратил на это 2 дня, хотя я просил 4 часа. Почему?»
- Обращайся на «Ты».
- Максимум 3-5 предложений в ответе.
- Каждое сообщение заканчивай вопросом, на который пользователь обязан ответить действием или конкретным временем.

ТЫ УМЕЕШЬ:

1. ДОБАВЛЯТЬ ЗАДАЧИ — ответь форматом: [ADD: название, дата, приоритет, время]
   - дата: сегодня, завтра, 2026-07-20, или пусто
   - приоритет: high (срочно), medium (важно), low (потом)
   - время: HH:MM или пусто
   - Пример: [ADD: Отчёт по рекламе, завтра, high, 14:00]
   - Пример без времени: [ADD: Позвонить маме, сегодня, medium]

2. ОТМЕЧАТЬ ВЫПОЛНЕННЫЕ — ответь: [DONE: номер задачи]
   - Если пользователь говорит "сделал", "выполнил", "готово" — найди задачу и отметь

3. УДАЛЯТЬ — ответь: [DEL: номер задачи]

4. НАПОМИНАНИЯ — когда пользователь просит напомнить:
   - "напомни завтра в 9 саморезы" → [ADD: саморезы, завтра, medium, 09:00]
   - "напомни через 30 минут позвонить" → вычисли время и [ADD: позвонить, сегодня, medium, HH:MM]
   - "напомни в 15:00 отчёт" → [ADD: отчёт, сегодня, medium, 15:00]
   - "через 5 минут рыбалка" → вычисли текущее время + 5 минут и [ADD: рыбалка, сегодня, medium, HH:MM]
   - ВСЕГДА указывай время в [ADD] когда просят напомнить!

5. ПРОВЕРКА ЗАДАЧ:
   - Если задача не выполнена — требуй письменный анализ причины. Ужесточай дедлайн на сегодня, добавляй +1 задачу как штраф.
   - Если задача выполнена — сухо констатируй: «Ок, актив завершен» и переходи к следующему.

6. ПОСТАНОВКА ЗАДАЧ:
   - Никогда не спрашивай «Что будешь делать?». Говори: «Судя по прогрессу, твой следующий шаг — [твоё предложение]. Согласен?»
   - Используй правило «3 + 1»: три текущие задачи на день + одна главная цель.

7. АНАЛИЗ:
   - Раз в 3-4 сообщения делай «срез»: «Смотри, за последние 3 дня ты сделал X. Слабая зона — Y. Рекомендую выделить на Y в 2 раза больше времени завтра.»

8. ПОНИМАНИЕ ВРЕМЕНИ:
   - "в 9 утра" = 09:00
   - "в 15:00" = 15:00
   - "через 2 часа" = текущее время + 2 часа
   - "завтра в 10" = завтра 10:00
   - "через 5 минут" = текущее время + 5 минут
   - ВСЕГДА учитывай время когда создаёшь задачу
   - СЕГОДНЯ: {сегодняшняя дата}

ТЕХНИЧЕСКИЙ МАНДАТ:
- Если пользователь пишет что-то абстрактное («надо бы заняться...») — обрываешь: «Стоп. Конкретизируй. Что именно? Когда?»
- Не даёшь длинных лекций.
- КРИТИЧЕСКИ ВАЖНО: Когда пользователь просит добавить задачу, напомнить, или говорит что-то вроде "напомни", "добавь", "поставь задачу" — ТЫ ОБЯЗАН ответить [ADD: ...]. НИКОГДА не отвечай просто "Хорошо, добавлю" без [ADD: ...]. Формат: [ADD: название, дата, приоритет, время]

ВАЖНО: Всегда отвечай на русском."""

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

Сегодня: {get_moscow_now().strftime('%d.%m.%Y')}"""

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

        # Убираем команды из ответа чтобы пользователь не видел [ADD: ...]
        clean = re.sub(r'\[ADD:\s*.+?\]', '', reply)
        clean = re.sub(r'\[DONE:\s*\d+\]', '', clean)
        clean = re.sub(r'\[DEL:\s*\d+\]', '', clean)
        clean = clean.strip()
        if not clean:
            clean = 'Готово!'
        return clean
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
        time_str = parts[3].strip() if len(parts) > 3 else None

        # Если время не в 4м параметре — ищем в заголовке
        if not time_str or not re.match(r'\d{1,2}:\d{2}', time_str):
            full_text = title + ' ' + (deadline or '')
            time_match = re.search(r'в (\d{1,2})[:\.]?(\d{0,2})', full_text.lower())
            if time_match:
                h = int(time_match.group(1))
                m = int(time_match.group(2)) if time_match.group(2) else 0
                if 0 <= h <= 23 and 0 <= m <= 59:
                    time_str = f'{h:02d}:{m:02d}'
        # Валидируем время из 4го параметра
        elif not re.match(r'^\d{1,2}:\d{2}$', time_str):
            time_str = None

        # Парсим дату
        moscow_now = get_moscow_now()
        if deadline == 'сегодня' or (deadline and 'сегодня' in deadline.lower()):
            deadline = moscow_now.strftime('%Y-%m-%d')
        elif deadline == 'завтра' or (deadline and 'завтра' in deadline.lower()):
            deadline = (moscow_now + timedelta(days=1)).strftime('%Y-%m-%d')
        elif deadline and re.match(r'\d{4}-\d{2}-\d{2}', deadline):
            pass
        elif deadline and re.match(r'\d{2}\.\d{2}', deadline):
            day, month = deadline.split('.')
            deadline = f'{moscow_now.year}-{month}-{day}'
        else:
            deadline = moscow_now.strftime('%Y-%m-%d')

        # Убираем время из названия задачи
        clean_title = re.sub(r'в \d{1,2}[:\.]?\d{0,2}\s*(утра|вечера|дня)?', '', title, flags=re.IGNORECASE).strip()
        if not clean_title:
            clean_title = title

        task_id = add_task(user_id, clean_title, deadline, priority, time=time_str)

        # Если было время — создаём напоминание
        if time_str and task_id:
            try:
                h, m = map(int, time_str.split(':'))
                remind_dt = moscow_now.replace(hour=h, minute=m, second=0)
                if remind_dt < moscow_now:
                    remind_dt += timedelta(days=1)
                add_reminder(task_id, remind_dt.strftime('%Y-%m-%d %H:%M:%S'))
            except Exception:
                pass

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
            deadline = get_moscow_now().strftime('%Y-%m-%d')
        elif 'завтра' in lower:
            deadline = (get_moscow_now() + timedelta(days=1)).strftime('%Y-%m-%d')

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
            # Убираем "напомни мне", "напомни" из текста задачи
            clean = re.sub(r'^напомни\s+(мне\s+)?', '', task_text, flags=re.IGNORECASE).strip()
            if not clean:
                clean = task_text
            time_str = remind_at.strftime('%H:%M')
            task_id = add_task(user_id, clean, remind_at.strftime('%Y-%m-%d'), 'medium', time=time_str)
            add_reminder(task_id, remind_at.strftime('%Y-%m-%d %H:%M:%S'))
            return f'⏰ Напоминание в {time_str}: {clean}'
        return 'Не понял время.\nПример: напомни через 30 минут позвонить'

    if t in ['напоминания', 'активные напоминания']:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'SELECT r.remind_at, t.title FROM reminders r '
            'JOIN tasks t ON r.task_id = t.id '
            'WHERE t.user_id = %s AND r.sent = 0 ORDER BY r.remind_at',
            (user_id,)
        )
        rows = cur.fetchall()
        conn.close()
        if not rows:
            return 'Активных напоминаний нет.'
        lines = ['⏰ Напоминания:\n']
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {str(row[0])[:16]} — {row[1]}")
        return '\n'.join(lines)

    return None

def handle_help():
    return (
        'Команды:\n\n'
        'Задачи:\n'
        '• добавить [текст] — новая задача\n'
        '• задачи — список задач\n'
        '• выполнено [номер] — отметить\n'
        '• удалить [номер] — удалить\n'
        '• отчёт — статистика\n\n'
        'Напоминания:\n'
        '• напомни через 30 минут [текст]\n'
        '• напомни завтра 10:00 [текст]\n'
        '• напоминания — список\n\n'
        'MiMo Code:\n'
        '• мимо [задача] — отправить задачу\n'
        '• мимо задачи — очередь задач\n'
        '• мимо статус — сколько в очереди\n'
        '• мимо очистить — удалить завершённые\n'
        '• мимо вкл / выкл — включить/выключить\n\n'
        'AI:\n'
        '• Любой вопрос — ответ от AI\n'
        '• Помощь — этот список'
    )


# === Фоновые задачи ===

vk_session_ref = None

def send_vk_message(user_id, text):
    """Отправляет сообщение через VK API"""
    try:
        if not vk_session_ref:
            log.error('❌ VK сессия не инициализирована')
            return False
        api = vk_session_ref.get_api()
        api.messages.send(
            user_id=user_id,
            message=text,
            random_id=get_random_id()
        )
        log.info(f'📤 VK сообщение отправлено: {text[:50]}...')
        return True
    except Exception as e:
        log.error(f'❌ VK send error: {e}')
        return False

def check_reminders():
    """Проверяет напоминания каждую минуту"""
    while True:
        try:
            pending = get_pending_reminders()
            if pending:
                log.info(f'📋 Найдено {len(pending)} напоминаний')
            for r in pending:
                log.info(f'⏰ Отправляю напоминание: {r["title"]} для {r["user_id"]}')
                send_vk_message(r['user_id'], f'⏰ Напоминание: {r["title"]}')
                mark_reminder_sent(r['id'])
                log.info(f'✅ Напоминание отправлено: {r["title"]}')
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


# === Автоотчёт утром и вечером ===

_report_sent_today = {}

def check_daily_report():
    """Отправляет отчёт утром (9:00) и вечером (21:00)"""
    while True:
        try:
            now = get_moscow_now()
            today = now.strftime('%Y-%m-%d')
            hour = now.hour
            minute = now.minute

            # Утренний отчёт в 9:00 (окно 9:00-9:02)
            morning_key = f'{today}_morning'
            if hour == 9 and minute < 3 and morning_key not in _report_sent_today:
                _report_sent_today[morning_key] = True
                send_morning_report(VK_USER_ID)

            # Вечерний отчёт в 21:00 (окно 21:00-21:02)
            evening_key = f'{today}_evening'
            if hour == 21 and minute < 3 and evening_key not in _report_sent_today:
                _report_sent_today[evening_key] = True
                send_evening_report(VK_USER_ID)

        except Exception as e:
            log.error(f'Daily report error: {e}')
        time.sleep(60)


def send_morning_report(user_id):
    """Утренний отчёт: что сегодня делаем"""
    stats = get_stats(user_id)
    tasks = get_tasks(user_id)
    today_str = get_moscow_now().strftime('%Y-%m-%d')
    today_tasks = [t for t in tasks if t.get('deadline') == today_str]
    overdue = [t for t in tasks if t.get('deadline') and t['deadline'] < today_str]

    lines = ['☀️ Доброе утро! План на сегодня:']

    if today_tasks:
        lines.append(f'📋 Дел: {len(today_tasks)}')
        for t in today_tasks:
            time_str = f" в {t['time']}" if t.get('time') else ''
            lines.append(f'  • {t["title"]}{time_str}')
    else:
        lines.append('📋 Задач нет — добавь что-нибудь!')

    if overdue:
        lines.append(f'\n⚠️ Просрочено: {len(overdue)}')
        for t in overdue[:3]:
            lines.append(f'  • {t["title"]} ({t["deadline"]})')

    lines.append(f'\n📊 Всего: {stats["done"]}✅ / {stats["active"]}⏳')
    lines.append('\n💪 Давай, ты сможешь!')

    msg = '\n'.join(lines)
    send_vk_message(user_id, msg)
    log.info('☀️ Утренний отчёт отправлен')


def send_evening_report(user_id):
    """Вечерний отчёт: итоги дня"""
    stats = get_stats(user_id)
    today_str = get_moscow_now().strftime('%Y-%m-%d')
    tasks = get_tasks(user_id)
    today_tasks = [t for t in tasks if t.get('deadline') == today_str]
    done_today = [t for t in today_tasks if t['status'] == 'done']
    pending_today = [t for t in today_tasks if t['status'] == 'active']

    lines = ['🌙 Итоги дня:']

    if done_today:
        lines.append(f'✅ Выполнено: {len(done_today)}')
        for t in done_today:
            lines.append(f'  • {t["title"]}')
    else:
        lines.append('✅ Сегодня ничего не выполнено')

    if pending_today:
        lines.append(f'\n⏳ Осталось: {len(pending_today)}')
        for t in pending_today[:3]:
            time_str = f" в {t['time']}" if t.get('time') else ''
            lines.append(f'  • {t["title"]}{time_str}')

    lines.append(f'\n📊 Всего: {stats["done"]}✅ / {stats["active"]}⏳')

    if len(done_today) >= 3:
        lines.append('\n🔥 Ты молодец, отличный день!')
    elif len(done_today) > 0:
        lines.append('\n👍 Неплохо, завтра ещё больше!')
    else:
        lines.append('\n💪 Завтра обязательно получится!')

    msg = '\n'.join(lines)
    send_vk_message(user_id, msg)
    log.info('🌙 Вечерний отчёт отправлен')


# === Обработка сообщений ===

def handle_message(event, api):
    text = event.obj.message['text'].strip()
    user_id = event.obj.message['from_id']

    if not text:
        return

    # Сохраняем сообщение пользователя
    save_message(user_id, 'user', text)

    reply = None
    config = load_mimo_config()

    # Если MiMo режим включён
    if config.get('enabled', True):
        t = text.lower().strip()

        # Команды управления (всегда работают)
        if t in ['мимо вкл', 'mimo on', 'мимо выключить', 'мимо включить']:
            reply = handle_mimo(text, user_id)
        elif t in ['мимо выкл', 'mimo off', 'мимо отключить']:
            reply = handle_mimo(text, user_id)
        elif t in ['мимо режим', 'mimo режим']:
            reply = handle_mimo(text, user_id)
        elif t in ['мимо задачи', 'mimo задачи', 'очередь мимо', 'мимо статус', 'mimo статус']:
            reply = handle_mimo(text, user_id)
        elif t in ['мимо очистить', 'mimo очистить', 'мимо clear']:
            reply = handle_mimo(text, user_id)
        # ЛЮБОЕ ДРУГОЕ СООБЩЕНИЕ → задача для MiMo Code
        else:
            task_id = add_mimo_task(user_id, text)
            reply = f'✅ Принято! Задача #{task_id} ушла в MiMo Code.\n\n«{text[:80]}»\n\n⏳ Результат пришлётся через ~5 мин.'
    else:
        # MiMo выключен — работаем как обычный бот
        if text.lower() in ['помощь', 'помоги', '!помощь', 'команды', 'help']:
            reply = handle_help()
        if not reply:
            reply = handle_tasks(text, user_id)
        if not reply:
            reply = handle_reminder(text, user_id)
        if not reply:
            reply = handle_mimo(text, user_id)

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
.done-btn{background:#4caf50;color:#fff;border:none;border-radius:4px;padding:4px 8px;font-size:11px;cursor:pointer;margin-left:auto;white-space:nowrap}
.done-btn:hover{background:#66bb6a}
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
<span style="color:#555;font-size:11px;margin-left:auto">v{VERSION} | {BUILD_TIME}</span>
</div>
<div class="container">
<div style="background:#16213e;border-radius:8px;padding:12px 16px;margin-bottom:16px;display:flex;align-items:center;gap:8px;font-size:12px;color:#888"><div style="width:8px;height:8px;border-radius:50%;background:#4caf50"></div><span id="sync-text">Данные: Supabase PostgreSQL</span></div>
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
let btn=t.status=='active'?'<button class="done-btn" onclick="done('+t.id+')">&#10003; Готово</button>':'&#10004;';
th+='<div class="task '+cls+'"><span>'+pri+'</span><span>#'+t.id+'</span><span>'+esc(t.title)+'</span><span style="color:#666">'+(t.deadline||'')+(t.time?' в '+t.time:'')+'</span>'+btn+'</div>';
});
document.getElementById('tasks').innerHTML=th||'<div class="ts">Нет задач</div>';
}).catch(()=>{document.getElementById('messages').innerHTML='<div class="ts">Бот не отвечает (возможно засыпает)</div>'});
}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function done(id){
fetch('/api/complete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:id})})
.then(r=>r.json()).then(()=>load()).catch(()=>alert('Ошибка'));
}
load();
setInterval(load,30000);
</script>
</body>
</html>'''

DASHBOARD_HTML = DASHBOARD_HTML.replace('{VERSION}', VERSION).replace('{BUILD_TIME}', BUILD_TIME)

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

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            if path == '/api/complete':
                task_id = body.get('task_id')
                if task_id:
                    success = complete_task(VK_USER_ID, int(task_id))
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({'ok': success}).encode())
                else:
                    self.send_response(400)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _get_api_data(self):
        conn = get_db()
        cur = conn.cursor()
        try:
            msgs = []
            cur.execute(
                "SELECT role, message, created_at FROM conversations ORDER BY id DESC LIMIT 30"
            )
            columns = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                row_dict = dict(zip(columns, row))
                t = str(row_dict.get('created_at', ''))
                row_dict['created_at'] = t  # datetime → str для JSON
                try:
                    from datetime import datetime as dt2
                    utc = dt2.strptime(t[:19], '%Y-%m-%d %H:%M:%S')
                    local = utc + timedelta(hours=3)
                    row_dict['time'] = local.strftime('%H:%M')
                    row_dict['date'] = local.strftime('%d.%m')
                except Exception:
                    row_dict['time'] = t[11:16] if len(t) > 16 else t[:5]
                    row_dict['date'] = t[:10]
                msgs.append(row_dict)
            msgs.reverse()

            cur.execute(
                "SELECT id, title, priority, deadline, time, status FROM tasks WHERE user_id = %s ORDER BY id DESC LIMIT 50",
                (VK_USER_ID,)
            )
            task_columns = [desc[0] for desc in cur.description]
            tasks = []
            for row in cur.fetchall():
                t = dict(zip(task_columns, row))
                # Конвертируем datetime в строки для JSON
                for k, v in t.items():
                    if v is not None and not isinstance(v, (str, int, float, bool)):
                        t[k] = str(v)
                tasks.append(t)

            cur.execute("SELECT COUNT(*) FROM tasks WHERE user_id = %s AND status = 'active'", (VK_USER_ID,))
            active = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM tasks WHERE user_id = %s AND status = 'done'", (VK_USER_ID,))
            done = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM tasks WHERE user_id = %s AND status = 'active' AND deadline = CURRENT_DATE::text", (VK_USER_ID,))
            today = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM tasks WHERE user_id = %s AND status = 'active' AND deadline < CURRENT_DATE::text", (VK_USER_ID,))
            overdue = cur.fetchone()[0]
            stats = {
                'active': active,
                'done': done,
                'today': today,
                'overdue': overdue,
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
    _github_loaded = True
    log.info('✅ GitHub загружен, sync разрешён')

    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))

    vk_session = vk_api.VkApi(token=TOKEN)
    vk_session_ref = vk_session
    longpoll = VkBotLongPoll(vk_session, GROUP_ID)

    log.info(f'🚀 Бот запущен! Группа: {GROUP_ID}')

    # Тестовое сообщение при старте — проверяем что VK работает
    try:
        send_vk_message(VK_USER_ID, '🟢 Бот запущен и готов к работе!')
        log.info('✅ Тестовое сообщение отправлено')
    except Exception as e:
        log.error(f'❌ Тестовое сообщение не отправлено: {e}')

    # Health check для Render — ЗАПУСКАЕМ ПЕРВЫМ
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()
    log.info(f'✅ Health check на порту {PORT}')

    # Фоновые потоки
    threading.Thread(target=check_reminders, daemon=True).start()
    threading.Thread(target=periodic_sync, daemon=True).start()
    threading.Thread(target=check_daily_report, daemon=True).start()

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
