# PvPMod — сайт

Fabric 1.21.4 PvP-мод: сайт с регистрацией, профилем и статусом подписки.

## Деплой на Render
1. Залей всю папку в свой репозиторий (например kryytoi/WDdwdw), в корень или в подпапку.
2. На render.com: New -> Blueprint -> выбери репозиторий (подхватит render.yaml)
   ИЛИ New -> Web Service вручную:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`
3. Обязательно добавь Persistent Disk (Render -> Disks), путь `/opt/render/project/src/data`,
   и переменную окружения `DATA_DIR=/opt/render/project/src/data`.
   Без диска база данных будет удаляться при каждом новом деплое!
4. Задай переменные окружения:
   - `SECRET_KEY` — любая случайная строка
   - `ADMIN_KEY` — секретный ключ для активации подписок
   - `LAUNCHER_URL` — прямая ссылка на .exe/.jar лаунчера (например GitHub Release)

## Активация подписки после оплаты
Пока нет автоматической оплаты, активируй вручную:
curl -X POST https://твой-сайт.onrender.com/admin/activate \
  -d "key=ТВОЙ_ADMIN_KEY&username=Логин&plan=30"
plan: 30 | 120 | forever
