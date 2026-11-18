"""
The instants the reminder tests reckon from.

Every test names its own `now` and hands it to the service, so nothing
depends on the wall clock: a suite run at 08:59 in Denver and one at 23:59
must agree.
"""

import datetime as dt
from zoneinfo import ZoneInfo

DENVER = ZoneInfo("America/Denver")
AUCKLAND = ZoneInfo("Pacific/Auckland")

#: 18:00 on Wednesday 18 November 2026 in Denver (MST, UTC-7): past the
#: reminder hour, so Thursday the 19th's visits are due.
NOW = dt.datetime(2026, 11, 18, 18, 0, tzinfo=DENVER)

#: 14:00 on Thursday the 19th, Denver: tomorrow, as NOW reckons it.
TOMORROW_2PM = dt.datetime(2026, 11, 19, 14, 0, tzinfo=DENVER)


def local(zone: ZoneInfo, *args) -> dt.datetime:
    return dt.datetime(*args, tzinfo=zone)
