import hashlib
import hmac
import json
import time
import unittest
import tempfile
from decimal import Decimal
from unittest.mock import AsyncMock
from datetime import datetime, timezone

from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer

from vpn_bot.app import Bot, verify_signature
from vpn_bot.store import Store, add_months


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(':memory:', {99})
        self.order = self.s.create_order(1, 1, False)

    def tearDown(self):
        self.s.db.close()

    def event(self, status='paid', **changes):
        value = dict(order_id=self.order['id'], payment_id='pay_1', amount='1400.00',
                     currency='RUB', test=False, status=status)
        value.update(changes)
        return value

    def test_no_ticket_before_payment_and_duplicate_payment(self):
        self.assertEqual(self.s.all('SELECT * FROM tickets'), [])
        self.s.apply_payment(self.event())
        self.s.apply_payment(self.event())
        self.assertEqual(len(self.s.all('SELECT * FROM tickets')), 1)
        self.assertEqual(len(self.s.all('SELECT * FROM outbox')), 2)
        self.assertEqual(self.s.all('SELECT * FROM subscriptions'), [])

    def test_mismatch_does_not_issue_ticket(self):
        for changes in ({'amount': '1'}, {'currency': 'USD'}, {'test': True}, {'test': 'false'}, {'payment_id': ''}):
            with self.assertRaises(ValueError):
                self.s.apply_payment(self.event(**changes))
        self.assertEqual(self.s.all('SELECT * FROM tickets'), [])

    def test_fulfill_once_and_extend_from_expiry(self):
        now = datetime(2026, 1, 31, 12, tzinfo=timezone.utc).timestamp()
        self.s.apply_payment(self.event())
        expiry = self.s.fulfill(1, 99, 'https://example.com/private', now)
        self.assertEqual(datetime.fromtimestamp(expiry, timezone.utc).day, 28)
        with self.assertRaises(ValueError):
            self.s.fulfill(1, 99, now=now)
        self.order = self.s.create_order(1, 1, False)
        self.s.apply_payment(self.event(payment_id='pay_2'))
        new_expiry = self.s.fulfill(2, 99, now=now + 86400)
        self.assertEqual(new_expiry, add_months(expiry, 1))

    def test_expired_renewal_starts_at_delivery(self):
        self.s.apply_payment(self.event())
        self.s.fulfill(1, 99, 'https://example.com/private', 100000)
        self.order = self.s.create_order(1, 1, False)
        self.s.apply_payment(self.event(payment_id='pay_2'))
        now = 20000000
        self.assertEqual(self.s.fulfill(2, 99, now=now), add_months(now, 1))

    def test_tariff_edits_do_not_change_order(self):
        self.s.save_tariff('Другой', '999', 12, 1)
        order = self.s.one('SELECT * FROM orders')
        self.assertEqual((order['amount'], order['months']), ('1400.00', 1))

    def test_refund_blocks_fulfillment_and_delayed_paid(self):
        self.s.apply_payment(self.event())
        self.s.apply_payment(self.event('refunded'))
        self.s.apply_payment(self.event())
        with self.assertRaises(ValueError):
            self.s.fulfill(1, 99, 'https://example.com/private')
        self.assertEqual(self.s.one('SELECT status FROM orders')['status'], 'refunded')

    def test_late_payment_after_expiry(self):
        self.s.apply_payment(self.event('expired'))
        self.s.apply_payment(self.event())
        self.assertEqual(len(self.s.all('SELECT * FROM tickets')), 1)

    def test_claim_ownership(self):
        self.s.apply_payment(self.event())
        with self.s.db:
            self.s.db.execute('UPDATE tickets SET assigned=77')
        with self.assertRaises(ValueError):
            self.s.fulfill(1, 99, 'https://example.com/private')

    def test_pending_order_blocks_duplicate_purchase(self):
        with self.assertRaises(ValueError):
            self.s.create_order(1, 1, False)

    def test_sandbox_never_grants_access(self):
        self.order = self.s.create_order(2, 1, True)
        self.s.apply_payment(self.event(test=True))
        self.s.fulfill(1, 99, 'https://example.com/test')
        self.assertEqual(self.s.all('SELECT * FROM subscriptions'), [])

    def test_reminders_deduplicated(self):
        self.s.apply_payment(self.event())
        expiry = self.s.fulfill(1, 99, 'https://example.com/private')
        self.s.reminders(expiry - 3600)
        self.s.reminders(expiry - 3600)
        self.assertEqual(len(self.s.all("SELECT * FROM outbox WHERE event_key LIKE 'remind:%'")), 1)
        self.s.reminders(expiry + 1)
        self.assertEqual(len(self.s.all("SELECT * FROM outbox WHERE event_key LIKE 'remind:%'")), 3)

    def test_location_snapshot_and_renewal(self):
        with self.s.db:
            self.s.db.execute("INSERT INTO locations(name) VALUES('Германия')")
        self.order = self.s.create_order(2, 1, False, 1)
        self.s.apply_payment(self.event())
        self.s.fulfill(1, 99, 'https://example.com/private')
        with self.s.db:
            self.s.db.execute("UPDATE locations SET active=0,name='Другое'")
        renewal = self.s.create_order(2, 1, False)
        self.assertEqual(renewal['location'], 'Германия')
        with self.assertRaises(ValueError):
            self.s.create_order(3, 1, False, 1)
        with self.assertRaises(ValueError):
            self.s.create_order(3, 1, False)

    def test_rental_discount_does_not_reduce_markup(self):
        tariffs = self.s.all('SELECT * FROM tariffs ORDER BY months')
        for tariff, discount in zip(tariffs, (0, 3, 6, 12)):
            months = tariff['months']
            expected = Decimal(700) * months * (2 - Decimal(discount) / 100)
            self.assertEqual(Decimal(tariff['amount']), expected)
        self.assertEqual(len(tariffs), 4)

    def test_price_migration_preserves_orders_and_admin_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            path = folder + '/test.sqlite3'
            db = Store(path, {99})
            with db.db:
                db.db.execute("DELETE FROM settings WHERE key IN ('rental_pricing_v1','rental_pricing_v2')")
                db.db.execute('DELETE FROM tariffs WHERE id>1')
                db.db.execute("UPDATE tariffs SET amount='1200.00' WHERE id=1")
            old_order = db.create_order(5, 1, False)
            db.db.close()
            db = Store(path, {99})
            self.assertEqual(db.one('SELECT amount FROM tariffs WHERE id=1')['amount'], '1400.00')
            self.assertEqual(db.one('SELECT amount FROM orders WHERE id=?', (old_order['id'],))['amount'], '1200.00')
            db.save_tariff('VPN — 1 месяц', '2500', 1, 1)
            db.db.close()
            db = Store(path, {99})
            self.assertEqual(db.one('SELECT amount FROM tariffs WHERE id=1')['amount'], '2500.00')
            self.assertEqual(len(db.all('SELECT * FROM tariffs')), 4)
            db.db.close()

    def test_bad_prices(self):
        for value in ('NaN', 'Infinity', '-1', '0', '1.001', 'x'):
            with self.assertRaises(ValueError):
                self.s.save_tariff('Test', value, 1)


class SignatureTests(unittest.TestCase):
    def test_signature_and_timestamp(self):
        body, timestamp, secret = b'{"test":false}', '10000', 'secret'
        signature = hmac.new(secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature(body, timestamp, signature, secret, now=10001))
        self.assertFalse(verify_signature(body + b' ', timestamp, signature, secret, now=10001))
        self.assertFalse(verify_signature(body, timestamp, signature, secret, now=20000))
        self.assertFalse(verify_signature(body, None, None, secret))


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = Store(':memory:', {99})
        self.requests = []
        self.sent = []
        async def payment(request):
            self.requests.append((dict(request.headers), await request.json()))
            return web.json_response({'payment_id': 'pay_1', 'pay_url': 'https://pay.example/1'})
        async def telegram(request):
            self.sent.append(await request.json())
            return web.json_response({'ok': True, 'result': {}})
        mock = web.Application()
        mock.router.add_post('/api/v1/payments', payment)
        mock.router.add_post('/bottoken/{method}', telegram)
        self.server = TestServer(mock)
        await self.server.start_server()
        self.session = ClientSession()
        self.bot = Bot(self.s, self.session, 'token', 'key', 'secret', False,
                       str(self.server.make_url('')), str(self.server.make_url('')))
        app = web.Application()
        app.router.add_post('/webhooks/rollypay', self.bot.webhook)
        self.webhook_server = TestServer(app)
        await self.webhook_server.start_server()

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()
        await self.webhook_server.close()
        self.s.db.close()

    async def post_event(self, event, signature=True):
        raw = json.dumps(event).encode()
        stamp = str(int(time.time()))
        sig = hmac.new(b'secret', stamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
        async with self.session.post(self.webhook_server.make_url('/webhooks/rollypay'), data=raw,
                   headers={'X-Timestamp': stamp, 'X-Signature': sig if signature else 'bad'}) as response:
            return response.status

    async def test_checkout_webhook_manual_delivery(self):
        await self.bot.checkout(1, 1)
        headers, payload = self.requests[0]
        self.assertEqual(headers['X-API-Key'], 'key')
        self.assertTrue(headers['X-Nonce'])
        self.assertEqual(payload['amount'], '1400.00')
        self.assertFalse(payload['test'])
        order = self.s.one('SELECT * FROM orders')
        event = dict(order_id=order['id'], payment_id='pay_1', amount='1400.00', currency='RUB',
                     status='paid', event_type='payment.paid', test=False)
        self.assertEqual(await self.post_event(event, False), 403)
        self.assertEqual(self.s.all('SELECT * FROM tickets'), [])
        self.assertEqual(await self.post_event(event), 200)
        self.assertEqual(await self.post_event(event), 200)
        self.assertEqual(len(self.s.all('SELECT * FROM tickets')), 1)
        self.s.fulfill(1, 99, 'https://example.com/personal')
        self.assertEqual(self.s.one('SELECT * FROM subscriptions')['link'], 'https://example.com/personal')

    async def test_admin_callback_denied_to_customer(self):
        for data in ('admin', 'tariffs', 'toggle:1', 'finish:1', 'reply:99', 'deliver:1', 'claim:1'):
            with self.assertRaises(ValueError):
                await self.bot.callback(1, data)
        self.assertEqual(self.s.one('SELECT active FROM tariffs')['active'], 1)

    async def test_support_and_admin_reply_persisted(self):
        async def message(user, text):
            await self.bot.handle({'update_id': 1, 'message': {'from': {'id': user, 'first_name': 'User'},
                    'chat': {'id': user, 'type': 'private'}, 'text': text}})
        await message(1, '/support')
        await message(1, 'Нужна помощь')
        await self.bot.callback(99, 'reply:1')
        await message(99, 'Вот ответ')
        self.assertEqual(len(self.s.all('SELECT * FROM messages')), 2)
        self.assertEqual(len(self.s.all('SELECT * FROM outbox')), 2)

    async def test_uncertain_creation_is_recoverable(self):
        original = self.bot.rolly
        self.bot.rolly = AsyncMock(side_effect=RuntimeError('timeout'))
        await self.bot.checkout(1, 1)
        order = self.s.one('SELECT * FROM orders')
        self.assertEqual(order['status'], 'uncertain')
        self.assertEqual(self.s.all('SELECT * FROM tickets'), [])
        self.bot.rolly = AsyncMock(return_value=dict(order_id=order['id'], payment_id='pay_recovered',
            amount='1400.00', payment_currency='RUB', status='paid', test=False))
        await self.bot.recover(99, order['id'], 'pay_recovered')
        self.assertEqual(len(self.s.all('SELECT * FROM tickets')), 1)
        self.bot.rolly = original

    async def test_missing_test_flag_cannot_recover_payment(self):
        order = self.s.create_order(1, 1, False)
        self.bot.rolly = AsyncMock(return_value=dict(order_id=order['id'], payment_id='pay_1',
            amount='1400.00', payment_currency='RUB', status='paid'))
        with self.assertRaises(ValueError):
            await self.bot.recover(99, order['id'], 'pay_1')
        self.assertEqual(self.s.all('SELECT * FROM tickets'), [])

    async def test_group_messages_ignored(self):
        await self.bot.handle({'message': {'from': {'id': 99}, 'chat': {'type': 'group'}, 'text': '/admin'}})
        self.assertEqual(self.sent, [])

    async def test_api_poll_confirms_without_webhook_and_handles_refund(self):
        await self.bot.checkout(1, 1)
        order = self.s.one('SELECT * FROM orders')
        self.bot.rolly = AsyncMock(return_value=dict(order_id=order['id'], payment_id=order['payment_id'],
            amount='1400.00', payment_currency='RUB', status='paid'))
        await self.bot.sync_payment(order)
        await self.bot.sync_payment(order)
        self.assertEqual(len(self.s.all('SELECT * FROM tickets')), 1)
        self.bot.rolly.return_value['status'] = 'refunded'
        await self.bot.sync_payment(order)
        self.assertEqual(self.s.one('SELECT state FROM tickets')['state'], 'blocked')

    async def test_api_poll_rejects_wrong_order(self):
        await self.bot.checkout(1, 1)
        order = self.s.one('SELECT * FROM orders')
        self.bot.rolly = AsyncMock(return_value=dict(order_id='other', payment_id=order['payment_id'],
            amount='1400.00', payment_currency='RUB', status='paid'))
        with self.assertRaises(ValueError):
            await self.bot.sync_payment(order)
        self.assertEqual(self.s.all('SELECT * FROM tickets'), [])

    async def test_demo_payment_requires_admin_and_never_calls_provider(self):
        self.bot.demo = True
        self.bot.test = True
        self.bot.rolly = AsyncMock(side_effect=AssertionError('No real payments in demo'))
        await self.bot.checkout(1, 1)
        order = self.s.one('SELECT * FROM orders')
        self.assertEqual(self.s.all('SELECT * FROM tickets'), [])
        with self.assertRaises(ValueError):
            await self.bot.callback(1, 'demopay:' + order['id'])
        await self.bot.callback(99, 'demopay:' + order['id'])
        await self.bot.callback(99, 'demopay:' + order['id'])
        self.assertEqual(len(self.s.all('SELECT * FROM tickets')), 1)
        self.s.fulfill(1, 99, 'https://example.com/demo')
        self.assertEqual(self.s.all('SELECT * FROM subscriptions'), [])
        self.bot.rolly.assert_not_called()

    async def test_demo_callback_disabled_in_normal_mode(self):
        with self.assertRaises(ValueError):
            await self.bot.callback(99, 'demopay:arbitrary')


if __name__ == '__main__':
    unittest.main()
