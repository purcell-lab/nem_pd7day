"""Real NEMWEB market notices kept as fixtures for #216.

Each file is unchanged from
https://nemweb.com.au/Reports/Current/Market_Notice/ (CRLF line endings kept).
"""

from __future__ import annotations

from pathlib import Path

NOTICES = Path(__file__).parent / "fixtures" / "market_notices"


def notice_text(notice_id: int) -> str:
    (path,) = NOTICES.glob(f"NEMITWEB1_MKTNOTICE_*.R{notice_id}")
    return path.read_text(encoding="ascii")
