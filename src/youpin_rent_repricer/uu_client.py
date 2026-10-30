# SPDX-License-Identifier: GPL-3.0-only
# Adapted with reference to CS2TradeMonitor contributors, revision
# 41d52fdb7997e023e6c431a7173baa9d8b00806e.
# Modified for YouPin Rent Repricer on 2026-10-30.
# See THIRD_PARTY_NOTICES.txt for provenance and combined-license terms.
"""Bounded YouPin authentication and query client; price writes need opt-in.

No I/O occurs during construction. The caller owns persistence of ``state`` and
must keep it private: it contains credentials. This module never logs or prints.

Protocol reference: cs2juece/CS2TradeMonitor master, commit
41d52fdb7997e023e6c431a7173baa9d8b00806e (2026-09-15),
YouPinAuthService.cs / YouPinMobileApiClient.cs / YouPinLandlordGateway.cs.
App-Version 5.45.4 is that source's value, not a claim about today's official
App version. Device/UK failures are surfaced; no alternate-header retry exists.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
import string
import time
from typing import Any
from urllib.parse import urlsplit


_BASE = "https://api.youpin898.com"
_APP_VERSION = "5.45.4"
_TIMEOUT = (10, 25)
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_USER_INFO = "/api/youpin/bff/user/Account/getUserInfoForApp"
_LEGACY_USER_INFO = "/api/user/Account/getUserInfo"
_DEVICE = "/api/deviceW2"
_SMS = "/api/user/Auth/SendSignInSmsCode"
_SIGN_IN = "/api/user/Auth/SmsSignIn"
_SMS_UP_CONFIG = "/api/user/Auth/GetSmsUpSignInConfig"
_SMS_UP_SIGN_IN = "/api/user/Auth/SmsUpSignIn"
_ZERO_CD = "/api/youpin/bff/new/commodity/v1/commodity/list/zeroCDLease"
_REPRICE_INIT = "/api/youpin/bff/new/commodity/commodity/change/price/v3/init/info"
_MARKET = "/api/homepage/v3/detail/commodity/list/lease"
_PRICE_WRITE = "/api/commodity/Commodity/PriceChangeWithLeaseV2"

# POST is also used for these read queries. The one reviewed price PUT is
# separately gated by allow_price_write. No order or deletion route is allowed.
_ALLOWED = frozenset({
    ("GET", _USER_INFO), ("GET", _LEGACY_USER_INFO),
    ("POST", _DEVICE), ("POST", _SMS), ("POST", _SIGN_IN),
    ("GET", _SMS_UP_CONFIG), ("POST", _SMS_UP_SIGN_IN),
    ("POST", _ZERO_CD), ("POST", _REPRICE_INIT), ("POST", _MARKET),
})

_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAv9BDdhCDahZNFuJeesx3gzoQfD7pE0AeWiNBZlc21ph6kU9zd58X/1warV3C1VIX0vMAmhOcj5u86i+L2Lb2V68dX2Nb70MIDeW6Ibe8d0nF8D30tPsM7kaAyvxkY6ECM6RHGNhV4RrzkHmf5DeR9bybQGE0A9jcjuxszD1wsW/n19eeom7MroHqlRorp5LLNR8bSbmhTw6M/RQ/Fm3lKjKcvs1QNVyBNimrbD+ZVPE/KHSZLQ1jdF6tppvFnGxgJU9NFmxGFU0hx6cZiQHkhOQfGDFkElxgtj8gFJ1narTwYbvfe5nGSiznv/EUJSjTHxzX1TEkex0+5j4vSANt1QIDAQAB
-----END PUBLIC KEY-----"""

_MESSAGES = {
    "invalid_input": "输入格式不正确。",
    "invalid_state": "本地会话格式不正确，请检查私有会话文件。",
    "dependency_missing": "缺少 requests 或 pycryptodome 运行依赖。",
    "not_authenticated": "尚未取得悠悠登录凭据。",
    "auth_expired": "悠悠登录状态已失效，请人工重新登录。",
    "sms_not_requested": "请先申请短信验证码，再在同一会话内登录。",
    "sms_cooldown": "刚申请过短信验证码，请稍后再试。",
    "sms_up_not_requested": "请先获取官方上行短信说明，并用对应手机号手动发送后再检查登录。",
    "sms_up_expired": "本次登录已超过本地十分钟验证期限，请重新获取官方说明。",
    "need_manual_verification": "平台要求人工验证，请在官方 App 检查并完成。",
    "app_verification_required": "平台要求使用官方 App 完成登录或版本验证。",
    "risk_control": "平台返回风控或安全验证要求，本次操作已停止。",
    "rate_limited": "平台限制请求频率，本次操作已停止。",
    "access_denied": "平台拒绝访问，请人工检查登录及验证状态。",
    "endpoint_not_found": "已审查接口当前不可用。",
    "endpoint_forbidden": "此客户端禁止调用该接口。",
    "redirect_blocked": "平台发生重定向，本次请求已停止。",
    "network_error": "连接悠悠服务失败，本次请求已停止。",
    "http_error": "悠悠服务返回异常 HTTP 状态。",
    "api_error": "悠悠服务未接受本次请求。",
    "protocol_error": "悠悠服务返回格式与已审查协议不符。",
    "response_too_large": "响应超出本次读取上限。",
    "device_error": "设备校验失败，请人工检查平台验证状态。",
    "price_writes_disabled": "当前客户端未启用价格写入。",
    "write_not_confirmed": "平台未明确确认本件改价，本次停止；请先回读核对，不自动重发。",
}


def redact(value: Any) -> str:
    """Defence-in-depth for callers; raw server messages are never emitted."""
    text = str(value)
    text = re.sub(r"(?i)\b(token|uk|mobile|phone|code)\s*[:=]\s*[^\s,;}]+", r"\1=[redacted]", text)
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(r"[A-Za-z0-9_+/.=\-]{24,}", "[redacted]", text)
    text = re.sub(r"(?<!\d)\d{4,}(?!\d)", "[redacted]", text)
    return text[:240]


class UUError(Exception):
    """Only fixed safe messages and numeric status fields escape the client."""

    def __init__(self, code: str, *, http_status: int | None = None,
                 api_code: int | None = None):
        self.code = code if code in _MESSAGES else "api_error"
        self.message = _MESSAGES[self.code]
        self.safe_message = self.message
        self.http_status = http_status if isinstance(http_status, int) else None
        self.api_code = api_code if isinstance(api_code, int) else None
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False, "status": self.code, "error": self.code,
            "message": self.message,
        }
        if self.http_status is not None:
            result["http_status"] = self.http_status
        if self.api_code is not None:
            result["api_code"] = self.api_code
        return result


class _Reply:
    """An already bounded response, detached from the closed HTTP stream."""

    def __init__(self, status: int, content: bytes):
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8-sig", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


def _pick(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return default


def _numeric_code(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,12}", value.strip()):
        return int(value)
    return None


def _bounded_int(value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise UUError("invalid_input")
    return value


def _id(value: Any) -> str:
    result = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,30}", result):
        raise UUError("invalid_input")
    return result


def _phone(value: str) -> str:
    if not isinstance(value, str):
        raise UUError("invalid_input")
    result = re.sub(r"[\s-]", "", value)
    if result.startswith("+86"):
        result = result[3:]
    if not re.fullmatch(r"1[3-9][0-9]{9}", result):
        raise UUError("invalid_input")
    return result


class UUClient:
    def __init__(self, state: dict[str, Any] | None = None, *, allow_price_write: bool = False):
        if state is not None and not isinstance(state, dict):
            raise UUError("invalid_state")
        self.state = state if state is not None else {}
        self.allow_price_write = allow_price_write is True
        # A verification link is a short-lived credential. Keep it in memory,
        # outside serializable session state, for the user's manual handoff.
        self._manual_verification: tuple[float, str] | None = None
        device = self.state.setdefault("device_token", self._random_string(24))
        self.state.setdefault("device_id", device)
        self.state.setdefault("request_tag", secrets.token_hex(16).upper())
        self.state.setdefault("device_uk", self._random_string(65))
        for key, length in (("device_token", 24), ("device_id", 24),
                            ("request_tag", 32), ("device_uk", 65)):
            value = self.state[key]
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]{%d}" % length, value):
                raise UUError("invalid_state")
        if self.state["device_token"] != self.state["device_id"]:
            raise UUError("invalid_state")
        for key in ("uk", "token", "user_id"):
            value = self.state.get(key, "")
            if not isinstance(value, str) or len(value) > 16384 or "\r" in value or "\n" in value:
                raise UUError("invalid_state")

    def __repr__(self) -> str:
        return "<UUClient credentials-private>"

    def manual_verification_url(self) -> str | None:
        entry = self._manual_verification
        if entry and time.monotonic() - entry[0] < 300:
            return entry[1]
        self._manual_verification = None
        return None

    def _set_manual_verification_url(self, value: Any) -> None:
        self._manual_verification = None
        # Browsers normalize backslashes in HTTPS URLs before choosing a host;
        # urlsplit does not. Reject them before checking the domain allowlist.
        if (not isinstance(value, str) or len(value) > 8192 or "\\" in value
                or any(ord(c) < 33 for c in value)):
            return
        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or '').lower()
            valid_host = host == 'youpin898.com' or host.endswith('.youpin898.com')
            if (parsed.scheme != 'https' or not valid_host or parsed.username
                    or parsed.password or parsed.port not in (None, 443)):
                return
        except ValueError:
            return
        self._manual_verification = (time.monotonic(), value)

    @staticmethod
    def _random_string(length: int) -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def _headers(self, authenticated: bool) -> dict[str, str]:
        token = self.state.get("token", "") if authenticated else ""
        if authenticated and not token:
            raise UUError("not_authenticated")
        info = {
            "deviceType": self.state["device_id"], "systemName ": "Android",
            "hasSteamApp": 1, "deviceId": self.state["device_id"],
            "requestTag": self.state["request_tag"], "systemVersion": "15",
        }
        result = {
            "authorization": "Bearer " + token,
            "content-type": "application/json; charset=utf-8",
            "user-agent": f"Android/15 official com.uu898.uuhavequality/{_APP_VERSION} okhttp/4.9.3",
            "App-Version": _APP_VERSION, "AppType": "4", "deviceType": "1",
            "package-type": "uuyp", "DeviceToken": self.state["device_token"],
            "DeviceId": self.state["device_id"], "deviceUk": self.state["device_uk"],
            "platform": "android", "Gameid": "730", "requestTag": self.state["request_tag"],
            "deviceBrand": "xiaomi", "systemVersion": "15", "App-Source": "h5",
            "traceId": secrets.token_hex(16), "currentTheme": "Dark",
            "Accept-Language": "zh-CN,zh;q=0.8",
            "Device-Info": json.dumps(info, ensure_ascii=False, separators=(",", ":")),
        }
        result["uk"] = self.state.get("uk") or self.state["device_uk"]
        return result

    def _send(self, method: str, path: str, payload: dict[str, Any] | None = None,
              *, authenticated: bool = True) -> Any:
        is_price_write = method == 'PUT' and path == _PRICE_WRITE
        if is_price_write and not self.allow_price_write:
            raise UUError('price_writes_disabled')
        if (method, path) not in _ALLOWED and not (is_price_write and self.allow_price_write):
            raise UUError("endpoint_forbidden")
        headers = self._headers(authenticated)
        try:
            import requests
        except ImportError:
            raise UUError("dependency_missing") from None
        response = None
        try:
            response = requests.request(
                method, _BASE + path, headers=headers, json=payload,
                timeout=_TIMEOUT, allow_redirects=False, stream=True,
            )
            status = response.status_code
            if 300 <= status < 400:
                raise UUError("redirect_blocked", http_status=status)
            if status == 429:
                raise UUError("rate_limited", http_status=status)
            if status == 401:
                raise UUError("auth_expired", http_status=status)
            if status == 403:
                raise UUError("access_denied", http_status=status)
            if status == 404:
                raise UUError("endpoint_not_found", http_status=status)
            if not 200 <= status < 300:
                raise UUError("http_error", http_status=status)
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                size += len(chunk)
                if size > _MAX_RESPONSE_BYTES:
                    raise UUError("response_too_large")
                chunks.append(chunk)
            return _Reply(status, b"".join(chunks))
        except UUError:
            raise
        except Exception:
            # requests exceptions can contain a URL/proxy or credentials.
            raise UUError("network_error") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    @staticmethod
    def _failure(code: int | None, message: str) -> UUError:
        lowered = message.lower()
        if code == 5050:
            return UUError("app_verification_required", api_code=code)
        if code in (401, 403, 84101) or any(term in lowered for term in (
            "登录状态失效", "登录已失效", "登录过期", "身份验证失效", "token expired",
        )):
            return UUError("auth_expired", api_code=code)
        if any(term in lowered for term in ("上行短信", "自行发送短信", "smsup", "发送短信至")):
            return UUError("need_manual_verification", api_code=code)
        if any(term in lowered for term in (
            "captcha", "人机", "滑块", "图形验证", "图片验证", "图像验证",
            "图形校验", "图片校验", "图像校验", "人工验证", "官方app", "最新版本app",
        )):
            return UUError("need_manual_verification", api_code=code)
        if code == 429 or any(term in lowered for term in ("频率", "过于频繁", "too many requests")):
            return UUError("rate_limited", api_code=code)
        if any(term in lowered for term in ("风控", "安全验证", "风险", "risk control", "访问受限")):
            return UUError("risk_control", api_code=code)
        return UUError("api_error", api_code=code)

    def _json_call(self, method: str, path: str, payload: dict[str, Any] | None = None,
                   *, authenticated: bool = True, empty_code: bool = False) -> Any:
        response = self._send(method, path, payload, authenticated=authenticated)
        try:
            body = response.json()
        except (ValueError, TypeError):
            error = self._failure(None, response.text[:4096])
            if error.code == "api_error":
                error = UUError("protocol_error")
            raise error from None
        if not isinstance(body, dict):
            raise UUError("protocol_error")
        code = _numeric_code(_pick(body, "Code", "code"))
        message = str(_pick(body, "Msg", "msg", "Message", "message", default=""))
        if path == _PRICE_WRITE:
            write_data = _pick(body, 'Data', 'data')
            details = _pick(write_data, 'Commoditys', 'commoditys') if isinstance(write_data, dict) else None
            self.state['last_write_status'] = {
                'api_code': code, 'message': redact(message),
                'data_type': type(write_data).__name__,
                'data_keys': [str(k)[:70] for k in list(write_data)[:24]] if isinstance(write_data, dict) else [],
                'success_count': _numeric_code(_pick(write_data, 'SuccessCount', 'successCount')) if isinstance(write_data, dict) else None,
                'fail_count': _numeric_code(_pick(write_data, 'FailCount', 'failCount')) if isinstance(write_data, dict) else None,
                'item_results': [{'success': _numeric_code(_pick(row, 'IsSuccess', 'isSuccess')),
                                  'message': redact(_pick(row, 'Message', 'message', 'Msg', 'msg', default=''))}
                                 for row in details[:5] if isinstance(row, dict)] if isinstance(details, list) else [],
            }
        if path == _SMS:
            self._manual_verification = None
            sms_data = _pick(body, "Data", "data")
            data_keys = list(sms_data)[:16] if isinstance(sms_data, dict) else []
            verification_keys = [str(key)[:60] for key in data_keys
                                 if re.search(r"captcha|verify|valid|challenge|risk|image|ticket", str(key), re.I)]
            self.state["last_sms_status"] = {
                "api_code": code,
                "message": redact(message),
                "data_type": type(sms_data).__name__,
                "data_keys": [str(key)[:60] for key in data_keys],
                "verification_field_names": verification_keys,
                "delivery_confirmed": False,
            }
            # A success code is not proof that a verification step was cleared.
            hint = self._failure(code, message)
            if hint.code in {"need_manual_verification", "app_verification_required", "risk_control", "rate_limited"}:
                if hint.code == 'need_manual_verification' and isinstance(sms_data, dict):
                    self._set_manual_verification_url(_pick(sms_data, 'ImgVerifyUrl', 'imgVerifyUrl'))
                raise hint
        if empty_code and code == 9004001:
            return {}
        if code is None:
            raise UUError("protocol_error")
        if code != 0:
            raise self._failure(code, message)
        if path == _SMS and not any(term in message.lower() for term in ("成功", "发送", "success", "sent")):
            raise UUError("protocol_error", api_code=code)
        return _pick(body, "Data", "data", default={})

    def _ensure_uk(self) -> None:
        if self.state.get("uk"):
            return
        try:
            from Crypto.Cipher import AES, PKCS1_v1_5
            from Crypto.PublicKey import RSA
            from Crypto.Util.Padding import pad, unpad
        except ImportError:
            raise UUError("dependency_missing") from None
        key = self._random_string(16).encode("ascii")
        try:
            plain = json.dumps({"iud": self.state["device_id"]}, separators=(",", ":")).encode("utf-8")
            payload = {
                "encryptedData": base64.b64encode(AES.new(key, AES.MODE_ECB).encrypt(pad(plain, AES.block_size))).decode("ascii"),
                "encryptedAesKey": base64.b64encode(PKCS1_v1_5.new(RSA.import_key(_PUBLIC_KEY)).encrypt(key)).decode("ascii"),
            }
        except Exception:
            raise UUError("device_error") from None
        response = self._send("POST", _DEVICE, payload, authenticated=False)
        # Device checks may themselves return a JSON verification error instead
        # of an encrypted success response. Preserve that classification.
        if response.text.lstrip().startswith("{"):
            try:
                body = response.json()
            except (ValueError, TypeError):
                raise UUError("device_error") from None
            if isinstance(body, dict):
                api_code = _numeric_code(_pick(body, "Code", "code"))
                message = str(_pick(body, "Msg", "msg", "Message", "message", default=""))
                if api_code != 0:
                    raise self._failure(api_code, message)
            raise UUError("device_error")
        try:
            encrypted = response.text.strip()
            if encrypted.startswith('"'):
                encrypted = json.loads(encrypted)
            decoded = base64.b64decode(encrypted, validate=True)
            result = json.loads(unpad(AES.new(key, AES.MODE_ECB).decrypt(decoded), AES.block_size))
            uk = _pick(result, "u", "uk", "Uk") if isinstance(result, dict) else None
            if not isinstance(uk, str) or not re.fullmatch(r"[A-Za-z0-9_\-+/=]{8,512}", uk):
                raise ValueError
        except Exception:
            # Do not replay the handshake, change headers, or return raw body.
            raise UUError("device_error") from None
        self.state["uk"] = uk

    @staticmethod
    def _manual_result(error: UUError) -> dict[str, Any]:
        result = error.as_dict()
        result["status"] = "need_manual_verification"
        return result

    def request_sms(self, phone: str) -> dict[str, Any]:
        normalized = _phone(phone)
        pending = self.state.get("sms_pending")
        if isinstance(pending, dict):
            sent = pending.get("sent_at", 0)
            if isinstance(sent, (int, float)) and time.time() - sent < 60:
                raise UUError("sms_cooldown")
        try:
            self._ensure_uk()
            self._json_call("POST", _SMS, {
                "Area": 86, "Mobile": normalized,
                "Sessionid": self.state["device_token"], "Code": "",
            }, authenticated=False)
        except UUError as error:
            if error.code in ("need_manual_verification", "app_verification_required", "risk_control"):
                return self._manual_result(error)
            raise
        self.state["sms_pending"] = {
            "phone_hash": hashlib.sha256(normalized.encode("ascii")).hexdigest(),
            "sent_at": time.time(), "session_id": self.state["device_token"],
        }
        return {"ok": True, "status": "sms_requested", "message": "平台已接受短信请求，尚未确认送达。收到后请在本页输入；若仍收不到，请勿连续发送，保留页面状态供排查。"}

    def sign_in(self, phone: str, code: str) -> dict[str, Any]:
        normalized = _phone(phone)
        if code == "":
            return self._manual_result(UUError("need_manual_verification"))
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{4,8}", code):
            raise UUError("invalid_input")
        pending = self.state.get("sms_pending")
        if not isinstance(pending, dict) or pending.get("phone_hash") != hashlib.sha256(normalized.encode("ascii")).hexdigest():
            raise UUError("sms_not_requested")
        device = pending.get("session_id")
        if device != self.state["device_token"]:
            raise UUError("invalid_state")
        try:
            data = self._json_call("POST", _SIGN_IN, {
                "Area": 86, "Code": code, "DeviceName": device,
                "Sessionid": device, "Mobile": normalized,
            }, authenticated=False)
        except UUError as error:
            if error.code in ("need_manual_verification", "app_verification_required", "risk_control"):
                return self._manual_result(error)
            raise
        token = _pick(data, "Token", "token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token or len(token) > 16384 or "\r" in token or "\n" in token:
            raise UUError("protocol_error")
        self.state["token"] = token
        self.state.pop("sms_pending", None)
        self.state.pop("user_id", None)
        return {"ok": True, "status": "signed_in", "message": "登录凭据已取得，接下来可进行只读验证。"}

    def request_sms_up(self, phone: str) -> dict[str, Any]:
        """Get the platform's alternate login instructions; never send SMS.

        Protocol: YouPinAuthService.cs GetSmsUpConfigAsync / Steamauto.
        The recipient and content are returned only to the local user-operated
        setup page. They are not put into serializable credential state.
        """
        normalized = _phone(phone)
        self._ensure_uk()
        data = self._json_call('GET', _SMS_UP_CONFIG, authenticated=False)
        if not isinstance(data, dict):
            raise UUError('protocol_error')
        content = _pick(data, 'SmsUpContent', 'smsUpContent', 'Content', 'content')
        number = _pick(data, 'SmsUpNumber', 'smsUpNumber', 'Number', 'number')
        if (not isinstance(content, str) or not 1 <= len(content) <= 300
                or any(ord(c) < 32 or ord(c) == 127 for c in content)
                or not isinstance(number, str) or not re.fullmatch(r'\+?[0-9]{5,20}', number)):
            raise UUError('protocol_error')
        self.state['sms_up_pending'] = {
            'phone_hash': hashlib.sha256(normalized.encode('ascii')).hexdigest(),
            'requested_at': time.time(), 'session_id': self.state['device_token'],
        }
        return {'ok': True, 'status': 'sms_up_instructions',
                'sms_number': number, 'sms_content': content,
                'message': '请使用悠悠账号绑定的手机号，手动向下列官方返回号码发送指定内容；本程序没有发送短信。'}

    def sign_in_up(self, phone: str) -> dict[str, Any]:
        normalized = _phone(phone)
        pending = self.state.get('sms_up_pending')
        phone_hash = hashlib.sha256(normalized.encode('ascii')).hexdigest()
        if not isinstance(pending, dict) or pending.get('phone_hash') != phone_hash:
            raise UUError('sms_up_not_requested')
        if pending.get('session_id') != self.state['device_token']:
            raise UUError('invalid_state')
        requested_at = pending.get('requested_at')
        if not isinstance(requested_at, (int, float)) or not 0 <= time.time() - requested_at <= 600:
            raise UUError('sms_up_expired')
        data = self._json_call('POST', _SMS_UP_SIGN_IN, {
            'Area': 86, 'Code': '', 'DeviceName': self.state['device_token'],
            'Sessionid': self.state['device_token'], 'Mobile': normalized,
        }, authenticated=False)
        token = _pick(data, 'Token', 'token') if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip() or len(token) > 16384 or '\r' in token or '\n' in token:
            raise UUError('protocol_error')
        self.state['token'] = token.strip()
        self.state.pop('sms_pending', None)
        self.state.pop('sms_up_pending', None)
        self.state.pop('user_id', None)
        return {'ok': True, 'status': 'signed_in', 'message': '登录凭据已取得，接下来进行只读验证。'}

    def userinfo(self) -> dict[str, Any]:
        try:
            data = self._json_call("GET", _USER_INFO)
        except UUError as error:
            # The current BFF can return an opaque non-JSON body. The audited
            # source also falls back to this authenticated legacy read. Never
            # retry authentication, access, challenge, risk or rate failures.
            if error.code not in {"endpoint_not_found", "protocol_error"}:
                raise
            data = self._json_call("GET", _LEGACY_USER_INFO)
        if not isinstance(data, dict):
            raise UUError("protocol_error")
        user_id = _pick(data, "UserId", "userId", "Id", "id")
        user_id_text = str(user_id).strip() if user_id is not None else ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", user_id_text):
            raise UUError("protocol_error")
        self.state["user_id"] = user_id_text
        uk = _pick(data, "uk", "Uk", "u", "U")
        if isinstance(uk, str) and re.fullmatch(r"[A-Za-z0-9_\-+/=]{8,512}", uk):
            self.state["uk"] = uk
        return data

    def zero_cd_shelf(self, page_size: int = 20, max_pages: int = 3) -> list[dict[str, Any]]:
        _bounded_int(page_size, 1, 100)
        _bounded_int(max_pages, 1, 50)
        result: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            data = self._json_call("POST", _ZERO_CD, {
                "pageIndex": page, "pageSize": page_size,
                "whetherMerge": 0, "Sessionid": self.state["device_token"],
            }, empty_code=True)
            if not isinstance(data, dict):
                raise UUError("protocol_error")
            rows = _pick(data, "commodityInfoList", "CommodityInfoList", "list", default=[])
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise UUError("protocol_error")
            result.extend(rows)
            if len(rows) < page_size:
                break
        return result

    def reprice_init(self, listing_ids: list[str]) -> Any:
        if not isinstance(listing_ids, list) or not 1 <= len(listing_ids) <= 30:
            raise UUError("invalid_input")
        ids = [_id(value) for value in listing_ids]
        return self._json_call("POST", _REPRICE_INIT, {
            "changePriceChannel": 0, "commodityIdList": ids,
            "gameId": "730", "Sessionid": self.state["device_token"],
        })

    def change_short_rent(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit exactly one reviewed item; acceptance still requires readback."""
        if not self.allow_price_write:
            raise UUError('price_writes_disabled')
        if (not isinstance(payload, dict) or set(payload) != {'Commoditys', 'Sessionid'}
                or payload['Sessionid'] != self.state['device_token']
                or not isinstance(payload['Commoditys'], list) or len(payload['Commoditys']) != 1):
            raise UUError('invalid_input')
        item = payload['Commoditys'][0]
        allowed_fields = {'CommodityId', 'IsCanLease', 'IsCanSold', 'LeaseDeposit',
                          'LeaseMaxDays', 'LeaseUnitPrice', 'LongLeaseUnitPrice', 'Price',
                          'CompensationType', 'SupportZeroCD', 'ZeroCDConfig', 'supportSubletIncentive'}
        if not isinstance(item, dict) or set(item) - allowed_fields:
            raise UUError('invalid_input')
        item_id = _id(item.get('CommodityId'))
        data = self._json_call('PUT', _PRICE_WRITE, payload)
        if not isinstance(data, dict):
            raise UUError('write_not_confirmed')
        success = _numeric_code(_pick(data, 'SuccessCount', 'successCount'))
        failed = _numeric_code(_pick(data, 'FailCount', 'failCount', default=0))
        details = _pick(data, 'Commoditys', 'commoditys')
        if success != 1 or failed != 0:
            raise UUError('write_not_confirmed')
        if details is not None:
            if not isinstance(details, list) or not any(
                isinstance(row, dict)
                and str(_pick(row, 'CommodityId', 'commodityId', default='')) == item_id
                and _numeric_code(_pick(row, 'IsSuccess', 'isSuccess')) == 1
                for row in details
            ):
                raise UUError('write_not_confirmed')
        return {'accepted': True, 'readback_required': True}

    def market_short_leases(self, template_id: Any, max_items: int = 10,
                            **filters: Any) -> list[dict[str, Any]]:
        """Return raw listing rows; no claim that default sorting is cheapest.

        Optional min_deposit/max_deposit filters are local and do not create
        unreviewed API parameters. sort_type_key currently accepts only the
        source-audited LEASE_DEFAULT value.
        """
        template = _id(template_id)
        _bounded_int(max_items, 1, 50)
        if set(filters) - {"min_deposit", "max_deposit", "sort_type_key"}:
            raise UUError("invalid_input")
        if filters.get("sort_type_key", "LEASE_DEFAULT") != "LEASE_DEFAULT":
            raise UUError("invalid_input")
        minimum = filters.get("min_deposit", 0)
        maximum = filters.get("max_deposit", math.inf)
        if isinstance(minimum, bool) or isinstance(maximum, bool):
            raise UUError("invalid_input")
        try:
            minimum, maximum = float(minimum), float(maximum)
        except (TypeError, ValueError, OverflowError):
            raise UUError("invalid_input") from None
        if not math.isfinite(minimum) or minimum < 0 or math.isnan(maximum) or maximum < minimum:
            raise UUError("invalid_input")
        if not self.state.get("user_id"):
            self.userinfo()
        data = self._json_call("POST", _MARKET, {
            "hasLease": "true", "haveBuZhangType": 0, "listSortType": "2",
            "listType": 30, "mergeFlag": 0, "pageIndex": 1, "pageSize": 50,
            "sortType": "1", "sortTypeKey": "LEASE_DEFAULT", "status": "20",
            "stickerAbrade": 0, "stickersIsSort": False, "templateId": template,
            "ultraLongLeaseMoreZones": 0, "userId": self.state["user_id"],
            "Sessionid": self.state["device_token"],
        })
        if isinstance(data, dict):
            rows = _pick(data, "CommodityList", "commodityList", "commodityInfoList", default=[])
        else:
            rows = data
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise UUError("protocol_error")
        if "min_deposit" in filters or "max_deposit" in filters:
            selected = []
            for row in rows:
                try:
                    deposit = float(_pick(row, "LeaseDeposit", "leaseDeposit", "depositAmount"))
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(deposit) and minimum <= deposit <= maximum:
                    selected.append(row)
            rows = selected
        return rows[:max_items]
