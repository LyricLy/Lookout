import math
from dataclasses import dataclass


@dataclass
class Winrate:
    s: int = 0
    n: int = 0

    def interval(self) -> tuple[float, float]:
        z = 3
        s = self.s
        n = self.n
        avg = s / n
        divisor = 1 + z*z / n
        return (avg + (z*z/(2*n))) / divisor, z/(2*n) * math.sqrt(4 * n * avg * (1-avg) + z*z) / divisor

    def centre(self) -> float:
        return self.interval()[0]

    def lower_bound(self) -> float:
        centre, radius = self.interval()
        return centre - radius

    def upper_bound(self) -> float:
        centre, radius = self.interval()
        return centre + radius

    def _ord_key(self) -> float:
        try:
            return self.lower_bound()
        except ZeroDivisionError:
            return float("-inf")

    def __str__(self) -> str:
        try:
            centre, radius = self.interval()
        except ZeroDivisionError:
            return "N/A (no games)"
        else:
            return f"{centre*100:.2f}% ± {radius*100:.2f}% ({self.s}/{self.n})"

    def __add__(self, other: Winrate) -> Winrate:
        return Winrate(self.s + other.s, self.n + other.n)

    def __sub__(self, other: Winrate) -> Winrate:
        return Winrate(self.s - other.s, self.n - other.n)

    def __lt__(self, other: Winrate) -> bool:
        if not isinstance(other, Winrate):
            return NotImplemented
        return self._ord_key() < other._ord_key()

    def __le__(self, other: Winrate) -> bool:
        if not isinstance(other, Winrate):
            return NotImplemented
        return self._ord_key() <= other._ord_key()

    def __gt__(self, other: Winrate) -> bool:
        if not isinstance(other, Winrate):
            return NotImplemented
        return self._ord_key() > other._ord_key()

    def __ge__(self, other: Winrate) -> bool:
        if not isinstance(other, Winrate):
            return NotImplemented
        return self._ord_key() >= other._ord_key()
