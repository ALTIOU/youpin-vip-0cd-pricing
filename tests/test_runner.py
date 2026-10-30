# SPDX-License-Identifier: AGPL-3.0-only
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from youpin_rent_repricer.reprice_runner import run_cycle
from youpin_rent_repricer.uu_client import UUError


POLICY = {'interval_seconds': 3600, 'minimum_price': '0.01',
          'undercut_price': '0.01', 'listing_ids': ['100']}


def shelf(item_id='100', price='0.31', template=3874):
    return {'id': item_id, 'name': 'own item', 'templateId': template,
            'shortLeaseAmount': price, 'longLeaseAmount': '0.20',
            'depositAmount': '606.4', 'leaseMaxDays': 30,
            'commodityCanLease': 1, 'commodityCanSell': 0, 'sellAmount': '0'}


def init_info(ids=('100',)):
    return {'normalLeaseCompensationMap': {item_id: {'compensationTypeCode': 0} for item_id in ids},
            'zeroCDRentConfigMap': {item_id: {
                'zeroCDRentSwitch': 1, 'pricingType': 0,
                'marketDynamicPricingMinCoefficient': '80', 'needReNew': False,
            } for item_id in ids}}


def quote(item_id='200', price='0.07', seller='777777', template=3874, **extras):
    return {'Id': item_id, 'UserId': seller, 'TemplateId': template,
            'CanLease': 1, 'LeaseMaxDays': 8, 'LeaseUnitPrice': price,
            'IsMine': False, **extras}


def snapshot(rows=None, complete=True, age_seconds=0):
    return {'coverage_complete': complete, 'rows': rows or [quote()],
            'query_started_at': (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()}


def client_for(row=None):
    client = Mock()
    client.state = {'user_id': '900', 'device_token': 'D' * 24, 'token': 'do-not-output-token'}
    client.userinfo.return_value = {}
    client.zero_cd_shelf.return_value = [row or shelf()]
    client.reprice_init.return_value = init_info()
    client.change_short_rent.return_value = {'accepted': True, 'readback_required': True}
    return client


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Path(self.temp.name) / 'pending-journal.json'
        self.market = patch('youpin_rent_repricer.reprice_runner.read_market_snapshot', return_value=snapshot())
        self.reader = self.market.start()

    def tearDown(self):
        self.market.stop()
        self.temp.cleanup()

    def test_read_only_default_previews_market_minus_cent(self):
        client = client_for()
        result = run_cycle(client, POLICY)
        self.assertTrue(result['ok'])
        self.assertEqual(result['mode'], 'preview')
        self.assertEqual(result['items'][0]['target_short_rent'], '0.06')
        self.assertEqual(result['items'][0]['current_long_rent'], '0.20')
        self.assertEqual(result['items'][0]['target_long_rent'], '0.20')
        self.assertFalse(result['items'][0]['cap_long_rent_at_short'])
        self.assertFalse(result['items'][0]['sync_long_rent_at_short'])
        client.change_short_rent.assert_not_called()
        self.assertFalse(self.journal.exists())
        serialized = json.dumps(result)
        self.assertNotIn('do-not-output-token', serialized)
        self.assertNotIn('777777', serialized)

    def test_own_price_and_ids_are_excluded_before_peer_minimum(self):
        own = shelf(price='0.15')
        client = client_for(own)
        self.reader.return_value = snapshot([
            quote('100', '0.15', seller='900'),
            quote('201', '0.01', seller='900'),
            quote('202', '0.01', IsMine=True),
            quote('203', '0.19'),
        ])
        result = run_cycle(client, POLICY)
        self.assertEqual(result['items'][0]['market_lowest'], '0.19')
        self.assertEqual(result['items'][0]['target_short_rent'], '0.18')

    def test_one_cent_market_has_one_cent_target(self):
        self.reader.return_value = snapshot([quote(price='0.01')])
        result = run_cycle(client_for(), POLICY)
        self.assertEqual(result['items'][0]['target_short_rent'], '0.01')

    def test_wrong_template_unavailable_and_invalid_prices_do_not_compete(self):
        self.reader.return_value = snapshot([
            quote('201', '0.01', template=999), quote('202', '0.01', CanLease=0),
            quote('203', '0.001'), quote('204', 'NaN'), quote('205', '0.01', LeaseMaxDays=0),
            quote('206', '0.08'),
        ])
        result = run_cycle(client_for(), POLICY)
        self.assertEqual(result['items'][0]['market_lowest'], '0.08')
        self.assertEqual(result['items'][0]['eligible_quotes'], 1)

    def test_incomplete_and_stale_market_never_write(self):
        for market in (snapshot(complete=False), snapshot(age_seconds=301)):
            with self.subTest(coverage=market['coverage_complete']):
                self.reader.return_value = market
                client = client_for()
                result = run_cycle(client, POLICY, apply=True, journal_path=self.journal)
                self.assertFalse(result['ok'])
                client.change_short_rent.assert_not_called()

    def test_existing_target_does_not_put(self):
        client = client_for(shelf(price='0.06'))
        result = run_cycle(client, POLICY, apply=True, journal_path=self.journal)
        self.assertEqual(result['items'][0]['status'], 'unchanged')
        client.reprice_init.assert_not_called()
        client.change_short_rent.assert_not_called()

    def test_verified_write_preserves_non_target_fields_and_clears_journal(self):
        client = client_for()
        client.zero_cd_shelf.side_effect = [[shelf()], [shelf()], [shelf(price='0.06')]]
        result = run_cycle(client, POLICY, apply=True, journal_path=self.journal)
        self.assertTrue(result['ok'])
        self.assertEqual(result['items'][0]['status'], 'verified')
        client.change_short_rent.assert_called_once()
        payload = client.change_short_rent.call_args.args[0]['Commoditys'][0]
        self.assertEqual(payload['LeaseUnitPrice'], 0.06)
        self.assertEqual(payload['LongLeaseUnitPrice'], 0.20)
        self.assertEqual(payload['LeaseDeposit'], '606.4')
        self.assertEqual(payload['ZeroCDConfig'], {'PricingType': 0, 'MinCoefficient': '80'})
        self.assertEqual(json.loads(self.journal.read_text())['pending'], {})
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)

    def test_authorized_long_cap_is_written_and_verified_with_short_rent(self):
        client = client_for()
        after = shelf(price='0.06')
        after['longLeaseAmount'] = '0.06'
        client.zero_cd_shelf.side_effect = [[shelf()], [shelf()], [after]]
        result = run_cycle(client, {**POLICY, 'cap_long_rent_at_short': True},
                           apply=True, journal_path=self.journal)
        self.assertTrue(result['ok'])
        item = result['items'][0]
        self.assertEqual(item['status'], 'verified')
        self.assertEqual(item['current_long_rent'], '0.20')
        self.assertEqual(item['target_long_rent'], '0.06')
        self.assertTrue(item['cap_long_rent_at_short'])
        payload = client.change_short_rent.call_args.args[0]['Commoditys'][0]
        self.assertEqual(payload['LongLeaseUnitPrice'], 0.06)
        self.assertEqual(json.loads(self.journal.read_text())['pending'], {})

    def test_authorized_cap_preserves_long_rent_that_is_already_lower(self):
        before = shelf()
        before['longLeaseAmount'] = '0.04'
        client = client_for(before)
        result = run_cycle(client, {**POLICY, 'cap_long_rent_at_short': True})
        self.assertTrue(result['ok'])
        self.assertEqual(result['items'][0]['target_long_rent'], '0.04')

    def test_long_cap_flag_is_strict_boolean(self):
        for flag in (None, 0, 1, 'true', 'false'):
            with self.subTest(flag=flag):
                client = client_for()
                result = run_cycle(client, {**POLICY, 'cap_long_rent_at_short': flag})
                self.assertEqual(result['reason'], 'invalid_long_rent_cap_flag')
                client.userinfo.assert_not_called()

    def test_long_sync_flag_is_strict_boolean(self):
        for flag in (None, 0, 1, 'true', 'false'):
            with self.subTest(flag=flag):
                client = client_for()
                result = run_cycle(client, {**POLICY, 'sync_long_rent_at_short': flag})
                self.assertEqual(result['reason'], 'invalid_long_rent_sync_flag')
                client.userinfo.assert_not_called()

    def test_long_modes_cannot_conflict(self):
        client = client_for()
        result = run_cycle(client, {**POLICY, 'sync_long_rent_at_short': True,
                                   'cap_long_rent_at_short': True})
        self.assertEqual(result['reason'], 'conflicting_long_rent_modes')
        client.userinfo.assert_not_called()

    def test_long_sync_previews_raising_a_lower_long_rent_to_target(self):
        before = shelf()
        before['longLeaseAmount'] = '0.04'
        result = run_cycle(client_for(before), {**POLICY, 'sync_long_rent_at_short': True})
        self.assertTrue(result['ok'])
        item = result['items'][0]
        self.assertEqual(item['current_long_rent'], '0.04')
        self.assertEqual(item['target_long_rent'], '0.06')
        self.assertTrue(item['sync_long_rent_at_short'])

    def test_short_at_target_but_long_different_still_writes_when_sync_enabled(self):
        before = shelf(price='0.06')
        before['longLeaseAmount'] = '0.04'
        after = shelf(price='0.06')
        after['longLeaseAmount'] = '0.06'
        client = client_for(before)
        client.zero_cd_shelf.side_effect = [[before], [before], [after]]
        result = run_cycle(client, {**POLICY, 'sync_long_rent_at_short': True},
                           apply=True, journal_path=self.journal)
        self.assertTrue(result['ok'])
        self.assertEqual(result['items'][0]['status'], 'verified')
        client.change_short_rent.assert_called_once()
        payload = client.change_short_rent.call_args.args[0]['Commoditys'][0]
        self.assertEqual(payload['LeaseUnitPrice'], 0.06)
        self.assertEqual(payload['LongLeaseUnitPrice'], 0.06)

    def test_long_sync_can_enable_long_rent_from_zero(self):
        before = shelf()
        before['longLeaseAmount'] = None
        after = shelf(price='0.06')
        after['longLeaseAmount'] = '0.06'
        client = client_for(before)
        client.zero_cd_shelf.side_effect = [[before], [before], [after]]
        result = run_cycle(client, {**POLICY, 'sync_long_rent_at_short': True},
                           apply=True, journal_path=self.journal)
        self.assertTrue(result['ok'])
        self.assertEqual(result['items'][0]['current_long_rent'], '0.00')
        self.assertEqual(result['items'][0]['target_long_rent'], '0.06')

    def test_long_sync_pending_retains_original_request_semantics(self):
        before = shelf()
        before['longLeaseAmount'] = '0.04'
        client = client_for(before)
        client.change_short_rent.side_effect = UUError('network_error')
        run_cycle(client, {**POLICY, 'sync_long_rent_at_short': True},
                  apply=True, journal_path=self.journal)
        pending = json.loads(self.journal.read_text())['pending']['100']
        self.assertTrue(pending['sync_long_rent_at_short'])
        self.assertFalse(pending['cap_long_rent_at_short'])
        after = shelf(price='0.06')
        after['longLeaseAmount'] = '0.06'
        recovery = client_for(after)
        result = run_cycle(recovery, POLICY, apply=True, journal_path=self.journal)
        self.assertTrue(result['ok'])
        self.assertEqual(result['items'][0]['reason'], 'previous_pending_write_verified_no_new_put')
        recovery.change_short_rent.assert_not_called()

    def test_cap_pending_remembers_sent_flag_even_if_future_policy_changes(self):
        client = client_for()
        client.change_short_rent.side_effect = UUError('network_error')
        run_cycle(client, {**POLICY, 'cap_long_rent_at_short': True},
                  apply=True, journal_path=self.journal)
        self.assertTrue(json.loads(self.journal.read_text())['pending']['100']['cap_long_rent_at_short'])
        after = shelf(price='0.06')
        after['longLeaseAmount'] = '0.06'
        recovery = client_for(after)
        result = run_cycle(recovery, POLICY, apply=True, journal_path=self.journal)
        self.assertTrue(result['ok'])
        self.assertEqual(result['items'][0]['reason'], 'previous_pending_write_verified_no_new_put')
        recovery.change_short_rent.assert_not_called()

    def test_old_pending_without_cap_flag_does_not_assume_new_policy_authorization(self):
        client = client_for()
        client.change_short_rent.side_effect = UUError('network_error')
        run_cycle(client, POLICY, apply=True, journal_path=self.journal)
        journal = json.loads(self.journal.read_text())
        del journal['pending']['100']['cap_long_rent_at_short']
        self.journal.write_text(json.dumps(journal))
        after = shelf(price='0.06')
        after['longLeaseAmount'] = '0.06'
        recovery = client_for(after)
        result = run_cycle(recovery, {**POLICY, 'cap_long_rent_at_short': True},
                           apply=True, journal_path=self.journal)
        self.assertFalse(result['ok'])
        self.assertEqual(result['items'][0]['reason'], 'pending_write_unconfirmed_no_replay')
        recovery.change_short_rent.assert_not_called()

    def test_long_cap_must_match_in_readback_or_remain_pending(self):
        client = client_for()
        client.zero_cd_shelf.side_effect = [[shelf()], [shelf()],
            [shelf(price='0.06')], [shelf(price='0.06')], [shelf(price='0.06')]]
        result = run_cycle(client, {**POLICY, 'cap_long_rent_at_short': True},
                           apply=True, journal_path=self.journal)
        self.assertFalse(result['ok'])
        self.assertEqual(result['items'][0]['reason'], 'readback_unconfirmed_no_replay')
        client.change_short_rent.assert_called_once()
        self.assertTrue(json.loads(self.journal.read_text())['pending']['100']['cap_long_rent_at_short'])

    def test_preflight_shelf_or_activity_drift_never_puts(self):
        client = client_for()
        changed = shelf()
        changed['depositAmount'] = '700'
        client.zero_cd_shelf.side_effect = [[shelf()], [changed]]
        result = run_cycle(client, POLICY, apply=True, journal_path=self.journal)
        self.assertEqual(result['items'][0]['reason'], 'shelf_changed_before_write')
        client.change_short_rent.assert_not_called()
        client = client_for()
        new_init = init_info()
        new_init['zeroCDRentConfigMap']['100']['marketDynamicPricingMinCoefficient'] = '81'
        client.reprice_init.side_effect = [init_info(), new_init]
        result = run_cycle(client, POLICY, apply=True, journal_path=self.journal)
        self.assertEqual(result['items'][0]['reason'], 'activity_changed_before_write')
        client.change_short_rent.assert_not_called()

    def test_quote_expiring_during_preflight_never_puts(self):
        client = client_for()
        market = snapshot()
        self.reader.return_value = market
        calls = 0
        def current_init(_):
            nonlocal calls
            calls += 1
            if calls == 2:
                market['query_started_at'] = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
            return init_info()
        client.reprice_init.side_effect = current_init
        result = run_cycle(client, POLICY, apply=True, journal_path=self.journal)
        self.assertEqual(result['items'][0]['reason'], 'market_snapshot_not_fresh')
        client.change_short_rent.assert_not_called()

    def test_timeout_keeps_pending_and_next_cycle_never_replays_put(self):
        client = client_for()
        client.change_short_rent.side_effect = UUError('network_error')
        result = run_cycle(client, POLICY, apply=True, journal_path=self.journal)
        self.assertFalse(result['ok'])
        client.change_short_rent.assert_called_once()
        self.assertIn('100', json.loads(self.journal.read_text())['pending'])
        next_client = client_for()
        self.reader.reset_mock()
        result = run_cycle(next_client, POLICY, apply=True, journal_path=self.journal)
        self.assertFalse(result['ok'])
        self.assertEqual(result['items'][0]['reason'], 'pending_write_unconfirmed_no_replay')
        next_client.change_short_rent.assert_not_called()
        self.reader.assert_not_called()

    def test_pending_target_can_be_reconciled_by_reads_without_put(self):
        client = client_for()
        client.change_short_rent.side_effect = UUError('network_error')
        run_cycle(client, POLICY, apply=True, journal_path=self.journal)
        recovery = client_for(shelf(price='0.06'))
        self.reader.reset_mock()
        result = run_cycle(recovery, POLICY, apply=True, journal_path=self.journal)
        self.assertTrue(result['ok'])
        self.assertEqual(result['items'][0]['reason'], 'previous_pending_write_verified_no_new_put')
        recovery.change_short_rent.assert_not_called()
        self.reader.assert_not_called()
        self.assertEqual(json.loads(self.journal.read_text())['pending'], {})

    def test_readback_must_verify_more_than_success_count_or_target_price(self):
        changed = shelf(price='0.06')
        changed['longLeaseAmount'] = '0.01'
        client = client_for()
        client.zero_cd_shelf.side_effect = [[shelf()], [shelf()], [changed], [changed], [changed]]
        result = run_cycle(client, POLICY, apply=True, journal_path=self.journal)
        self.assertFalse(result['ok'])
        self.assertEqual(result['items'][0]['reason'], 'readback_unconfirmed_no_replay')
        client.change_short_rent.assert_called_once()
        self.assertIn('100', json.loads(self.journal.read_text())['pending'])

    def test_unknown_error_is_sanitized_and_journal_retained(self):
        client = client_for()
        client.change_short_rent.side_effect = RuntimeError('do-not-output-token')
        result = run_cycle(client, POLICY, apply=True, journal_path=self.journal)
        self.assertFalse(result['ok'])
        self.assertNotIn('do-not-output-token', json.dumps(result))
        self.assertEqual(result['reason'], 'unexpected_cycle_error_no_retry')
        self.assertIn('100', json.loads(self.journal.read_text())['pending'])

    def test_apply_requires_journal_and_rejects_extra_floor(self):
        client = client_for()
        result = run_cycle(client, POLICY, apply=True)
        self.assertEqual(result['reason'], 'durable_journal_required_for_apply')
        client.userinfo.assert_not_called()
        result = run_cycle(client, {**POLICY, 'floor_price': '0.10'})
        self.assertEqual(result['reason'], 'unsupported_extra_floor')
        client.userinfo.assert_not_called()

    def test_missing_allowlisted_item_does_not_price_other_own_goods(self):
        client = client_for(shelf('101'))
        result = run_cycle(client, POLICY, apply=True, journal_path=self.journal)
        self.assertTrue(result['ok'])
        self.assertEqual(result['items'][0]['status'], 'skipped')
        self.assertEqual(result['items'][0]['reason'], 'allowlisted_listing_not_on_own_shelf')
        self.reader.assert_not_called()
        client.change_short_rent.assert_not_called()

    def test_missing_shelf_item_with_unknown_pending_stays_blocked(self):
        client = client_for()
        client.change_short_rent.side_effect = UUError('network_error')
        run_cycle(client, POLICY, apply=True, journal_path=self.journal)
        missing = client_for()
        missing.zero_cd_shelf.return_value = []
        result = run_cycle(missing, POLICY, apply=True, journal_path=self.journal)
        self.assertFalse(result['ok'])
        self.assertEqual(result['items'][0]['status'], 'blocked')
        self.assertEqual(result['items'][0]['reason'], 'pending_write_unconfirmed_no_replay')
        missing.change_short_rent.assert_not_called()


if __name__ == '__main__':
    unittest.main()
