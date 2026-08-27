from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "src/firmquant/application/execution_evidence.py"
STORE = ROOT / "src/firmquant/application/promotion_store.py"
RUNTIME = ROOT / "src/firmquant/application/execution_evidence_runtime.py"
TEST = ROOT / "tests/unit/application/test_execution_evidence.py"
WORKFLOW = ROOT / ".github/workflows/immutable-planning-blockers.yml"


def replace_once(path: Path, old: str, new: str, *, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    EVIDENCE,
    "@dataclass(frozen=True, slots=True)\nclass OrderObservation:\n",
    '''@dataclass(frozen=True, slots=True)
class PlanningBlockerObservation:
    uquant_order_id: str
    symbol: str
    reason_code: str

    def __post_init__(self) -> None:
        _text(self.uquant_order_id, label="planning blocker order id")
        _text(self.symbol, label="planning blocker symbol")
        _text(self.reason_code, label="planning blocker reason code")

    def payload(self) -> dict[str, object]:
        return {
            "uquant_order_id": self.uquant_order_id,
            "symbol": self.symbol,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class OrderObservation:
''',
    label="blocker observation class",
)
replace_once(
    EVIDENCE,
    "    planned_orders: tuple[OrderObservation, ...]\n    targets: tuple[TargetObservation, ...]\n",
    "    planned_orders: tuple[OrderObservation, ...]\n"
    "    planning_blockers: tuple[PlanningBlockerObservation, ...]\n"
    "    targets: tuple[TargetObservation, ...]\n",
    label="blocker field",
)
replace_once(
    EVIDENCE,
    "            (\"planned orders\", self.planned_orders, OrderObservation),\n"
    "            (\"targets\", self.targets, TargetObservation),\n",
    "            (\"planned orders\", self.planned_orders, OrderObservation),\n"
    "            (\"planning blockers\", self.planning_blockers, PlanningBlockerObservation),\n"
    "            (\"targets\", self.targets, TargetObservation),\n",
    label="blocker validation",
)
replace_once(
    EVIDENCE,
    "            \"planned_orders\": [item.payload() for item in self.planned_orders],\n"
    "            \"targets\": [item.payload() for item in self.targets],\n",
    "            \"planned_orders\": [item.payload() for item in self.planned_orders],\n"
    "            \"planning_blockers\": [item.payload() for item in self.planning_blockers],\n"
    "            \"targets\": [item.payload() for item in self.targets],\n",
    label="blocker payload",
)
replace_once(
    EVIDENCE,
    "    \"PositionObservation\",\n    \"TargetObservation\",\n",
    "    \"PlanningBlockerObservation\",\n    \"PositionObservation\",\n    \"TargetObservation\",\n",
    label="blocker export",
)

replace_once(
    STORE,
    "    OrderObservation,\n    PositionObservation,\n",
    "    OrderObservation,\n    PlanningBlockerObservation,\n    PositionObservation,\n",
    label="store blocker import",
)
replace_once(
    STORE,
    "def _decode_target(payload: object) -> TargetObservation:\n",
    '''def _decode_planning_blocker(payload: object) -> PlanningBlockerObservation:
    value = _mapping(payload, label="planning blocker observation")
    return PlanningBlockerObservation(
        uquant_order_id=cast(str, _text(value.get("uquant_order_id"), label="blocker order id")),
        symbol=cast(str, _text(value.get("symbol"), label="blocker symbol")),
        reason_code=cast(str, _text(value.get("reason_code"), label="blocker reason code")),
    )


def _decode_target(payload: object) -> TargetObservation:
''',
    label="store blocker decoder",
)
replace_once(
    STORE,
    "        planned_orders=tuple(\n"
    "            _decode_order(item) for item in _array(value.get(\"planned_orders\"), label=\"planned orders\")\n"
    "        ),\n"
    "        targets=tuple(_decode_target(item) for item in _array(value.get(\"targets\"), label=\"targets\")),\n",
    "        planned_orders=tuple(\n"
    "            _decode_order(item) for item in _array(value.get(\"planned_orders\"), label=\"planned orders\")\n"
    "        ),\n"
    "        planning_blockers=tuple(\n"
    "            _decode_planning_blocker(item)\n"
    "            for item in _array(value.get(\"planning_blockers\"), label=\"planning blockers\")\n"
    "        ),\n"
    "        targets=tuple(_decode_target(item) for item in _array(value.get(\"targets\"), label=\"targets\")),\n",
    label="store blocker construction",
)

replace_once(
    RUNTIME,
    "    OrderObservation,\n    PositionObservation,\n",
    "    OrderObservation,\n    PlanningBlockerObservation,\n    PositionObservation,\n",
    label="runtime blocker import",
)
replace_once(
    RUNTIME,
    "def _positions(values: tuple[object, ...]) -> tuple[PositionObservation, ...]:\n",
    '''def _planning_blockers(plan: ExecutionPlan) -> tuple[PlanningBlockerObservation, ...]:
    return tuple(
        PlanningBlockerObservation(
            uquant_order_id=item.uquant_order_id,
            symbol=item.symbol,
            reason_code=item.reason_code,
        )
        for item in plan.blockers
    )


def _positions(values: tuple[object, ...]) -> tuple[PositionObservation, ...]:
''',
    label="runtime blocker helper",
)
replace_once(
    RUNTIME,
    "        planned_orders=tuple(order_observations),\n        targets=targets,\n",
    "        planned_orders=tuple(order_observations),\n"
    "        planning_blockers=_planning_blockers(plan),\n"
    "        targets=targets,\n",
    label="shadow blocker construction",
)
replace_once(
    RUNTIME,
    "        \"blockers\": [\n"
    "            {\n"
    "                \"uquant_order_id\": item.uquant_order_id,\n"
    "                \"symbol\": item.symbol.canonical,\n"
    "                \"reason_code\": item.reason_code,\n"
    "            }\n"
    "            for item in plan.blockers\n"
    "        ],\n",
    "        \"blockers\": [item.payload() for item in _planning_blockers(plan)],\n",
    label="canary blocker plan payload",
)
replace_once(
    RUNTIME,
    "def _plan_targets(payload: Mapping[str, object]) -> tuple[TargetObservation, ...]:\n",
    '''def _plan_blockers(payload: Mapping[str, object]) -> tuple[PlanningBlockerObservation, ...]:
    raw = payload.get("blockers")
    if not isinstance(raw, list):
        raise RuntimeEvidenceError("CANARY plan blockers are missing")
    blockers: list[PlanningBlockerObservation] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeEvidenceError("CANARY blocker evidence is malformed")
        blockers.append(
            PlanningBlockerObservation(
                uquant_order_id=str(item.get("uquant_order_id", "")),
                symbol=str(item.get("symbol", "")),
                reason_code=str(item.get("reason_code", "")),
            )
        )
    return tuple(blockers)


def _plan_targets(payload: Mapping[str, object]) -> tuple[TargetObservation, ...]:
''',
    label="canary blocker decoder",
)
replace_once(
    RUNTIME,
    "        planned_orders=tuple(order_observations),\n        targets=targets,\n        fills=tuple(fill_observations),\n",
    "        planned_orders=tuple(order_observations),\n"
    "        planning_blockers=_plan_blockers(plan),\n"
    "        targets=targets,\n"
    "        fills=tuple(fill_observations),\n",
    label="canary blocker construction",
)

replace_once(
    TEST,
    "    OrderObservation,\n    PositionObservation,\n",
    "    OrderObservation,\n    PlanningBlockerObservation,\n    PositionObservation,\n",
    label="test blocker import",
)
replace_once(
    TEST,
    "        planned_orders=(\n",
    "        planning_blockers=(\n"
    "            PlanningBlockerObservation(\n"
    "                uquant_order_id=\"uq-blocked\",\n"
    "                symbol=\"000001.SZ\",\n"
    "                reason_code=\"TARGET_ALREADY_SATISFIED\",\n"
    "            ),\n"
    "        ),\n"
    "        planned_orders=(\n",
    label="test blocker construction",
)
replace_once(
    TEST,
    "    assert aggregate.blocker_counts[BlockerCode.VOLUME_LIMIT] == 1\n",
    "    assert aggregate.blocker_counts[BlockerCode.VOLUME_LIMIT] == 1\n"
    "    assert observation.payload()[\"planning_blockers\"][0][\"reason_code\"] == \"TARGET_ALREADY_SATISFIED\"\n",
    label="test blocker assertion",
)

Path(__file__).unlink()
WORKFLOW.unlink()
