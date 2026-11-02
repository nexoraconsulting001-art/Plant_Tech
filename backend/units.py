"""The unit a product is sold in (the app's src/units.ts has the same list).

Pieces and packs are counted in whole numbers; weight, volume and length may be 1.5. Plants are always
pieces: a product in a plant category is saved as "pcs" whatever was asked. Products saved before units
existed have none and count as "pcs".
"""
import re
from typing import Literal

from errors import AppError

Unit = Literal["pcs", "bag", "packet", "tray", "kg", "g", "litre", "ml", "metre"]
DECIMAL_UNITS = {"kg", "g", "litre", "ml", "metre"}
SHORT = {"pcs": "pcs", "bag": "bag", "packet": "pkt", "tray": "tray",
         "kg": "kg", "g": "g", "litre": "L", "ml": "ml", "metre": "m"}  # as printed on the customer's bill
UNITS = set(SHORT)
_PLANT = re.compile(r"plant|succulent|cact|tree|shrub|herb|sapling|bonsai|पौध")


def is_plant_category(name: str | None) -> bool:
    """'Indoor Plants', 'Succulents', 'Cacti', 'Trees', 'पौधे'… (not Seeds, Pots, Soil)."""
    return bool(_PLANT.search((name or "").lower()))


def unit_of(value: str | None) -> str:
    return value if value in UNITS else "pcs"


def whole(unit: str | None) -> bool:
    return unit_of(unit) not in DECIMAL_UNITS


def check_quantity(unit: str | None, quantity: float, name: str):
    """Refuse 1.5 of something counted in pieces (the app rounds these; older apps only send whole numbers)."""
    if whole(unit) and abs(quantity - round(quantity)) > 1e-9:
        raise AppError(400, "{{name}} is sold in whole numbers. Please change the quantity.", "whole_quantity", name=name)


def count(line: dict) -> float:
    """How many items a bill line adds to "3 items": its quantity, or 1 for 1.5 kg of something."""
    return line["quantity"] if whole(line.get("unit")) else 1


def short(unit: str | None) -> str:
    return SHORT.get(unit or "", "")
