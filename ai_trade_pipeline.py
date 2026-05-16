"""
ai_trade_pipeline.py - Safe AI trade decision wrapper

Purpose:
    Glue the already-tested modules together without touching live execution.

Flow:
    market snapshot
    -> optional Claude review
    -> deterministic Python validation
    -> decision audit log
    -> final action dict

Important:
    This file does NOT place trades.
    This file does NOT call OANDA.
    This file only decides/logs what the bot would do.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional
import inspect
import json
import shutil


from claude_reviewer import ClaudeReviewer
from decision_audit import DecisionAuditor
from python_validation import PythonTradeValidator, ValidationConfig


@dataclass
class AITradePipelineConfig:
    instrument: str = "XAU_USD"

    # Claude control
    use_real_claude: bool = False
    only_review_interesting_setups: bool = True

    # Safety/R:R
    min_rr: float = 2.5
    min_stop_distance: float = 20.0
    max_stop_distance: float = 100.0
    stop_buffer: float = 2.0
    block_extended: bool = True
    block_moderately_extended: bool = False

    # Session control
    allow_london: bool = True
    allow_new_york: bool = True
    allow_off_hours: bool = False

    # Audit
    audit_enabled: bool = True
    audit_db_path: str = "logs/decision_audit.db"
    source: str = "ai_trade_pipeline"


class AITradePipeline:
    """
    Safe AI decision pipeline.

    The live monitor should call this only after it has built a market snapshot.
    The result can then be inspected by gold_monitor.py / eurjpy_monitor.py.
    """

    def __init__(
        self,
        config: Optional[AITradePipelineConfig] = None,
        reviewer: Optional[Any] = None,
        auditor: Optional[Any] = None,
    ):
        self.config = config or AITradePipelineConfig()

        validator_config = ValidationConfig(
            instrument=self.config.instrument,
            min_rr=self.config.min_rr,
            min_stop_distance=self.config.min_stop_distance,
            max_stop_distance=self.config.max_stop_distance,
            stop_buffer=self.config.stop_buffer,
            allow_london=self.config.allow_london,
            allow_new_york=self.config.allow_new_york,
            allow_off_hours=self.config.allow_off_hours,
            block_extended=self.config.block_extended,
            block_moderately_extended=self.config.block_moderately_extended,
        )
        self.validator = PythonTradeValidator(validator_config)

        self.reviewer = reviewer
        if self.reviewer is None and self.config.use_real_claude:
            self.reviewer = ClaudeReviewer()

        self.auditor = auditor
        if self.auditor is None and self.config.audit_enabled:
            self.auditor = self._create_auditor(self.config.audit_db_path)

    def evaluate_snapshot(self, market_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate a pre-built market snapshot.

        Returns:
            {
                "final_action": "ENTER_NOW | BLOCKED | WAIT_PULLBACK | NO_TRADE",
                "final_reason": "...",
                "final_reason_code": "...",
                "market_snapshot": {...},
                "claude_decision": {...},
                "python_validation": {...},
                "audit_logged": true/false
            }
        """
        snapshot = dict(market_snapshot or {})
        if not snapshot.get("instrument"):
            snapshot["instrument"] = self.config.instrument

        if self.config.only_review_interesting_setups and not self._is_interesting_setup(snapshot):
            claude_decision = self._no_trade_decision(
                reason="Snapshot is not interesting enough for Claude review."
            )
        else:
            claude_decision = self._get_claude_decision(snapshot)

        validation = self.validator.validate(snapshot, claude_decision)
        final = self._make_final_action(claude_decision, validation)

        audit_logged = self._audit_decision(
            snapshot=snapshot,
            claude_decision=claude_decision,
            validation=validation,
            final=final,
        )

        return {
            **final,
            "market_snapshot": snapshot,
            "claude_decision": claude_decision,
            "python_validation": validation,
            "audit_logged": audit_logged,
        }

    # ── Decision flow helpers ────────────────────────────────────────────────

    def _is_interesting_setup(self, snapshot: Dict[str, Any]) -> bool:
        trigger_direction = self._clean_code(snapshot.get("trigger_direction", "NONE"))

        return bool(
            snapshot.get("breakout_confirmed")
            or snapshot.get("breakdown_confirmed")
            or trigger_direction in {"LONG", "SHORT"}
        )

    def _get_claude_decision(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        if self.reviewer is None:
            return self._no_trade_decision(
                reason="Claude review disabled and no reviewer was provided."
            )

        try:
            if hasattr(self.reviewer, "review_setup"):
                result = self.reviewer.review_setup(snapshot)
            elif callable(self.reviewer):
                result = self.reviewer(snapshot)
            else:
                return self._no_trade_decision(
                    reason="Reviewer is not callable and has no review_setup() method."
                )

            if not isinstance(result, dict):
                return self._no_trade_decision(
                    reason="Reviewer returned invalid non-dict response."
                )

            return result

        except Exception as e:
            return self._no_trade_decision(
                reason=f"Claude/reviewer failed safely: {e}"
            )

    def _make_final_action(
        self,
        claude_decision: Dict[str, Any],
        validation: Dict[str, Any],
    ) -> Dict[str, str]:
        passed = validation.get("passed")
        claude_action = self._clean_code(claude_decision.get("decision", "NO_TRADE"))

        if passed is True:
            return {
                "final_action": "ENTER_NOW",
                "final_reason": validation.get("reason", "Validation passed."),
                "final_reason_code": validation.get("reason_code", "VALIDATION_PASSED"),
            }

        if passed is False:
            return {
                "final_action": "BLOCKED",
                "final_reason": validation.get("reason", "Python validation blocked trade."),
                "final_reason_code": validation.get("reason_code", "PYTHON_BLOCKED"),
            }

        if claude_action == "WAIT_PULLBACK":
            return {
                "final_action": "WAIT_PULLBACK",
                "final_reason": claude_decision.get("reason", validation.get("reason", "Waiting for pullback.")),
                "final_reason_code": validation.get("reason_code", "CLAUDE_REQUESTED_PULLBACK"),
            }

        return {
            "final_action": "NO_TRADE",
            "final_reason": claude_decision.get("reason", validation.get("reason", "No trade.")),
            "final_reason_code": validation.get("reason_code", "CLAUDE_DID_NOT_REQUEST_ENTRY"),
        }

    # ── Audit helpers ────────────────────────────────────────────────────────

    def _create_auditor(self, db_path: str) -> Optional[DecisionAuditor]:
        """
        Create DecisionAuditor safely across different constructor styles.

        Some versions expect a full db path.
        Some versions expect a log directory and create decision_audit.db inside it.
        """
        try:
            target = Path(db_path)

            # If caller gives ".../decision_audit.db", make sure parent exists.
            if target.suffix in {".db", ".sqlite", ".sqlite3"}:
                target.parent.mkdir(parents=True, exist_ok=True)

                # Clean up accidental directory created at the db file path.
                if target.exists() and target.is_dir():
                    shutil.rmtree(target)

                try:
                    return DecisionAuditor(db_path=str(target))
                except TypeError:
                    # Older DecisionAuditor likely expects a directory, not db_path kwarg.
                    return DecisionAuditor(str(target.parent))

            # If caller gives a directory, pass it through as directory.
            target.mkdir(parents=True, exist_ok=True)
            try:
                return DecisionAuditor(db_path=str(target / "decision_audit.db"))
            except TypeError:
                return DecisionAuditor(str(target))

        except Exception as e:
            print(f"[AI_PIPELINE] Could not create auditor safely: {e}")
            return None

    def _audit_decision(
        self,
        snapshot: Dict[str, Any],
        claude_decision: Dict[str, Any],
        validation: Dict[str, Any],
        final: Dict[str, Any],
    ) -> bool:
        if not self.config.audit_enabled or self.auditor is None:
            return False

        kwargs = {
            "market_snapshot": snapshot,
            "snapshot": snapshot,
            "claude_decision": claude_decision,
            "validation": validation,
            "python_validation": validation,
            "final_action": final.get("final_action"),
            "final_reason": final.get("final_reason"),
            "final_reason_code": final.get("final_reason_code"),
            "source": self.config.source,
        }

        try:
            self._call_with_supported_kwargs(self.auditor.log_decision, kwargs)
            return True
        except Exception as e:
            print(f"[AI_PIPELINE] Audit failed safely: {e}")
            return False

    def _call_with_supported_kwargs(self, func: Callable[..., Any], kwargs: Dict[str, Any]) -> Any:
        signature = inspect.signature(func)

        accepts_var_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in signature.parameters.values()
        )

        if accepts_var_kwargs:
            return func(**kwargs)

        supported = {
            key: value
            for key, value in kwargs.items()
            if key in signature.parameters
        }

        return func(**supported)

    # ── Utility helpers ──────────────────────────────────────────────────────

    def _no_trade_decision(self, reason: str) -> Dict[str, Any]:
        return {
            "decision": "NO_TRADE",
            "setup": "NONE",
            "confidence": "LOW",
            "entry_style": "NONE",
            "reason": reason,
            "risk_comment": "No execution considered.",
            "is_late_chase": False,
            "needs_pullback": False,
        }

    def _clean_code(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip().upper().replace(" ", "_")


# ── Mock reviewer + tests ────────────────────────────────────────────────────

class MockReviewer:
    """
    Deterministic fake Claude reviewer for local tests.
    """

    def review_setup(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        trigger = str(snapshot.get("trigger_direction", "NONE")).upper()

        if trigger == "LONG" and snapshot.get("breakout_confirmed"):
            return {
                "decision": "ENTER_NOW",
                "setup": "LONG",
                "confidence": "HIGH",
                "entry_style": "BREAKOUT",
                "reason": "Mock reviewer: bullish breakout confirmed.",
                "risk_comment": "Python must validate R:R and extension.",
                "is_late_chase": False,
                "needs_pullback": False,
            }

        if trigger == "SHORT" and snapshot.get("breakdown_confirmed"):
            return {
                "decision": "ENTER_NOW",
                "setup": "SHORT",
                "confidence": "HIGH",
                "entry_style": "BREAKOUT",
                "reason": "Mock reviewer: bearish breakdown confirmed.",
                "risk_comment": "Python must validate R:R and extension.",
                "is_late_chase": False,
                "needs_pullback": False,
            }

        return {
            "decision": "NO_TRADE",
            "setup": "NONE",
            "confidence": "LOW",
            "entry_style": "NONE",
            "reason": "Mock reviewer: no confirmed setup.",
            "risk_comment": "No trade.",
            "is_late_chase": False,
            "needs_pullback": False,
        }


def _clean_test_db(path: str) -> None:
    p = Path(path)

    if p.exists():
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()

    # SQLite WAL sidecars
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            if sidecar.is_dir():
                shutil.rmtree(sidecar)
            else:
                sidecar.unlink()


def _assert(name: str, condition: bool, result: Dict[str, Any]) -> None:
    if not condition:
        raise AssertionError(f"{name} failed. Result: {result}")


def _long_snapshot_ok() -> Dict[str, Any]:
    return {
        "instrument": "XAU_USD",
        "current_price": 4725.0,
        "session": "LONDON",
        "regime": "BULLISH",
        "daily_trend": "RANGING",
        "market_state": "BULLISH_TREND_IGNITION",
        "breakout_level": 4720.0,
        "next_resistance": 4785.0,
        "nearest_support": 4715.0,
        "level_name": "today_high",
        "trigger_direction": "LONG",
        "breakout_confirmed": True,
        "breakdown_confirmed": False,
        "candles_above_level": 3,
        "candles_below_level": 0,
        "consecutive_bullish_candles": 3,
        "consecutive_bearish_candles": 0,
        "ema_alignment": "bullish",
        "price_vs_ema50": "above",
        "extension_check": "OK - within 1.1x ATR of EMA9",
        "distance_from_entry": 5.0,
        "news_nearby": False,
    }


def _long_snapshot_extended() -> Dict[str, Any]:
    snap = _long_snapshot_ok()
    snap["extension_check"] = "EXTENDED - 2.0x ATR from EMA9"
    return snap


def _no_setup_snapshot() -> Dict[str, Any]:
    snap = _long_snapshot_ok()
    snap.update({
        "trigger_direction": "NONE",
        "breakout_confirmed": False,
        "breakdown_confirmed": False,
        "market_state": "RANGING",
    })
    return snap


def run_tests() -> None:
    print("=" * 80)
    print("TESTING AI TRADE PIPELINE")
    print("=" * 80)

    db_path = "logs/ai_pipeline_test/decision_audit.db"
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _clean_test_db(db_path)

    pipeline = AITradePipeline(
        config=AITradePipelineConfig(
            instrument="XAU_USD",
            use_real_claude=False,
            audit_db_path=db_path,
            source="ai_pipeline_test",
        ),
        reviewer=MockReviewer(),
    )

    tests = [
        ("Clean LONG enters", _long_snapshot_ok(), "ENTER_NOW", "VALIDATION_PASSED"),
        ("Extended LONG blocked", _long_snapshot_extended(), "BLOCKED", "PRICE_EXTENDED"),
        ("No setup no trade", _no_setup_snapshot(), "NO_TRADE", "CLAUDE_DID_NOT_REQUEST_ENTRY"),
    ]

    for i, (name, snapshot, expected_action, expected_code) in enumerate(tests, start=1):
        print(f"\n[TEST {i}] {name}")
        print("-" * 80)

        result = pipeline.evaluate_snapshot(snapshot)
        print(json.dumps({
            "final_action": result["final_action"],
            "final_reason_code": result["final_reason_code"],
            "audit_logged": result["audit_logged"],
            "claude_decision": result["claude_decision"],
            "python_validation": result["python_validation"],
        }, indent=2))

        _assert(name, result["final_action"] == expected_action, result)
        _assert(name, result["final_reason_code"] == expected_code, result)
        print(f"✅ {name}")

    auditor = pipeline._create_auditor(db_path)
    if auditor is None:
        raise AssertionError("Could not create DecisionAuditor for summary check.")

    summary = auditor.get_summary()

    print("\nAUDIT SUMMARY")
    print("-" * 80)
    print(json.dumps(summary, indent=2))

    _assert("Audit total decisions", summary["total_decisions"] == 3, summary)

    print("\n" + "=" * 80)
    print("AI TRADE PIPELINE TESTS COMPLETE")
    print("=" * 80)
    print(f"Audit DB saved to: {db_path}")


if __name__ == "__main__":
    run_tests()
