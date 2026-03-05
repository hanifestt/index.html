"""
watchlist.py — Persistent watchlist with risk-change alerts
Stores data in a local JSON file (works fine for small scale).
"""

import json
import os
import asyncio
from scanner import scan_token

WATCHLIST_FILE = "watchlist.json"


def _load() -> dict:
    if not os.path.exists(WATCHLIST_FILE):
        return {}
    with open(WATCHLIST_FILE, "r") as f:
        return json.load(f)


def _save(data: dict):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)


def add_to_watchlist(user_id: str, ca: str):
    data = _load()
    if user_id not in data:
        data[user_id] = {}
    # Store CA with last known risk score
    data[user_id][ca] = data[user_id].get(ca, {"last_score": None})
    _save(data)


def remove_from_watchlist(user_id: str, ca: str) -> bool:
    data = _load()
    if user_id in data and ca in data[user_id]:
        del data[user_id][ca]
        _save(data)
        return True
    return False


def get_watchlist(user_id: str) -> list:
    data = _load()
    return list(data.get(user_id, {}).keys())


async def check_watchlist_alerts() -> list:
    """
    Re-scan all watched tokens. If risk score changes by 15+, generate alert.
    Returns list of (user_id, ca, alert_message) tuples.
    """
    data = _load()
    alerts = []

    for user_id, tokens in data.items():
        for ca, meta in tokens.items():
            try:
                result = await scan_token(ca)
                new_score = result.get("risk_score", 0)
                last_score = meta.get("last_score")

                # Update stored score
                data[user_id][ca]["last_score"] = new_score

                if last_score is None:
                    continue  # First scan, no alert yet

                change = new_score - last_score

                if abs(change) >= 15:
                    direction = "📈 INCREASED" if change > 0 else "📉 DECREASED"
                    alert_msg = (
                        f"Risk score {direction} by {abs(change)} points\n"
                        f"Previous: {last_score}/100 → Now: {new_score}/100\n\n"
                        f"AI Note: {result.get('ai_summary', '')}"
                    )
                    alerts.append((user_id, ca, alert_msg))

                # Small delay to avoid rate limits
                await asyncio.sleep(2)

            except Exception:
                continue

    _save(data)
    return alerts
