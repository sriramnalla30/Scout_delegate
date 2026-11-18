"""Turns what the scraper returned into the app's own place model, and works out everything that can be derived from it.

Nothing here touches the network, the disk or the clock (the clock is passed in), so every rule can be tested alone.

A search document is stored once and never changes:
    {"args", "elapsed_ms", "places": [place, ...], "summary", "coverage"}
Anything that depends on the time it is looked at (is the place open now?) is added when the document is read: see view().

A place keeps the scraper's own record untouched in place["raw"]. The fields around it are the app's model, so a screen
or a later step (calling, negotiating, a map, charts) reads one shape whatever the scraper gives or later adds.
"""
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
PRIOR_REVIEWS = 30   # a place needs about this many reviews before its own rating counts more than the average of the results
FEW_REVIEWS = 10     # below this a rating is flagged as thin
STD_METRO = ('80', '40', '11', '22', '44', '20', '33', '79')  # area codes of the big cities: a 10 digit number starting with one is a landline
IST = timezone(timedelta(hours=5, minutes=30))


def _text(value) -> str:
    return '' if value is None else str(value).replace(' ', ' ').replace(' ', ' ').strip()


def _num(*values):
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number:  # not NaN
            return number
    return None


def filled(value) -> bool:
    if value in (None, '', [], {}, 0):
        return False
    if isinstance(value, dict):
        return any(filled(v) for v in value.values())
    if isinstance(value, list):
        return any(filled(v) for v in value)
    return True


# ---- phone ----------------------------------------------------------------------------------------------------------

def phone_info(raw) -> dict | None:
    """Kind and +91 form of a Google phone string. The kind is a guess from the number's shape."""
    shown = _text(raw)
    digits = re.sub(r'\D', '', shown)
    if not digits:
        return None
    if digits.startswith(('1800', '1860')):
        return {'display': shown, 'e164': '+91' + digits, 'kind': 'toll-free'}
    national = digits[1:] if digits.startswith('0') else digits
    if national.startswith('91') and len(national) == 12:
        national = national[2:]
    kind = 'mobile' if len(national) == 10 and national[0] in '6789' and not national.startswith(STD_METRO) else 'landline'
    return {'display': shown, 'e164': '+91' + national, 'kind': kind}


# ---- opening hours ----------------------------------------------------------------------------------------------------

_TIME = re.compile(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?')


def _minutes(match, suffix):
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if suffix:
        hour = hour % 12 + (12 if suffix == 'pm' else 0)
    return hour * 60 + minute


def _range(text: str):
    """One opening range such as "5 am-10 pm" or "6-11 am" as (start, end) minutes. end passes 1440 when it runs past midnight.
    None when the text cannot be read."""
    text = _text(text).lower()
    if 'open 24' in text or text in ('24 hours', 'all day'):
        return (0, 1440)
    parts = re.split(r'\s*[–—-]\s*|\s+to\s+', text)
    if len(parts) != 2:
        return None
    first, last = _TIME.fullmatch(parts[0]), _TIME.fullmatch(parts[1])
    if not first or not last:
        return None
    end_suffix = last.group(3)
    start_suffix = first.group(3) or end_suffix
    start, end = _minutes(first, start_suffix), _minutes(last, end_suffix)
    if not first.group(3) and end_suffix and start >= end:  # "11-1 pm": the start borrowed the wrong half of the day
        start = _minutes(first, 'am' if end_suffix == 'pm' else 'pm')
    if end <= start:
        end += 1440
    return (start, end)


def parse_schedule(open_hours) -> list | None:
    """Monday..Sunday, each a list of [start, end] minute ranges ([] when closed). None when the hours are missing,
    incomplete or in a form that cannot be read, so the app never claims open or closed without being sure."""
    if not isinstance(open_hours, dict) or not open_hours:
        return None
    days = {_text(k).lower(): v for k, v in open_hours.items()}
    schedule = []
    for day in DAYS:
        texts = days.get(day.lower())
        if texts is None:
            return None
        ranges = []
        for text in ([texts] if isinstance(texts, str) else texts):
            if _text(text).lower().startswith('closed'):
                continue
            for piece in str(text).split(','):
                parsed = _range(piece)
                if parsed is None:
                    return None
                ranges.append(list(parsed))
        schedule.append(sorted(ranges))
    return schedule


def _clock(minute: int) -> str:
    minute %= 1440
    hour, rest = divmod(minute, 60)
    shown = f'{hour % 12 or 12}' + (f':{rest:02d}' if rest else '')
    return f'{shown} {"am" if hour < 12 else "pm"}'


def _zone(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # no time zone database installed
        return IST if name in ('Asia/Calcutta', 'Asia/Kolkata') else None


def live_status(place: dict, now: datetime) -> dict:
    """Whether the place is open at `now` (a timezone-aware time) and what to tell the person."""
    schedule, zone = place.get('schedule'), _zone(place.get('timezone') or 'Asia/Kolkata')
    if schedule is None or zone is None:
        return {'open': None, 'text': 'Hours not listed', 'today': ''}
    local = now.astimezone(zone)
    weekday, minute = local.weekday(), local.hour * 60 + local.minute
    today = schedule[weekday]
    label = ', '.join(f'{_clock(s)}–{_clock(e)}' for s, e in today) if today else 'Closed'
    if today == [[0, 1440]]:
        label = 'Open 24 hours'
    for _start, e in schedule[(weekday - 1) % 7]:  # a range from yesterday that runs past midnight
        if e > 1440 and minute < e - 1440:
            return {'open': True, 'text': f'Open · closes {_clock(e)}', 'today': label}
    for s, e in today:
        if s <= minute < e:
            if (s, e) == (0, 1440):
                return {'open': True, 'text': 'Open 24 hours', 'today': label}
            return {'open': True, 'text': f'Open · closes {_clock(e)}', 'today': label}
    for offset in range(8):
        day = (weekday + offset) % 7
        starts = [s for s, _ in schedule[day] if offset > 0 or s > minute]
        if starts:
            when = '' if offset == 0 else ' tomorrow' if offset == 1 else f' {DAYS[day][:3]}'
            return {'open': False, 'text': f'Closed · opens{when} {_clock(min(starts))}', 'today': label}
    return {'open': False, 'text': 'Closed', 'today': label}


# ---- one place ----------------------------------------------------------------------------------------------------------

def _distance_m(a_lat, a_lng, b_lat, b_lng):
    if None in (a_lat, a_lng, b_lat, b_lng):
        return None
    rad = math.pi / 180
    h = math.sin((b_lat - a_lat) * rad / 2) ** 2 + math.cos(a_lat * rad) * math.cos(b_lat * rad) * math.sin((b_lng - a_lng) * rad / 2) ** 2
    return round(12_742_000 * math.asin(math.sqrt(h)))


def _maps_url(raw, lat, lng) -> str:
    if _text(raw.get('link')):
        return _text(raw['link'])
    data_id = _text(raw.get('data_id'))
    if ':' in data_id:
        try:
            return f'https://www.google.com/maps?cid={int(data_id.split(":")[1], 16)}'
        except ValueError:
            pass
    return f'https://www.google.com/maps/search/?api=1&query={lat},{lng}' if lat is not None and lng is not None else ''


def normalise(raw: dict, rank: int, pin: dict) -> dict:
    """One scraper record -> one place. `rank` is its position in Google's own ordering (1 = first)."""
    lat, lng = _num(raw.get('latitude')), _num(raw.get('longitude'), raw.get('longtitude'))  # the scraper spells longtitude both ways
    categories = [_text(c) for c in (raw.get('categories') or [raw.get('category')]) if _text(c)]
    hours = raw.get('open_hours') if isinstance(raw.get('open_hours'), dict) else {}
    by_day = {_text(k).lower(): ([v] if isinstance(v, str) else v or []) for k, v in hours.items()}
    rating = _num(raw.get('review_rating')) or None
    return {
        'id': _text(raw.get('data_id')) or f'rank-{rank}',
        'rank': rank,
        'name': _text(raw.get('title')) or 'Unnamed place',
        'categories': categories,
        'address': _text(raw.get('address')),
        'lat': lat, 'lng': lng,
        'distance_m': _distance_m(pin.get('lat'), pin.get('lng'), lat, lng),
        'rating': rating,
        'reviews': int(_num(raw.get('review_count')) or 0),
        'phone': phone_info(raw.get('phone')),
        'website': _text(raw.get('web_site')),
        'hours': {day: ', '.join(_text(t) for t in by_day[day.lower()]) for day in DAYS if day.lower() in by_day},
        'schedule': parse_schedule(hours),
        'timezone': _text(raw.get('timezone')),
        'plus_code': _text(raw.get('plus_code')),
        'maps_url': _maps_url(raw, lat, lng),
        'directions_url': f'https://www.google.com/maps/dir/?api=1&destination={lat},{lng}' if lat is not None and lng is not None else '',
        # optional extras: empty in fast mode, filled by the richer scraper modes, shown whenever present
        'business_status': _text(raw.get('status')),
        'price_range': _text(raw.get('price_range')),
        'description': _text(raw.get('description')),
        'thumbnail': _text(raw.get('thumbnail')),
        'raw': raw,
    }


def _add_scores(places: list[dict]) -> None:
    """Phones used by several listings, and a score that weighs the rating by how many people gave it."""
    shared = Counter(re.sub(r'\D', '', p['phone']['display']) for p in places if p['phone'])
    rated = [p['rating'] for p in places if p['rating']]
    mean = sum(rated) / len(rated) if rated else 0.0
    for p in places:
        if p['phone']:
            p['phone']['shared'] = shared[re.sub(r'\D', '', p['phone']['display'])]
        # a Bayesian average: a 5.0 from 3 people stays near the group's mean, a 4.6 from 900 people stays near 4.6
        p['score'] = round((p['reviews'] * p['rating'] + PRIOR_REVIEWS * mean) / (p['reviews'] + PRIOR_REVIEWS), 2) if p['rating'] else None
        p['flags'] = [flag for flag, on in (
            ('few_reviews', bool(p['rating']) and p['reviews'] < FEW_REVIEWS), ('no_rating', not p['rating']),
            ('shared_phone', bool(p['phone']) and p['phone']['shared'] > 1), ('no_website', not p['website']),
            ('no_phone', not p['phone']), ('no_hours', p['schedule'] is None)) if on]


# ---- one search ---------------------------------------------------------------------------------------------------------

def coverage(raws: list[dict]) -> dict:
    """For every field the scraper returned: how many places have a value. Shows what a search mode does and does not give."""
    keys = list(dict.fromkeys(k for r in raws for k in r))
    return {k: sum(1 for r in raws if filled(r.get(k))) for k in keys}


def summarise(places: list[dict]) -> dict:
    rated = [p for p in places if p['rating']]
    ratings = [p['rating'] for p in rated]
    near = [p for p in places if p['distance_m'] is not None]
    pick = lambda items, key: max(items, key=key)['id'] if items else None  # noqa: E731
    kinds = Counter(p['phone']['kind'] for p in places if p['phone'])
    return {
        'places': len(places),
        'avg_rating': round(sum(ratings) / len(ratings), 2) if ratings else None,
        'total_reviews': sum(p['reviews'] for p in places),
        'with_phone': sum(1 for p in places if p['phone']),
        'with_website': sum(1 for p in places if p['website']),
        'with_hours': sum(1 for p in places if p['schedule'] is not None),
        'phone_kinds': dict(kinds),
        'shared_phone_places': sum(1 for p in places if p['phone'] and p['phone']['shared'] > 1),
        'categories': [{'name': name, 'count': n} for name, n in Counter(c for p in places for c in p['categories']).most_common(8)],
        'rating_buckets': {label: sum(1 for r in ratings if lo <= r < hi) for label, lo, hi in
                           (('under 3.5', 0, 3.5), ('3.5 to 4', 3.5, 4), ('4 to 4.5', 4, 4.5), ('4.5 and up', 4.5, 5.01))},
        'max_distance_m': max((p['distance_m'] for p in near), default=None),
        'picks': {  # place ids, so a screen can point at them
            'best': pick(rated, lambda p: (p['score'], p['reviews'])),
            'most_reviewed': pick(rated, lambda p: p['reviews']),
            'closest': pick(near, lambda p: -p['distance_m']),
        },
    }


def build(raws: list[dict], args: dict, elapsed_ms: int) -> dict:
    """The stored document of one search."""
    pin, seen, places = {'lat': args['lat'], 'lng': args['lng']}, set(), []
    for raw in raws:
        place = normalise(raw, len(places) + 1, pin)
        if place['id'] in seen:  # the scraper can list a place twice
            continue
        seen.add(place['id'])
        places.append(place)
    _add_scores(places)
    return {'args': args, 'elapsed_ms': elapsed_ms, 'places': places, 'summary': summarise(places), 'coverage': coverage(raws)}


def view(doc: dict, now: datetime | None = None) -> dict:
    """The document as a screen should see it right now: every place gets its live open/closed status."""
    now = now or datetime.now(timezone.utc)
    places = [{**p, 'live': live_status(p, now)} for p in doc['places']]
    summary = {**doc['summary'], 'open_now': sum(1 for p in places if p['live']['open'])}
    return {**doc, 'places': places, 'summary': summary, 'viewed_at': now.timestamp()}


def highlights(doc: dict) -> dict:
    """The few facts about a search that a language model needs to talk about it. The full data stays out of its context."""
    by_id = {p['id']: p for p in doc['places']}
    best = by_id.get(doc['summary']['picks']['best'])
    return {'avg_rating': doc['summary']['avg_rating'],
            'best': {'name': best['name'], 'rating': best['rating'], 'reviews': best['reviews']} if best else None}
