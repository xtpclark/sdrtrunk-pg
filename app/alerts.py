"""
Alert engine.

check_keyword_alerts(call_id)  — match transcript against keyword rules
check_volume_spike(tg, cat)    — detect call volume anomalies
send_alert(alert_id)           — POST to webhook and mark notified
run_alert_check()              — full sweep: keywords + volume + pending sends
"""

import logging
from typing import Optional

import requests

from app.config import ALERT_WEBHOOK_URL
from app.db import db

log = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# Keyword alerts
# -----------------------------------------------------------------------

def check_keyword_alerts(call_id: int):
    """
    Compare call transcript against all enabled keyword alert rules.
    Inserts a row into alerts for each rule that matches.

    Rule config JSON schema:
        {"keywords": ["fire", "shots fired"], "tg": 12345}   (tg optional)
    """
    with db() as conn:
        cur = conn.cursor()

        # Fetch transcript
        cur.execute("SELECT tg, transcript FROM calls WHERE id = %s", (call_id,))
        call = cur.fetchone()

    if not call or not call["transcript"]:
        return

    transcript = call["transcript"].lower()
    tg = call["tg"]

    with db() as conn:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT id, name, config
            FROM alert_rules
            WHERE enabled = true
              AND rule_type = 'keyword'
            """
        )
        rules = cur.fetchall()

        for rule in rules:
            config   = rule["config"] or {}
            keywords = config.get("keywords", [])
            rule_tg  = config.get("tg")  # optional: restrict to specific TG

            if rule_tg and rule_tg != tg:
                continue

            matched = [kw for kw in keywords if kw.lower() in transcript]
            if not matched:
                continue

            msg = f"Keyword match on call {call_id} (tg {tg}): {', '.join(matched)}"
            log.info("Alert rule '%s' matched: %s", rule["name"], msg)

            cur.execute(
                """
                INSERT INTO alerts (rule_id, call_id, message)
                VALUES (%s, %s, %s)
                """,
                (rule["id"], call_id, msg),
            )


# -----------------------------------------------------------------------
# Volume spike alerts
# -----------------------------------------------------------------------

def check_volume_spike(tg: int, category: Optional[str] = None):
    """
    Compare the last-hour call count for tg against the materialized
    baseline (mv_call_volume_baseline). Fires an alert if current volume
    exceeds 2× the historical average for this tg/hour/day-of-week.
    """
    with db() as conn:
        cur = conn.cursor()

        # Current hour call count for this tg
        cur.execute(
            """
            SELECT count(*) AS cnt
            FROM calls
            WHERE tg = %s
              AND ts >= date_trunc('hour', now())
              AND ts <  date_trunc('hour', now()) + interval '1 hour'
            """,
            (tg,),
        )
        current_count = cur.fetchone()["cnt"]

        if current_count == 0:
            return

        # Historical baseline for same tg / day-of-week / hour-of-day
        cur.execute(
            """
            SELECT call_count
            FROM mv_call_volume_baseline
            WHERE tg  = %s
              AND dow = extract(dow FROM now())::int
              AND hod = extract(hour FROM now() AT TIME ZONE 'America/New_York')::int
            """,
            (tg,),
        )
        baseline_row = cur.fetchone()

    if not baseline_row:
        # Not enough history to compare
        return

    baseline = baseline_row["call_count"]

    # Check if any volume_spike rule exists and get its threshold multiplier
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, name, config
            FROM alert_rules
            WHERE enabled = true
              AND rule_type = 'volume_spike'
            LIMIT 1
            """
        )
        rule = cur.fetchone()

    if not rule:
        return

    config    = rule["config"] or {}
    threshold = float(config.get("threshold", 2.0))

    if baseline > 0 and current_count >= threshold * baseline:
        msg = (
            f"Volume spike on tg {tg}"
            + (f" ({category})" if category else "")
            + f": {current_count} calls this hour vs {baseline} historical avg"
        )
        log.info("Volume spike alert: %s", msg)

        with db() as conn:
            cur = conn.cursor()
            # Avoid duplicate alerts within the same hour
            cur.execute(
                """
                SELECT id FROM alerts
                WHERE rule_id = %s
                  AND fired_at >= date_trunc('hour', now())
                  AND message LIKE %s
                """,
                (rule["id"], f"%tg {tg}%"),
            )
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO alerts (rule_id, call_id, message)
                    VALUES (%s, NULL, %s)
                    """,
                    (rule["id"], msg),
                )


# -----------------------------------------------------------------------
# Alert delivery
# -----------------------------------------------------------------------

def send_alert(alert_id: int):
    """
    POST alert details to ALERT_WEBHOOK_URL (if configured).
    Marks notified=true on success or if no webhook is configured.
    """
    if not ALERT_WEBHOOK_URL:
        # No webhook configured — mark as notified so we don't retry
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE alerts SET notified = true WHERE id = %s", (alert_id,)
            )
        return

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.id, a.message, a.fired_at, a.call_id,
                   r.name AS rule_name, r.rule_type
            FROM alerts a
            LEFT JOIN alert_rules r ON r.id = a.rule_id
            WHERE a.id = %s
            """,
            (alert_id,),
        )
        alert = cur.fetchone()

    if not alert:
        log.warning("send_alert: alert %d not found", alert_id)
        return

    payload = {
        "alert_id":  alert["id"],
        "rule":      alert["rule_name"],
        "type":      alert["rule_type"],
        "message":   alert["message"],
        "fired_at":  str(alert["fired_at"]),
        "call_id":   alert["call_id"],
    }

    try:
        resp = requests.post(ALERT_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Alert %d delivered to webhook (HTTP %d)", alert_id, resp.status_code)
        success = True
    except Exception as exc:
        log.error("Failed to deliver alert %d: %s", alert_id, exc)
        success = False

    if success:
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE alerts SET notified = true WHERE id = %s", (alert_id,)
            )


# -----------------------------------------------------------------------
# Full alert sweep
# -----------------------------------------------------------------------

def run_alert_check():
    """
    1. Check volume spikes for all talkgroups active in the last hour.
    2. Deliver all pending (notified=false) alerts.
    """
    log.debug("Running alert check…")

    # Volume spikes: check tgs active this hour
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT c.tg, t.category
                FROM calls c
                LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
                WHERE c.ts >= date_trunc('hour', now())
                GROUP BY c.tg, t.category
                """
            )
            active_tgs = cur.fetchall()

        for row in active_tgs:
            try:
                check_volume_spike(row["tg"], row["category"])
            except Exception as exc:
                log.error("volume spike check failed for tg=%d: %s", row["tg"], exc)
    except Exception as exc:
        log.error("Failed to query active tgs for volume check: %s", exc)

    # Deliver pending alerts
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM alerts WHERE notified = false ORDER BY fired_at"
            )
            pending = [r["id"] for r in cur.fetchall()]

        for alert_id in pending:
            try:
                send_alert(alert_id)
            except Exception as exc:
                log.error("send_alert failed for id=%d: %s", alert_id, exc)
    except Exception as exc:
        log.error("Failed to query pending alerts: %s", exc)
