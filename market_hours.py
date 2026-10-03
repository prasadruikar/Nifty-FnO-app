"""
market_hours.py  -  single source of truth for "is the NSE open right now?"
=====================================================================
Both orderflow_scanner.py and nse_scanner.py import this, so weekend / holiday /
after-hours handling lives in ONE place instead of being duplicated (and missed
in one of them, which is exactly why the RANK scanner kept fetching on a holiday).

EDIT THE HOLIDAY LIST BELOW once a year from the official NSE calendar. Weekends
are handled automatically and never need listing. Lunar-festival dates shift year
to year, so the entries marked (verify) should be checked against NSE's published
list - a wrong date here would wrongly pause trading on a real session, or scan on
a closed one.
"""

import datetime

# Regular-session timings (NSE equity/F&O)
MARKET_START = (9, 15)
MARKET_END   = (15, 30)

# NSE TRADING HOLIDAYS - {'YYYY-MM-DD': 'Name'}. Verify against the official NSE
# list each year. (2026 best-effort below; the fixed-date ones are reliable,
# lunar ones marked 'verify' - correct them from the NSE holiday calendar.)
NSE_HOLIDAYS = {
    "2026-01-26": "Republic Day",
    "2026-02-15": "Maha Shivaratri",            # verify
    "2026-03-03": "Holi",                       # verify
    "2026-03-21": "Id-ul-Fitr (Ramzan)",        # verify
    "2026-04-03": "Good Friday",                # verify
    "2026-04-14": "Dr. Ambedkar Jayanti",       # verify
    "2026-05-01": "Maharashtra Day",
    "2026-05-27": "Bakri Id (Id-ul-Adha)",      # verify
    "2026-06-16": "Muharram",                   # verify
    "2026-08-15": "Independence Day",
    "2026-10-02": "Gandhi Jayanti",
    "2026-10-20": "Dussehra",                   # verify
    "2026-11-09": "Diwali (Laxmi Pujan)",       # verify
    "2026-11-10": "Diwali Balipratipada",       # verify
    "2026-11-24": "Guru Nanak Jayanti",         # verify
    "2026-12-25": "Christmas",
}


def is_holiday(d=None):
    """True if the given date (default today) is an NSE trading holiday."""
    d = d or datetime.date.today()
    return d.isoformat() in NSE_HOLIDAYS


def holiday_name(d=None):
    """The holiday's name for that date, or '' if it isn't a holiday."""
    d = d or datetime.date.today()
    return NSE_HOLIDAYS.get(d.isoformat(), "")


def is_trading_day(d=None):
    """True only on a weekday that is NOT a holiday."""
    d = d or datetime.date.today()
    return d.weekday() < 5 and not is_holiday(d)


def mkt_open(now=None, start=MARKET_START, end=MARKET_END):
    """True only during live market: a trading day AND within session hours.
    `start`/`end` let a caller use a slightly narrower window if it wants."""
    now = now or datetime.datetime.now()
    if not is_trading_day(now.date()):
        return False
    return start <= (now.hour, now.minute) < end


def market_status(now=None, start=MARKET_START, end=MARKET_END):
    """(open_bool, reason) - reason in {'weekend','holiday','pre-market',
    'after-hours','open'} for a clear banner/log message."""
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:
        return False, "weekend"
    if is_holiday(now.date()):
        return False, "holiday"
    hm = (now.hour, now.minute)
    if hm < start:
        return False, "pre-market"
    if hm >= end:
        return False, "after-hours"
    return True, "open"


def reason_text(now=None, start=MARKET_START, end=MARKET_END):
    """A full human-readable sentence for WHY the market is closed (or that it's
    open), e.g. 'NSE holiday — Gandhi Jayanti', 'Weekend', 'Pre-market — opens at
    09:15', 'After-hours — closed at 15:30'. For the UI banner / logs."""
    now = now or datetime.datetime.now()
    sh, sm = start
    eh, em = end
    wd = now.weekday()
    if wd >= 5:
        day = "Saturday" if wd == 5 else "Sunday"
        return f"Weekend ({day}) — market shut"
    nm = holiday_name(now.date())
    if nm:
        return f"NSE holiday — {nm}"
    if is_holiday(now.date()):
        return "NSE holiday"
    hm = (now.hour, now.minute)
    if hm < start:
        return f"Pre-market — opens at {sh:02d}:{sm:02d}"
    if hm >= end:
        return f"After-hours — closed at {eh:02d}:{em:02d}"
    return "Market open"