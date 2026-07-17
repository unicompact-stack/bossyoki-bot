"""Точка входа — запуск бота BossYoki."""

import os
import sys
import signal
import threading
from http.server import HTTPServer

from utils.config import load_config
from utils.logging_config import setup_logging
from database.models import init_db
from database.database import Database
from bot_instance import VKBot
from scheduler import Scheduler
from utils.sync import GitHubSync
from handlers.dashboard import DashboardHandler
from handlers.tasks import handle_tasks
from handlers.reminders import handle_reminder
from handlers.ai import handle_ai


def main():
    log = setup_logging(os.path.dirname(__file__))
    config = load_config()

    def stop(sig, frame):
        log.info('Остановлен.')
        pid_file = os.path.join(os.path.dirname(__file__), 'smart_bot.pid')
        if os.path.exists(pid_file):
            os.remove(pid_file)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    init_db()
    db = Database()
    sync = GitHubSync(config, db)
    sync.load_from_github()

    pid_file = os.path.join(os.path.dirname(__file__), 'smart_bot.pid')
    with open(pid_file, 'w') as f:
        f.write(str(os.getpid()))

    vk_bot = VKBot(config)
    vk_bot.connect()

    log.info(f'Бот запущен! Группа: {config["group_id"]}')

    vk_bot.send_message(config['vk_user_id'], '🟢 Бот запущен и готов к работе!')

    # Health check для Render
    def start_health_server():
        try:
            handler = lambda *args, **kwargs: DashboardHandler(*args, db=db, user_id=config['vk_user_id'], **kwargs)
            server = HTTPServer(('0.0.0.0', config['port']), handler)
            print(f'Dashboard started on port {config["port"]}', flush=True)
            server.serve_forever()
        except Exception as e:
            print(f'Dashboard server error: {e}', flush=True)

    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()
    log.info(f'Health check на порту {config["port"]}')

    # Фоновые задачи
    scheduler = Scheduler(db, vk_bot, sync, config)
    threading.Thread(target=scheduler.check_reminders, daemon=True).start()
    threading.Thread(target=scheduler.periodic_sync, daemon=True).start()
    threading.Thread(target=scheduler.check_daily_report, daemon=True).start()

    # Обработка сообщений
    for event in vk_bot.listen():
        if event.type == vk_bot.get_event_type():
            text = event.obj.message.get('text', '').strip()
            user_id = event.obj.message['from_id']

            if not text:
                continue

            log.info(f'← {user_id}: {text}')
            db.save_message(user_id, 'user', text)

            reply = None

            if text.lower() in ['помощь', 'помоги', '!помощь', 'команды', 'help']:
                reply = (
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

            if not reply:
                reply = handle_tasks(text, user_id, db)
            if not reply:
                reply = handle_reminder(text, user_id, db)
            if not reply:
                reply = handle_ai(text, user_id, db, config)

            if not reply:
                reply = 'Не понял. Напиши "помощь" для списка команд.'

            db.save_message(user_id, 'assistant', reply)
            vk_bot.send_message(user_id, reply)
            log.info(f'→ {user_id}: {reply[:100]}')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'stop':
        pid_file = os.path.join(os.path.dirname(__file__), 'smart_bot.pid')
        if os.path.exists(pid_file):
            with open(pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            print(f'Остановлен (PID {pid})')
        else:
            print('Бот не запущен')
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == 'log':
        log_file = os.path.join(os.path.dirname(__file__), 'bossyoki.log')
        if os.path.exists(log_file):
            os.system(f'tail -30 "{log_file}"')
        else:
            print('Логов нет')
        sys.exit(0)

    main()
