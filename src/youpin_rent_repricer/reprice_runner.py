# SPDX-License-Identifier: GPL-3.0-only
# Adapted with reference to CS2TradeMonitor contributors, revision
# 41d52fdb7997e023e6c431a7173baa9d8b00806e.
# Modified for YouPin VIP 0CD Pricing on 2026-10-30.
# See THIRD_PARTY_NOTICES.txt for provenance and combined-license terms.
"""Bounded hourly repricing with private recovery journal; read-only by default.

No I/O occurs on import. Only an explicit --apply enables the client's reviewed
single-item PUT. The journal is written before that PUT, and uncertain requests
are never replayed automatically in later cycles.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .market_reader import read_market_snapshot
from .reprice_writer import (
    RepriceContractError, activity_fields, build_short_rent_payload,
    listing_id, readback_matches, shelf_values,
)
from .uu_client import UUClient, UUError


_CENT = Decimal('0.01')
_SHELF_PAGE_SIZE = 50
_SHELF_MAX_PAGES = 3


class RunnerError(ValueError):
    """Fixed reasons only; raw inputs and credentials never enter messages."""


def _pick(obj, *keys, default=None):
    if not isinstance(obj, dict):
        return default
    for key in keys:
        if key in obj:
            return obj[key]
    return default


def _identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise RunnerError('invalid_identifier')
    value = str(value)
    if not re.fullmatch(r'[0-9]{1,30}', value) or int(value) <= 0:
        raise RunnerError('invalid_identifier')
    return value


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise RunnerError('invalid_price')
    try:
        price = Decimal(str(value))
        if not price.is_finite() or price <= 0 or price % _CENT:
            raise RunnerError('invalid_price')
        return price.quantize(_CENT)
    except (InvalidOperation, ValueError):
        raise RunnerError('invalid_price') from None


def _text(value):
    return format(value, '.2f')


def _flag(value):
    if value is True or type(value) is int and value == 1 or value in ('true', '1'):
        return True
    if value is False or type(value) is int and value == 0 or value in ('false', '0'):
        return False
    raise RunnerError('invalid_flag')


def validate_policy(policy):
    if not isinstance(policy, dict):
        raise RunnerError('invalid_policy')
    if policy.get('interval_seconds', 3600) != 3600 or type(policy.get('interval_seconds', 3600)) is not int:
        raise RunnerError('unsupported_interval')
    if _money(policy.get('minimum_price', '0.01')) != _CENT or _money(policy.get('undercut_price', '0.01')) != _CENT:
        raise RunnerError('unsupported_price_rule')
    if policy.get('floor_price') is not None:
        raise RunnerError('unsupported_extra_floor')
    cap_long = policy.get('cap_long_rent_at_short', False)
    if type(cap_long) is not bool:
        raise RunnerError('invalid_long_rent_cap_flag')
    sync_long = policy.get('sync_long_rent_at_short', False)
    if type(sync_long) is not bool:
        raise RunnerError('invalid_long_rent_sync_flag')
    if cap_long and sync_long:
        raise RunnerError('conflicting_long_rent_modes')
    ids = policy.get('listing_ids')
    if ids is not None and (not isinstance(ids, list) or not ids or len(ids) > 150):
        raise RunnerError('invalid_listing_allowlist')
    ids = [_identifier(v) for v in ids] if ids is not None else None
    if ids is not None and len(ids) != len(set(ids)):
        raise RunnerError('duplicate_allowlist_id')
    cap = policy.get('max_market_pages', 10)
    if type(cap) is not int or not 1 <= cap <= 20:
        raise RunnerError('invalid_market_page_limit')
    return {'interval_seconds': 3600, 'minimum_price': '0.01', 'undercut_price': '0.01',
            'listing_ids': ids, 'max_market_pages': cap, 'cap_long_rent_at_short': cap_long,
            'sync_long_rent_at_short': sync_long}


def atomic_private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _load_journal(path):
    if path is None or not Path(path).exists():
        return {'schema_version': 1, 'pending': {}}
    try:
        value = json.loads(Path(path).read_text())
        if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(value.get('pending'), dict):
            raise ValueError
        for item_id, pending in value['pending'].items():
            _identifier(item_id)
            if not isinstance(pending, dict) or not isinstance(pending.get('before_row'), dict) or not isinstance(pending.get('activity'), dict):
                raise ValueError
            _money(pending.get('target'))
            if type(pending.get('cap_long_rent_at_short', False)) is not bool:
                raise ValueError
            if type(pending.get('sync_long_rent_at_short', False)) is not bool:
                raise ValueError
            if pending.get('cap_long_rent_at_short', False) and pending.get('sync_long_rent_at_short', False):
                raise ValueError
        return value
    except (ValueError, TypeError, OSError):
        raise RunnerError('invalid_pending_journal') from None


def _private_before_row(row):
    values = shelf_values(row)
    return {
        'id': str(values['CommodityId']),
        'shortLeaseAmount': _text(values['LeaseUnitPrice']),
        'longLeaseAmount': _text(values['LongLeaseUnitPrice']),
        'depositAmount': str(values['LeaseDeposit']),
        'leaseMaxDays': values['LeaseMaxDays'],
        'commodityCanLease': values['IsCanLease'],
        'commodityCanSell': values['IsCanSold'], 'sellAmount': str(values['Price']),
    }


def _shelf(client):
    rows = client.zero_cd_shelf(page_size=_SHELF_PAGE_SIZE, max_pages=_SHELF_MAX_PAGES)
    if not isinstance(rows, list) or len(rows) >= _SHELF_PAGE_SIZE * _SHELF_MAX_PAGES:
        raise RunnerError('own_shelf_coverage_incomplete')
    mapping = {}
    for row in rows:
        item_id = listing_id(row)
        if item_id in mapping:
            raise RunnerError('own_shelf_duplicate_id')
        mapping[item_id] = row
    return mapping


def _eligible_prices(rows, template_id, own_seller_id, own_ids):
    if not isinstance(rows, list):
        raise RunnerError('invalid_market_rows')
    prices = []
    for row in rows:
        try:
            quote_id = listing_id(row)
            seller_id = _identifier(_pick(row, 'UserId', 'userId'))
            if quote_id in own_ids or seller_id == own_seller_id:
                continue
            mine = _pick(row, 'IsMine', 'isMine', 'Mine', 'mine')
            if mine is not None and _flag(mine):
                continue
            if _identifier(_pick(row, 'TemplateId', 'templateId')) != template_id:
                continue
            if not _flag(_pick(row, 'CanLease', 'canLease', 'IsCanLease')):
                continue
            max_days = _pick(row, 'LeaseMaxDays', 'leaseMaxDays')
            if isinstance(max_days, bool) or not isinstance(max_days, (str, int)) or not str(max_days).isdigit() or int(max_days) <= 0:
                continue
            prices.append(_money(_pick(row, 'LeaseUnitPrice', 'leaseUnitPrice', 'ShortLeaseAmount', 'shortLeaseAmount')))
        except (RunnerError, RepriceContractError):
            continue
    return prices


def _fresh_snapshot(client, template_id, cap):
    started = time.monotonic()
    snapshot = read_market_snapshot(client, template_id, max_pages=cap)
    if time.monotonic() - started > 300:
        raise RunnerError('market_read_exceeded_five_minutes')
    if not isinstance(snapshot, dict) or snapshot.get('coverage_complete') is not True:
        raise RunnerError('market_coverage_incomplete')
    _assert_snapshot_fresh(snapshot)
    return snapshot


def _assert_snapshot_fresh(snapshot):
    try:
        began = datetime.fromisoformat(snapshot['query_started_at'].replace('Z', '+00:00'))
        if began.tzinfo is None:
            raise ValueError
        age = (datetime.now(timezone.utc) - began).total_seconds()
        if not -5 <= age <= 300:
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError):
        raise RunnerError('market_snapshot_not_fresh') from None


def _verify_pending(client, pending):
    """Read once to reconcile a previous unknown result, without any new PUT."""
    item_id = listing_id(pending['before_row'])
    after = _shelf(client).get(item_id)
    if after is None or not readback_matches(pending['before_row'], after, pending['target'],
            cap_long_rent_at_short=pending.get('cap_long_rent_at_short', False),
            sync_long_rent_at_short=pending.get('sync_long_rent_at_short', False)):
        return False
    return activity_fields(client.reprice_init([item_id]), item_id) == pending['activity']


def run_cycle(client, policy, *, apply=False, journal_path=None):
    """Run one bounded cycle. ``apply`` callers must hold an exclusive lock.

    The CLI holds a per-state lock for its entire lifetime. In addition to the
    explicit client gate, application mode requires a durable journal path.
    Results contain only own listing details and aggregate peer counts/prices.
    """
    result = {'ok': False, 'mode': 'apply' if apply else 'preview', 'writes_enabled': bool(apply),
              'queried_at': datetime.now(timezone.utc).isoformat(), 'items': [], 'reason': None}
    try:
        policy = validate_policy(policy)
        if apply and journal_path is None:
            raise RunnerError('durable_journal_required_for_apply')
        journal = _load_journal(journal_path)
        client.userinfo()
        own_seller_id = _identifier(client.state.get('user_id'))
        own = _shelf(client)
        own_ids = set(own)
        selected_ids = policy['listing_ids'] if policy['listing_ids'] is not None else list(own)
        for item_id in selected_ids:
            row = own.get(item_id)
            item = {'listing_id': item_id, 'name': str(_pick(row, 'name', 'Name', default=''))[:160],
                    'status': 'blocked', 'reason': None}
            result['items'].append(item)
            if item_id in journal['pending']:
                if not apply:
                    item['reason'] = 'pending_write_requires_reconciliation'
                elif _verify_pending(client, journal['pending'][item_id]):
                    del journal['pending'][item_id]
                    atomic_private_json(journal_path, journal)
                    item['status'], item['reason'] = 'verified', 'previous_pending_write_verified_no_new_put'
                else:
                    item['reason'] = 'pending_write_unconfirmed_no_replay'
                continue
            if row is None:
                item['status'], item['reason'] = 'skipped', 'allowlisted_listing_not_on_own_shelf'
                continue
            try:
                before_values = shelf_values(row)
                if before_values['IsCanLease'] is not True:
                    raise RunnerError('own_listing_not_available_for_lease')
                template_id = _identifier(_pick(row, 'templateId', 'TemplateId'))
                snapshot = _fresh_snapshot(client, template_id, policy['max_market_pages'])
                prices = _eligible_prices(snapshot['rows'], template_id, own_seller_id, own_ids)
                if not prices:
                    raise RunnerError('no_eligible_other_seller_quotes')
                lowest = min(prices)
                target = max(_CENT, lowest - _CENT).quantize(_CENT)
                target_long = before_values['LongLeaseUnitPrice']
                if policy['sync_long_rent_at_short']:
                    target_long = target
                elif policy['cap_long_rent_at_short'] and target_long > target:
                    target_long = target
                item.update({'current_short_rent': _text(before_values['LeaseUnitPrice']),
                             'market_lowest': _text(lowest), 'target_short_rent': _text(target),
                             'current_long_rent': _text(before_values['LongLeaseUnitPrice']),
                             'target_long_rent': _text(target_long),
                             'cap_long_rent_at_short': policy['cap_long_rent_at_short'],
                             'sync_long_rent_at_short': policy['sync_long_rent_at_short'],
                             'market_rows': len(snapshot['rows']), 'eligible_quotes': len(prices),
                             'market_coverage_complete': True})
                if before_values['LeaseUnitPrice'] == target and before_values['LongLeaseUnitPrice'] == target_long:
                    item['status'], item['reason'] = 'unchanged', 'already_at_target'
                    continue
                init = client.reprice_init([item_id])
                expected_activity = activity_fields(init, item_id)
                if expected_activity.get('SupportZeroCD') != 1:
                    raise RunnerError('own_zero_cd_not_enabled')
                payload = build_short_rent_payload(row, init, target,
                    device_session=client.state['device_token'], expected_listing_id=item_id,
                    cap_long_rent_at_short=policy['cap_long_rent_at_short'],
                    sync_long_rent_at_short=policy['sync_long_rent_at_short'])
                if not apply:
                    item['status'], item['reason'] = 'preview', 'ready_for_explicit_apply'
                    continue
                fresh = _shelf(client).get(item_id)
                if fresh is None or shelf_values(fresh) != before_values:
                    raise RunnerError('shelf_changed_before_write')
                if activity_fields(client.reprice_init([item_id]), item_id) != expected_activity:
                    raise RunnerError('activity_changed_before_write')
                _assert_snapshot_fresh(snapshot)
                # Journal before the one and only PUT. A crash or an unknown
                # transport response therefore blocks replay in later cycles.
                pending = {'created_at': datetime.now(timezone.utc).isoformat(),
                           'before_row': _private_before_row(row), 'target': _text(target),
                           'cap_long_rent_at_short': policy['cap_long_rent_at_short'],
                           'sync_long_rent_at_short': policy['sync_long_rent_at_short'],
                           'activity': expected_activity}
                journal['pending'][item_id] = pending
                atomic_private_json(journal_path, journal)
                accepted = client.change_short_rent(payload)
                if not isinstance(accepted, dict) or accepted.get('accepted') is not True or accepted.get('readback_required') is not True:
                    raise RunnerError('write_acceptance_unconfirmed')
                verified = False
                for _ in range(3):
                    after = _shelf(client).get(item_id)
                    if after is not None and readback_matches(row, after, target,
                            cap_long_rent_at_short=policy['cap_long_rent_at_short'],
                            sync_long_rent_at_short=policy['sync_long_rent_at_short']):
                        after_activity = activity_fields(client.reprice_init([item_id]), item_id)
                        if after_activity == expected_activity:
                            verified = True
                            break
                if verified:
                    del journal['pending'][item_id]
                    atomic_private_json(journal_path, journal)
                    item['status'], item['reason'] = 'verified', 'short_rent_and_preserved_fields_verified'
                else:
                    item['reason'] = 'readback_unconfirmed_no_replay'
                    result['reason'] = 'pending_write_unconfirmed_no_replay'
                    return result
            except (RunnerError, RepriceContractError) as error:
                item['reason'] = str(error)
                # Once a request may have been sent, stop the cycle. Never send
                # another PUT after an unknown result within the same cycle.
                if item_id in journal['pending']:
                    result['reason'] = 'pending_write_unconfirmed_no_replay'
                    return result
            except UUError as error:
                item['reason'] = error.code
                result['reason'] = 'api_request_failed_no_retry'
                return result
        result['ok'] = all(item['status'] in ('preview', 'unchanged', 'verified', 'skipped') for item in result['items'])
        result['reason'] = 'cycle_complete' if result['ok'] else 'one_or_more_items_blocked'
        return result
    except UUError as error:
        result['reason'] = error.code
    except (RunnerError, RepriceContractError) as error:
        result['reason'] = str(error)
    except Exception:
        result['reason'] = 'unexpected_cycle_error_no_retry'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read or update your own YouPin 0CD rental prices; preview is the default.')
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--loop', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = args.state.with_name('.runner.lock')
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({'ok': False, 'reason': 'runner_already_running'}))
                return 2
            policy = validate_policy(json.loads(args.policy.read_text()))
            args.state.chmod(0o600)
            state = json.loads(args.state.read_text())
            client = UUClient(state, allow_price_write=args.apply)
            journal_path = args.output.with_name('pending-journal.json')
            while not stop.is_set():
                started = time.monotonic()
                result = run_cycle(client, policy, apply=args.apply, journal_path=journal_path)
                atomic_private_json(args.output, result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if not args.loop:
                    return 0 if result['ok'] else 3
                stop.wait(max(0, policy['interval_seconds'] - (time.monotonic() - started)))
            return 0
    except Exception:
        print(json.dumps({'ok': False, 'reason': 'local_runner_setup_failed'}), flush=True)
        return 4


if __name__ == '__main__':
    raise SystemExit(main())
