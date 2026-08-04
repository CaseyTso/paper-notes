"""Legacy Obsidian migration: discovery and dry-run plans (Phase IV).

Task 17 scope: convert old title-folder / Zotero-linked layouts into
deterministic, reviewable migration plans WITHOUT writing to the vault.
Apply / verify / rollback land in Task 18.
"""

from .legacy import (
    LegacyDiscovery,
    LegacyItem,
    discover_legacy_items,
)
from .manifest import (
    RUN_ID_RE,
    MigrationPlan,
    build_migration_plan,
    default_state_root,
    new_run_id,
)

__all__ = [
    "RUN_ID_RE",
    "LegacyDiscovery",
    "LegacyItem",
    "MigrationPlan",
    "build_migration_plan",
    "default_state_root",
    "discover_legacy_items",
    "new_run_id",
]
