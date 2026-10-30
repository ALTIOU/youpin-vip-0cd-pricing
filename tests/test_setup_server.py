# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer

from youpin_rent_repricer.setup_server import Inspector, make_handler
from youpin_rent_repricer.uu_client import UUClient, UUError, _SMS, _SMS_UP_CONFIG, _SMS_UP_SIGN_IN, _USER_INFO, _LEGACY_USER_INFO


class AccountReadTests(unittest.TestCase):
    def test_incompatible_bff_uses_one_authenticated_legacy_read(self):
        client = UUClient({'token': 'synthetic-token'})
        client._json_call = Mock(side_effect=[UUError('protocol_error'), {'UserId': 123}])
        client.userinfo()
        self.assertEqual(client.state['user_id'], '123')
        self.assertEqual([call.args for call in client._json_call.call_args_list], [('GET', _USER_INFO), ('GET', _LEGACY_USER_INFO)])

    def test_security_or_rate_failures_never_trigger_fallback(self):
        for code in ('auth_expired', 'need_manual_verification', 'risk_control', 'rate_limited', 'access_denied'):
            with self.subTest(code=code):
                client = UUClient({'token': 'synthetic-token'})
                client._json_call = Mock(side_effect=UUError(code))
                with self.assertRaises(UUError):
                    client.userinfo()
                client._json_call.assert_called_once_with('GET', _USER_INFO)


class UpSmsTests(unittest.TestCase):
    def setUp(self):
        self.client = UUClient({})
        self.client._ensure_uk = Mock()
        self.client._json_call = Mock(return_value={'SmsUpNumber': '106900000000000', 'SmsUpContent': 'ABCD'})

    def test_config_does_not_send_sms_or_persist_instructions(self):
        result = self.client.request_sms_up('13800000000')
        self.assertEqual(result['status'], 'sms_up_instructions')
        self.assertNotIn('token', self.client.state)
        self.client._json_call.assert_called_once_with('GET', _SMS_UP_CONFIG, authenticated=False)
        serialized = json.dumps(self.client.state)
        for secret in ('13800000000', '106900000000000', 'ABCD'):
            self.assertNotIn(secret, serialized)

    def test_login_requires_original_phone_and_fresh_device_session(self):
        self.client.request_sms_up('13800000000')
        self.client._json_call.reset_mock()
        with self.assertRaises(UUError):
            self.client.sign_in_up('13900000000')
        self.client._json_call.assert_not_called()
        self.client.state['sms_up_pending']['requested_at'] = time.time() - 601
        with self.assertRaises(UUError) as caught:
            self.client.sign_in_up('13800000000')
        self.assertEqual(caught.exception.code, 'sms_up_expired')
        self.client._json_call.assert_not_called()

    def test_login_requires_real_token_and_retains_original_session(self):
        self.client.request_sms_up('13800000000')
        self.client._json_call.return_value = {}
        with self.assertRaises(UUError):
            self.client.sign_in_up('13800000000')
        self.assertNotIn('token', self.client.state)
        self.client._json_call.return_value = {'Token': 'synthetic-private-token'}
        result = self.client.sign_in_up('13800000000')
        self.assertTrue(result['ok'])
        device = self.client.state['device_token']
        self.client._json_call.assert_called_with('POST', _SMS_UP_SIGN_IN, {
            'Area': 86, 'Code': '', 'DeviceName': device, 'Sessionid': device, 'Mobile': '13800000000',
        }, authenticated=False)
        self.assertNotIn('synthetic-private-token', json.dumps(result))
        self.assertNotIn('sms_up_pending', self.client.state)


class SmsClassificationTests(unittest.TestCase):
    def test_official_handoff_is_ephemeral_and_not_serialized(self):
        client = UUClient({})
        verification_url = 'https://h5.youpin898.com/verify?challenge=synthetic-private-value'
        response = Mock()
        response.json.return_value = {'Code': 0, 'Msg': '需进行图形校验', 'Data': {'ImgVerifyUrl': verification_url}}
        client._send = Mock(return_value=response)
        with self.assertRaises(UUError):
            client._json_call('POST', _SMS, {}, authenticated=False)
        self.assertEqual(client.manual_verification_url(), verification_url)
        self.assertNotIn('synthetic-private-value', json.dumps(client.state))
        client._manual_verification = (time.monotonic() - 301, verification_url)
        self.assertIsNone(client.manual_verification_url())

    def test_untrusted_handoff_destinations_are_rejected(self):
        client = UUClient({})
        for url in ('http://h5.youpin898.com/verify', 'https://youpin898.com.evil.invalid/',
                    'https://youpin898.com@evil.invalid/', 'https://h5.youpin898.com:444/',
                    'https://h5.youpin898.com/\r\nLocation:bad', 'https://example.invalid/',
                    r'https://evil.invalid\h5.youpin898.com/'):
            client._set_manual_verification_url(url)
            self.assertIsNone(client.manual_verification_url())

    def test_code_zero_graphic_check_requires_manual_verification_without_raw_data(self):
        verification_url = 'https://example.invalid/verification?challenge=synthetic-private-value'
        client = UUClient({})
        response = Mock()
        response.json.return_value = {
            'Code': 0, 'Msg': '需进行图形校验',
            'Data': {'Secs': 60, 'ImgVerifyUrl': verification_url, 'Mobile': '13800000000'},
        }
        client._send = Mock(return_value=response)
        with self.assertRaises(UUError) as caught:
            client._json_call('POST', _SMS, {}, authenticated=False)
        self.assertEqual(caught.exception.as_dict(), {
            'ok': False, 'status': 'need_manual_verification',
            'error': 'need_manual_verification',
            'message': '平台要求人工验证，请在官方 App 检查并完成。', 'api_code': 0,
        })
        diagnostics = client.state['last_sms_status']
        self.assertFalse(diagnostics['delivery_confirmed'])
        self.assertIn('ImgVerifyUrl', diagnostics['verification_field_names'])
        recorded = json.dumps(client.state, ensure_ascii=False)
        self.assertNotIn(verification_url, recorded)
        self.assertNotIn('synthetic-private-value', recorded)
        self.assertNotIn('13800000000', recorded)
        client._send.assert_called_once_with('POST', _SMS, {}, authenticated=False)

    def test_image_check_wording_uses_same_fixed_safe_error(self):
        for message in ('需进行图片校验', '请完成图像校验', '请完成图像验证'):
            with self.subTest(message=message):
                error = UUClient._failure(0, message)
                self.assertEqual(error.code, 'need_manual_verification')
                self.assertEqual(error.safe_message, '平台要求人工验证，请在官方 App 检查并完成。')


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Inspector(Path(self.temp.name))
        self.app.client.request_sms = Mock(return_value={'ok': False, 'error': 'need_manual_verification'})
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.app))
        self.base = f'http://127.0.0.1:{self.httpd.server_port}'
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.temp.cleanup()

    def request(self, path, data=None, origin=True, csrf=True):
        headers = {}
        if origin:
            headers['Origin'] = self.base
        if csrf:
            headers['X-Setup-CSRF'] = self.app.csrf
        req = Request(self.base+path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
        try:
            response = urlopen(req, timeout=2)
        except HTTPError as error:
            response = error
        body = response.read().decode()
        return response.status, response.headers, body

    def test_status_exposes_no_credentials(self):
        self.app.client.state['token'] = 'private-test-value'
        self.app.client._set_manual_verification_url('https://h5.youpin898.com/verify?challenge=private-url-value')
        status, _, body = self.request('/api/status')
        self.assertEqual(status, 200)
        self.assertNotIn('private-test-value', body)
        self.assertFalse(json.loads(body)['writes_enabled'])
        self.assertFalse(json.loads(body)['authenticated'])
        self.assertNotIn('private-url-value', body)
        self.assertFalse(json.loads(body)['verification_available'])

    def test_native_only_graphic_handoff_is_retired(self):
        status, _, _ = self.request('/verification')
        self.assertEqual(status, 410)
        url = 'https://h5.youpin898.com/verify?challenge=synthetic-private-value'
        self.app.client._set_manual_verification_url(url)
        status, headers, body = self.request('/verification')
        self.assertEqual(status, 410)
        self.assertIsNone(headers.get('Location'))
        self.assertNotIn('synthetic-private-value', body)

    def test_price_update_endpoint_is_absent(self):
        status, _, _ = self.request('/api/reprice', {})
        self.assertEqual(status, 403)

    def test_cross_origin_cannot_request_sms(self):
        status, _, _ = self.request('/api/sms', {'phone': '13800000000'}, origin=False)
        self.assertEqual(status, 403)
        self.app.client.request_sms.assert_not_called()

    def test_manual_verification_is_not_reported_as_success(self):
        status, _, body = self.request('/api/sms', {'phone': '13800000000'})
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)['ok'])
        self.assertFalse(self.app.state_file.exists())

    def test_failed_graphic_handoff_does_not_encourage_retries(self):
        self.app.client._set_manual_verification_url('https://h5.youpin898.com/verify?challenge=synthetic')
        status, _, body = self.request('/api/sms', {'phone': '13800000000'})
        result = json.loads(body)
        self.assertEqual(status, 400)
        self.assertFalse(result['ok'])
        self.assertIn('浏览器完成不能接续', result['message'])
        self.assertIn('请勿重复发码', result['message'])
        self.assertFalse(result['verification_available'])

    def test_up_login_requires_user_sent_confirmation(self):
        self.app.client.sign_in_up = Mock()
        status, _, body = self.request('/api/up-login', {'phone': '13800000000'})
        self.assertEqual(status, 400)
        self.app.client.sign_in_up.assert_not_called()

    def test_up_config_rejects_cross_origin_requests(self):
        self.app.client.request_sms_up = Mock()
        status, _, _ = self.request('/api/up-config', {'phone': '13800000000'}, origin=False)
        self.assertEqual(status, 403)
        self.app.client.request_sms_up.assert_not_called()

    def test_first_request_on_fresh_boot_is_allowed_then_cooldown_applies(self):
        for path, method_name, cooldown in (
            ('/api/sms', 'request_sms', 60),
            ('/api/up-config', 'request_sms_up', 30),
        ):
            with self.subTest(path=path):
                provider = Mock(return_value={'ok': True, 'message': 'synthetic response'})
                setattr(self.app.client, method_name, provider)
                with patch('time.monotonic', return_value=10):
                    status, _, body = self.request(path, {'phone': '13800000000'})
                    self.assertEqual(status, 200, body)
                    self.assertTrue(json.loads(body)['ok'])
                    status, _, body = self.request(path, {'phone': '13800000000'})
                    self.assertEqual(status, 400)
                    self.assertIn(f'{cooldown} 秒', json.loads(body)['message'])
                provider.assert_called_once_with('13800000000')
                with patch('time.monotonic', return_value=10 + cooldown):
                    status, _, body = self.request(path, {'phone': '13800000000'})
                    self.assertEqual(status, 200, body)
                self.assertEqual(provider.call_count, 2)

    def test_zero_monotonic_timestamp_is_still_a_recorded_request(self):
        self.app.client.request_sms.return_value = {'ok': True, 'message': 'synthetic response'}
        with patch('time.monotonic', return_value=0):
            status, _, _ = self.request('/api/sms', {'phone': '13800000000'})
            self.assertEqual(status, 200)
        with patch('time.monotonic', return_value=1):
            status, _, body = self.request('/api/sms', {'phone': '13800000000'})
            self.assertEqual(status, 400)
            self.assertIn('60 秒', json.loads(body)['message'])
        self.app.client.request_sms.assert_called_once_with('13800000000')

    def test_login_form_is_loopback_scoped_and_has_csp(self):
        status, headers, body = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertIn('获取官方验证短信说明', body)
        self.assertNotIn('href="/verification"', body)
        self.assertNotIn('api.openai.com', body)

    def test_success_code_with_verification_message_stops(self):
        client = UUClient({})
        response = Mock()
        response.json.return_value = {'Code': 0, 'Msg': '请先完成图形验证', 'Data': {'captchaRequired': True}}
        client._send = Mock(return_value=response)
        with self.assertRaises(UUError) as caught:
            client._json_call('POST', _SMS, {}, authenticated=False)
        self.assertEqual(caught.exception.code, 'need_manual_verification')
        self.assertFalse(client.state['last_sms_status']['delivery_confirmed'])

    def test_sms_diagnostics_never_include_raw_data(self):
        client = UUClient({})
        response = Mock()
        response.json.return_value = {'Code': 0, 'Msg': '发送成功', 'Data': {'Token': 'not-for-output', 'Mobile': '13800000000'}}
        client._send = Mock(return_value=response)
        client._json_call('POST', _SMS, {}, authenticated=False)
        diagnostics = json.dumps(client.state['last_sms_status'])
        self.assertNotIn('not-for-output', diagnostics)
        self.assertNotIn('13800000000', diagnostics)


if __name__ == '__main__':
    unittest.main()
