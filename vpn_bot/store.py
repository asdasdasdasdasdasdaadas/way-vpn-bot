import calendar
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


DEFAULT_TARIFFS = (
    ('VPN — 1 месяц', '1400.00', 1),
    ('VPN — 3 месяца', '4137.00', 3),
    ('VPN — 6 месяцев', '8148.00', 6),
    ('VPN — 12 месяцев', '15792.00', 12),
)


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > 1000000 or amount.as_tuple().exponent < -2:
            raise ValueError('Цена должна быть от 0.01 до 1 000 000 ₽, не более двух знаков после точки.')
        return format(amount, '.2f')
    except InvalidOperation:
        raise ValueError('Некорректная цена.')


def add_months(timestamp, months):
    dt = datetime.fromtimestamp(timestamp, timezone.utc)
    year, month = divmod(dt.year * 12 + dt.month - 1 + months, 12)
    return dt.replace(year=year, month=month + 1,
                      day=min(dt.day, calendar.monthrange(year, month + 1)[1])).timestamp()


class Store:
    def __init__(self, path, admins):
        self.admins = admins
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tariffs(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
          amount TEXT NOT NULL, months INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
          name TEXT NOT NULL, amount TEXT NOT NULL, months INTEGER NOT NULL, test INTEGER NOT NULL,
          payment_id TEXT UNIQUE, pay_url TEXT, status TEXT NOT NULL DEFAULT 'creating',
          fulfilled INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY, order_id TEXT NOT NULL UNIQUE,
          state TEXT NOT NULL DEFAULT 'new', assigned INTEGER);
        CREATE TABLE IF NOT EXISTS subscriptions(user_id INTEGER PRIMARY KEY, link TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
          admin_id INTEGER, body TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
          chat_id INTEGER NOT NULL, body TEXT NOT NULL, keyboard TEXT, sent INTEGER NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS state(user_id INTEGER PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        self.db.execute('CREATE TABLE IF NOT EXISTS locations(id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)')
        for table in ('orders', 'subscriptions'):
            columns = {row[1] for row in self.db.execute(f'PRAGMA table_info({table})')}
            if 'location' not in columns:
                self.db.execute(f'ALTER TABLE {table} ADD COLUMN location TEXT')
        self.db.commit()
        with self.db:
            if not self.db.execute("SELECT 1 FROM settings WHERE key='initialized'").fetchone():
                self.db.executemany('INSERT INTO tariffs(name,amount,months) VALUES(?,?,?)', DEFAULT_TARIFFS)
                self.db.execute("INSERT INTO settings VALUES('initialized','1')")
            if not self.db.execute("SELECT 1 FROM settings WHERE key='rental_pricing_v1'").fetchone():
                # Migrate the original seed only; preserve admin-created prices and orders.
                self.db.execute("UPDATE tariffs SET amount='2400.00' WHERE id=1 AND name='VPN — 1 месяц' AND months=1 AND amount='1200.00'")
                for name, amount, months in DEFAULT_TARIFFS:
                    if not self.db.execute('SELECT 1 FROM tariffs WHERE name=? AND months=?', (name, months)).fetchone():
                        self.db.execute('INSERT INTO tariffs(name,amount,months) VALUES(?,?,?)', (name, amount, months))
                self.db.execute("INSERT INTO settings VALUES('rental_pricing_v1','1')")
            if not self.db.execute("SELECT 1 FROM settings WHERE key='rental_pricing_v2'").fetchone():
                for (name, amount, months), previous in zip(DEFAULT_TARIFFS, ('2400.00', '7092.00', '13968.00', '27072.00')):
                    self.db.execute('UPDATE tariffs SET amount=? WHERE name=? AND months=? AND amount=?', (amount, name, months, previous))
                self.db.execute("INSERT INTO settings VALUES('rental_pricing_v2','1')")


    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args)]

    def enqueue(self, key, user, body, keyboard=None):
        self.db.execute('INSERT OR IGNORE INTO outbox(event_key,chat_id,body,keyboard) VALUES(?,?,?,?)',
                        (key, user, body, json.dumps(keyboard) if keyboard else None))

    def notify_admins(self, key, body, keyboard=None):
        for admin in self.admins:
            self.enqueue(f'{key}:{admin}', admin, body, keyboard)

    def set_state(self, user, value=None):
        with self.db:
            if value is None:
                self.db.execute('DELETE FROM state WHERE user_id=?', (user,))
            else:
                self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (user, value))

    def save_tariff(self, name, amount, months, tariff_id=None):
        amount = money(amount)
        months = int(months)
        if not 1 <= months <= 60 or not 1 <= len(name.strip()) <= 80:
            raise ValueError('Название: 1–80 символов. Срок: от 1 до 60 месяцев.')
        with self.db:
            if tariff_id:
                self.db.execute('UPDATE tariffs SET name=?,amount=?,months=? WHERE id=?',
                                (name.strip(), amount, months, tariff_id))
            else:
                self.db.execute('INSERT INTO tariffs(name,amount,months) VALUES(?,?,?)', (name.strip(), amount, months))

    def create_order(self, user, tariff_id, test, location_id=None):
        with self.db:
            tariff = self.one('SELECT * FROM tariffs WHERE id=? AND active=1', (tariff_id,))
            if not tariff:
                raise ValueError('Этот вариант подписки больше не продаётся.')
            pending = self.one("SELECT id FROM orders WHERE user_id=? AND (status IN ('creating','created','processing','uncertain') OR (status='paid' AND fulfilled=0))", (user,))
            if pending:
                raise ValueError('У вас уже есть незавершённый заказ. Откройте «Мои заказы».')
            sub = self.one('SELECT location FROM subscriptions WHERE user_id=?', (user,))
            location = sub['location'] if sub else None
            if not location and location_id is not None:
                selected = self.one('SELECT name FROM locations WHERE id=? AND active=1', (location_id,))
                if not selected:
                    raise ValueError('Эта страна сейчас недоступна. Выберите другую.')
                location = selected['name']
            if not location and self.one('SELECT id FROM locations LIMIT 1'):
                raise ValueError('Сначала выберите страну подключения.')
            order_id = uuid.uuid4().hex
            self.db.execute('INSERT INTO orders(id,user_id,name,amount,months,test,created,location) VALUES(?,?,?,?,?,?,?,?)',
                            (order_id, user, tariff['name'], tariff['amount'], tariff['months'], int(test), time.time(), location))
        return self.one('SELECT * FROM orders WHERE id=?', (order_id,))

    def apply_payment(self, event):
        with self.db:
            order = self.one('SELECT * FROM orders WHERE id=?', (event.get('order_id'),))
            if not order:
                raise ValueError('Unknown order')
            if (not isinstance(event.get('test'), bool) or event['test'] != bool(order['test'])
                    or money(event.get('amount')) != order['amount'] or event.get('currency') != 'RUB'
                    or not event.get('payment_id')
                    or (order['payment_id'] and order['payment_id'] != event['payment_id'])):
                raise ValueError('Payment mismatch')
            status = event.get('status')
            if status not in {'created', 'processing', 'paid', 'canceled', 'expired', 'refunded', 'chargeback'}:
                raise ValueError('Unknown payment status')
            # Refunds cannot be undone by a delayed delivery of payment.paid.
            if order['status'] in {'refunded', 'chargeback'}:
                return
            if order['status'] == 'paid' and status in {'created', 'processing', 'canceled', 'expired'}:
                return
            if order['status'] in {'expired', 'canceled'} and status in {'created', 'processing'}:
                return
            self.db.execute('UPDATE orders SET status=?,payment_id=? WHERE id=?',
                            (status, event['payment_id'], order['id']))
            if status == 'paid':
                self.db.execute('INSERT OR IGNORE INTO tickets(order_id) VALUES(?)', (order['id'],))
                ticket = self.one('SELECT * FROM tickets WHERE order_id=?', (order['id'],))
                prefix = '[ТЕСТ] ' if order['test'] else ''
                self.enqueue(f"paid:{order['id']}", order['user_id'],
                             f"{prefix}Оплата получена. Заявка №{ticket['id']} создана. Администратор подготовит подключение или продление.")
                self.notify_admins(f"paid:{order['id']}",
                    f"{prefix}Оплаченная заявка №{ticket['id']}\nКлиент: {order['user_id']}\n{order['name']} — {order['amount']} ₽\nСтрана: {order['location'] or 'не указана'}\nСрок: {order['months']} мес.",
                    [[{'text': 'Открыть заявку', 'callback_data': f"ticket:{ticket['id']}"}]])
            elif status in {'refunded', 'chargeback'}:
                self.db.execute("UPDATE tickets SET state='blocked' WHERE order_id=?", (order['id'],))
                self.notify_admins(f"reversed:{order['id']}",
                    f"Возврат/отзыв оплаты. Клиент {order['user_id']}, заказ {order['id']}. Проверьте VPN и вручную скорректируйте доступ. Автоматически срок не изменён.")
                self.enqueue(f"reversed:{order['id']}", order['user_id'], 'По вашему заказу получен возврат/отзыв оплаты. Свяжитесь с поддержкой.')

    def fulfill(self, ticket_id, admin, link=None, now=None):
        now = time.time() if now is None else now
        with self.db:
            order = self.one('SELECT o.*,t.state,t.assigned FROM orders o JOIN tickets t ON t.order_id=o.id WHERE t.id=?', (ticket_id,))
            if not order or order['status'] != 'paid' or order['state'] == 'blocked':
                raise ValueError('Заявка не оплачена или заблокирована.')
            if order['fulfilled']:
                raise ValueError('Заявка уже выполнена.')
            if order['assigned'] not in (None, admin):
                raise ValueError('Заявка находится у другого администратора.')
            sub = self.one('SELECT * FROM subscriptions WHERE user_id=?', (order['user_id'],))
            link = link or (sub['link'] if sub else None)
            if not link or len(link) > 2000 or any(c.isspace() for c in link) or not link.startswith(('https://', 'http://', 'vless://', 'vmess://', 'trojan://', 'ss://', 'hysteria2://', 'hy2://', 'wg://')):
                raise ValueError('Нужна ссылка подключения без пробелов (https://, vless:// и т. п.).')
            expires = add_months(max(now, sub['expires'] if sub else now), order['months'])
            # Sandbox orders exercise the flow but never grant real subscription time.
            if not order['test']:
                self.db.execute('INSERT OR REPLACE INTO subscriptions(user_id,link,expires,location) VALUES(?,?,?,?)', (order['user_id'], link, expires, order['location']))
            self.db.execute('UPDATE orders SET fulfilled=1 WHERE id=?', (order['id'],))
            self.db.execute("UPDATE tickets SET state='done',assigned=? WHERE id=?", (admin, ticket_id))
            label = '[ТЕСТ: реальная подписка не изменена]\n' if order['test'] else ''
            date = datetime.fromtimestamp(expires, timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
            self.enqueue(f"fulfilled:{order['id']}", order['user_id'], f'{label}Заявка №{ticket_id} выполнена.\nПодключение: {link}\nСрок до: {date}')
            return expires

    def reminders(self, now=None):
        now = time.time() if now is None else now
        with self.db:
            for sub in self.all('SELECT * FROM subscriptions WHERE expires<=?', (now + 3 * 86400,)):
                remaining = sub['expires'] - now
                stage = 'expired' if remaining <= 0 else '1d' if remaining <= 86400 else '3d'
                key = f"remind:{sub['user_id']}:{sub['expires']}:{stage}"
                text = 'Срок подписки истёк.' if stage == 'expired' else 'Срок подписки скоро закончится.'
                self.enqueue(key, sub['user_id'], text + ' Для продления откройте «Тарифы / продлить».')
                if stage == 'expired':
                    self.notify_admins(key, f"Подписка клиента {sub['user_id']} истекла. Проверьте и вручную отключите VPN, если продление не оплачено.")
