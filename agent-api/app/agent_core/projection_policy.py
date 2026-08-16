"""AgentViewの表示量だけを制御する、意味判断を持たないPolicy。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectionPolicy:
    material_max_items: int
    material_max_chars: int

    def __post_init__(self) -> None:
        if self.material_max_items < 1:
            raise ValueError("material_max_items must be positive")
        if self.material_max_chars < 1:
            raise ValueError("material_max_chars must be positive")

