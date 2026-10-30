# SPDX-License-Identifier: AGPL-3.0-only
import unittest
from unittest.mock import Mock

from youpin_rent_repricer.uu_client import UUClient, UUError, _PRICE_WRITE


class WriteBoundaryTests(unittest.TestCase):
    def payload(self, client):
        return {'Sessionid': client.state['device_token'], 'Commoditys': [{'CommodityId': 123, 'LeaseUnitPrice': 0.06}]}

    def test_default_client_cannot_write(self):
        client = UUClient({'token': 'synthetic'})
        client._json_call = Mock()
        with self.assertRaises(UUError) as caught:
            client.change_short_rent(self.payload(client))
        self.assertEqual(caught.exception.code, 'price_writes_disabled')
        client._json_call.assert_not_called()
        with self.assertRaises(UUError):
            client._send('PUT', _PRICE_WRITE, {})

    def test_only_the_exact_reviewed_route_is_reachable(self):
        client = UUClient({'token': 'synthetic'}, allow_price_write=True)
        for method, path in [('DELETE', _PRICE_WRITE), ('PUT', '/api/other'), ('POST', _PRICE_WRITE)]:
            with self.subTest(method=method, path=path), self.assertRaises(UUError):
                client._send(method, path, {})

    def test_acceptance_requires_requested_item_and_still_needs_readback(self):
        client = UUClient({'token': 'synthetic'}, allow_price_write=True)
        client._json_call = Mock(return_value={'SuccessCount': 1, 'FailCount': 0, 'Commoditys': [{'CommodityId': 456, 'IsSuccess': 1}]})
        with self.assertRaises(UUError):
            client.change_short_rent(self.payload(client))
        client._json_call.return_value['Commoditys'][0]['CommodityId'] = 123
        self.assertEqual(client.change_short_rent(self.payload(client)), {'accepted': True, 'readback_required': True})

    def test_foreign_device_and_multiple_items_are_rejected(self):
        client = UUClient({'token': 'synthetic'}, allow_price_write=True)
        client._json_call = Mock()
        payload = self.payload(client)
        payload['Sessionid'] = 'foreign-session'
        with self.assertRaises(UUError):
            client.change_short_rent(payload)
        payload = self.payload(client)
        payload['Commoditys'].append({'CommodityId': 456})
        with self.assertRaises(UUError):
            client.change_short_rent(payload)
        client._json_call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
