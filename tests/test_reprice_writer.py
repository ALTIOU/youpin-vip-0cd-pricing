# SPDX-License-Identifier: AGPL-3.0-only
from copy import deepcopy
from decimal import Decimal
import json
import unittest

from youpin_rent_repricer.reprice_writer import (
    RepriceContractError, activity_fields, build_short_rent_payload, readback_matches,
)


def sample_row():
    return {'id': '123456789', 'shortLeaseAmount': '0.31', 'longLeaseAmount': '0.20',
            'depositAmount': '1234.50', 'leaseMaxDays': 8,
            'commodityCanLease': True, 'commodityCanSell': False, 'sellAmount': None}


def sample_init():
    return {'normalLeaseCompensationMap': {'123456789': {'compensationTypeCode': 2}},
            'zeroCDRentConfigMap': {'123456789': {'zeroCDRentSwitch': 1, 'needReNew': False,
                                                'pricingType': 0, 'marketDynamicPricingMinCoefficient': 80}},
            'subletIncentiveConfigMap': {'123456789': {'subletIncentiveSwitch': 1}}}


class WriterTests(unittest.TestCase):
    def test_dynamic_profile_and_all_non_target_values_are_preserved(self):
        row, init = sample_row(), sample_init()
        original_row, original_init = deepcopy(row), deepcopy(init)
        body = build_short_rent_payload(row, init, Decimal('0.25'), device_session='A'*24)
        self.assertEqual(body['Commoditys'], [{
            'CommodityId': 123456789, 'IsCanLease': True, 'IsCanSold': False,
            'LeaseDeposit': '1234.5', 'LeaseMaxDays': 8, 'LeaseUnitPrice': 0.25,
            'LongLeaseUnitPrice': 0.2, 'Price': 0,
            'CompensationType': 2, 'SupportZeroCD': 1, 'supportSubletIncentive': 1,
            'ZeroCDConfig': {'PricingType': 0, 'MinCoefficient': '80'},
        }])
        json.dumps(body)
        self.assertEqual(row, original_row)
        self.assertEqual(init, original_init)

    def test_minimum_target_has_no_extra_floor(self):
        body = build_short_rent_payload(sample_row(), sample_init(), '0.01', device_session='A'*24)
        self.assertEqual(body['Commoditys'][0]['LeaseUnitPrice'], 0.01)

    def test_optional_long_rent_cap_only_lowers_when_required(self):
        row, init = sample_row(), sample_init()
        before_row, before_init = deepcopy(row), deepcopy(init)
        unchanged = build_short_rent_payload(row, init, '0.10', device_session='A'*24)
        capped = build_short_rent_payload(row, init, '0.10', device_session='A'*24, cap_long_rent_at_short=True)
        self.assertEqual(unchanged['Commoditys'][0]['LongLeaseUnitPrice'], 0.2)
        self.assertEqual(capped['Commoditys'][0]['LongLeaseUnitPrice'], 0.1)
        expected = deepcopy(unchanged)
        expected['Commoditys'][0]['LongLeaseUnitPrice'] = 0.1
        self.assertEqual(capped, expected)
        lower_already = build_short_rent_payload(row, init, '0.25', device_session='A'*24, cap_long_rent_at_short=True)
        self.assertEqual(lower_already['Commoditys'][0]['LongLeaseUnitPrice'], 0.2)
        self.assertEqual(row, before_row)
        self.assertEqual(init, before_init)

    def test_optional_long_rent_cap_readback_is_exact(self):
        before = sample_row()
        after = deepcopy(before)
        after.update(shortLeaseAmount='0.10', longLeaseAmount='0.10')
        self.assertFalse(readback_matches(before, after, '0.10'))
        self.assertTrue(readback_matches(before, after, '0.10', cap_long_rent_at_short=True))
        after['longLeaseAmount'] = '0.09'
        self.assertFalse(readback_matches(before, after, '0.10', cap_long_rent_at_short=True))
        after.update(shortLeaseAmount='0.25', longLeaseAmount='0.20')
        self.assertTrue(readback_matches(before, after, '0.25', cap_long_rent_at_short=True))
        after['depositAmount'] = '1234.51'
        self.assertFalse(readback_matches(before, after, '0.25', cap_long_rent_at_short=True))

    def test_long_rent_cap_requires_a_boolean_and_preserves_absence(self):
        for flag in ('true', 1, None):
            with self.subTest(flag=flag), self.assertRaises(RepriceContractError):
                build_short_rent_payload(sample_row(), sample_init(), '0.10', device_session='A'*24, cap_long_rent_at_short=flag)
        row = sample_row()
        row['longLeaseAmount'] = None
        body = build_short_rent_payload(row, sample_init(), '0.01', device_session='A'*24, cap_long_rent_at_short=True)
        self.assertNotIn('LongLeaseUnitPrice', body['Commoditys'][0])

    def test_sync_long_rent_always_matches_short_target(self):
        for old_long, target in (('0.15', '0.18'), ('0.31', '0.05'), (None, '0.01'), ('0', '0.01')):
            with self.subTest(old_long=old_long, target=target):
                row, init = sample_row(), sample_init()
                row['longLeaseAmount'] = old_long
                original_row, original_init = deepcopy(row), deepcopy(init)
                body = build_short_rent_payload(row, init, target, device_session='A'*24, sync_long_rent_at_short=True)
                item = body['Commoditys'][0]
                self.assertEqual(item['LongLeaseUnitPrice'], item['LeaseUnitPrice'])
                self.assertEqual(Decimal(str(item['LongLeaseUnitPrice'])), Decimal(target))
                self.assertEqual(item['ZeroCDConfig'], {'PricingType': 0, 'MinCoefficient': '80'})
                self.assertEqual(row, original_row)
                self.assertEqual(init, original_init)
                after = deepcopy(row)
                after.update(shortLeaseAmount=target, longLeaseAmount=target)
                self.assertTrue(readback_matches(row, after, target, sync_long_rent_at_short=True))
                after['longLeaseAmount'] = str(Decimal(target) + Decimal('0.01'))
                self.assertFalse(readback_matches(row, after, target, sync_long_rent_at_short=True))
                after.update(longLeaseAmount=target, depositAmount='1234.51')
                self.assertFalse(readback_matches(row, after, target, sync_long_rent_at_short=True))

    def test_sync_mode_is_boolean_and_cannot_be_combined_with_cap(self):
        for flag in ('true', 1, None):
            with self.subTest(flag=flag), self.assertRaises(RepriceContractError):
                build_short_rent_payload(sample_row(), sample_init(), '0.18', device_session='A'*24, sync_long_rent_at_short=flag)
            with self.subTest(readback_flag=flag), self.assertRaises(RepriceContractError):
                readback_matches(sample_row(), sample_row(), '0.18', sync_long_rent_at_short=flag)
        with self.assertRaises(RepriceContractError):
            build_short_rent_payload(sample_row(), sample_init(), '0.18', device_session='A'*24, cap_long_rent_at_short=True, sync_long_rent_at_short=True)
        with self.assertRaises(RepriceContractError):
            readback_matches(sample_row(), sample_row(), '0.18', cap_long_rent_at_short=True, sync_long_rent_at_short=True)

    def test_fixed_profile_prices_remain_separate_from_main_short_rent(self):
        init = sample_init()
        init['zeroCDRentConfigMap']['123456789'].update({'pricingType': 1, 'shortRentPrice': '0.40', 'longRentPrice': '0.30'})
        body = build_short_rent_payload(sample_row(), {'data': init}, '0.25', device_session='A'*24)
        item = body['Commoditys'][0]
        self.assertEqual(item['LeaseUnitPrice'], 0.25)
        self.assertEqual(item['ZeroCDConfig'], {'PricingType': 1, 'ShortRentPrice': '0.4', 'LongRentPrice': '0.3'})

    def test_missing_non_target_fields_and_id_mismatch_are_rejected(self):
        for key in ('sellAmount', 'depositAmount', 'commodityCanSell', 'longLeaseAmount'):
            row = sample_row()
            del row[key]
            with self.subTest(key=key), self.assertRaises(RepriceContractError):
                build_short_rent_payload(row, sample_init(), '0.25', device_session='A'*24)
        with self.assertRaises(RepriceContractError):
            build_short_rent_payload(sample_row(), sample_init(), '0.25', device_session='A'*24, expected_listing_id='999')

    def test_renewal_and_unknown_activity_modes_are_rejected(self):
        for patch in ({'needReNew': True}, {'needReNew': None}, {'pricingType': 2}, {'marketDynamicPricingMinCoefficient': 0}):
            init = sample_init()
            init['zeroCDRentConfigMap']['123456789'].update(patch)
            with self.subTest(patch=patch), self.assertRaises(RepriceContractError):
                build_short_rent_payload(sample_row(), init, '0.25', device_session='A'*24)

    def test_no_zero_long_rent_field_is_invented(self):
        row = sample_row()
        row['longLeaseAmount'] = None
        body = build_short_rent_payload(row, sample_init(), '0.25', device_session='A'*24)
        self.assertNotIn('LongLeaseUnitPrice', body['Commoditys'][0])

    def test_readback_checks_every_preserved_shelf_field(self):
        before = sample_row()
        after = deepcopy(before)
        after['shortLeaseAmount'] = '0.25'
        self.assertTrue(readback_matches(before, after, '0.25'))
        for key, changed in (('depositAmount', '1234.51'), ('longLeaseAmount', '0.21'),
                             ('leaseMaxDays', 9), ('commodityCanSell', True), ('id', '123456790')):
            candidate = deepcopy(after)
            candidate[key] = changed
            self.assertFalse(readback_matches(before, candidate, '0.25'))

    def test_non_sale_valuation_can_change_but_active_sale_price_cannot(self):
        before = sample_row()
        before['commodityCanSell'] = False
        after = deepcopy(before)
        after.update(shortLeaseAmount='0.25', sellAmount='607.6')
        self.assertTrue(readback_matches(before, after, '0.25'))
        before['commodityCanSell'] = after['commodityCanSell'] = True
        self.assertFalse(readback_matches(before, after, '0.25'))

    def test_no_silent_rounding_or_invalid_prices(self):
        for target in ('0', '-0.01', '0.001', 'NaN', 'Infinity', True):
            with self.subTest(target=target), self.assertRaises(RepriceContractError):
                build_short_rent_payload(sample_row(), sample_init(), target, device_session='A'*24)
        row = sample_row()
        row['depositAmount'] = '1234.501'
        with self.assertRaises(RepriceContractError):
            build_short_rent_payload(row, sample_init(), '0.25', device_session='A'*24)


if __name__ == '__main__':
    unittest.main()
