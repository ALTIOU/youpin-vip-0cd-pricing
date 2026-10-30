# SPDX-License-Identifier: AGPL-3.0-only
import unittest
from unittest.mock import Mock

from youpin_rent_repricer.market_reader import read_market_snapshot
from youpin_rent_repricer.uu_client import UUError, _MARKET


def row(identifier, price='0.20'):
    return {'Id': identifier, 'LeaseUnitPrice': price}


class MarketSnapshotTests(unittest.TestCase):
    def client(self, *pages):
        client = Mock()
        client.state = {'user_id': 'own-seller', 'device_token': 'stable-device'}
        client._json_call.side_effect = list(pages)
        return client

    def test_single_explicit_exhausted_page_is_complete(self):
        client = self.client({'hasNext': False, 'CommodityList': [row(1), row(2)]})
        result = read_market_snapshot(client, 3874)
        self.assertTrue(result['coverage_complete'])
        self.assertEqual(result['reason'], 'query_exhausted')
        self.assertEqual(result['rows'], [row(1), row(2)])
        self.assertEqual(result['pages_read'], 1)
        self.assertTrue(result['observed_at'])
        method, path, payload = client._json_call.call_args.args
        self.assertEqual((method, path), ('POST', _MARKET))
        self.assertEqual(payload['pageIndex'], 1)
        self.assertEqual(payload['pageSize'], 50)
        self.assertEqual(payload['templateId'], '3874')
        self.assertEqual(payload['Sessionid'], 'stable-device')

    def test_reads_all_pages_without_assuming_ascending_price(self):
        client = self.client(
            {'hasNext': True, 'CommodityList': [row(1, '0.50')]},
            {'hasNext': False, 'CommodityList': [row(2, '0.01')]},
        )
        result = read_market_snapshot(client, '3874')
        self.assertTrue(result['coverage_complete'])
        self.assertEqual(result['pages_read'], 2)
        self.assertEqual([r['LeaseUnitPrice'] for r in result['rows']], ['0.50', '0.01'])
        self.assertEqual([c.args[2]['pageIndex'] for c in client._json_call.call_args_list], [1, 2])

    def test_full_final_page_still_complete_when_has_next_false(self):
        client = self.client({'hasNext': False, 'CommodityList': [row(i) for i in range(1, 51)]})
        self.assertTrue(read_market_snapshot(client, 1)['coverage_complete'])

    def test_short_page_is_not_proof_of_exhaustion(self):
        for value in (None, 0, 'false', 'False', '0', [], {}):
            with self.subTest(value=value):
                client = self.client({'hasNext': value, 'CommodityList': [row(1)]})
                result = read_market_snapshot(client, 1)
                self.assertFalse(result['coverage_complete'])
                self.assertEqual(result['reason'], 'has_next_missing_or_invalid')
                self.assertEqual(client._json_call.call_count, 1)

    def test_missing_has_next_is_incomplete(self):
        client = self.client({'CommodityList': []})
        self.assertFalse(read_market_snapshot(client, 1)['coverage_complete'])

    def test_empty_exhausted_market_is_complete_but_has_no_quote(self):
        result = read_market_snapshot(self.client({'hasNext': False, 'CommodityList': []}), 1)
        self.assertTrue(result['coverage_complete'])
        self.assertEqual(result['rows'], [])

    def test_empty_page_claiming_more_stops_without_more_requests(self):
        client = self.client({'hasNext': True, 'CommodityList': []})
        result = read_market_snapshot(client, 1)
        self.assertEqual(result['reason'], 'empty_page_with_has_next')
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(client._json_call.call_count, 1)

    def test_duplicate_between_pages_is_incomplete_even_on_last_page(self):
        client = self.client(
            {'hasNext': True, 'CommodityList': [row(1)]},
            {'hasNext': False, 'CommodityList': [row(1)]},
        )
        result = read_market_snapshot(client, 1)
        self.assertEqual(result['reason'], 'duplicate_listing_id')
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(len(result['rows']), 2)

    def test_duplicate_in_final_page_is_incomplete(self):
        result = read_market_snapshot(self.client({'hasNext': False, 'CommodityList': [row(1), row('1')]}), 1)
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(result['reason'], 'duplicate_listing_id')

    def test_missing_or_invalid_listing_id_blocks_coverage(self):
        for identifier in (None, True, '', 'not-numeric'):
            with self.subTest(identifier=identifier):
                result = read_market_snapshot(self.client({'hasNext': False, 'CommodityList': [row(identifier)]}), 1)
                self.assertFalse(result['coverage_complete'])
                self.assertEqual(result['reason'], 'listing_id_missing_or_invalid')

    def test_page_cap_is_bounded_and_incomplete(self):
        client = self.client(
            {'hasNext': True, 'CommodityList': [row(1)]},
            {'hasNext': True, 'CommodityList': [row(2)]},
        )
        result = read_market_snapshot(client, 1, max_pages=2)
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(result['reason'], 'page_limit_reached')
        self.assertEqual(client._json_call.call_count, 2)

    def test_errors_propagate_without_retry_or_fallback(self):
        for code in ('risk_control', 'rate_limited', 'auth_expired', 'network_error'):
            with self.subTest(code=code):
                client = self.client(UUError(code))
                with self.assertRaises(UUError) as caught:
                    read_market_snapshot(client, 1)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(client._json_call.call_count, 1)

    def test_invalid_page_payload_is_not_complete(self):
        for page in ([], {'hasNext': False}, {'hasNext': False, 'CommodityList': [None]},
                     {'hasNext': False, 'CommodityList': [row(i) for i in range(1, 52)]}):
            with self.subTest(page_type=type(page).__name__):
                result = read_market_snapshot(self.client(page), 1)
                self.assertFalse(result['coverage_complete'])

    def test_invalid_arguments_do_not_request_anything(self):
        for template, cap in ((True, 1), ('bad', 1), (1, 0), (1, 21), (1, True)):
            client = self.client()
            with self.assertRaises(UUError):
                read_market_snapshot(client, template, max_pages=cap)
            client._json_call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
