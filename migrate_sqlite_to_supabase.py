"""
migrate_sqlite_to_supabase.py — Перенос данных из SQLite в Supabase PostgreSQL
Запуск: python3 migrate_sqlite_to_supabase.py
"""
import os
import sys
import sqlite3
import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

SUPABASE_KEY = os.getenv('SUPABASE_KEY')
DB_FILE = os.path.join(os.path.dirname(__file__), 'tasks.db')


def migrate():
    if not os.path.exists(DB_FILE):
        print(f'❌ Файл {DB_FILE} не найден. Нечего мигрировать.')
        return

    if not SUPABASE_KEY:
        print('❌ SUPABASE_KEY не задан в .env')
        return

    print(f'📂 Читаю SQLite: {DB_FILE}')
    sqlite_conn = sqlite3.connect(DB_FILE)
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg2.connect(SUPABASE_KEY)
    pg_conn.autocommit = False

    try:
        # Миграция tasks
        tasks = sqlite_conn.execute('SELECT * FROM tasks').fetchall()
        print(f'📋 Задач в SQLite: {len(tasks)}')

        if tasks:
            cur = pg_conn.cursor()
            for t in tasks:
                cur.execute('''
                    INSERT INTO tasks (id, user_id, title, note, deadline, time, priority, category, status, created_at, completed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                ''', (
                    t['id'], t['user_id'], t['title'], t['note'] or '',
                    t['deadline'], t['time'] or '', t['priority'] or 'medium',
                    t['category'] or '', t['status'] or 'active',
                    t['created_at'], t['completed_at']
                ))
            pg_conn.commit()
            print(f'✅ Задач перенесено: {len(tasks)}')

        # Миграция reminders
        reminders = sqlite_conn.execute('SELECT * FROM reminders').fetchall()
        print(f'⏰ Напоминаний в SQLite: {len(reminders)}')

        if reminders:
            cur = pg_conn.cursor()
            for r in reminders:
                cur.execute('''
                    INSERT INTO reminders (id, task_id, remind_at, sent)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                ''', (r['id'], r['task_id'], r['remind_at'], r['sent'] or 0))
            pg_conn.commit()
            print(f'✅ Напоминаний перенесено: {len(reminders)}')

        # Миграция conversations
        conversations = sqlite_conn.execute('SELECT * FROM conversations').fetchall()
        print(f'💬 Сообщений в SQLite: {len(conversations)}')

        if conversations:
            cur = pg_conn.cursor()
            for c in conversations:
                cur.execute('''
                    INSERT INTO conversations (id, user_id, role, message, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                ''', (c['id'], c['user_id'], c['role'], c['message'], c['created_at']))
            pg_conn.commit()
            print(f'✅ Сообщений перенесено: {len(conversations)}')

        # Сброс sequence (чтобы ID шли дальше)
        cur = pg_conn.cursor()
        cur.execute("SELECT setval('tasks_id_seq', (SELECT COALESCE(MAX(id), 1) FROM tasks))")
        cur.execute("SELECT setval('reminders_id_seq', (SELECT COALESCE(MAX(id), 1) FROM reminders))")
        cur.execute("SELECT setval('conversations_id_seq', (SELECT COALESCE(MAX(id), 1) FROM conversations))")
        pg_conn.commit()

        print('\n🎉 Миграция завершена!')
        print('📊 Итого:')
        print(f'   Задач: {len(tasks)}')
        print(f'   Напоминаний: {len(reminders)}')
        print(f'   Сообщений: {len(conversations)}')

    except Exception as e:
        pg_conn.rollback()
        print(f'❌ Ошибка миграции: {e}')
        raise
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == '__main__':
    migrate()
