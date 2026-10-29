#!/usr/bin/env python3
"""Create/validate deployment settings without displaying secrets."""
import getpass
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / '.env.production'
FIELDS = {
    'BOT_TOKEN': ('Токен Telegram-бота', ''),
    'ADMIN_IDS': ('Telegram ID администраторов через запятую', ''),
    'ROLLYPAY_API_KEY': ('API-ключ RollyPay', ''),
    'ROLLYPAY_TEST': ('Тестовые платежи RollyPay: true/false', 'true'),
}
SECRET = {'BOT_TOKEN', 'ROLLYPAY_API_KEY', 'ROLLYPAY_SIGNING_SECRET'}


def validate(values):
    errors = []
    for key in FIELDS:
        value = values.get(key, '')
        if not value or any(c in value for c in "\r\n'\x00"):
            errors.append(key)
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', values.get('BOT_TOKEN', '')):
        errors.append('BOT_TOKEN')
    if not re.fullmatch(r'[1-9]\d*(?:\s*,\s*[1-9]\d*)*', values.get('ADMIN_IDS', '')):
        errors.append('ADMIN_IDS')
    if values.get('ROLLYPAY_TEST') not in {'true', 'false'}:
        errors.append('ROLLYPAY_TEST')
    return sorted(set(errors))


def read_config(path):
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep:
            raise ValueError('Строка настроек должна иметь формат KEY=value.')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def main():
    if not TARGET.exists():
        if not sys.stdin.isatty():
            raise SystemExit('Нет .env.production. Запустите скрипт в терминале или создайте файл по .env.production.example.')
        print('Настройка сервера. Секреты при вводе не отображаются.')
        values = {}
        for key, (label, default) in FIELDS.items():
            prompt = label + (f' [{default}]' if default else '') + ': '
            values[key] = ((getpass.getpass(prompt) if key in SECRET else input(prompt)).strip() or default)
        errors = validate(values)
        if errors:
            raise SystemExit('Некорректные поля: ' + ', '.join(errors) + '. Файл не создан.')
        fd = os.open(TARGET, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as file:
            file.write('# Private deployment settings. Never commit this file.\n')
            for key in FIELDS:
                file.write(f"{key}='{values[key]}'\n")
    try:
        values = read_config(TARGET)
    except ValueError as exc:
        raise SystemExit(str(exc))
    errors = validate(values)
    if errors:
        raise SystemExit('Заполните корректно .env.production: ' + ', '.join(errors))
    os.chmod(TARGET, 0o600)
    print('Настройки проверены. ' + ('Платежи тестовые.' if values['ROLLYPAY_TEST'] == 'true' else 'Включены реальные платежи.'))


if __name__ == '__main__':
    main()
