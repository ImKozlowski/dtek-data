import asyncio
import json
import os
from datetime import datetime, timezone

from curl_cffi.requests import AsyncSession
from dtek_client import DtekClient
from dtek_client.browser_auth import get_cleared_cookies

# ================= НАЛАШТУВАННЯ =================
SCHEDULE_URL = "https://www.dtek-krem.com.ua/ua/shutdowns"
AJAX_URL = "https://www.dtek-krem.com.ua/ua/ajax"
REGION = "krem"
CITY = os.environ.get("DTEK_CITY", "м. Ірпінь")
STREET = os.environ.get("DTEK_STREET", "вул. Незалежності")
OUTPUT_FILE = "schedule.json"
ATTEMPT_TIMEOUT = 150  # секунд на одну спробу: не даємо браузеру «зависнути» на 10 хвилин
MAX_DAY_GAP = 90000  # 25 год: запас на перехід на зимовий час
# ================================================


def slot_to_str(status) -> str:
    if status is None:
        return "unknown"
    if hasattr(status, "value"):
        return status.value
    return str(status)


def load_previous() -> dict:
    try:
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def previous_yesterday(prev_group: dict, today_ts: str):
    """Вчорашній факт із попереднього файлу (після півночі він там ще був «сьогоднішнім»)."""
    ts, hours = prev_group.get("fact_timestamp"), prev_group.get("fact")
    if ts and hours and 0 < int(today_ts) - int(ts) <= MAX_DAY_GAP:
        return hours, ts
    ts, hours = prev_group.get("fact_yesterday_timestamp"), prev_group.get("fact_yesterday")
    if ts and hours and 0 < int(today_ts) - int(ts) <= MAX_DAY_GAP:
        return hours, ts
    return None, None


def carry_over(data: dict, previous: dict):
    """Якщо сайт цього разу віддав неповні дані, зберігаємо раніше отриманий факт (дати захищають від плутанини)."""
    prev_groups = previous.get("groups", {})
    for gid, g in data["groups"].items():
        for kind in ("fact", "fact_tomorrow"):  # порожній факт = відсутній
            if not g.get(kind):
                g.pop(kind, None)
                g.pop(kind + "_timestamp", None)
        have = {g.get(k + "_timestamp") for k in ("fact", "fact_tomorrow", "fact_yesterday") if k in g}
        pg = prev_groups.get(gid, {})
        for kind in ("fact", "fact_tomorrow"):
            ts = pg.get(kind + "_timestamp")
            if kind not in g and pg.get(kind) and ts and ts not in have:
                g[kind] = pg[kind]
                g[kind + "_timestamp"] = ts
                have.add(ts)
    if not data.get("fact_update") and previous.get("fact_update"):
        data["fact_update"] = previous["fact_update"]


def convert_schedule(result, previous: dict) -> dict:
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "groups": {},
    }

    # 1. Пресет (тижневий графік для всіх груп)
    if result.preset and hasattr(result.preset, "groups"):
        for group_id, group_week in result.preset.groups.items():
            days_out = {}
            for day_num, day_schedule in group_week.days.items():
                days_out[str(day_num)] = {
                    str(h): slot_to_str(s) for h, s in day_schedule.slots.items()
                }
            out["groups"][group_id] = {"preset": days_out}

    # 2. Факт: сьогодні, завтра, (якщо є на сайті) вчора
    if result.fact and hasattr(result.fact, "days"):
        today_ts = str(result.fact.today_ts)
        today_i = int(today_ts)

        # «Завтра» = найближчий наступний ts. Не today+86400: двічі на рік доба має 23/25 год.
        future = sorted(int(t) for t in result.fact.days if int(t) > today_i)
        tomorrow_i = future[0] if future and future[0] - today_i <= MAX_DAY_GAP else None

        for ts, groups_fact in result.fact.days.items():
            ts_i = int(ts)
            if ts_i == today_i:
                kind = "fact"
            elif tomorrow_i is not None and ts_i == tomorrow_i:
                kind = "fact_tomorrow"
            elif 0 < today_i - ts_i <= MAX_DAY_GAP:
                kind = "fact_yesterday"
            else:
                continue
            for group_id, slots in groups_fact.items():
                g = out["groups"].setdefault(group_id, {})
                g[kind] = {str(h): slot_to_str(s) for h, s in slots.items()}
                g[kind + "_timestamp"] = str(ts)

        # Якщо сайт не віддає вчорашній факт, беремо його з попереднього schedule.json.
        for group_id, g in out["groups"].items():
            if "fact_yesterday" in g:
                continue
            hours, ts = previous_yesterday(previous.get("groups", {}).get(group_id, {}), today_ts)
            if hours:
                g["fact_yesterday"] = hours
                g["fact_yesterday_timestamp"] = ts

        out["fact_update"] = getattr(result.fact, "update", "")

    # 3. Додаткові дані
    if result.preset:
        if hasattr(result.preset, "time_zone"):
            out["time_zone"] = result.preset.time_zone
        if hasattr(result.preset, "sch_names"):
            out["group_names"] = result.preset.sch_names
        if hasattr(result.preset, "days"):
            out["day_names"] = result.preset.days
        if hasattr(result.preset, "is_active"):
            out["is_active"] = result.preset.is_active

    if hasattr(result, "update_timestamp"):
        out["update_timestamp"] = result.update_timestamp

    carry_over(out, previous)
    return out


async def fetch_result():
    cookies, csrf_token = await get_cleared_cookies(SCHEDULE_URL, show_browser=False)
    print(f"Отримано {len(cookies)} cookies")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    if csrf_token:
        headers["X-CSRF-Token"] = csrf_token
    session = AsyncSession(timeout=30.0, headers=headers, cookies=cookies, impersonate="chrome120")
    async with DtekClient(REGION, ajax_url=AJAX_URL, session=session) as client:
        return await client.get_home_num(city=CITY, street=STREET)


async def main():
    print(f"[{datetime.now(timezone.utc)}] Починаємо отримання даних ДТЕК...")
    print(f"Використовуємо: city={CITY}, street={STREET}")
    previous = load_previous()  # до перезапису файлу

    result = None
    for attempt in range(1, 4):  # сайт може бути перевантажений: кілька спроб
        try:
            result = await asyncio.wait_for(fetch_result(), timeout=ATTEMPT_TIMEOUT)
            break
        except Exception as e:
            print(f"Спроба {attempt}/3 невдала: {e!r}")
            if attempt == 3:
                raise  # файл не перезаписуємо: лишається попередня версія
            await asyncio.sleep(20 * attempt)

    data = convert_schedule(result, previous)
    if not data["groups"]:
        raise RuntimeError("Порожня відповідь: schedule.json не перезаписано")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Успішно записано у {OUTPUT_FILE}, груп: {len(data['groups'])}")


if __name__ == "__main__":
    asyncio.run(main())
