"""Asset lifecycle and capital planning.

SPDX-License-Identifier: MIT
"""

from app.services.assets.capital import (
    DEFAULT_INFLATION,
    CapitalPlan,
    ForecastEntry,
    PlanYear,
    forecast_asset,
    inflate,
    plan_as_rows,
    plan_capital,
)
from app.services.assets.lifecycle import (
    REPAIR_VS_REPLACE_RATIO,
    RepairAdvice,
    WarrantyCheck,
    check_warranty,
    record_service,
    recover_under_warranty,
    repair_or_replace,
    retire_asset,
)
from app.services.assets.spaces import (
    MAX_DEPTH,
    SpaceNode,
    ancestors,
    assets_in,
    create_space,
    descendants,
    link_geometry,
    move_space,
    path_of,
    rolled_up_area,
    space_by_code,
    space_tree,
)

__all__ = [
    "DEFAULT_INFLATION",
    "MAX_DEPTH",
    "SpaceNode",
    "ancestors",
    "assets_in",
    "create_space",
    "descendants",
    "link_geometry",
    "move_space",
    "path_of",
    "rolled_up_area",
    "space_by_code",
    "space_tree",
    "CapitalPlan",
    "ForecastEntry",
    "PlanYear",
    "REPAIR_VS_REPLACE_RATIO",
    "forecast_asset",
    "inflate",
    "plan_as_rows",
    "plan_capital",
    "RepairAdvice",
    "WarrantyCheck",
    "check_warranty",
    "record_service",
    "recover_under_warranty",
    "repair_or_replace",
    "retire_asset",
]
