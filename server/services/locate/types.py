# !/usr/bin/env python
# -*-coding:utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class LocateResult:
    position: Optional[Tuple[int, int]] = None
    method: str = "none"
    detail: str = ""
    target_rect: Optional[Dict[str, Any]] = None
    debug: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.position is not None

    def to_click_extra(self) -> Dict[str, Any]:
        if not self.debug:
            return {}
        return {"locate_debug": self.debug}
