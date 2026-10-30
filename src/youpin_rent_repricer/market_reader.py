# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, read-only pagination for the platform's visible lease market.

Every page uses the source-reviewed query from UUClient.market_short_leases.
Completeness describes this exact query, not hidden/unavailable platform stock,
nor the comparability of individual rows. Consumers must still exclude their
own listings, validate conditions and require fresh quotes before writing.

No credentials or peer listing data are logged. Returned raw rows are private
data and belong only in the caller's private snapshots.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .uu_client import UUError, _MARKET, _bounded_int, _id, _pick


def read_market_snapshot(client: Any, template_id: Any, max_pages: int = 10) -> dict[str, Any]:
    """Read up to ``max_pages`` of 50 leases without relying on sort order.

    Only an explicit Boolean ``hasNext: false`` proves the query was exhausted.
    Missing/non-Boolean pagination, unverifiable IDs, duplicate rows and a page
    limit leave ``coverage_complete`` false. They never produce an executable
    pricing claim. Network/authentication/risk errors propagate unchanged and
    are never retried through another header set, host or endpoint.
    """
    template = _id(template_id)
    _bounded_int(max_pages, 1, 20)
    if not client.state.get('user_id'):
        client.userinfo()

    started_at = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        'rows': [],
        'coverage_complete': False,
        'reason': 'page_limit_reached',
        'pages_read': 0,
        'page_size': 50,
        'requested_template_id': template,
        'market_scope': 'visible_lease_query',
        'query_started_at': started_at,
        'observed_at': None,
    }
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        data = client._json_call('POST', _MARKET, {
            'hasLease': 'true', 'haveBuZhangType': 0, 'listSortType': '2',
            'listType': 30, 'mergeFlag': 0, 'pageIndex': page, 'pageSize': 50,
            'sortType': '1', 'sortTypeKey': 'LEASE_DEFAULT', 'status': '20',
            'stickerAbrade': 0, 'stickersIsSort': False, 'templateId': template,
            'ultraLongLeaseMoreZones': 0, 'userId': client.state['user_id'],
            'Sessionid': client.state['device_token'],
        })
        result['pages_read'] = page
        result['observed_at'] = datetime.now(timezone.utc).isoformat()
        if not isinstance(data, dict):
            result['reason'] = 'invalid_page_shape'
            return result
        rows = _pick(data, 'CommodityList', 'commodityList', 'commodityInfoList')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            result['reason'] = 'invalid_rows_shape'
            return result
        # Keep the actual response rows. A duplicate is a coverage failure,
        # not something silently removed to manufacture an exhausted market.
        result['rows'].extend(rows)
        if len(rows) > 50:
            result['reason'] = 'page_exceeds_requested_size'
            return result
        for row in rows:
            try:
                listing_id = _id(_pick(row, 'Id', 'id', 'CommodityId', 'commodityId'))
            except UUError:
                result['reason'] = 'listing_id_missing_or_invalid'
                return result
            if listing_id in seen:
                result['reason'] = 'duplicate_listing_id'
                return result
            seen.add(listing_id)
        has_next = data.get('hasNext')
        if type(has_next) is not bool:
            result['reason'] = 'has_next_missing_or_invalid'
            return result
        if has_next is False:
            result['coverage_complete'] = True
            result['reason'] = 'query_exhausted'
            return result
        if not rows:
            result['reason'] = 'empty_page_with_has_next'
            return result
    return result
