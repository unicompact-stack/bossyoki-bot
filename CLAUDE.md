# CLAUDE.md — BossYoki

## Описание проекта
BossYoki — проактивный тайм-менеджер. VK-бот с AI, который напоминает о задачах и помогает доводить дела до конца.

## Стек технологий
- **VK API** — для общения с пользователем
- **GitHub Models** — для AI-функций (gpt-4o-mini, бесплатно)
- **Python 3** — основной язык
- **psycopg2** — подключение к PostgreSQL (Supabase)
- **pytz** — работа с часовыми поясами (Europe/Moscow)
- **APScheduler** — планировщик напоминаний
- **requests** — HTTP-запросы

## Хранилище данных
- **Supabase PostgreSQL** — основная база (tasks, reminders, conversations, mimo_tasks)
- **GitHub tasks.json** — синхронизация задач (unicompact-stack/bossyoki)
- **Render.com** — хостинг бота (Free tier)

## Структура папок
- `smart_bot.py` — ГЛАВНЫЙ файл бота (1235+ строк, монолит)
- `code/` — модульная версия (пока не используется на Render)
- `migrate_sqlite_to_supabase.py` — скрипт миграции данных
- `idea/` — замысел проекта
- `architecture/` — чертёж проекта
- `business/` — база знаний о бизнесе
- `memory/` — память агента (feedback, правила)

## Роутинг
- Вопрос про «задачи / функции» → `smart_bot.py`
- Вопрос про «архитектуру / структуру» → `architecture/`
- Вопрос про «бизнес / аналитику» → `business/`
- Вопрос про «идею / видение» → `idea/`

## Ключевые ограничения
1. **GitHub Token** — используется для AI (GitHub Models) и sync задач
2. **.env** — хранить локально, НЕ коммитить (содержит секреты)
3. **Безопасность** — проверять перед деплоем
4. **Репозиторий для Render** — `unicompact-stack/bossyoki-bot` (НЕ bossyoki!)

## Важно для новой сессии

### Где что лежит
| Что | Где |
|-----|-----|
| Код бота | `smart_bot.py` |
| Настройки | `.env` (SUPABASE_KEY, VK_TOKEN, GITHUB_TOKEN) |
| Деплой | Render.com → bossyoki-bot |
| БД | Supabase PostgreSQL (wyfwofsotrijlahoupau) |
| GitHub sync | unicompact-stack/bossyoki/tasks.json |
| Дашборд | https://bossyoki-bot.onrender.com |

### Текущие проблемы (над чем работать)
1. **Render не подхватывает код автоматически** — нужен Manual Deploy после каждого изменения
2. **AI не всегда создаёт задачи через [ADD: ...]** — иногда просто пишет "Добавлю" без реального создания
3. **Часовой пояс** — бот использует UTC на Render, показывает московское время через +3 часа (фикс в v2.1.0)
4. **code/ версия** — модульная версия не задеплоена, нужна миграция

### Как деплоить
1. Изменить `smart_bot.py` локально
2. `git add smart_bot.py && git commit -m "описание"`
3. Создать чистую ветку: `git checkout -b branch-name bossyoki-bot/main`
4. Скопировать файлы: `git checkout main -- smart_bot.py requirements.txt`
5. Запушить: `git push bossyoki-bot branch-name:main`
6. В Render: Manual Deploy → Clear build cache & deploy

### Версии
| Версия | Что |
|--------|-----|
| v2.2.0 | MiMo Code интеграция (таблица mimo_tasks) |
| v2.1.1 | Версия в дашборде + timezone fix |
| v2.1.0 | Timezone fix (pytz, Europe/Moscow) |
| v2.0.0 | Миграция SQLite → Supabase PostgreSQL |
| v1.0.0 | Оригинальный бот на SQLite |