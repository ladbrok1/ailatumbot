# AILatumBot — Valorant Tracker + AI Coach

**Telegram bot для отслеживания профилей Valorant с Smart Analytics и оповещениями.**

---

## ⚡ Быстрый старт

### 1. Установка зависимостей
```bash
pip install -r requirements.txt
```

### 2. Переменные окружения
```bash
export TELEGRAM_TOKEN="ваш_token_от_BotFather"
export GROQ_API_KEY="ваш_API_key_от_Groq"
export HENRIK_API_KEY="ваш_API_key_от_HenrikDev"
export BOT_USERNAME="@ваше_имя_бота"
export PORT="10000"  # для health check (опционально)
```

### 3. Запуск
```bash
python bot.py
```

Бот начнёт слушать обновления от Telegram.

---

## 📌 Основные команды

| Команда | Описание |
|---------|---------|
| `/start` | Проверить статус бота |
| `/help` | Справка по командам |
| `/tracker name#tag` | Анализ профиля Valorant |
| `/progress name#tag` | Тренд за 30 дней (WR, KDA) |
| `/vs name1#tag1 vs name2#tag2` | Сравнение двух игроков |
| `/alert name#tag wr < 45` | Оповещение если WR упадёт |
| `/alerts` | Список твоих оповещений |

---

## 🎯 Что умеет

### Smart Analytics (без LLM галлюцинаций)
- ✅ Парсит Henrik API → вычисляет реальные метрики
- ✅ Выводит WR, KDA, Main Agent, Main Map
- ✅ Рекомендации на основе **цифр**, не LLM

### Контекстный диалог
- ✅ Помнит последний упомянутый RiotID
- ✅ Follow-up: "скинь все" → полный отчёт
- ✅ Поддержка в личных чатах и группах

### Система оповещений
- ✅ `/alert aisokuro#ako wr < 45` → оповещение при падении
- ✅ Срабатывает автоматически при каждом запросе трекера
- ✅ Поддержка WR и KDA оповещений

### Отслеживание прогресса
- ✅ Snapshots сохраняют метрики каждый запрос
- ✅ `/progress` показывает реальный тренд за 30 дней
- ✅ Вывод: 📈 вверх, 📉 вниз, ➡️ стабильно

### Сравнение игроков
- ✅ `/vs` — кто сильнее?
- ✅ Сравнение WR, KDA, Kills/Deaths
- ✅ Интеллектуальный вывод

### LLM для обычного диалога
- ✅ Groq API (llama, qwen, мультимодель)
- ✅ Model cooldown для RateLimits
- ✅ Resilient polling с retry

---

## 🗄️ База данных

**SQLite** с таблицами:
- `players` — информация о игроках (роль, агент)
- `chat` — история сообщений (сохраняются последние 60)
- `player_profiles` — профиль (ранг, MMR, регион)
- `match_history` — история матчей (K/D/A, агент, карта)
- `player_analytics` — агрегированные метрики
- `player_snapshots` — снимки для отслеживания тренда
- `player_alerts` — оповещения пользователя

---

## 📋 Примеры использования

### 1. Получить анализ профиля
```
User: @ailatumbot трекер aisokuro#ako
Bot: 
📊 **aisokuro#ako** [Platinum 1]
• Матчей: 20
• Winrate: 54%
• Avg KDA: 1.35
• Main Agent: Jett
• Main Map: Pearl
• Recent Form: 3/5W

**РЕКОМЕНДАЦИИ:**
✅ Хороший винрейт!
✅ Сильный фраг. Стабилизируй перформанс.
```

### 2. Посмотреть прогресс
```
User: @ailatumbot progress aisokuro#ako
Bot:
📈 **ПРОГРЕСС aisokuro#ako (последние 30 дней)**
• WR: 52% → 54% (📈 +2%)
• KDA: 1.2 → 1.35 (📈 +0.15)
• Сыграно: +15 матчей
```

### 3. Сравнить двух игроков
```
User: @ailatumbot vs player1#tag vs player2#tag
Bot:
⚔️ **СРАВНЕНИЕ**
WR: 54% vs 48% ✅ player1 лучше на 6%
KDA: 1.35 vs 1.1 ✅ player1 лучше на 0.25
🏆 player1 явно сильнее
```

### 4. Установить оповещение
```
User: /alert aisokuro#ako wr < 45
Bot: ✅ Оповещение установлено

[Когда WR упадёт ниже 45%]
Bot: ⚠️ WR упал ниже 45%! Сейчас: 44%
```

---

## 🔧 Конфигурация

### Переменные окружения

| Переменная | Описание | Обязательна |
|-----------|---------|-----------|
| `TELEGRAM_TOKEN` | Token от BotFather | ✅ |
| `GROQ_API_KEY` | API key Groq | ✅ |
| `HENRIK_API_KEY` | API key HenrikDev | ❌ (для трекера) |
| `HDEV_API_KEY` | Альтернатива HENRIK_API_KEY | ❌ |
| `BOT_USERNAME` | Имя бота (для упоминания) | ❌ (@latumbot) |
| `GROQ_MODELS` | Модели через запятую | ❌ |
| `MODEL_COOLDOWN_SECONDS` | Cooldown для моделей | ❌ (90) |
| `DB_PATH` | Путь к БД | ❌ (bot.db) |
| `PORT` | Порт для health check | ❌ |

### Пример .env
```env
TELEGRAM_TOKEN=123456789:ABCDefGhIjKlMnOpQrStUvWxYz
GROQ_API_KEY=gsk_1234567890abcdefghijk
HENRIK_API_KEY=hdev_1234567890abcdefghijk
BOT_USERNAME=@latumbot
GROQ_MODELS=llama-3.1-8b-instant,qwen/qwen3-32b,llama-3.3-70b-versatile
MODEL_COOLDOWN_SECONDS=90
PORT=10000
```

---

## 📈 Архитектура

```
Telegram ← → Bot Handler
              ↓
         [Determine Command]
              ↓
         ├→ /tracker    → Henrik API → Parser → Smart Analyzer → DB
         ├→ /progress   → DB (snapshots) → Formatter
         ├→ /vs         → DB (analytics) → Comparison
         ├→ /alert      → DB (alerts) → Save & Check
         └→ LLM Chat    → Groq API → Response
              ↓
         SQLite DB (bot.db)
         ├→ players, chat, player_profiles
         ├→ match_history, player_analytics
         └→ player_snapshots, player_alerts
```

---

## 🐛 Логирование

Бот выводит логи на уровне `INFO`:
```
INFO:aiogram.event:Update id=37865298 is handled. Duration 5 ms
INFO:ailatumbot:Handling message from John (123456) in chat 789: трекер aisokuro#ako
INFO:ailatumbot:Parsed and saved analytics for aisokuro#ako: 20 matches
INFO:ailatumbot:Sent analysis to chat 789 for aisokuro#ako
```

Смотри логи в консоли для отладки.

---

## ⚙️ Deployment (Render)

Используется `Procfile`:
```
worker: python bot.py
```

И `render.yaml`:
```yaml
services:
  - type: worker
    name: ailatumbot
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: python bot.py
```

---

## 📚 Дополнительная документация

- **ARCHITECTURE.md** — Полное описание архитектуры и дизайна
- **USAGE.md** — Примеры использования с подробными выводами
- **bot.py** — Исходный код с комментариями

---

## 🚀 Что дальше

### Próximos features
1. **Детальный анализ по картам**: `/map_stats name#tag pearl`
2. **Лучшие матчи**: `/best name#tag` → топ-5 матчей
3. **Еженедельные отчёты**: автоматические сводки
4. **Рекомендации по экипировке**: на основе main agent
5. **Discord интеграция**: для команд/клубов
6. **Веб-панель**: графики и визуализация

---

## 🤝 Поддержка

Если есть вопросы или баги:
1. Проверь логи (`INFO` уровень)
2. Убедись что переменные окружения установлены
3. Проверь доступ к Henrik API и Groq API
4. Перезагрузи бота

---

## 📝 Лицензия

MIT
