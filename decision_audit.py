"""
decision_audit.py - SQLite decision audit logger for trading decisions

Purpose:
    Create a traceable, readable, and analysis-ready record of every bot decision.

Responsibilities:
    - Store every decision in SQLite
    - Keep readable summary columns
    - Keep typed columns for later algorithmic/statistical analysis
    - Keep full JSON backup for deep debugging
    - Never execute trades
    - Never call Claude
    - Work with both gold_monitor.py and eurjpy_monitor.py
"""

import os
import csv
import json
import sqlite3
import argparse
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List


class DecisionAuditor:
    """
    SQLite audit logger for trading decisions.

    Records the full chain:
        market_snapshot → claude_decision → python_validation → final_action

    Main database:
        logs/decision_audit.db
    """

    def __init__(
        self,
        log_dir: str = "logs",
        instrument: str = "UNKNOWN",
        db_name: str = "decision_audit.db",
    ):
        self.log_dir = log_dir
        self.instrument = instrument
        self.db_path = os.path.join(log_dir, db_name)

        os.makedirs(log_dir, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """
        Create a SQLite connection with timeout for concurrent writes.

        WAL mode helps when gold_monitor.py and eurjpy_monitor.py both write/read.
        busy_timeout makes SQLite wait instead of failing instantly if locked.
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row

        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")

        return conn

    def _init_db(self) -> None:
        """Create the decision audit table and indexes if they do not exist."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS decision_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    timestamp TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    source TEXT,

                    price REAL,
                    session TEXT,
                    market_state TEXT,
                    regime TEXT,
                    daily_trend TEXT,

                    breakout_level REAL,
                    next_resistance REAL,
                    nearest_support REAL,

                    breakout_confirmed INTEGER,
                    breakdown_confirmed INTEGER,
                    candles_above_level INTEGER,
                    candles_below_level INTEGER,
                    consecutive_bullish_candles INTEGER,
                    consecutive_bearish_candles INTEGER,

                    ema_alignment TEXT,
                    price_vs_ema50 TEXT,
                    extension_status TEXT,
                    distance_from_entry REAL,
                    news_nearby INTEGER,

                    claude_decision TEXT,
                    claude_setup TEXT,
                    claude_confidence TEXT,
                    claude_entry_style TEXT,
                    claude_reason TEXT,
                    claude_risk_comment TEXT,
                    is_late_chase INTEGER,
                    needs_pullback INTEGER,

                    validation_passed INTEGER,
                    python_block_reason TEXT,
                    rr REAL,
                    stop_distance REAL,

                    final_action TEXT NOT NULL,
                    final_reason TEXT,
                    final_reason_code TEXT,

                    full_data_json TEXT,

                    created_at TEXT NOT NULL
                );
                """
            )

            indexes = [
                "CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON decision_audit(timestamp);",
                "CREATE INDEX IF NOT EXISTS idx_audit_instrument ON decision_audit(instrument);",
                "CREATE INDEX IF NOT EXISTS idx_audit_final_action ON decision_audit(final_action);",
                "CREATE INDEX IF NOT EXISTS idx_audit_claude_decision ON decision_audit(claude_decision);",
                "CREATE INDEX IF NOT EXISTS idx_audit_python_block_reason ON decision_audit(python_block_reason);",
                "CREATE INDEX IF NOT EXISTS idx_audit_market_state ON decision_audit(market_state);",
                "CREATE INDEX IF NOT EXISTS idx_audit_session ON decision_audit(session);",
            ]

            for index_sql in indexes:
                conn.execute(index_sql)

            conn.commit()

    def log_decision(
        self,
        market_snapshot: Dict[str, Any],
        claude_decision: Dict[str, Any],
        python_validation: Optional[Dict[str, Any]] = None,
        final_action: str = "NO_TRADE",
        final_reason: str = "",
        source: str = "unknown",
        metadata: Optional[Dict[str, Any]] = None,
        final_reason_code: Optional[str] = None,
    ) -> None:
        """
        Log one trading decision.

        Args:
            market_snapshot:
                Objective facts calculated by Python.

            claude_decision:
                Structured ClaudeReviewer output.

            python_validation:
                Python's final safety/geometry/R:R validation.

            final_action:
                What the bot actually did:
                    ENTER_NOW, WAIT_PULLBACK, NO_TRADE, BLOCKED

            final_reason:
                Human-readable final reason.

            source:
                Source script, e.g. gold_monitor.py or eurjpy_monitor.py.

            metadata:
                Extra context.

            final_reason_code:
                Optional structured reason code, e.g. RR_TOO_LOW.
        """
        try:
            market_snapshot = market_snapshot or {}
            claude_decision = claude_decision or {}
            python_validation = python_validation or {}
            metadata = metadata or {}

            timestamp = datetime.now(timezone.utc).isoformat()
            instrument = self._clean_code(
                market_snapshot.get("instrument", self.instrument)
            )

            final_action_clean = self._clean_text(final_action or "NO_TRADE").upper()
            final_reason_clean = str(final_reason or "")

            # Single JSON backup with all data
            full_data = {
                "market_snapshot": self._clean_snapshot(market_snapshot),
                "claude_decision": claude_decision,
                "python_validation": python_validation,
                "metadata": metadata,
            }

            row = {
                "timestamp": timestamp,
                "instrument": self._clean_code(instrument),
                "source": source,

                "price": self._get_float(market_snapshot, "current_price", "price"),
                "session": self._clean_code(market_snapshot.get("session")),
                "market_state": self._clean_code(market_snapshot.get("market_state")),
                "regime": self._clean_code(market_snapshot.get("regime")),
                "daily_trend": self._clean_code(market_snapshot.get("daily_trend")),

                "breakout_level": self._get_float(market_snapshot, "breakout_level"),
                "next_resistance": self._get_float(market_snapshot, "next_resistance"),
                "nearest_support": self._get_float(market_snapshot, "nearest_support"),

                "breakout_confirmed": self._bool_to_int(market_snapshot.get("breakout_confirmed")),
                "breakdown_confirmed": self._bool_to_int(market_snapshot.get("breakdown_confirmed")),
                "candles_above_level": self._get_int(market_snapshot, "candles_above_level"),
                "candles_below_level": self._get_int(market_snapshot, "candles_below_level"),
                "consecutive_bullish_candles": self._get_int(market_snapshot, "consecutive_bullish_candles"),
                "consecutive_bearish_candles": self._get_int(market_snapshot, "consecutive_bearish_candles"),

                "ema_alignment": self._clean_code(market_snapshot.get("ema_alignment")),
                "price_vs_ema50": self._clean_code(market_snapshot.get("price_vs_ema50")),
                "extension_status": self._extract_extension_status(market_snapshot.get("extension_check")),
                "distance_from_entry": self._get_float(market_snapshot, "distance_from_entry"),
                "news_nearby": self._bool_to_int(market_snapshot.get("news_nearby")),

                "claude_decision": self._clean_text(claude_decision.get("decision")).upper(),
                "claude_setup": self._clean_text(claude_decision.get("setup")).upper(),
                "claude_confidence": self._clean_text(claude_decision.get("confidence")).upper(),
                "claude_entry_style": self._clean_text(claude_decision.get("entry_style")).upper(),
                "claude_reason": str(claude_decision.get("reason", "")),
                "claude_risk_comment": str(claude_decision.get("risk_comment", "")),
                "is_late_chase": self._bool_to_int(claude_decision.get("is_late_chase")),
                "needs_pullback": self._bool_to_int(claude_decision.get("needs_pullback")),

                "validation_passed": self._validation_passed_to_int(python_validation),
                "python_block_reason": self._extract_python_block_reason(
                    python_validation,
                    final_action_clean,
                ),
                "rr": self._get_float(python_validation, "rr", "risk_reward", "r_r"),
                "stop_distance": self._get_float(python_validation, "stop_distance", "stop_dist"),

                "final_action": final_action_clean,
                "final_reason": final_reason_clean,
                "final_reason_code": self._reason_code(final_reason_code or python_validation.get("reason_code") or final_reason_clean),

                "full_data_json": self._to_json(full_data),

                "created_at": timestamp,
            }

            columns = list(row.keys())
            placeholders = ", ".join(["?"] * len(columns))
            sql = f"""
                INSERT INTO decision_audit ({", ".join(columns)})
                VALUES ({placeholders});
            """

            with self._connect() as conn:
                conn.execute(sql, [row[col] for col in columns])
                conn.commit()

        except Exception as e:
            # Audit failures should never crash the bot.
            print(f"[AUDIT] Failed to log decision: {e}")

    def get_recent_decisions(self, count: int = 20) -> List[Dict[str, Any]]:
        """Return most recent decisions, newest first."""
        count = max(1, int(count))

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM decision_audit
                ORDER BY timestamp DESC
                LIMIT ?;
                """,
                (count,),
            ).fetchall()

        return [self._row_to_dict(row) for row in rows]

    def search_decisions(
        self,
        instrument: Optional[str] = None,
        final_action: Optional[str] = None,
        claude_decision: Optional[str] = None,
        python_block_reason: Optional[str] = None,
        session: Optional[str] = None,
        market_state: Optional[str] = None,
        since: Optional[datetime] = None,
        max_results: int = 100,
    ) -> List[Dict[str, Any]]:
        """Search decisions using structured columns."""
        max_results = max(1, int(max_results))

        conditions = []
        params: List[Any] = []

        if instrument:
            conditions.append("instrument = ?")
            params.append(instrument.strip().upper())

        if final_action:
            conditions.append("final_action = ?")
            params.append(final_action.strip().upper())

        if claude_decision:
            conditions.append("claude_decision = ?")
            params.append(claude_decision.strip().upper())

        if python_block_reason:
            conditions.append("python_block_reason = ?")
            params.append(python_block_reason.strip().upper())

        if session:
            conditions.append("session = ?")
            params.append(session.strip().upper())

        if market_state:
            conditions.append("market_state = ?")
            params.append(market_state.strip().upper())

        if since:
            since_utc = self._normalize_datetime(since)
            conditions.append("timestamp >= ?")
            params.append(since_utc.isoformat())

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        sql = f"""
            SELECT *
            FROM decision_audit
            {where_clause}
            ORDER BY timestamp DESC
            LIMIT ?;
        """
        params.append(max_results)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [self._row_to_dict(row) for row in rows]

    def get_summary(self) -> Dict[str, Any]:
        """
        Return summary counts for audit analysis.

        Important:
            block_reasons must only include rows where final_action == BLOCKED.
            A Claude NO_TRADE reason is not a Python block reason.
        """
        summary = {
            "total_decisions": 0,
            "final_actions": [],
            "claude_vs_final": [],
            "block_reasons": [],
            "by_instrument": [],
            "by_session": [],
        }

        try:
            with self._connect() as conn:
                total_row = conn.execute(
                    "SELECT COUNT(*) AS count FROM decision_audit"
                ).fetchone()
                summary["total_decisions"] = int(total_row["count"]) if total_row else 0

                summary["final_actions"] = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT final_action, COUNT(*) AS count
                        FROM decision_audit
                        GROUP BY final_action
                        ORDER BY count DESC, final_action ASC
                        """
                    ).fetchall()
                ]

                summary["claude_vs_final"] = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT claude_decision, final_action, COUNT(*) AS count
                        FROM decision_audit
                        GROUP BY claude_decision, final_action
                        ORDER BY count DESC, claude_decision ASC, final_action ASC
                        """
                    ).fetchall()
                ]

                # Only real Python-blocked decisions belong here.
                summary["block_reasons"] = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT python_block_reason, COUNT(*) AS count
                        FROM decision_audit
                        WHERE final_action = 'BLOCKED'
                          AND python_block_reason IS NOT NULL
                          AND python_block_reason != ''
                        GROUP BY python_block_reason
                        ORDER BY count DESC, python_block_reason ASC
                        """
                    ).fetchall()
                ]

                summary["by_instrument"] = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT instrument, COUNT(*) AS count
                        FROM decision_audit
                        GROUP BY instrument
                        ORDER BY count DESC, instrument ASC
                        """
                    ).fetchall()
                ]

                summary["by_session"] = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT session, COUNT(*) AS count
                        FROM decision_audit
                        GROUP BY session
                        ORDER BY count DESC, session ASC
                        """
                    ).fetchall()
                ]

        except Exception as e:
            print(f"[AUDIT] Failed to get summary: {e}")

        return summary

    def export_csv(self, output_path: Optional[str] = None) -> str:
        """
        Export readable + analysis columns to CSV.

        JSON backup column is intentionally excluded to keep CSV usable.
        """
        if output_path is None:
            output_path = os.path.join(self.log_dir, "decision_audit_export.csv")

        columns = [
            "id",
            "timestamp",
            "instrument",
            "source",
            "price",
            "session",
            "market_state",
            "regime",
            "daily_trend",
            "breakout_level",
            "next_resistance",
            "nearest_support",
            "breakout_confirmed",
            "breakdown_confirmed",
            "candles_above_level",
            "candles_below_level",
            "consecutive_bullish_candles",
            "consecutive_bearish_candles",
            "ema_alignment",
            "price_vs_ema50",
            "extension_status",
            "distance_from_entry",
            "news_nearby",
            "claude_decision",
            "claude_setup",
            "claude_confidence",
            "claude_entry_style",
            "is_late_chase",
            "needs_pullback",
            "validation_passed",
            "python_block_reason",
            "rr",
            "stop_distance",
            "final_action",
            "final_reason_code",
            "final_reason",
            "claude_reason",
            "claude_risk_comment",
        ]

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(columns)}
                FROM decision_audit
                ORDER BY timestamp DESC;
                """
            ).fetchall()

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()

            for row in rows:
                data = self._row_to_dict(row)
                writer.writerow({col: data.get(col) for col in columns})

        return output_path

    def _clean_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Remove huge objects from the JSON backup."""
        cleaned = dict(snapshot)

        keys_to_skip = [
            "df_1m",
            "df_5m",
            "df_15m",
            "df_h1",
            "df_daily",
            "candles",
            "raw_candles",
            "raw_data",
            "dataframe",
        ]

        for key in keys_to_skip:
            cleaned.pop(key, None)

        return cleaned

    def _to_json(self, data: Any) -> str:
        """Safely serialize data to JSON."""
        return json.dumps(data, default=self._json_default, ensure_ascii=False, sort_keys=True)

    def _json_default(self, obj: Any) -> Any:
        """Handle datetimes, numpy scalars, pandas timestamps, etc."""
        if isinstance(obj, datetime):
            return obj.isoformat()

        if hasattr(obj, "item"):
            try:
                return obj.item()
            except Exception:
                pass

        if hasattr(obj, "tolist"):
            try:
                return obj.tolist()
            except Exception:
                pass

        return str(obj)

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def _clean_text(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def _clean_code(self, value: Any) -> str:
        return self._clean_text(value).upper()

    def _get_float(self, data: Dict[str, Any], *keys: str) -> Optional[float]:
        for key in keys:
            value = data.get(key)
            if value is None or value == "":
                continue

            try:
                return float(value)
            except (TypeError, ValueError):
                continue

        return None

    def _get_int(self, data: Dict[str, Any], key: str) -> Optional[int]:
        value = data.get(key)

        if value is None or value == "":
            return None

        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _bool_to_int(self, value: Any) -> Optional[int]:
        if value is None:
            return None

        if isinstance(value, bool):
            return int(value)

        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "yes", "1", "y"}:
                return 1
            if lowered in {"false", "no", "0", "n"}:
                return 0

        try:
            return int(bool(value))
        except Exception:
            return None

    def _validation_passed_to_int(self, validation: Dict[str, Any]) -> Optional[int]:
        for key in ["passed", "valid", "success", "approved"]:
            if key in validation:
                return self._bool_to_int(validation.get(key))

        return None

    def _extract_extension_status(self, extension_check: Any) -> str:
        text = str(extension_check or "").strip()
        lowered = text.lower()

        if not text:
            return ""

        not_extended_phrases = [
            "not extended",
            "not_extended",
            "not-extended",
            "within",
            "ok",
        ]

        if any(phrase in lowered for phrase in not_extended_phrases):
            return "OK"

        if "extended" in lowered:
            return "EXTENDED"

        return text[:80].upper()

    def _extract_python_block_reason(
        self,
        validation: Dict[str, Any],
        final_action: str = "",
    ) -> str:
        """
        Extract a Python block reason only when validation actually failed
        or the final action was BLOCKED.

        This prevents successful validations from appearing as block reasons.
        """
        validation_passed = self._validation_passed_to_int(validation)
        final_action = self._clean_code(final_action)

        if validation_passed == 1 and final_action != "BLOCKED":
            return ""

        candidates = [
            validation.get("block_reason_code"),
            validation.get("reason_code"),
            validation.get("block_reason"),
            validation.get("reason"),
        ]

        for candidate in candidates:
            if candidate:
                return self._reason_code(candidate)

        return ""

    def _reason_code(self, value: Any) -> str:
        """
        Convert text into an algorithm-friendly reason code.

        Example:
            "Price too extended from EMA9" -> PRICE_TOO_EXTENDED_FROM_EMA9
        """
        text = str(value or "").strip().upper()

        if not text:
            return ""

        cleaned = []
        previous_was_underscore = False

        for char in text:
            if char.isalnum():
                cleaned.append(char)
                previous_was_underscore = False
            else:
                if not previous_was_underscore:
                    cleaned.append("_")
                    previous_was_underscore = True

        code = "".join(cleaned).strip("_")
        return code[:80]

    def _normalize_datetime(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)


def print_decisions(decisions: List[Dict[str, Any]]) -> None:
    """Print decisions in a readable terminal format."""
    if not decisions:
        print("No decisions found.")
        return

    for d in decisions:
        print("-" * 100)
        print(
            f"ID {d.get('id')} | {d.get('timestamp')} | "
            f"{d.get('instrument')} @ {d.get('price')}"
        )
        print(
            f"Claude: {d.get('claude_decision')} "
            f"{d.get('claude_setup')} "
            f"confidence={d.get('claude_confidence')}"
        )
        print(
            f"Final:  {d.get('final_action')} | "
            f"code={d.get('final_reason_code')}"
        )
        print(f"Reason: {d.get('final_reason')}")
        print(
            f"State:  session={d.get('session')} | "
            f"market_state={d.get('market_state')} | "
            f"regime={d.get('regime')}"
        )
        print(
            f"Checks: rr={d.get('rr')} | "
            f"stop_distance={d.get('stop_distance')} | "
            f"validation_passed={d.get('validation_passed')} | "
            f"block_reason={d.get('python_block_reason')}"
        )


def print_summary(summary: Dict[str, Any]) -> None:
    """Print summary stats."""
    print(f"\nTOTAL DECISIONS: {summary['total_decisions']}")
    print("=" * 60)

    print("\nFINAL ACTIONS")
    print("-" * 60)
    for row in summary["final_actions"]:
        print(f"{row['final_action']:<20} {row['count']}")

    print("\nCLAUDE VS FINAL")
    print("-" * 60)
    for row in summary["claude_vs_final"]:
        print(f"{row['claude_decision']:<15} -> {row['final_action']:<15} {row['count']}")

    print("\nPYTHON BLOCK REASONS")
    print("-" * 60)
    if summary["block_reasons"]:
        for row in summary["block_reasons"]:
            print(f"{row['python_block_reason']:<40} {row['count']}")
    else:
        print("(none)")

    print("\nBY INSTRUMENT")
    print("-" * 60)
    for row in summary["by_instrument"]:
        print(f"{row['instrument']:<15} {row['count']}")

    print("\nBY SESSION")
    print("-" * 60)
    if summary["by_session"]:
        for row in summary["by_session"]:
            print(f"{row['session']:<15} {row['count']}")
    else:
        print("(none)")


def run_test() -> None:
    """Run standalone tests using a separate test database."""
    print("=" * 80)
    print("TESTING SQLITE DECISION AUDITOR")
    print("=" * 80)

    test_log_dir = "logs/audit_test"
    test_db_path = os.path.join(test_log_dir, "decision_audit.db")

    os.makedirs(test_log_dir, exist_ok=True)

    if os.path.exists(test_db_path):
        os.remove(test_db_path)

    auditor = DecisionAuditor(log_dir=test_log_dir, instrument="XAU_USD")

    print("\n[TEST 1] Logging ENTER_NOW decision")
    print("-" * 80)

    auditor.log_decision(
        market_snapshot={
            "instrument": "XAU_USD",
            "current_price": 4732.5,
            "session": "LONDON",
            "regime": "BULLISH",
            "daily_trend": "RANGING",
            "market_state": "BULLISH_TREND_IGNITION",
            "breakout_level": 4721.7,
            "next_resistance": 4750.0,
            "nearest_support": 4710.0,
            "breakout_confirmed": True,
            "breakdown_confirmed": False,
            "candles_above_level": 2,
            "candles_below_level": 0,
            "consecutive_bullish_candles": 3,
            "consecutive_bearish_candles": 0,
            "ema_alignment": "bullish",
            "price_vs_ema50": "above",
            "extension_check": "OK - within 1.2x ATR of EMA9",
            "distance_from_entry": 0,
            "news_nearby": False,
        },
        claude_decision={
            "decision": "ENTER_NOW",
            "setup": "LONG",
            "confidence": "HIGH",
            "entry_style": "BREAKOUT",
            "reason": "Clean breakout above trigger with confirmation.",
            "risk_comment": "Python should validate R:R.",
            "is_late_chase": False,
            "needs_pullback": False,
        },
        python_validation={
            "passed": True,
            "rr": 2.8,
            "stop_distance": 22.5,
            "reason_code": "VALIDATION_PASSED",
        },
        final_action="ENTER_NOW",
        final_reason="All validations passed.",
        final_reason_code="VALIDATION_PASSED",
        source="test_script.py",
        metadata={"balance": 10000},
    )

    print("✅ Logged ENTER_NOW")

    print("\n[TEST 2] Logging BLOCKED decision")
    print("-" * 80)

    auditor.log_decision(
        market_snapshot={
            "instrument": "XAU_USD",
            "current_price": 4748.0,
            "session": "NEW YORK",
            "regime": "BULLISH",
            "daily_trend": "BULLISH",
            "market_state": "BULLISH_TREND_IGNITION_EXTENDED",
            "breakout_level": 4710.0,
            "next_resistance": 4750.0,
            "nearest_support": 4710.0,
            "breakout_confirmed": True,
            "breakdown_confirmed": False,
            "candles_above_level": 5,
            "candles_below_level": 0,
            "consecutive_bullish_candles": 5,
            "consecutive_bearish_candles": 0,
            "ema_alignment": "bullish",
            "price_vs_ema50": "above",
            "extension_check": "EXTENDED - 2.5x ATR from EMA9",
            "distance_from_entry": 38,
            "news_nearby": False,
        },
        claude_decision={
            "decision": "ENTER_NOW",
            "setup": "LONG",
            "confidence": "MEDIUM",
            "entry_style": "BREAKOUT",
            "reason": "Bullish structure aligned.",
            "risk_comment": "Move may be extended.",
            "is_late_chase": True,
            "needs_pullback": True,
        },
        python_validation={
            "passed": False,
            "rr": 1.1,
            "stop_distance": 25.0,
            "reason_code": "PRICE_EXTENDED",
            "reason": "Price too extended from EMA9.",
        },
        final_action="BLOCKED",
        final_reason="Python extension check failed - price too extended.",
        final_reason_code="PRICE_EXTENDED",
        source="test_script.py",
        metadata={"balance": 10000},
    )

    print("✅ Logged BLOCKED")

    print("\n[TEST 3] Logging NO_TRADE decision")
    print("-" * 80)

    auditor.log_decision(
        market_snapshot={
            "instrument": "EUR_JPY",
            "current_price": 171.25,
            "session": "NEW YORK",
            "regime": "CHOP",
            "daily_trend": "RANGING",
            "market_state": "NO_TRADE_NEWS_RISK",
            "news_nearby": True,
        },
        claude_decision={
            "decision": "NO_TRADE",
            "setup": "NONE",
            "confidence": "LOW",
            "entry_style": "NONE",
            "reason": "News risk nearby.",
            "risk_comment": "Wait for news to pass.",
            "is_late_chase": False,
            "needs_pullback": False,
        },
        python_validation={},
        final_action="NO_TRADE",
        final_reason="Claude recommended NO_TRADE due to news risk.",
        final_reason_code="NEWS_RISK",
        source="test_script.py",
        metadata={"balance": 10000},
    )

    print("✅ Logged NO_TRADE")

    print("\n[TEST 4] Recent decisions")
    print("-" * 80)
    recent = auditor.get_recent_decisions(count=3)
    print_decisions(recent)

    print("\n[TEST 5] Search: BLOCKED XAU_USD decisions")
    print("-" * 80)
    blocked = auditor.search_decisions(
        instrument="XAU_USD",
        final_action="BLOCKED",
        max_results=10,
    )
    print_decisions(blocked)

    print("\n[TEST 6] Search: Claude said ENTER_NOW but bot did not enter")
    print("-" * 80)
    overrides = auditor.search_decisions(
        claude_decision="ENTER_NOW",
        max_results=10,
    )
    overrides_filtered = [d for d in overrides if d["final_action"] != "ENTER_NOW"]
    print(f"Found {len(overrides_filtered)} cases where Claude said ENTER_NOW but bot did not enter:")
    print_decisions(overrides_filtered)

    print("\n[TEST 7] Summary")
    print("-" * 80)
    print_summary(auditor.get_summary())

    csv_path = auditor.export_csv()
    print("\n" + "=" * 80)
    print("TESTS COMPLETE")
    print("=" * 80)
    print(f"Test database saved to: {auditor.db_path}")
    print(f"Test CSV exported to: {csv_path}")


def main() -> None:
    """CLI for querying the audit database."""
    parser = argparse.ArgumentParser(description="SQLite decision audit logger")
    parser.add_argument(
        "command",
        nargs="?",
        default="test",
        choices=["test", "recent", "blocked", "overrides", "summary", "export", "search"],
        help="Command to run",
    )
    parser.add_argument("--log-dir", default="logs", help="Log directory")
    parser.add_argument("--instrument", default=None, help="Filter by instrument")
    parser.add_argument("--session", default=None, help="Filter by session")
    parser.add_argument("--limit", type=int, default=20, help="Max results")

    args = parser.parse_args()

    if args.command == "test":
        run_test()
        return

    auditor = DecisionAuditor(log_dir=args.log_dir, instrument=args.instrument or "UNKNOWN")

    if args.command == "recent":
        print_decisions(auditor.get_recent_decisions(count=args.limit))

    elif args.command == "blocked":
        print_decisions(
            auditor.search_decisions(
                instrument=args.instrument,
                final_action="BLOCKED",
                max_results=args.limit,
            )
        )

    elif args.command == "overrides":
        # Show cases where Claude said ENTER_NOW but bot didn't
        decisions = auditor.search_decisions(
            instrument=args.instrument,
            claude_decision="ENTER_NOW",
            max_results=args.limit,
        )
        overrides = [d for d in decisions if d["final_action"] != "ENTER_NOW"]
        print(f"Found {len(overrides)} cases where Claude said ENTER_NOW but bot did not enter:")
        print_decisions(overrides)

    elif args.command == "summary":
        print_summary(auditor.get_summary())

    elif args.command == "export":
        path = auditor.export_csv()
        print(f"Exported CSV to: {path}")

    elif args.command == "search":
        # Interactive search - could be expanded
        print_decisions(
            auditor.search_decisions(
                instrument=args.instrument,
                session=args.session,
                max_results=args.limit,
            )
        )


if __name__ == "__main__":
    main()
