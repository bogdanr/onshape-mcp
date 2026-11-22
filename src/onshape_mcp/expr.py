"""Read CAD expressions such as "58.8 mm", "#thickness / 2 + 3 mm" or "37 deg" without running code.

Values come back in millimetres (lengths) or radians (angles). A unit multiplies what stands before it, with
the same priority as * and /, which is how CAD dialogs read "206/2 mm": (206 / 2) mm.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

UNITS = {
    "mm": (1.0, "length"), "cm": (10.0, "length"), "m": (1000.0, "length"), "in": (25.4, "length"),
    "deg": (math.pi / 180, "angle"), "rad": (1.0, "angle"),
}
TOKEN = re.compile(r"\s*(?:(\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+)|#(\w+)|([A-Za-z]+)|(.))")


class ExpressionError(ValueError):
    pass


class UnknownVariable(ExpressionError):
    """The expression is well formed but uses a variable this tab does not define (it may live elsewhere)."""


@dataclass(frozen=True)
class Quantity:
    value: float
    kind: str  # "length" (mm), "angle" (rad) or "number"


def _combine(a: Quantity, b: Quantity, value: float) -> Quantity:
    return Quantity(value, a.kind if a.kind != "number" else b.kind)


class _Parser:
    def __init__(self, text: str, variables: dict[str, Quantity]):
        self.text, self.variables = text, variables
        self.tokens = [m.groups() for m in TOKEN.finditer(text) if m.group(0).strip()]
        self.at = 0

    def peek(self, symbol: str | None = None):
        token = self.tokens[self.at] if self.at < len(self.tokens) else None
        return token if symbol is None else (token is not None and token[3] == symbol)

    def take(self):
        self.at += 1
        return self.tokens[self.at - 1]

    def fail(self, why: str):
        raise ExpressionError(f"Cannot read \"{self.text}\": {why}")

    def expression(self) -> Quantity:
        left = self.term()
        while self.peek("+") or self.peek("-"):
            sign = 1 if self.take()[3] == "+" else -1
            right = self.term()
            if "number" not in (left.kind, right.kind) and left.kind != right.kind:
                self.fail("it adds a length to an angle")
            left = _combine(left, right, left.value + sign * right.value)
        return left

    def term(self) -> Quantity:
        left = self.unary()
        while True:
            token = self.peek()
            if token and token[3] in ("*", "/"):
                self.take()
                right = self.unary()
                if token[3] == "*":
                    left = _combine(left, right, left.value * right.value)
                elif right.value == 0:
                    self.fail("it divides by zero")
                elif left.kind == right.kind:
                    left = Quantity(left.value / right.value, "number")  # a length over a length is a ratio
                else:
                    left = _combine(left, right, left.value / right.value)
            elif token and token[2]:
                unit = self.unit(self.take()[2])
                left = _combine(left, unit, left.value * unit.value)
            else:
                return left

    def unit(self, name: str) -> Quantity:
        if name not in UNITS:
            self.fail(f"'{name}' is not a unit (use mm, cm, m, in, deg or rad)")
        return Quantity(*UNITS[name])

    def unary(self) -> Quantity:
        if self.peek("-"):
            self.take()
            inner = self.unary()
            return Quantity(-inner.value, inner.kind)
        number, variable, word, symbol = self.take() if self.peek() else self.fail("it ends too soon")
        if number:
            return Quantity(float(number), "number")
        if variable:
            if variable not in self.variables:
                known = ", ".join("#" + v for v in sorted(self.variables)) or "none"
                raise UnknownVariable(f"Cannot read \"{self.text}\": there is no variable #{variable} (known: {known})")
            return self.variables[variable]
        if word:
            return self.unit(word)
        if symbol == "(":
            inner = self.expression()
            if not self.peek(")"):
                self.fail("a bracket is not closed")
            self.take()
            return inner
        self.fail(f"unexpected '{symbol}'")


def evaluate(text: str, variables: dict[str, Quantity] | None = None) -> Quantity:
    parser = _Parser(str(text), variables or {})
    result = parser.expression()
    if parser.peek() is not None:
        parser.fail(f"unexpected '{''.join(t or '' for t in parser.peek())}'")
    return result


def length(text: str, variables: dict[str, Quantity] | None = None) -> float:
    """A length in millimetres. A bare number is refused: "20" could be millimetres or inches."""
    result = evaluate(text, variables)
    if result.kind != "length":
        raise ExpressionError(f"\"{text}\" needs a length unit, for example \"{text} mm\"")
    return result.value
