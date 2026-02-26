# Daily Report Automation

Автоматически заполняет ежедневную таблицу отчёта (Астана) данными из AmoCRM и Google Sheets.

## Что заполняется

| Столбец | Поле | Источник |
|---------|------|----------|
| R | New sales purch 1d-3d | AmoCRM — сделки закрыты за нужный день, город=Астана, отдел=Оффлайн, дельта создание→закрытие ≤ 3 дней |
| S | New sales purch total | AmoCRM — все сделки закрыты за нужный день, город=Астана, отдел=Оффлайн |
| T | Expires | AmoCRM — лиды в воронках подписок с датой окончания = нужный день, город=Астана, отдел=Оффлайн |
| U | Upsales purch | Google Sheets — кол-во «Повторные продажи» за нужный день по всем листам сотрудников |
| V | New sales revenue | Google Sheets, лист «План еженедельный» — D47+D48 за нужный день |
| W | Upsales revenue | Google Sheets, лист «План еженедельный» — D49+D50 за нужный день |

## Установка

### 1. Зависимости

```bash
pip install -r requirements.txt
```

### 2. Конфигурация

```bash
cp .env.example .env
```

Открой `.env` и заполни все переменные (подробные комментарии внутри файла).

**Обязательные шаги:**

**AmoCRM:**
- Зайди в настройки AmoCRM → Интеграции, создай интеграцию и скопируй `access_token`
- Запусти `python helper_find_field_ids.py` — скрипт выведет все ID полей и воронок
- Найди ID полей «Город», «Отдел», «Дата окончания занятий» и вставь в `.env`
- Найди ID статуса «Договор подписан» / «Успешно реализовано» и вставь в `AMO_WON_STATUS_IDS`

**Google Sheets:**
- Перейди на [Google Cloud Console](https://console.cloud.google.com/)
- Создай сервисный аккаунт (IAM → Сервисные аккаунты → Создать → создай ключ JSON)
- Сохрани JSON-файл как `service_account.json` рядом со скриптом
- **Выдай сервисному аккаунту доступ «Редактор»** к обеим таблицам (поделиться по email аккаунта)
- Укажи ID таблиц в `.env` (берётся из URL: `docs.google.com/spreadsheets/d/**ВОТ_ЭТО**/edit`)

### 3. Проверка

```bash
# Запустить за вчерашний день
python main.py

# Запустить за конкретную дату
python main.py --date 25.02.2026
```

## Автозапуск каждый день в 10:00

### Вариант A — встроенный планировщик (рекомендуется для VPS/сервера)

```bash
python main.py --schedule
```

Запусти в фоне (например через `screen`, `tmux` или systemd).

### Вариант B — cron (классический)

```bash
crontab -e
```

Добавь строку (замени `/path/to` на реальный путь):

```
0 10 * * * cd /path/to/daily && /usr/bin/python3 main.py >> /var/log/daily_report.log 2>&1
```

### Вариант C — systemd service (для Ubuntu/Debian сервера)

Создай файл `/etc/systemd/system/daily-report.service`:

```ini
[Unit]
Description=Daily Report Automation
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/daily
EnvironmentFile=/path/to/daily/.env
ExecStart=/usr/bin/python3 /path/to/daily/main.py --schedule
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
```

Затем:

```bash
sudo systemctl daemon-reload
sudo systemctl enable daily-report
sudo systemctl start daily-report
sudo systemctl status daily-report
```

## Структура проекта

```
daily/
├── main.py                  # Точка входа, планировщик
├── amo_client.py            # Клиент AmoCRM API v4
├── sheets_client.py         # Клиент Google Sheets
├── config.py                # Загрузка конфигурации из .env
├── helper_find_field_ids.py # Утилита для поиска ID полей AmoCRM
├── requirements.txt
├── .env.example             # Шаблон конфигурации
└── service_account.json     # (создать самостоятельно, не коммитить!)
```

## Важно

- Файл `service_account.json` и `.env` **никогда не коммить** в git (они в `.gitignore`)
- Сервисный аккаунт должен иметь доступ к обеим Google-таблицам
- AmoCRM access token обычно действует 24 часа — настрой [автообновление токена](https://www.amocrm.ru/developers/content/oauth/oauth) если нужно долгосрочная работа
