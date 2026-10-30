# SPDX-License-Identifier: GPL-3.0-only
# Adapted with reference to CS2TradeMonitor contributors, revision
# 41d52fdb7997e023e6c431a7173baa9d8b00806e.
# Modified for YouPin Rent Repricer on 2026-10-30.
# See THIRD_PARTY_NOTICES.txt for provenance and combined-license terms.
"""Pure construction of the reviewed YouPin lease repricing PUT body.

No network, credential loading, persistence, scheduling or writes occur here.
The caller must revalidate ownership/current shelf and obtain user authorization.
Source attribution is recorded in THIRD_PARTY_NOTICES.txt.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any


class RepriceContractError(ValueError):
    """A fixed safe reason; never includes a raw row or credential."""


_MISSING = object()


def _pick(obj: Any, *keys: str, default: Any = _MISSING) -> Any:
    if not isinstance(obj, dict):
        raise RepriceContractError('invalid_object')
    for key in keys:
        if key in obj:
            return obj[key]
    if default is not _MISSING:
        return default
    raise RepriceContractError('required_field_missing')


def _decimal(value: Any, *, nullable_zero: bool = False) -> Decimal:
    if value is None and nullable_zero:
        return Decimal(0)
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise RepriceContractError('invalid_decimal')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RepriceContractError('invalid_decimal') from None
    if not number.is_finite() or number < 0:
        raise RepriceContractError('invalid_decimal')
    return number


def _int(value: Any) -> int:
    if isinstance(value, bool):
        raise RepriceContractError('invalid_integer')
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r'[0-9]+', value):
        return int(value)
    raise RepriceContractError('invalid_integer')


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise RepriceContractError('invalid_boolean')


def _text(value: Decimal) -> str:
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    number = float(value)
    if not math.isfinite(number) or Decimal(str(number)) != value:
        raise RepriceContractError('numeric_precision_loss')
    return number


def listing_id(row: dict[str, Any]) -> str:
    number = _int(_pick(row, 'id', 'Id', 'commodityId', 'CommodityId'))
    if not 0 < number <= 9223372036854775807:
        raise RepriceContractError('invalid_listing_id')
    return str(number)


def activity_fields(init_info: dict[str, Any], item_id: str) -> dict[str, Any]:
    """Parse the profile into its exact outgoing preservation fields."""
    data = init_info
    if 'Data' in init_info or 'data' in init_info:
        data = _pick(init_info, 'Data', 'data')
    compensation = _pick(_pick(data, 'normalLeaseCompensationMap'), item_id)
    compensation_type = _int(_pick(compensation, 'compensationTypeCode'))
    if compensation_type < 0:
        raise RepriceContractError('invalid_compensation_type')
    config = _pick(_pick(data, 'zeroCDRentConfigMap'), item_id)
    enabled = _int(_pick(config, 'zeroCDRentSwitch'))
    if enabled not in (0, 1):
        raise RepriceContractError('invalid_zero_cd_switch')
    fields: dict[str, Any] = {'CompensationType': compensation_type, 'SupportZeroCD': enabled}
    incentive_map = _pick(data, 'subletIncentiveConfigMap', default=None)
    if incentive_map is not None:
        incentive = _pick(incentive_map, item_id, default=None)
        if incentive is not None:
            switch = _pick(incentive, 'subletIncentiveSwitch', default=None)
            if switch is not None:
                switch = _int(switch)
                if switch not in (0, 1):
                    raise RepriceContractError('invalid_sublet_incentive_switch')
                fields['supportSubletIncentive'] = switch
    if enabled == 0:
        return fields
    if 'needReNew' in config and config['needReNew'] is not False:
        raise RepriceContractError('renewal_required_or_unknown')
    pricing_type = _int(_pick(config, 'pricingType'))
    zero_cd: dict[str, Any] = {'PricingType': pricing_type}
    if pricing_type == 0:
        coefficient = _decimal(_pick(config, 'marketDynamicPricingMinCoefficient'))
        if coefficient <= 0:
            raise RepriceContractError('invalid_dynamic_coefficient')
        zero_cd['MinCoefficient'] = _text(coefficient)
    elif pricing_type == 1:
        short_rent = _decimal(_pick(config, 'shortRentPrice'))
        if short_rent <= 0:
            raise RepriceContractError('invalid_fixed_short_rent')
        zero_cd['ShortRentPrice'] = _text(short_rent)
        long_rent = _pick(config, 'longRentPrice', default=None)
        if long_rent is not None:
            zero_cd['LongRentPrice'] = _text(_decimal(long_rent))
    else:
        raise RepriceContractError('unsupported_pricing_type')
    fields['ZeroCDConfig'] = zero_cd
    return fields


def shelf_values(row: dict[str, Any]) -> dict[str, Any]:
    """Canonical values for before/after equality, independent of JSON casing."""
    short_rent = _decimal(_pick(row, 'shortLeaseAmount', 'ShortLeaseAmount', 'leaseUnitPrice', 'LeaseUnitPrice'))
    long_rent = _decimal(_pick(row, 'longLeaseAmount', 'LongLeaseAmount', 'longLeaseUnitPrice', 'LongLeaseUnitPrice'), nullable_zero=True)
    deposit = _decimal(_pick(row, 'depositAmount', 'DepositAmount', 'LeaseDeposit'))
    max_days = _int(_pick(row, 'leaseMaxDays', 'LeaseMaxDays'))
    if short_rent < Decimal('0.01') or max_days <= 0:
        raise RepriceContractError('invalid_shelf_rental_values')
    return {
        'CommodityId': int(listing_id(row)), 'LeaseUnitPrice': short_rent,
        'LongLeaseUnitPrice': long_rent, 'LeaseDeposit': deposit,
        'LeaseMaxDays': max_days,
        'IsCanLease': _bool(_pick(row, 'commodityCanLease', 'CommodityCanLease', 'canLease', 'CanLease', 'IsCanLease')),
        'IsCanSold': _bool(_pick(row, 'commodityCanSell', 'CommodityCanSell', 'IsCanSold', 'isCanSold')),
        'Price': _decimal(_pick(row, 'sellAmount', 'SellAmount', 'sellPrice', 'SellPrice', 'Price'), nullable_zero=True),
    }


def build_short_rent_payload(
    shelf_row: dict[str, Any], init_info: dict[str, Any],
    target_short_rent: Any, *, device_session: str,
    expected_listing_id: str | None = None,
    cap_long_rent_at_short: bool = False,
    sync_long_rent_at_short: bool = False,
) -> dict[str, Any]:
    """Return a JSON-serializable single-item PUT body.

    The explicit platform minimum is 0.01; no extra price floor is introduced.
    The caller decides the market minimum-minus-0.01 target and owns freshness,
    ownership and comparison checks. All non-target mandatory values must exist.
    Long rent is preserved by default. Only an explicitly enabled
    cap_long_rent_at_short lowers an existing higher long rent to the target.
    sync_long_rent_at_short instead always sets long rent to the new short rent,
    including when the old long rent is lower or absent. Modes are exclusive.
    """
    if not isinstance(cap_long_rent_at_short, bool):
        raise RepriceContractError('invalid_long_rent_cap_flag')
    if not isinstance(sync_long_rent_at_short, bool):
        raise RepriceContractError('invalid_long_rent_sync_flag')
    if cap_long_rent_at_short and sync_long_rent_at_short:
        raise RepriceContractError('conflicting_long_rent_modes')
    item_id = listing_id(shelf_row)
    if expected_listing_id is not None and item_id != str(expected_listing_id):
        raise RepriceContractError('listing_id_mismatch')
    if not isinstance(device_session, str) or not re.fullmatch(r'[A-Za-z0-9]{24}', device_session):
        raise RepriceContractError('invalid_device_session')
    target = _decimal(target_short_rent)
    if target < Decimal('0.01') or target.as_tuple().exponent < -2:
        raise RepriceContractError('invalid_target_short_rent')
    current = shelf_values(shelf_row)
    deposit = current['LeaseDeposit']
    # The source formats deposit to two decimals. Refuse a more precise input
    # rather than silently altering an unrelated field through rounding.
    if deposit != deposit.quantize(Decimal('0.01')):
        raise RepriceContractError('deposit_precision_would_change')
    commodity: dict[str, Any] = {
        'CommodityId': current['CommodityId'], 'IsCanLease': current['IsCanLease'],
        'IsCanSold': current['IsCanSold'], 'LeaseDeposit': _text(deposit),
        'LeaseMaxDays': current['LeaseMaxDays'], 'LeaseUnitPrice': _number(target),
        'Price': _number(current['Price']),
    }
    long_rent = current['LongLeaseUnitPrice']
    if sync_long_rent_at_short:
        long_rent = target
    elif cap_long_rent_at_short and long_rent > target:
        long_rent = target
    if long_rent > 0:
        commodity['LongLeaseUnitPrice'] = _number(long_rent)
    commodity.update(activity_fields(init_info, item_id))
    return {'Commoditys': [commodity], 'Sessionid': device_session}


def readback_matches(
    before_row: dict[str, Any], after_row: dict[str, Any], target_short_rent: Any,
    *, cap_long_rent_at_short: bool = False,
    sync_long_rent_at_short: bool = False,
) -> bool:
    """Verify the targets and all remaining copied shelf fields unchanged."""
    if not isinstance(cap_long_rent_at_short, bool):
        raise RepriceContractError('invalid_long_rent_cap_flag')
    if not isinstance(sync_long_rent_at_short, bool):
        raise RepriceContractError('invalid_long_rent_sync_flag')
    if cap_long_rent_at_short and sync_long_rent_at_short:
        raise RepriceContractError('conflicting_long_rent_modes')
    expected = shelf_values(before_row)
    expected['LeaseUnitPrice'] = _decimal(target_short_rent)
    if sync_long_rent_at_short:
        expected['LongLeaseUnitPrice'] = expected['LeaseUnitPrice']
    elif cap_long_rent_at_short and expected['LongLeaseUnitPrice'] > expected['LeaseUnitPrice']:
        expected['LongLeaseUnitPrice'] = expected['LeaseUnitPrice']
    actual = shelf_values(after_row)
    # 0CD rental-only rows are not sale listings. The live platform revalued
    # their displayed sellAmount while the submitted Price was the original
    # amount. Verify the sale switch itself, and every configurable rental
    # value, but do not treat that non-sale display valuation as a sale edit.
    if expected['IsCanSold'] is False and actual['IsCanSold'] is False:
        expected.pop('Price')
        actual.pop('Price')
    return actual == expected
