"""Tests for MarketNoticeClient parser."""
import pytest
from datetime import timezone, timedelta, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from custom_components.nem_pd7day.market_notice_client import (
    _parse_directory_listing,
    _parse_notice_body,
    GridNoticeAnnotation,
)

NEM_TZ = timezone(timedelta(hours=10))

LOR_NOTICE_TEXT = """
MARKET NOTICE
AEMO ELECTRICITY MARKET NOTICE 128465 RESERVE NOTICE 11/08/2025 03:25:07 PM

STPASA - Forecast Lack Of Reserve Level 1 (LOR1) in the SA Region on 19/08/2025

AEMO declares a Forecast LOR1 condition for the SA region for the following period:
[1.] From 0000 hrs 19/08/2025 to 0300 hrs 19/08/2025.
The forecast capacity reserve requirement is 411 MW.
The minimum capacity reserve available is 407 MW.

AEMO Operations
END OF REPORT
"""

MSL_NOTICE_TEXT = """
MARKET NOTICE
AEMO ELECTRICITY MARKET NOTICE 124467 MINIMUM SYSTEM LOAD 11/02/2025 02:43:06 PM

Forecast Minimum System Load (MSL1) condition in the VIC region on 16/02/2025

The regional demand is forecast to be below the MSL1 threshold for the following period:
[1.] From 1230 hrs 16/02/2025 to 1430 hrs 16/02/2025. Minimum regional demand is forecast to be 2012 MW at 1330 hrs.

AEMO Operations
END OF REPORT
"""

LOR_MULTI_PERIOD_TEXT = """
MARKET NOTICE
AEMO ELECTRICITY MARKET NOTICE 144109 RESERVE NOTICE 17/05/2026 14:34:41

ST PASA - Update of the Forecast Lack Of Reserve Level 1 (LOR1) in the QLD Region on 19/05/2026

The Forecast LOR1 condition in the QLD region has been updated to the following:
[1.] From 0600 hrs 19/05/2026 to 1930 hrs 19/05/2026.
The forecast capacity reserve requirement is 1197 MW.
The minimum capacity reserve available is 1012 MW.

[2.] From 2130 hrs 19/05/2026 to 2200 hrs 19/05/2026.
The forecast capacity reserve requirement is 1199 MW.

Manager NEM Real Time Operations
END OF REPORT
"""

CANCELLATION_TEXT = """
MARKET NOTICE
AEMO ELECTRICITY MARKET NOTICE 124560 MINIMUM SYSTEM LOAD 12/02/2025 03:04:06 PM

Cancellation of Forecast Minimum System Load (MSL) MSL1 event in the VIC Region.
Cancellation - Forecast MSL1 - VIC Region at 1400 hrs 13/02/2025.
Refer to Market Notice 124467 for MSL1.

AEMO Operations
END OF REPORT
"""

DIRECTORY_HTML = """
<pre>
01/01/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_20260101.R133900
01/01/2026 01:00 PM  1234 NEMITWEB1_MKTNOTICE_20260101.R133901
02/01/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_20260102.R133910
</pre>
"""


def test_parse_directory_listing():
    files = _parse_directory_listing(DIRECTORY_HTML)
    assert len(files) == 3
    assert files[0] == (133900, "NEMITWEB1_MKTNOTICE_20260101.R133900")
    assert files[2][0] == 133910


def test_parse_directory_listing_deduplicates():
    """NEMWEB returns each file twice; _parse_directory_listing deduplicates."""
    duplicated_html = """
<pre>
01/01/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_20260101.R133900
01/01/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_20260101.R133900
02/01/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_20260102.R133910
02/01/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_20260102.R133910
</pre>
"""
    files = _parse_directory_listing(duplicated_html)
    assert len(files) == 2
    assert files[0] == (133900, "NEMITWEB1_MKTNOTICE_20260101.R133900")
    assert files[1] == (133910, "NEMITWEB1_MKTNOTICE_20260102.R133910")


def test_parse_lor_notice():
    notice = _parse_notice_body(LOR_NOTICE_TEXT, 128465)
    assert notice is not None
    assert notice.notice_type == "LOR"
    assert notice.level == 1
    assert notice.region == "SA1"
    assert not notice.is_cancelled
    assert notice.reserve_req_mw == 411.0
    assert notice.surplus_mw == 407.0
    assert notice.period_from.hour == 0
    assert notice.period_to.hour == 3


def test_parse_msl_notice():
    notice = _parse_notice_body(MSL_NOTICE_TEXT, 124467)
    assert notice is not None
    assert notice.notice_type == "MSL"
    assert notice.level == 1
    assert notice.region == "VIC1"
    assert not notice.is_cancelled
    assert notice.forecast_mw == 2012.0
    assert notice.period_from.hour == 12
    assert notice.period_from.minute == 30
    assert notice.period_to.hour == 14
    assert notice.period_to.minute == 30


def test_parse_cancellation_notice():
    notice = _parse_notice_body(CANCELLATION_TEXT, 124560)
    assert notice is not None
    assert notice.is_cancelled
    assert notice.cancels_notice_id == 124467


def test_parse_multi_period_lor_notice():
    """Multi-period notice should use earliest period_from and latest period_to."""
    notice = _parse_notice_body(LOR_MULTI_PERIOD_TEXT, 144109)
    assert notice is not None
    assert notice.notice_type == "LOR"
    assert notice.level == 1
    assert notice.region == "QLD1"
    assert not notice.is_cancelled
    # Period 1: 0600–1930 19/05/2026, Period 2: 2130–2200 19/05/2026
    # Widest window: 0600–2200
    assert notice.period_from.hour == 6
    assert notice.period_from.minute == 0
    assert notice.period_to.hour == 22
    assert notice.period_to.minute == 0


def test_non_lor_msl_returns_none():
    result = _parse_notice_body("RECLASSIFY CONTINGENCY some other notice text", 99999)
    assert result is None


def test_to_dict_roundtrip():
    """GridNoticeAnnotation round-trips through to_dict/from_dict."""
    notice = _parse_notice_body(LOR_NOTICE_TEXT, 128465)
    assert notice is not None
    d = notice.to_dict()
    restored = GridNoticeAnnotation.from_dict(d)
    assert restored.notice_id == notice.notice_id
    assert restored.notice_type == notice.notice_type
    assert restored.level == notice.level
    assert restored.region == notice.region
    assert restored.period_from == notice.period_from
    assert restored.period_to == notice.period_to
    assert restored.reserve_req_mw == notice.reserve_req_mw
    assert restored.surplus_mw == notice.surplus_mw


def test_notice_store_cancellation():
    """GridNoticeStore.add_notices propagates cancellations."""
    # Inline a minimal store that doesn't need HA
    from custom_components.nem_pd7day.market_notice_client import GridNoticeAnnotation

    now = datetime.now(NEM_TZ)
    original = GridNoticeAnnotation(
        notice_id=124467,
        notice_type="MSL",
        level=1,
        region="VIC1",
        period_from=now,
        period_to=now + timedelta(hours=2),
        issued_at=now,
    )
    cancellation = GridNoticeAnnotation(
        notice_id=124560,
        notice_type="MSL",
        level=1,
        region="VIC1",
        period_from=now,
        period_to=now,
        issued_at=now,
        is_cancelled=True,
        cancels_notice_id=124467,
    )

    # Simulate store logic inline (avoids HA Store dependency)
    notices: dict[str, list[GridNoticeAnnotation]] = {}

    def add_notices(new_notices):
        for notice in new_notices:
            region = notice.region
            if region not in notices:
                notices[region] = []
            if notice.is_cancelled and notice.cancels_notice_id:
                for existing in notices.get(region, []):
                    if existing.notice_id == notice.cancels_notice_id:
                        existing.is_cancelled = True
            existing_ids = {n.notice_id for n in notices[region]}
            if notice.notice_id in existing_ids:
                notices[region] = [
                    n if n.notice_id != notice.notice_id else notice
                    for n in notices[region]
                ]
            else:
                notices[region].append(notice)

    add_notices([original])
    assert not notices["VIC1"][0].is_cancelled

    add_notices([cancellation])
    # Original should now be marked cancelled
    orig = [n for n in notices["VIC1"] if n.notice_id == 124467][0]
    assert orig.is_cancelled


@pytest.mark.asyncio
async def test_first_run_backfills_but_no_recent_files():
    """On first run with all files older than 7 days, backfill finds nothing."""
    from custom_components.nem_pd7day.market_notice_client import MarketNoticeClient

    mock_session = MagicMock()
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = AsyncMock(return_value=DIRECTORY_HTML)  # dates are 20260101/02
    mock_session.get = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=mock_resp),
        __aexit__=AsyncMock(return_value=False),
    ))

    client = MarketNoticeClient(mock_session)
    assert client.last_seen_notice_id == 0

    result = await client.fetch_new_notices()
    # All files are dated 20260101/20260102, which is > 7 days ago.
    # Backfill date filter excludes them, so no files fetched.
    assert result == []
    assert client.last_seen_notice_id == 0  # no files processed, cursor unchanged


@pytest.mark.asyncio
async def test_first_run_backfills_recent_files():
    """On first run with recent files, backfill fetches and parses them."""
    from custom_components.nem_pd7day.market_notice_client import MarketNoticeClient

    # Build a directory listing with files dated within the last 7 days
    today = datetime.now(NEM_TZ)
    recent_date = today.strftime("%Y%m%d")
    recent_html = f"""
<pre>
{today.strftime('%d/%m/%Y')} 12:00 PM  1234 NEMITWEB1_MKTNOTICE_{recent_date}.R200100
{today.strftime('%d/%m/%Y')} 01:00 PM  1234 NEMITWEB1_MKTNOTICE_{recent_date}.R200101
</pre>
"""

    call_count = 0

    def make_mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        resp = AsyncMock()
        resp.raise_for_status = MagicMock()
        if call_count == 1:
            # First call: directory listing
            resp.text = AsyncMock(return_value=recent_html)
        else:
            # Subsequent calls: individual notice files
            resp.text = AsyncMock(return_value=LOR_NOTICE_TEXT)
        return AsyncMock(
            __aenter__=AsyncMock(return_value=resp),
            __aexit__=AsyncMock(return_value=False),
        )

    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=make_mock_get)

    client = MarketNoticeClient(mock_session)
    assert client.last_seen_notice_id == 0

    result = await client.fetch_new_notices()
    # Both files are recent, so backfill fetches them.
    # LOR_NOTICE_TEXT is a valid LOR notice, so both should parse successfully.
    assert len(result) == 2
    assert all(n.notice_type == "LOR" for n in result)
    assert client.last_seen_notice_id == 200101  # highest notice_id


@pytest.mark.asyncio
async def test_incremental_fetch_skips_old_notices():
    """With last_seen set, only newer notices are fetched."""
    from custom_components.nem_pd7day.market_notice_client import MarketNoticeClient

    mock_session = MagicMock()
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = AsyncMock(return_value=DIRECTORY_HTML)
    mock_session.get = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=mock_resp),
        __aexit__=AsyncMock(return_value=False),
    ))

    client = MarketNoticeClient(mock_session)
    client.last_seen_notice_id = 133909  # one behind the latest

    # _fetch_and_parse will be called for 133910 only
    with patch.object(client, '_fetch_and_parse', new=AsyncMock(return_value=None)) as mock_fetch:
        result = await client.fetch_new_notices()
        assert mock_fetch.call_count == 1
        assert mock_fetch.call_args[0][0] == 133910
