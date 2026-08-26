from __future__ import annotations

from pathlib import Path

SERVICES = Path("src/firmquant/application/production_services.py")
DAEMON = Path("src/firmquant/application/production_daemon.py")


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"{label}: expected exactly one anchor, got {text.count(old)}")
    return text.replace(old, new, 1)


def patch_services() -> None:
    text = SERVICES.read_text(encoding="utf-8")
    pairs = (
        ("from types import ModuleType\n", "", "ModuleType import"),
        (
            "from firmquant.risk.capability import WriteAuthorizationContext, WriteCapabilityFactory, WriteOperation\n",
            "from firmquant.risk.capability import (\n"
            "    BrokerWriteCapability,\n"
            "    WriteAuthorizationContext,\n"
            "    WriteCapabilityFactory,\n"
            "    WriteOperation,\n"
            ")\n",
            "capability import",
        ),
        ("from firmquant.strategy.account_sync import AccountStateContract\n", "", "account cast import"),
        (
            "from firmquant.strategy.adapter import DecisionRequest, StrategyAdapter\n",
            "from firmquant.strategy.adapter import DecisionRequest, ProductionEngineContract, StrategyAdapter\n",
            "engine protocol import",
        ),
        (
            "def _load_engine(source_checkout: Path, data_directory: Path) -> object:\n",
            "def _load_engine(source_checkout: Path, data_directory: Path) -> ProductionEngineContract:\n",
            "engine return type",
        ),
        ("        module = cast(ModuleType, current)\n", "        module = current\n", "redundant module cast"),
        (
            "    return engine_type(data_directory, config)\n",
            "    return cast(ProductionEngineContract, engine_type(data_directory, config))\n",
            "engine result cast",
        ),
        ("            cast(AccountStateContract, account),\n", "            account,\n", "account redundant cast"),
        (
            "            canonical_universe=frozenset(Symbol.parse(item) for item in self._universe.canonical_symbols),\n",
            "            canonical_universe=frozenset(Symbol.parse(item) for item in self._universe.base_symbols),\n",
            "canonical universe",
        ),
        (
            "    def _capability(self, authorities: _ExecutionAuthorities):\n",
            "    def _capability(self, authorities: _ExecutionAuthorities) -> BrokerWriteCapability:\n",
            "capability return type",
        ),
        (
            "    universe = UniversePolicy.from_uquant(configured_symbols=None)\n",
            "    observed_now = clock()\n"
            "    if observed_now.tzinfo is None or observed_now.utcoffset() is None:\n"
            "        raise ProductionServicesUnavailable(\"PRODUCTION_CLOCK_INVALID\")\n"
            "    universe = UniversePolicy.from_uquant(\n"
            "        configured_symbols=None,\n"
            "        as_of=observed_now.astimezone(_SHANGHAI).date(),\n"
            "    )\n",
            "universe as_of",
        ),
    )
    for old, new, label in pairs:
        text = replace_once(text, old, new, label=label)

    anchor = (
        "def _fraction(value: Decimal, denominator: Decimal) -> Decimal:\n"
        "    if denominator <= 0:\n"
        "        return Decimal(0)\n"
        "    return min(Decimal(1), max(Decimal(0), value / denominator))\n\n\n"
    )
    helper = anchor + (
        "def _count(value: object, *, label: str) -> int:\n"
        "    if value is None:\n"
        "        return 0\n"
        "    if isinstance(value, bool) or not isinstance(value, int) or value < 0:\n"
        "        raise ProductionServicesUnavailable(f\"{label}_INVALID\")\n"
        "    return value\n\n\n"
    )
    text = replace_once(text, anchor, helper, label="count helper")

    counts = (
        (
            "        attempts = int(\n"
            "            self._database.scalar(\n"
            "                \"SELECT count(*) FROM broker_order_attempts WHERE started_at >= ?\",\n"
            "                (snapshot.captured_at.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),),\n"
            "            )\n"
            "            or 0\n"
            "        )\n",
            "        attempts = _count(\n"
            "            self._database.scalar(\n"
            "                \"SELECT count(*) FROM broker_order_attempts WHERE started_at >= ?\",\n"
            "                (snapshot.captured_at.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),),\n"
            "            ),\n"
            "            label=\"BROKER_ATTEMPT_COUNT\",\n"
            "        )\n",
            "risk attempts",
        ),
        (
            "            open_order_count=int(\n"
            "                self._database.scalar(\n"
            "                    \"SELECT count(*) FROM execution_intents WHERE state IN \"\n"
            "                    \"('SUBMITTING','ACKNOWLEDGED','PARTIALLY_FILLED','CANCEL_REQUESTED')\"\n"
            "                )\n"
            "                or 0\n"
            "            ),\n",
            "            open_order_count=_count(\n"
            "                self._database.scalar(\n"
            "                    \"SELECT count(*) FROM execution_intents WHERE state IN \"\n"
            "                    \"('SUBMITTING','ACKNOWLEDGED','PARTIALLY_FILLED','CANCEL_REQUESTED')\"\n"
            "                ),\n"
            "                label=\"OPEN_ORDER_COUNT\",\n"
            "            ),\n",
            "open order count",
        ),
        (
            "            consecutive_rejections=int(\n"
            "                self._database.scalar(\n"
            "                    \"SELECT count(*) FROM execution_intents WHERE state = 'REJECTED' \"\n"
            "                    \"AND strategy_session = ?\",\n"
            "                    (planned.strategy_session.isoformat(),),\n"
            "                )\n"
            "                or 0\n"
            "            ),\n",
            "            consecutive_rejections=_count(\n"
            "                self._database.scalar(\n"
            "                    \"SELECT count(*) FROM execution_intents WHERE state = 'REJECTED' \"\n"
            "                    \"AND strategy_session = ?\",\n"
            "                    (planned.strategy_session.isoformat(),),\n"
            "                ),\n"
            "                label=\"CONSECUTIVE_REJECTION_COUNT\",\n"
            "            ),\n",
            "rejection count",
        ),
        (
            "            unresolved_order_count=int(\n"
            "                self._database.scalar(\n"
            "                    \"SELECT count(*) FROM execution_intents WHERE state IN \"\n"
            "                    \"('SUBMITTING','CANCEL_REQUESTED','UNKNOWN')\"\n"
            "                )\n"
            "                or 0\n"
            "            ),\n",
            "            unresolved_order_count=_count(\n"
            "                self._database.scalar(\n"
            "                    \"SELECT count(*) FROM execution_intents WHERE state IN \"\n"
            "                    \"('SUBMITTING','CANCEL_REQUESTED','UNKNOWN')\"\n"
            "                ),\n"
            "                label=\"UNRESOLVED_ORDER_COUNT\",\n"
            "            ),\n",
            "risk unresolved count",
        ),
        (
            "            attempts = int(\n"
            "                self._database.scalar(\n"
            "                    \"SELECT count(*) FROM broker_order_attempts WHERE started_at >= ?\",\n"
            "                    (snapshot.captured_at.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),),\n"
            "                )\n"
            "                or 0\n"
            "            )\n",
            "            attempts = _count(\n"
            "                self._database.scalar(\n"
            "                    \"SELECT count(*) FROM broker_order_attempts WHERE started_at >= ?\",\n"
            "                    (snapshot.captured_at.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),),\n"
            "                ),\n"
            "                label=\"BROKER_ATTEMPT_COUNT\",\n"
            "            )\n",
            "capability attempts",
        ),
        (
            "                unresolved_order_count=int(\n"
            "                    self._database.scalar(\n"
            "                        \"SELECT count(*) FROM execution_intents WHERE state IN ('CANCEL_REQUESTED','UNKNOWN')\"\n"
            "                    )\n"
            "                    or 0\n"
            "                ),\n",
            "                unresolved_order_count=_count(\n"
            "                    self._database.scalar(\n"
            "                        \"SELECT count(*) FROM execution_intents WHERE state IN ('CANCEL_REQUESTED','UNKNOWN')\"\n"
            "                    ),\n"
            "                    label=\"UNRESOLVED_ORDER_COUNT\",\n"
            "                ),\n",
            "capability unresolved count",
        ),
        (
            "                submitting_unresolved_count=int(\n"
            "                    self._database.scalar(\"SELECT count(*) FROM execution_intents WHERE state = 'SUBMITTING'\")\n"
            "                    or 0\n"
            "                ),\n",
            "                submitting_unresolved_count=_count(\n"
            "                    self._database.scalar(\"SELECT count(*) FROM execution_intents WHERE state = 'SUBMITTING'\"),\n"
            "                    label=\"SUBMITTING_ORDER_COUNT\",\n"
            "                ),\n",
            "submitting count",
        ),
        (
            "                unresolved_orders=int(\n"
            "                    self._database.scalar(\n"
            "                        \"SELECT count(*) FROM execution_intents WHERE state IN \"\n"
            "                        \"('SUBMITTING','CANCEL_REQUESTED','UNKNOWN')\"\n"
            "                    )\n"
            "                    or 0\n"
            "                ),\n",
            "                unresolved_orders=_count(\n"
            "                    self._database.scalar(\n"
            "                        \"SELECT count(*) FROM execution_intents WHERE state IN \"\n"
            "                        \"('SUBMITTING','CANCEL_REQUESTED','UNKNOWN')\"\n"
            "                    ),\n"
            "                    label=\"UNRESOLVED_ORDER_COUNT\",\n"
            "                ),\n",
            "shadow unresolved count",
        ),
        (
            "                duplicate_economic_orders=int(\n"
            "                    self._database.scalar(\n"
            "                        \"SELECT count(*) FROM (SELECT decision_id,uquant_order_id FROM execution_intents \"\n"
            "                        \"GROUP BY decision_id,uquant_order_id HAVING count(*) > 1)\"\n"
            "                    )\n"
            "                    or 0\n"
            "                ),\n",
            "                duplicate_economic_orders=_count(\n"
            "                    self._database.scalar(\n"
            "                        \"SELECT count(*) FROM (SELECT decision_id,uquant_order_id FROM execution_intents \"\n"
            "                        \"GROUP BY decision_id,uquant_order_id HAVING count(*) > 1)\"\n"
            "                    ),\n"
            "                    label=\"DUPLICATE_ECONOMIC_ORDER_COUNT\",\n"
            "                ),\n",
            "duplicate order count",
        ),
        (
            "                duplicate_fills=int(\n"
            "                    self._database.scalar(\n"
            "                        \"SELECT count(*) FROM (SELECT broker_fill_id FROM fills \"\n"
            "                        \"GROUP BY broker_fill_id HAVING count(*) > 1)\"\n"
            "                    )\n"
            "                    or 0\n"
            "                ),\n",
            "                duplicate_fills=_count(\n"
            "                    self._database.scalar(\n"
            "                        \"SELECT count(*) FROM (SELECT broker_fill_id FROM fills \"\n"
            "                        \"GROUP BY broker_fill_id HAVING count(*) > 1)\"\n"
            "                    ),\n"
            "                    label=\"DUPLICATE_FILL_COUNT\",\n"
            "                ),\n",
            "duplicate fill count",
        ),
    )
    for old, new, label in counts:
        text = replace_once(text, old, new, label=label)
    SERVICES.write_text(text, encoding="utf-8", newline="\n")


def patch_daemon() -> None:
    text = DAEMON.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "from firmquant.application.production_runtime import ProductionRuntime, ProductionRuntimeReceipt\n",
        "from firmquant.application.production_runtime import ProductionRuntime, ProductionRuntimeReceipt\n"
        "from firmquant.broker.gateway import BrokerEventSink\n",
        label="daemon event sink import",
    )
    text = replace_once(
        text,
        "    def subscribe(self, callback_sink: object) -> None: ...\n",
        "    def subscribe(self, callback_sink: BrokerEventSink) -> None: ...\n",
        label="daemon subscribe protocol",
    )
    DAEMON.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    patch_services()
    patch_daemon()
