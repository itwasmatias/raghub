"""RAGHub AI Controller Mission Orchestrator."""

from .models import (
    SCHEMA_VERSION,
    MissionDefinition,
    MissionTaskDefinition,
    MilestoneDefinition,
    MissionBudget,
    MissionState,
    MissionTaskState,
    BudgetUsage,
    MissionEvent,
    MissionStatus,
    MissionTaskStatus,
    FailurePolicy,
    ApprovalPolicy,
    ProviderPolicy,
    ValidationError,
    ValidationResult,
)
from .validation import validate_mission
from .graph import DependencyGraph
from .events import MissionEventLog, make_event
from .store import MissionStore
from .materializer import TaskMaterializer, make_legacy_queue_task_id
from .scheduler import MissionScheduler
from .reporter import generate_mission_report
from .planner import outline_to_mission, load_outline_file, save_mission_file
from .repair import (
    RepairAction,
    RepairActionKind,
    RepairApplicationResult,
    RepairApplyStatus,
    RepairClassification,
    apply_mission_repair,
    plan_mission_repairs,
)

__all__ = [
    "SCHEMA_VERSION",
    "MissionDefinition", "MissionTaskDefinition", "MilestoneDefinition",
    "MissionBudget", "MissionState", "MissionTaskState", "BudgetUsage",
    "MissionEvent", "MissionStatus", "MissionTaskStatus",
    "FailurePolicy", "ApprovalPolicy", "ProviderPolicy",
    "ValidationError", "ValidationResult",
    "validate_mission", "DependencyGraph",
    "MissionEventLog", "make_event",
    "MissionStore", "TaskMaterializer", "make_legacy_queue_task_id", "MissionScheduler",
    "generate_mission_report",
    "outline_to_mission", "load_outline_file", "save_mission_file",
    "RepairAction", "RepairActionKind", "RepairApplicationResult",
    "RepairApplyStatus", "RepairClassification",
    "apply_mission_repair", "plan_mission_repairs",
]
