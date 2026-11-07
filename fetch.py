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
# ================================================

def slot_to_str(status) -> str:
    if status is None:
        return "unknown"
    if hasattr(status, "value"):
        return status.value
    return str(status)

def convert_schedule(result) -> dict:
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "groups": {},
    }

    if result.preset and hasattr(result.preset, "groups"):
        for group_id, group_week in result.preset.groups.items():
            days_out = {}
            for day_num, day_schedule in group_week.days.items():
                slots_out = {}
                for hour_str, status in day_schedule.slots.items():
                    slots_out[str(hour_str)] = slot_to_str(status)
                days_out[str(day_num)] = slots_out
            out["groups"][group_id] = {"preset": days_out}

    # ✅ ЗБЕРІГАЄМО FACT ЛИШЕ НА СЬОГОДНІ (і, якщо є, на завтра)
    if result.fact and hasattr(result.fact, "days"):
        today_ts = str(result.fact.today_ts)
        for ts, groups_fact in result.fact.days.items():
            if ts != today_ts:
                continue  # Пропускаємо інші дні (післязавтра тощо)
            for group_id, slots in groups_fact.items():
                if group_id not in out["groups"]:
                    out["groups"][group_id] = {}
                slots_out = {str(h): slot_to_str(s) for h, s in slots.items()}
                out["groups"][group_id]["fact"] = slots_out
                out["groups"][group_id]["fact_timestamp"] = ts
        out["fact_update"] = getattr(result.fact, "update", "")

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

    return out

async def main():
    print(f"[{datetime.now(timezone.utc)}] Починаємо отримання даних ДТЕК...")
    print(f"Використовуємо: city={CITY}, street={STREET}")

    cookies, csrf_token = await get_cleared_cookies(SCHEDULE_URL, show_browser=False)
    print(f"Отримано {len(cookies)} cookies")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    if csrf_token:
        headers["X-CSRF-Token"] = csrf_token

    session = AsyncSession(
        timeout=30.0,
        headers=headers,
        cookies=cookies,
        impersonate="chrome120",
    )

    async with DtekClient(REGION, ajax_url=AJAX_URL, session=session) as client:
        result = await client.get_home_num(city=CITY, street=STREET)
        print("Дані отримано, конвертуємо...")

    data = convert_schedule(result)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Успішно записано у {OUTPUT_FILE}")
    print(f"Груп: {len(data['groups'])}")

if __name__ == "__main__":
    asyncio.run(main())
