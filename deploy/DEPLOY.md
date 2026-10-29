# Deployment without webhook

Run `./scripts/deploy.sh` on Linux with Docker Compose, Git, Python 3 and flock. Required settings: BOT_TOKEN, ADMIN_IDS, ROLLYPAY_API_KEY, ROLLYPAY_TEST. No domain, email, signing secret or inbound ports are needed. Payment statuses are polled through the RollyPay API. See README.md for the current setup instructions.

If this project previously ran Caddy, stop only this project's Caddy container after verifying its ownership. Do not stop another bot's proxy. The new configuration does not start Caddy.

## Резервные копии и обновление

```sh
./scripts/backup.sh
```

Скрипт делает согласованную копию SQLite и проверяет её целостность. Копии содержат личные VPN-ссылки; храните их вне сервера в защищённом месте. Скрипт не отправляет их автоматически и не удаляет старые копии. Для ежедневного запуска можно добавить в crontab сервера `0 3 * * * cd /opt/vpn-bot && ./scripts/backup.sh`; замените путь на реальный.

Перед обновлением сделайте копию и сохраните предыдущую версию исходников. Замените файлы из нового архива, сохранив `.env.production`, затем:

```sh
./scripts/production.sh up -d --build --wait --wait-timeout 180
./scripts/production.sh logs --tail=100 bot
```

Не используйте `down -v`: эта команда удаляет базу и сертификаты. Проект имеет постоянное имя `vpn-service`, поэтому обновление из той же установки сохраняет тома. Не запускайте вторую установку с этим именем на том же Docker-хосте.

## Восстановление

Восстановление заменяет базу. Сначала остановите бот и сохраните текущее состояние. Перед восстановлением старой базы учтите платежи, полученные после даты копии: потребуется сверка с RollyPay и повторная доставка событий, иначе возможна потеря учёта оплат.

```sh
./scripts/production.sh stop bot
./scripts/production.sh cp bot:/app/data ./before-restore-data
./scripts/production.sh run --rm --no-deps -v "$PWD/backups:/restore:ro" bot python -c 'import sqlite3,sys; src=sqlite3.connect("file:/restore/"+sys.argv[1]+"?mode=ro", uri=True); assert src.execute("PRAGMA integrity_check").fetchone()[0]=="ok"; dst=sqlite3.connect("/app/data/bot.sqlite3"); src.backup(dst); dst.close(); src.close()' ИМЯ-КОПИИ.sqlite3
./scripts/production.sh up -d --wait
```

Для отката приложения восстановите предыдущие исходники и пересоберите. Если изменилась схема базы несовместимым образом, нужен согласованный откат базы с учётом новых платежей; автоматического отката схемы нет.

## Что проверено и что осталось

Локальные автоматические тесты проверяют бизнес-логику и HTTP-интеграцию с имитацией внешних сервисов. На компьютере подготовки Docker отсутствует: сборка контейнера, получение сертификата и реальные уведомления RollyPay должны быть проверены на сервере командами выше. Серверные ключи RollyPay и домен в пакет не включены.

Требование Telegram использовать Stars для продажи цифровых услуг внутри ботов остаётся в силе. Внешняя ссылка RollyPay сама по себе не снимает ограничение. См. раздел в README перед публичным запуском.
