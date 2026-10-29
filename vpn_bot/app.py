import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

from .store import Store

log = logging.getLogger('vpn_bot')


def verify_signature(body, timestamp, signature, secret, now=None):
    try:
        # Allow provider retries (~1 hour) while rejecting old replay attempts.
        if abs((time.time() if now is None else now) - int(timestamp)) > 7200:
            return False
        expected = hmac.new(secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (ValueError, TypeError, AttributeError):
        return False


def buttons(*pairs):
    return [[{'text': label, 'callback_data': data, 'style': 'success' if data.split(':')[0] in {'plans', 'buy', 'finish', 'demopay', 'deliver'} else 'primary'}] for label, data in pairs]


def date(value):
    return datetime.fromtimestamp(value, ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')


class Bot:
    def __init__(self, store, session, token, key, secret, test=True, api_base='https://rollypay.io', telegram_base='https://api.telegram.org', demo=False):
        self.store, self.session = store, session
        self.token, self.key, self.secret, self.test = token, key, secret, test
        self.api_base, self.telegram_base = api_base.rstrip('/'), telegram_base.rstrip('/')
        self.demo = demo
        if demo and not test:
            raise ValueError('Демо доступно только в тестовом режиме.')

    async def telegram(self, method, **data):
        async with self.session.post(f'{self.telegram_base}/bot{self.token}/{method}', json=data,
                                     timeout=aiohttp.ClientTimeout(total=45)) as response:
            result = await response.json()
            if not result.get('ok'):
                # Never log request URLs (the bot token is part of them).
                raise RuntimeError(f"Telegram request failed: {result.get('error_code')}")
            return result['result']

    async def send(self, user, text, keyboard=None):
        data = dict(chat_id=user, text=text, link_preview_options={'is_disabled': True})
        if keyboard:
            data['reply_markup'] = {'inline_keyboard': [[dict(button, style=button.get('style', 'success' if 'url' in button else 'primary')) for button in row] for row in keyboard]}
        await self.telegram('sendMessage', **data)

    async def rolly(self, method, path, payload=None):
        async with self.session.request(method, self.api_base + path, json=payload,
                headers={'X-API-Key': self.key, 'X-Nonce': str(uuid.uuid4())},
                timeout=aiohttp.ClientTimeout(total=20)) as response:
            if response.status >= 400:
                raise RuntimeError(f'RollyPay request failed: HTTP {response.status}')
            result = await response.json()
            if not isinstance(result, dict):
                raise RuntimeError('Invalid payment response')
            return result

    async def home(self, user):
        items = [('Подключить / продлить', 'plans'), ('Мой VPN', 'subscription'),
                 ('Мои заказы', 'orders:0'), ('Поддержка', 'support')]
        keyboard = buttons(*items)
        keyboard = [keyboard[0] + keyboard[1], keyboard[2] + keyboard[3]]
        if user in self.store.admins:
            keyboard[1] += buttons(('Админка', 'admin'))[0]
        await self.send(user,
            'Ваш личный VPN для работы с нейросетями\n\n'
            'Для Claude, Claude Code и других привычных сервисов. '
            'Только для вас — без посторонних пользователей, с постоянным адресом и страной подключения.\n\n'
            'Несколько способов подключения по одной ссылке — для ваших устройств. '
            'После оплаты мы всё подготовим и пришлём ссылку сюда. '
            'Срок подписки начнётся, когда подключение будет готово.'
            + ('\n\n🧪 Сейчас можно всё попробовать без оплаты.' if self.demo else '\n\n🧪 Тестовый режим.' if self.test else ''), keyboard)

    async def checkout(self, user, tariff, location_id=None):
        order = self.store.create_order(user, tariff, self.test, location_id)
        if self.demo:
            with self.store.db:
                self.store.db.execute("UPDATE orders SET status='created',payment_id=? WHERE id=?", ('demo_' + order['id'], order['id']))
                self.store.notify_admins('demo:' + order['id'], f"ДЕМО: клиент {user} выбрал {order['name']}, страна: {order['location'] or 'не указана'}. Денег не списываем.", buttons(('Имитировать оплату', 'demopay:' + order['id'])))
            await self.send(user, 'ДЕМО: заказ создан без реального платежа. Администратор может имитировать оплату кнопкой в уведомлении.')
            return
        try:
            result = await self.rolly('POST', '/api/v1/payments', {
                'amount': order['amount'], 'payment_currency': 'RUB', 'order_id': order['id'],
                'description': order['name'], 'customer_id': str(user), 'test': self.test})
            if not result.get('payment_id') or not str(result.get('pay_url', '')).startswith('https://'):
                raise RuntimeError('Invalid payment response')
            with self.store.db:
                existing = self.store.one('SELECT payment_id FROM orders WHERE id=?', (order['id'],))
                if existing['payment_id'] not in (None, result['payment_id']):
                    raise RuntimeError('Payment identifier mismatch')
                self.store.db.execute("UPDATE orders SET payment_id=?,pay_url=?,status=CASE WHEN status='creating' THEN 'created' ELSE status END WHERE id=?",
                    (result['payment_id'], result['pay_url'], order['id']))
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError):
            with self.store.db:
                self.store.db.execute("UPDATE orders SET status='uncertain' WHERE id=? AND status='creating'", (order['id'],))
                self.store.notify_admins('uncertain:' + order['id'],
                    f"Не удалось получить результат создания платежа. Заказ {order['id']}, клиент {user}. Проверьте заказ в RollyPay. Не создавайте повторный платёж до сверки.")
            await self.send(user, 'Не удалось получить ссылку оплаты. Заказ сохранён для проверки, администратор уведомлён. Попробуйте «Мои заказы» позднее.')
            return
        await self.send(user, f"{order['name']}\nСтрана: {order['location'] or 'не указана'}\nСрок: {order['months']} мес.\nК оплате: {order['amount']} ₽\nПосле оплаты заявка создастся автоматически.",
            [[{'text': 'Оплатить', 'url': result['pay_url']}],
             [{'text': 'Проверить оплату', 'callback_data': 'check:' + order['id']} ]])

    async def check(self, user, order_id):
        order = self.store.one('SELECT * FROM orders WHERE id=? AND user_id=?', (order_id, user))
        if not order or not order['payment_id']:
            raise ValueError('Ссылка пока недоступна. Обратитесь в поддержку.')
        if self.demo:
            await self.send(user, 'ДЕМО. Статус заказа: ' + order['status'])
            return
        result = await self.rolly('GET', '/api/v1/payments/' + order['payment_id'])
        # Some GET responses omit test; the signed webhook remains authoritative for such cases.
        if 'test' in result:
            result['currency'] = result.get('payment_currency', result.get('currency'))
            self.store.apply_payment(result)
        status = self.store.one('SELECT status FROM orders WHERE id=?', (order_id,))['status']
        await self.send(user, 'Статус: ' + status + ('\nОжидаем подтверждение уведомлением RollyPay.' if status != 'paid' else ''))

    async def show_ticket(self, user, ticket_id):
        ticket = self.store.one('SELECT t.*,o.user_id,o.name,o.amount,o.months,o.status,o.test,o.location,u.name AS customer FROM tickets t JOIN orders o ON o.id=t.order_id LEFT JOIN users u ON u.id=o.user_id WHERE t.id=?', (ticket_id,))
        if not ticket:
            raise ValueError('Заявка не найдена.')
        sub = self.store.one('SELECT * FROM subscriptions WHERE user_id=?', (ticket['user_id'],))
        text = (f"Заявка №{ticket_id}" + (' [ТЕСТ]' if ticket['test'] else '') +
                f"\nКлиент: {ticket['customer']} ({ticket['user_id']})\n{ticket['name']}, {ticket['amount']} ₽, {ticket['months']} мес.\nСтрана: {ticket['location'] or 'не указана'}\nОплата: {ticket['status']}\nЗаявка: {ticket['state']}\nАдминистратор: {ticket['assigned'] or 'не назначен'}")
        if sub:
            text += f"\nТекущий срок: {date(sub['expires'])}\nСсылка: {sub['link']}"
        actions = [('Написать клиенту', f"reply:{ticket['user_id']}")]
        if ticket['state'] in {'new', 'working'} and ticket['status'] == 'paid':
            actions = [('Взять в работу', f'claim:{ticket_id}'), ('Выдать / заменить ссылку', f'deliver:{ticket_id}')] + actions
            if sub:
                actions.append(('Продление выполнено', f'renew:{ticket_id}'))
        await self.send(user, text, buttons(*actions))

    async def recover(self, user, order_id, payment_id):
        order = self.store.one('SELECT * FROM orders WHERE id=?', (order_id,))
        if not order:
            raise ValueError('Заказ не найден.')
        if not payment_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in payment_id):
            raise ValueError('Некорректный идентификатор платежа.')
        result = await self.rolly('GET', '/api/v1/payments/' + payment_id)
        if result.get('order_id') != order_id or result.get('payment_id') != payment_id:
            raise ValueError('Этот платёж не соответствует заказу.')
        if 'test' not in result:
            raise ValueError('API не вернул признак тестовой оплаты. Запросите повторную доставку вебхука в RollyPay; вручную подтверждать оплату нельзя.')
        result['currency'] = result.get('payment_currency', result.get('currency'))
        self.store.apply_payment(result)
        if str(result.get('pay_url', '')).startswith('https://'):
            with self.store.db:
                self.store.db.execute('UPDATE orders SET pay_url=? WHERE id=?', (result['pay_url'], order_id))
        await self.send(user, 'Платёж сверён с RollyPay. Заказ обновлён.')

    async def callback(self, user, data):
        action, _, arg = data.partition(':')
        public = {'home', 'plans', 'buy', 'location', 'subscription', 'orders', 'check', 'support'}
        if action not in public and user not in self.store.admins:
            raise ValueError('Нет доступа.')
        if action == 'demopay':
            if not self.demo:
                raise ValueError('Имитация платежей выключена.')
            order = self.store.one('SELECT * FROM orders WHERE id=?', (arg,))
            if not order or not order['test'] or not (order['payment_id'] or '').startswith('demo_'):
                raise ValueError('Это не демонстрационный заказ.')
            self.store.apply_payment(dict(order_id=arg, payment_id=order['payment_id'], amount=order['amount'], currency='RUB', test=True, status='paid'))
            await self.send(user, 'Тестовая оплата подтверждена. Оплаченная заявка появится в админке.')
        elif action == 'home':
            self.store.set_state(user)
            await self.home(user)
        elif action == 'plans':
            plans = self.store.all('SELECT * FROM tariffs WHERE active=1 ORDER BY months,id')
            await self.send(user, 'Выберите срок подписки:' if plans else 'Продажи временно приостановлены.',
                buttons(*[(f"{p['name']} · {p['amount']} ₽ · {p['months']} мес.", f"buy:{p['id']}") for p in plans]))
        elif action == 'buy':
            sub = self.store.one('SELECT location FROM subscriptions WHERE user_id=?', (user,))
            if sub and sub['location']:
                await self.checkout(user, int(arg))
            else:
                rows = self.store.all('SELECT * FROM locations WHERE active=1 ORDER BY id')
                keyboard = buttons(*[(row['name'], f"location:{int(arg)}:{row['id']}") for row in rows])
                keyboard = [sum(keyboard[i:i+2], []) for i in range(0, len(keyboard), 2)]
                await self.send(user, 'Выберите страну для вашего VPN. Она останется за вами при продлении.' if rows else 'Список стран пока готовится. Напишите в поддержку.', keyboard)
        elif action == 'location':
            tariff, location = map(int, arg.split(':'))
            await self.checkout(user, tariff, location)
        elif action == 'check':
            await self.check(user, arg)
        elif action == 'subscription':
            sub = self.store.one('SELECT * FROM subscriptions WHERE user_id=?', (user,))
            await self.send(user, f"Подписка до {date(sub['expires'])}\nСтрана: {sub['location'] or 'не указана'}\n{sub['link']}" if sub else 'Подключение ещё не выдано.', buttons(('Тарифы / продлить', 'plans')))
        elif action == 'orders':
            offset = max(0, int(arg or 0))
            orders = self.store.all('SELECT * FROM orders WHERE user_id=? ORDER BY created DESC LIMIT 10 OFFSET ?', (user, offset))
            await self.send(user, 'Ваши заказы:' if orders else 'Заказов пока нет.')
            for order in orders:
                keyboard = []
                if order['pay_url'] and order['status'] in {'created', 'processing'}:
                    keyboard.append([{'text': 'Оплатить', 'url': order['pay_url']}])
                if order['payment_id']:
                    keyboard += buttons(('Проверить', 'check:' + order['id']))
                await self.send(user, f"{order['name']} · {order['amount']} ₽\nСтрана: {order['location'] or 'не указана'}\nСтатус: {order['status']}\nВыдано: {'да' if order['fulfilled'] else 'нет'}", keyboard)
            if len(orders) == 10:
                await self.send(user, 'Другие заказы', buttons(('Далее', f'orders:{offset + 10}')))
        elif action == 'support':
            self.store.set_state(user, 'support')
            await self.send(user, 'Напишите вопрос одним текстовым сообщением (до 3000 символов). Для отмены — /cancel.')
        elif action == 'admin':
            await self.send(user, 'Админка', buttons(('Оплаченные заявки', 'tickets:0'), ('Управление тарифами', 'tariffs'), ('Локации', 'locations'), ('Клиенты', 'clients:0'), ('Проверить зависшие оплаты', 'uncertain')))
        elif action == 'locations':
            rows = self.store.all('SELECT * FROM locations ORDER BY id')
            for row in rows:
                await self.send(user, row['name'] + (' — доступна' if row['active'] else ' — отключена'), buttons(('Отключить' if row['active'] else 'Включить', f"loc_toggle:{row['id']}")))
            await self.send(user, 'Добавляйте только страны, в которых можете выдать VPN. Отключение скрывает страну для новых подключений, существующие подписки сохраняют её.', buttons(('Добавить страну', 'loc_add')))
        elif action == 'loc_add':
            self.store.set_state(user, 'loc_add')
            await self.send(user, 'Отправьте название страны, например: 🇩🇪 Германия. /cancel — отмена.')
        elif action == 'loc_toggle':
            with self.store.db:
                self.store.db.execute('UPDATE locations SET active=1-active WHERE id=?', (int(arg),))
            await self.send(user, 'Доступность страны изменена.', buttons(('К локациям', 'locations')))
        elif action == 'uncertain':
            rows = self.store.all("SELECT * FROM orders WHERE status IN ('creating','uncertain') ORDER BY created LIMIT 20")
            await self.send(user, 'Заказы требуют сверки в RollyPay:' if rows else 'Зависших оплат нет.')
            for row in rows:
                await self.send(user, f"Заказ: {row['id']}\nКлиент: {row['user_id']}\nСумма: {row['amount']} ₽\nНайдите платёж в RollyPay по order_id. Для сверки отправьте:\n/recover {row['id']} payment_id\nЕсли платёж отсутствует, после проверки можно закрыть заказ:\n/close_unpaid {row['id']}")
        elif action == 'tickets':
            offset = max(0, int(arg or 0))
            rows = self.store.all("SELECT * FROM tickets WHERE state IN ('new','working') ORDER BY id LIMIT 20 OFFSET ?", (offset,))
            actions = [(f"№{r['id']} · {r['state']}", f"ticket:{r['id']}") for r in rows]
            if len(rows) == 20:
                actions.append(('Далее', f'tickets:{offset + 20}'))
            await self.send(user, 'Открытые заявки:' if rows else 'Открытых заявок нет.', buttons(*actions))
        elif action == 'ticket':
            await self.show_ticket(user, int(arg))
        elif action == 'claim':
            with self.store.db:
                changed = self.store.db.execute("UPDATE tickets SET state='working',assigned=? WHERE id=? AND state IN ('new','working') AND (assigned IS NULL OR assigned=?)", (user, int(arg), user)).rowcount
            if not changed:
                raise ValueError('Заявка уже занята или закрыта.')
            await self.show_ticket(user, int(arg))
        elif action == 'deliver':
            self.store.set_state(user, f'deliver:{int(arg)}')
            await self.send(user, 'После создания/продления VPN отправьте ссылку подключения одним сообщением. Это завершит заявку и начнёт/продлит срок. /cancel — отмена.')
        elif action == 'renew':
            await self.send(user, 'Вы уже продлили VPN на сервере? Подтвердите выполнение. Клиент получит прежнюю ссылку.', buttons(('Подтвердить выполнение', f'finish:{int(arg)}')))
        elif action == 'finish':
            self.store.fulfill(int(arg), user)
            await self.send(user, 'Продление учтено. Уведомление клиенту поставлено в очередь.')
        elif action == 'tariffs':
            for tariff in self.store.all('SELECT * FROM tariffs ORDER BY id'):
                await self.send(user, f"{tariff['name']}\n{tariff['amount']} ₽ / {tariff['months']} мес.\n{'В продаже' if tariff['active'] else 'Отключён'}",
                    buttons(('Изменить', f"edit:{tariff['id']}"), ('Отключить' if tariff['active'] else 'Включить', f"toggle:{tariff['id']}")))
            await self.send(user, 'Новый вариант срока и цены', buttons(('Добавить', 'add')))
        elif action in {'add', 'edit'}:
            self.store.set_state(user, 'tariff:' + (str(int(arg)) if action == 'edit' else 'new'))
            await self.send(user, 'Введите: название | цена в рублях | число месяцев\nНапример: VPN — 3 месяца | 3300 | 3\n/cancel — отмена.')
        elif action == 'toggle':
            with self.store.db:
                self.store.db.execute('UPDATE tariffs SET active=1-active WHERE id=?', (int(arg),))
            await self.send(user, 'Доступность тарифа изменена.', buttons(('К тарифам', 'tariffs')))
        elif action == 'clients':
            offset = max(0, int(arg or 0))
            rows = self.store.all('SELECT u.*,s.expires FROM users u LEFT JOIN subscriptions s ON s.user_id=u.id ORDER BY u.id LIMIT 20 OFFSET ?', (offset,))
            for row in rows:
                await self.send(user, f"{row['name']} · {row['id']}\n" + (f"До {date(row['expires'])}" if row['expires'] else 'Нет подписки'), buttons(('Написать', f"reply:{row['id']}")))
            if len(rows) == 20:
                await self.send(user, 'Другие клиенты', buttons(('Далее', f'clients:{offset + 20}')))
        elif action == 'reply':
            target = int(arg)
            if not self.store.one('SELECT id FROM users WHERE id=?', (target,)):
                raise ValueError('Клиент не найден.')
            self.store.set_state(user, f'reply:{target}')
            await self.send(user, 'Введите ответ клиенту одним текстовым сообщением (до 3000 символов). /cancel — отмена.')
        else:
            raise ValueError('Неизвестная команда. /start — главное меню.')

    async def handle(self, update):
        query = update.get('callback_query')
        message = query.get('message', {}) if query else update.get('message', {})
        sender = query.get('from', {}) if query else message.get('from', {})
        user = sender.get('id')
        if not user or message.get('chat', {}).get('type') != 'private':
            return
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO users VALUES(?,?)', (user, sender.get('first_name', '')[:100]))
        try:
            if query:
                await self.telegram('answerCallbackQuery', callback_query_id=query['id'])
                await self.callback(user, query.get('data', ''))
                return
            text = message.get('text', '').strip()
            command = text.split(' ', 1)[0].split('@')[0]
            if command in {'/start', '/cancel'}:
                self.store.set_state(user)
                await self.home(user)
                return
            if command == '/admin':
                await self.callback(user, 'admin')
                return
            if command in {'/support', '/paysupport'}:
                await self.callback(user, 'support')
                return
            if command == '/id':
                await self.send(user, f'Ваш Telegram ID: {user}')
                return
            if command in {'/recover', '/close_unpaid'}:
                if user not in self.store.admins:
                    raise ValueError('Нет доступа.')
                parts = text.split()
                if command == '/recover':
                    if len(parts) != 3:
                        raise ValueError('Формат: /recover order_id payment_id')
                    await self.recover(user, parts[1], parts[2])
                else:
                    if len(parts) != 2:
                        raise ValueError('Формат: /close_unpaid order_id')
                    self.store.set_state(user, 'close:' + parts[1])
                    await self.send(user, 'Проверьте в RollyPay, что платёж по этому order_id НЕ создан. Если платёж найден, используйте /recover. Для подтверждения отсутствия отправьте: НЕ СОЗДАН. /cancel — отмена.')
                return
            state = self.store.one('SELECT value FROM state WHERE user_id=?', (user,))
            if not state:
                await self.home(user)
                return
            if not text or len(text) > 3000:
                raise ValueError('Отправьте текст до 3000 символов. Файлы пока не поддерживаются.')
            action, _, arg = state['value'].partition(':')
            if action != 'support' and user not in self.store.admins:
                raise ValueError('Нет доступа.')
            if action == 'close':
                if text != 'НЕ СОЗДАН':
                    raise ValueError('Для закрытия отправьте НЕ СОЗДАН или /cancel.')
                with self.store.db:
                    changed = self.store.db.execute("UPDATE orders SET status='canceled' WHERE id=? AND status IN ('creating','uncertain') AND payment_id IS NULL", (arg,)).rowcount
                    if not changed:
                        raise ValueError('Заказ уже изменился или не требует сверки.')
                    order = self.store.one('SELECT * FROM orders WHERE id=?', (arg,))
                    self.store.enqueue('closed:' + arg, order['user_id'], 'Неудачная попытка создания платежа закрыта. Можно снова выбрать тариф.')
                    self.store.db.execute('DELETE FROM state WHERE user_id=?', (user,))
                await self.send(user, 'Заказ закрыт как неоплаченный. История сохранена.')
            elif action == 'loc_add':
                if len(text) > 60 or '\n' in text:
                    raise ValueError('Название страны — одна строка до 60 символов.')
                with self.store.db:
                    if self.store.one('SELECT id FROM locations WHERE name=?', (text,)):
                        raise ValueError('Эта страна уже добавлена.')
                    self.store.db.execute('INSERT INTO locations(name) VALUES(?)', (text,))
                    self.store.db.execute('DELETE FROM state WHERE user_id=?', (user,))
                await self.send(user, 'Страна добавлена.', buttons(('К локациям', 'locations')))
            elif action == 'tariff':
                parts = [p.strip() for p in text.split('|')]
                if len(parts) != 3:
                    raise ValueError('Формат: название | цена | число месяцев')
                self.store.save_tariff(*parts, tariff_id=None if arg == 'new' else int(arg))
                self.store.set_state(user)
                await self.send(user, 'Тариф сохранён.', buttons(('К тарифам', 'tariffs')))
            elif action == 'deliver':
                self.store.fulfill(int(arg), user, text)
                self.store.set_state(user)
                await self.send(user, 'Заявка выполнена. Ссылка и срок будут доставлены клиенту.')
            elif action in {'support', 'reply'}:
                target = user if action == 'support' else int(arg)
                with self.store.db:
                    row = self.store.db.execute('INSERT INTO messages(user_id,admin_id,body,created) VALUES(?,?,?,?)',
                        (target, None if action == 'support' else user, text, time.time()))
                    if action == 'support':
                        self.store.notify_admins(f'msg:{row.lastrowid}', f'Сообщение от клиента {user}:\n{text}', buttons(('Ответить', f'reply:{user}')))
                    else:
                        self.store.enqueue(f'msg:{row.lastrowid}', target, 'Ответ поддержки:\n' + text, buttons(('Ответить', 'support')))
                    self.store.db.execute('DELETE FROM state WHERE user_id=?', (user,))
                await self.send(user, 'Сообщение передано.')
        except (ValueError, OverflowError) as exc:
            # ValueError messages come only from validation; API parsing errors are generic below.
            await self.send(user, str(exc) or 'Некорректные данные.')
        except Exception:
            log.error('Update processing failed (update_id=%s)', update.get('update_id'))
            await self.send(user, 'Не удалось выполнить действие. Попробуйте позднее или обратитесь в поддержку.')

    async def webhook(self, request):
        if self.demo:
            return web.Response(status=503, text='Demo mode: payments disabled')
        raw = await request.read()
        if not verify_signature(raw, request.headers.get('X-Timestamp'), request.headers.get('X-Signature'), self.secret):
            return web.Response(status=403)
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError('Invalid event')
            event_type = event.get('event_type', '')
            if event_type.startswith('payout.'):
                return web.Response(text='OK')
            expected = {'payment.' + str(event.get('status'))}
            if event.get('status') == 'refunded':
                expected.add('refund_request.completed')
            if event_type not in expected:
                raise ValueError('Event mismatch')
            self.store.apply_payment(event)
        except (ValueError, TypeError):
            return web.Response(status=400, text='Invalid payment event')
        return web.Response(text='OK')

    async def poll(self):
        saved = self.store.one("SELECT value FROM settings WHERE key='offset'")
        offset = int(saved['value']) if saved else 0
        while True:
            try:
                updates = await self.telegram('getUpdates', offset=offset, timeout=30, allowed_updates=['message', 'callback_query'])
                for update in updates:
                    await self.handle(update)
                    offset = update['update_id'] + 1
                    with self.store.db:
                        self.store.db.execute("INSERT OR REPLACE INTO settings VALUES('offset',?)", (str(offset),))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.error('Telegram polling failed; retrying in 5 seconds')
                await asyncio.sleep(5)

    async def worker(self):
        last_reminder = 0
        while True:
            try:
                if time.time() - last_reminder > 3600:
                    self.store.reminders()
                    last_reminder = time.time()
                for item in self.store.all('SELECT * FROM outbox WHERE sent=0 AND next_try<=? ORDER BY id LIMIT 25', (time.time(),)):
                    try:
                        await self.send(item['chat_id'], item['body'], json.loads(item['keyboard']) if item['keyboard'] else None)
                    except Exception:
                        with self.store.db:
                            self.store.db.execute('UPDATE outbox SET attempts=attempts+1,next_try=? WHERE id=?',
                                (time.time() + min(3600, 2 ** min(item['attempts'] + 1, 12)), item['id']))
                        log.warning('Notification delivery deferred (outbox_id=%s)', item['id'])
                    else:
                        with self.store.db:
                            self.store.db.execute('UPDATE outbox SET sent=1 WHERE id=?', (item['id'],))
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.error('Notification worker failed; retrying')
            await asyncio.sleep(1)


async def main():
    demo = os.environ.get('DEMO_MODE', 'false').lower() == 'true'
    required = ['BOT_TOKEN', 'ADMIN_IDS'] + ([] if demo else ['ROLLYPAY_API_KEY', 'ROLLYPAY_SIGNING_SECRET'])
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise SystemExit('Заполните переменные окружения: ' + ', '.join(missing))
    admins = {int(value.strip()) for value in os.environ['ADMIN_IDS'].split(',')}
    if not admins or any(value <= 0 for value in admins):
        raise SystemExit('ADMIN_IDS должны содержать положительные Telegram ID.')
    mode = os.environ.get('ROLLYPAY_TEST', 'true').lower()
    if mode not in {'true', 'false'}:
        raise SystemExit('ROLLYPAY_TEST: true или false')
    db_path = Path(os.environ.get('DATABASE_PATH', 'data/bot.sqlite3'))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(str(db_path), admins)
    if demo and not store.one("SELECT value FROM settings WHERE key='demo_locations'"):
        with store.db:
            if not store.one('SELECT id FROM locations LIMIT 1'):
                store.db.executemany('INSERT INTO locations(name) VALUES(?)', [('🇩🇪 Германия',), ('🇳🇱 Нидерланды',), ('🇫🇮 Финляндия',)])
            store.db.execute("INSERT INTO settings VALUES('demo_locations','1')")
    # Only one process may own Telegram polling and this SQLite database.
    import fcntl
    lock = open(str(db_path) + '.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Бот с этой базой уже запущен.')
    async with aiohttp.ClientSession() as session:
        bot = Bot(store, session, os.environ['BOT_TOKEN'], os.environ.get('ROLLYPAY_API_KEY', ''),
                  os.environ.get('ROLLYPAY_SIGNING_SECRET', ''), mode == 'true', demo=demo)
        info = await bot.telegram('getWebhookInfo')
        if info.get('url'):
            raise SystemExit('У бота установлен Telegram webhook. Удалите его перед запуском polling.')
        await bot.telegram('setMyCommands', commands=[{'command': 'start', 'description': 'Главное меню'},
            {'command': 'support', 'description': 'Поддержка'}, {'command': 'paysupport', 'description': 'Помощь с оплатой'},
            {'command': 'cancel', 'description': 'Отменить ввод'}, {'command': 'id', 'description': 'Мой Telegram ID'}])
        app = web.Application(client_max_size=64 * 1024)
        app.router.add_post('/webhooks/rollypay', bot.webhook)
        async def health(request):
            return web.json_response({'status': 'ok'})
        app.router.add_get('/health', health)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, os.environ.get('HOST', '127.0.0.1'), int(os.environ.get('PORT', '8080'))).start()
        log.info('Bot started; payment test mode=%s', bot.test)
        tasks = [asyncio.create_task(bot.poll()), asyncio.create_task(bot.worker())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await runner.cleanup()
            store.db.close()
            lock.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
