# 悠悠有品大会员0cd 转租省心定价方案

[![CI](https://github.com/ALTIOU/youpin-vip-0cd-pricing/actions/workflows/ci.yml/badge.svg)](https://github.com/ALTIOU/youpin-vip-0cd-pricing/actions/workflows/ci.yml)

面向悠悠有品大会员 0CD 转租的 NAS 常驻调价工具。按小时读取行情、排除自己的挂单，将长租和短租同步到目标价。默认只预览，明确加 `--apply` 后才改价。

> Unofficial, local-first YouPin 0CD rental repricer. Hourly checks, explicit apply, and post-write verification. No GPT or OpenAI API is used.

当前版本为 **0.1.0 beta**，面向 Linux / macOS；Windows 请使用 WSL 或 Docker。已做单账号实测，尚未完成多账号、长时间运行验证。它使用非官方 App 服务接口，接口变动时可能需要适配。

## 定价规则

```text
目标短租 = max(0.01, 其他卖家可比短租最低价 - 0.01)
目标长租 = 目标短租
检查间隔 = 3600 秒
```

模拟示例：其他卖家最低短租为 `0.07` 时，长短租一起设为 `0.06`；最低价已经是 `0.01` 时，两栏都保持 `0.01`。自己的挂单不会参与这个最低价计算。

只处理本人当前 0CD 货架中可租赁的商品。市场读取会遍历当前查询的分页；达到页数上限、分页不完整或没有可用报价时，不提交改价。最低价指接口当前可见、通过条件检查的报价，不代表不可见或其他交易条件下的报价。

当前比较条件为同一 `templateId`、可租赁、有效租期和有效短租价；磨损、贴纸、特殊图案等附加价值需要用户自己核对。

改价前核对货架和 0CD 配置，改价后回读确认。请求结果不明确时，记录在私有 journal 中，后续先核对，不自动重发该笔写入。

## 本机登录与预览

需要 Python 3.11+。请先在自己可信任的电脑上完成登录；登录页仅绑定 `127.0.0.1`。

```sh
git clone https://github.com/ALTIOU/youpin-vip-0cd-pricing.git
cd youpin-vip-0cd-pricing
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
mkdir -p state
cp examples/policy.example.json policy.json
youpin-login --state-dir ./state
```

打开 `http://127.0.0.1:18478/`，输入自己的悠悠手机号，获取上行短信说明。使用该手机号手动发送平台返回的内容，再点“已发送”检查登录。程序不会代发短信，不需要填写 OpenAI API Key。

登录成功后，可按 `Ctrl+C` 关闭登录页服务，再运行一次预览：

```sh
youpin-reprice --state state/session.json --policy policy.json --output state/preview-result.json --once
```

确认当前账号、待改商品和目标价后，才执行真实单次改价：

```sh
youpin-reprice --state state/session.json --policy policy.json --output state/latest-result.json --once --apply
```

`session.json` 包含账号凭据。不要上传它、`state/`、运行日志或真实账号截图到 GitHub / Issue。项目自带的忽略规则只是辅助，提交前仍需检查实际文件。

## Docker / NAS 每小时运行

把代码、`policy.json` 和已登录的 `state/session.json` 放在自己的 NAS 项目目录。容器用户必须与状态文件所有者一致，才能读取 `0600` 会话和写入 `0700` 状态目录。

在本机 Linux / macOS 登录后，可将当前用户 ID 写入仅供 Compose 使用的 `.env`：

```sh
printf 'PUID=%s\nPGID=%s\n' "$(id -u)" "$(id -g)" > .env
docker compose up -d --build
docker compose logs --tail=20 repricer
```

**以上仍是每小时预览，不会改价。** NAS 文件若由另一个用户上传，请将 `.env` 中的 `PUID` / `PGID` 设置为 NAS 上 `state` 目录及会话文件的所有者 ID，并保证该用户可写状态目录；不要把凭据权限放宽到 `777`。

确认预览后，使用显式 live 配置启用每小时改价：

```sh
docker compose -f compose.yaml -f compose.live.yaml up -d --build
docker compose -f compose.yaml -f compose.live.yaml logs --tail=20 repricer
```

停止任务：

```sh
docker compose down
```

无需开放任何容器端口。容器运行在 NAS 上时，Mac 和手机无需一直开着。登录失效、平台验证或无法确认的写入会保留日志 / journal，需要处理后再恢复。不要删除 pending journal 来盲目重试未知请求。

## 策略配置

复制 `examples/policy.example.json` 到项目根目录的 `policy.json`。当前版本固定支持每小时、最低一分、减一分的定价公式。

- `sync_long_rent_at_short: true`：长租、短租始终同价。
- `max_market_pages`：市场读取页数上限，默认 10；达到上限但仍有下一页时跳过改价。
- 可选 `listing_ids`：只处理指定的本人货架 ID；省略时读取当前本人 0CD 货架。这里不要填写账号 ID。

## 开发与验证

```sh
python -m unittest discover -s tests -v
python -m youpin_rent_repricer --help
```

测试使用合成数据与 Mock；本地 HTTP 测试只监听回环地址，不需要平台账号，也不发送真实价格请求。

## 来源与许可

接口行为、请求结构及部分移植逻辑参考 [CS2TradeMonitor](https://github.com/cs2juece/CS2TradeMonitor) 与 [Steamauto](https://github.com/Steamauto/Steamauto)。本项目不隶属于悠悠有品或上述项目。

新编写部分使用 **AGPL-3.0-only**；从 CS2TradeMonitor 适配或保守按派生处理的模块保留 **GPL-3.0-only**。整个源码分发的 SPDX 表达式为 `AGPL-3.0-only AND GPL-3.0-only`，并非把原 GPL 代码重新许可为 AGPL。

逐模块来源和完整说明见 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) 与 [LICENSING.txt](LICENSING.txt)。完整许可文本保存在 `LICENSE`、`COPYING.GPL-3.0` 和 `LICENSES/`。
