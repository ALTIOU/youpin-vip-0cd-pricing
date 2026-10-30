# SPDX-License-Identifier: AGPL-3.0-only
"""Loopback-only, user-operated YouPin login and read-only inspection.

No price-update, order, listing, or scheduling endpoint is implemented.
Credentials stay in a private state directory and never enter HTTP responses.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .uu_client import UUClient, UUError


MANUAL_VERIFICATION_UNSUPPORTED = '此验证需要悠悠 App 内回传，浏览器完成不能接续此登录，请勿重复发码。'


def pick(obj, *keys, default=None):
    if not isinstance(obj, dict):
        return default
    for key in keys:
        if key in obj:
            return obj[key]
    return default


def safe_message(value):
    text = str(value)
    text = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[redacted]", text)
    text = re.sub(r"\b1[3-9]\d{9}\b", "[phone redacted]", text)
    text = re.sub(r"[A-Za-z0-9_\-\.]{45,}", "[redacted]", text)
    return text[:300]


def atomic_private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    tmp.chmod(0o600)
    tmp.replace(path)


PAGE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>悠悠有品 · 只读验证</title>
<style>
body{font:16px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;background:#f4f6fa;color:#192230;margin:0}main{max-width:900px;margin:36px auto;padding:0 22px}h1{font-size:25px;margin-bottom:6px}h2{font-size:19px}.card{background:white;border:1px solid #dde3ed;border-radius:14px;padding:22px;margin:18px 0}.tag{color:#266545;background:#e7f6ed;display:inline-block;padding:4px 10px;border-radius:7px;font-size:13px}.muted{color:#65728a;font-size:14px}label{display:block;margin-top:12px}input,select,button{font:inherit;border:1px solid #c9d2e1;border-radius:8px;padding:9px 12px}input{width:min(310px,85%)}button{cursor:pointer;background:#235bda;color:white;margin:12px 8px 0 0}button:disabled{opacity:.5;cursor:wait}button.secondary{background:#fff;color:#235bda}#message{white-space:pre-wrap;overflow-wrap:anywhere}.error{color:#ab2a29}table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:10px 7px;border-bottom:1px solid #e8edf4}pre{white-space:pre-wrap;font-size:13px;background:#f4f6fa;border-radius:8px;padding:12px;overflow-wrap:anywhere}.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
</style><main><h1>悠悠有品 · 只读验证</h1><span class="tag">当前版本只登录和读取，不修改价格</span>
<p class="muted">先验证 0CD 转租短租租金的真实字段及市场报价。此页面仅用于本机登录和核对；定时运行器需要单独启动。</p>
<section class="card"><h2>1. 用手机主动发送一条验证短信</h2>
<p>先获取悠悠官方返回的接收号码和短信内容，再用账号绑定的手机号手动发送。这个流程不需要等待验证码下发。登录凭证只保存在本机服务的私有目录。</p>
<p class="muted">首次登录可能触发平台的多设备登录策略。登录完成后，请同时检查手机 App 是否仍保持登录。出现验证或限制时，本程序会停止。</p>
<label>悠悠有品手机号（中国大陆）<br><input id="phone" type="tel" autocomplete="tel" inputmode="numeric" placeholder="输入账号绑定的手机号"></label>
<button id="upconfig">获取官方验证短信说明</button>
<div id="smsinfo" hidden><p>请使用上面填写的手机号，在手机短信应用中发送：</p><p>接收号码：<strong id="smsnumber"></strong><br>短信内容：<strong id="smscontent"></strong></p><p class="muted">本程序不会替你发送短信。运营商可能按套餐计费；请核对号码与内容，发送后再点下面的按钮。</p><button id="uplogin">我已用该手机号发送，检查登录</button></div>
<button id="read" class="secondary">使用已有登录读取</button><p id="message" role="status">尚未获取验证说明，也未发送短信。</p>
<p class="muted">平台若要求 App 内验证，请在 App 中处理；本页不代做图形校验。</p></section>
<section class="card"><h2>2. 选择一件商品核对</h2><p class="muted">页面只展示必要字段；接口里的活动短租价与货架短租价分开展示，不自动认定两者语义相同。</p>
<div id="inventory">等待登录。</div><div class="row"><select id="item"><option value="">请选择商品</option></select><button id="inspect">读取该件配置和市场租金</button></div><pre id="details">尚未读取。</pre></section>
</main><script nonce="__NONCE__">
const csrf='__CSRF__'; const $=id=>document.getElementById(id);
function message(text,error=false){$('message').textContent=text;$('message').className=error?'error':'';}
async function call(path,data){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Setup-CSRF':csrf},body:JSON.stringify(data)});let result=await response.json();if(!response.ok||!result.ok)throw new Error(result.message||'请求未成功');return result;}
function inventory(result){const data=result.items||[];$('inventory').textContent=`已读取 ${data.length} 件 0CD 商品（本次最多读取 3 页）。`;$('item').replaceChildren(new Option('请选择商品',''));for(const item of data){const label=`${item.name||'未命名商品'} · 当前短租 ${item.short_rent??'未知'}`;$('item').appendChild(new Option(label,item.id));}}
async function run(id,action){const b=$(id);b.disabled=true;try{await action();}catch(error){message(error.message,true);}finally{b.disabled=false;}}
$('phone').oninput=()=>{$('smsinfo').hidden=true;};
$('upconfig').onclick=()=>run('upconfig',async()=>{$('smsinfo').hidden=true;message('正在获取悠悠官方的验证短信说明，不会发送短信…');const r=await call('/api/up-config',{phone:$('phone').value});$('smsnumber').textContent=r.sms_number;$('smscontent').textContent=r.sms_content;$('smsinfo').hidden=false;message('已取得官方说明。请用手机手动发送，随后检查登录。能否登录以平台实际结果为准。');});
$('uplogin').onclick=()=>run('uplogin',async()=>{message('正在检查上行短信登录并读取少量 0CD 商品…');const r=await call('/api/up-login',{phone:$('phone').value,sent:true});inventory(r);$('smsinfo').hidden=true;message('已登录并完成只读查询。请核对手机 App 是否仍能正常使用；选择商品可继续核对租金字段。');});
$('read').onclick=()=>run('read',async()=>{message('正在使用保存的登录做只读查询…');const r=await call('/api/read',{});inventory(r);message('读取成功。本次没有修改任何价格。');});
$('inspect').onclick=()=>run('inspect',async()=>{if(!$('item').value)throw new Error('请先选择一件商品');message('正在读取配置和市场报价…');const r=await call('/api/inspect',{listing_id:$('item').value});$('details').textContent=JSON.stringify(r.comparison,null,2);message('已读取，请对照手机 App 中同一件商品的短租租金。当前未生成或提交改价。');});
fetch('/api/status').then(r=>r.json()).then(r=>{if(r.authenticated)message('检测到本机已保存的登录。可以先点“使用已有登录读取”。');}).catch(()=>{});
</script></html>'''


class Inspector:
    def __init__(self, directory):
        self.directory = directory
        self.state_file = directory / 'session.json'
        state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.client = UUClient(state)
        self.items = []
        self.lock = threading.Lock()
        self.csrf = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(20)
        # A monotonic clock is elapsed time since boot, not Unix time. Zero is
        # a valid reading on a fresh system, so use None for "never requested".
        self.last_sms: float | None = None
        self.last_up_config: float | None = None

    def save_state(self):
        atomic_private_json(self.state_file, self.client.state)

    def read(self):
        self.client.userinfo()
        self.save_state()
        self.items = self.client.zero_cd_shelf(page_size=20, max_pages=3)
        atomic_private_json(self.directory / 'shelf-private.json', {'queried_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'items': self.items})
        return {'ok': True, 'items': [self.summary(x) for x in self.items]}

    @staticmethod
    def summary(item):
        return {'id': str(pick(item, 'id', 'commodityId', 'CommodityId', default='')), 'template_id': pick(item, 'templateId', 'TemplateId'), 'name': pick(item, 'name', 'Name'), 'short_rent': pick(item, 'shortLeaseAmount', 'leaseUnitPrice', 'LeaseUnitPrice'), 'long_rent': pick(item, 'longLeaseAmount', 'LongLeaseUnitPrice')}

    def inspect(self, listing_id):
        item = next((x for x in self.items if self.summary(x)['id'] == listing_id), None)
        if item is None:
            raise ValueError('请先读取货架并从列表中选择商品')
        summary = self.summary(item)
        config = self.client.reprice_init([listing_id])
        market = self.client.market_short_leases(summary['template_id'], max_items=10)
        queried_at = dt.datetime.now(dt.timezone.utc).isoformat()
        atomic_private_json(self.directory / 'sample-private.json', {'queried_at': queried_at, 'item': item, 'init': config, 'market': market})
        zero_map = pick(config, 'zeroCDRentConfigMap', 'ZeroCDRentConfigMap', default={}) or {}
        nested = zero_map.get(listing_id, {}) if isinstance(zero_map, dict) else {}
        quotes = market if isinstance(market, list) else pick(market, 'CommodityList', 'commodityList', 'commodityInfoList', default=[])
        comparison = {'说明': '只读快照。查询时间不代表源数据更新时间；默认市场排序尚未验证为最低价排序。', '查询时间': queried_at, '商品': summary, '0CD活动配置': {key: pick(nested, key, key[0].upper()+key[1:]) for key in ['pricingType', 'shortRentPrice', 'longRentPrice']}, '市场报价样本': [{'short_rent': pick(x, 'LeaseUnitPrice', 'leaseUnitPrice', 'shortLeaseAmount'), 'long_rent': pick(x, 'LongLeaseUnitPrice', 'longLeaseUnitPrice'), 'min_days': pick(x, 'LeaseMinDays', 'leaseMinDays'), 'max_days': pick(x, 'LeaseMaxDays', 'leaseMaxDays')} for x in (quotes or [])[:10]], '可执行改价': False}
        return {'ok': True, 'comparison': comparison}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, value, html=False):
            body = value.encode() if html else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', f"default-src 'none'; script-src 'nonce-{app.nonce}'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/':
                self.send(200, PAGE.replace('__NONCE__', app.nonce).replace('__CSRF__', app.csrf), html=True)
            elif self.path == '/api/status':
                self.send(200, {'ok': True, 'authenticated': bool(app.client.state.get('token') and app.client.state.get('user_id')), 'mode': 'read_only', 'writes_enabled': False, 'last_sms_status': app.client.state.get('last_sms_status'), 'verification_available': False})
            elif self.path == '/verification':
                self.send(410, {'ok': False, 'message': MANUAL_VERIFICATION_UNSUPPORTED, 'verification_available': False})
            else:
                self.send(404, {'ok': False, 'message': '未提供此功能'})

        def do_POST(self):
            expected_origin = f'http://127.0.0.1:{self.server.server_port}'
            if self.headers.get('Origin') != expected_origin or not hmac.compare_digest(self.headers.get('X-Setup-CSRF', ''), app.csrf):
                return self.send(403, {'ok': False, 'message': '请从本机验证页面发起操作'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                return self.send(400, {'ok': False, 'message': '输入长度不正确'})
            if length <= 0 or length > 2048:
                return self.send(400, {'ok': False, 'message': '输入长度不正确'})
            if self.path not in {'/api/sms', '/api/login', '/api/up-config', '/api/up-login', '/api/read', '/api/inspect'}:
                return self.send(403, {'ok': False, 'message': '此版本不提供任何价格修改功能'})
            if not app.lock.acquire(blocking=False):
                return self.send(409, {'ok': False, 'message': '上一项操作仍在执行，请稍候'})
            try:
                payload = json.loads(self.rfile.read(length))
                if self.path in {'/api/sms', '/api/login', '/api/up-config', '/api/up-login'}:
                    phone = str(payload.get('phone', '')).strip()
                    if not re.fullmatch(r'1[3-9]\d{9}', phone):
                        raise ValueError('请填写正确的中国大陆手机号')
                if self.path == '/api/up-config':
                    import time
                    if app.last_up_config is not None and time.monotonic() - app.last_up_config < 30:
                        raise ValueError('请间隔至少 30 秒再获取说明')
                    app.last_up_config = time.monotonic()
                    result = app.client.request_sms_up(phone)
                    app.save_state()
                    self.send(200, result)
                elif self.path == '/api/up-login':
                    if payload.get('sent') is not True:
                        raise ValueError('请先用自己的手机发送官方指定短信，再手动检查登录')
                    app.client.sign_in_up(phone)
                    app.save_state()
                    self.send(200, app.read())
                elif self.path == '/api/sms':
                    import time
                    if app.last_sms is not None and time.monotonic() - app.last_sms < 60:
                        raise ValueError('请间隔至少 60 秒再申请验证码')
                    app.last_sms = time.monotonic()
                    result = app.client.request_sms(phone)
                    if not result.get('ok'):
                        raise UUError(result.get('error', 'need_manual_verification'))
                    app.save_state()
                    self.send(200, {'ok': True, 'message': safe_message(result.get('message', '验证码请求已完成，请查看手机短信'))})
                elif self.path == '/api/login':
                    code = str(payload.get('code', '')).strip()
                    if not re.fullmatch(r'\d{4,8}', code):
                        raise ValueError('请填写短信验证码')
                    result = app.client.sign_in(phone, code)
                    if not result.get('ok'):
                        raise UUError(result.get('error', 'need_manual_verification'))
                    app.save_state()
                    self.send(200, app.read())
                elif self.path == '/api/read':
                    self.send(200, app.read())
                else:
                    self.send(200, app.inspect(str(payload.get('listing_id', ''))))
            except (UUError, ValueError) as error:
                if self.path in {'/api/sms', '/api/login'} and app.client.state.get('last_sms_status'):
                    app.save_state()
                message = MANUAL_VERIFICATION_UNSUPPORTED if isinstance(error, UUError) and error.code == 'need_manual_verification' else safe_message(error)
                self.send(400, {'ok': False, 'message': message, 'verification_available': False})
            except Exception:
                self.send(500, {'ok': False, 'message': '本次查询未完成；已停止，没有执行价格修改。'})
            finally:
                app.lock.release()
    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18478)
    parser.add_argument('--state-dir', type=Path, default=Path.cwd() / 'state')
    args = parser.parse_args(argv)
    app = Inspector(args.state_dir)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app))
    print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}/', 'mode': 'read_only', 'price_writes_enabled': False}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
