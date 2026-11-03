"""Discord-free helpers: duration parsing, a safe calculator and dice."""
from __future__ import annotations

import ast
import math
import operator
import random
import re
from datetime import timedelta
from typing import Optional

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_TOKEN = re.compile(r"(\d{1,6})([smhdw])")
_FULL = re.compile(r"(?:\d{1,6}[smhdw])+")


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    parts = []
    for label, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{label}")
            seconds %= size
    return " ".join(parts) or "0s"


def parse_duration(
    text: str, *, min_seconds: int = 1, max_seconds: Optional[int] = None
) -> timedelta:
    """Parse things like ``30m``, ``2h``, ``1d12h``, ``1w`` into a timedelta."""
    cleaned = re.sub(r"\s+", "", (text or "").lower())
    if not _FULL.fullmatch(cleaned):
        raise ValueError("Use a duration like `30m`, `2h`, `1d12h` or `1w`.")
    total = sum(int(n) * _UNIT_SECONDS[u] for n, u in _TOKEN.findall(cleaned))
    if total < min_seconds:
        raise ValueError(f"Duration must be at least {format_duration(min_seconds)}.")
    if max_seconds is not None and total > max_seconds:
        raise ValueError(f"Duration can't be longer than {format_duration(max_seconds)}.")
    return timedelta(seconds=total)


# ---------------------------------------------------------------- calculator

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "log": math.log,
    "ln": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "deg": math.degrees,
    "rad": math.radians,
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def safe_calc(expression: str) -> float:
    """Evaluate arithmetic without ``eval``: only a whitelist of AST nodes runs."""
    if len(expression) > 200:
        raise ValueError("That expression is too long.")
    try:
        tree = ast.parse(expression.strip().replace("^", "**"), mode="eval")
    except SyntaxError:
        raise ValueError("I couldn't parse that expression.") from None

    def ev(node, depth=0):
        if depth > 40:
            raise ValueError("That expression is nested too deeply.")
        if isinstance(node, ast.Expression):
            return ev(node.body, depth + 1)
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)
        ):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left, right = ev(node.left, depth + 1), ev(node.right, depth + 1)
            if isinstance(node.op, ast.Pow) and abs(right) > 10000:
                raise ValueError("That exponent is too large.")
            return _BIN_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](ev(node.operand, depth + 1))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCS
            and not node.keywords
            and 1 <= len(node.args) <= 2
        ):
            return _FUNCS[node.func.id](*[ev(a, depth + 1) for a in node.args])
        raise ValueError("Only numbers, + - * / // % ^, parentheses and basic math functions are allowed.")

    try:
        result = ev(tree)
    except ZeroDivisionError:
        raise ValueError("Division by zero.") from None
    except OverflowError:
        raise ValueError("That result is too large.") from None
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc):
            raise
        raise ValueError("That isn't a valid calculation.") from None
    if isinstance(result, complex) or not math.isfinite(result):
        raise ValueError("The result isn't a real, finite number.")
    return result


def format_number(value: float) -> str:
    if float(value).is_integer() and abs(value) < 1e15:
        return f"{int(value):,}"
    return f"{value:.10g}"


# ---------------------------------------------------------------------- dice

_DICE = re.compile(r"^\s*(\d{0,3})\s*d\s*(\d{1,4})\s*(?:([+-])\s*(\d{1,6}))?\s*$", re.I)
_rng = random.SystemRandom()


def roll_dice(notation: str) -> tuple:
    """Return (rolls, modifier, total) for notation such as ``2d6+3``."""
    m = _DICE.match(notation or "")
    if not m:
        raise ValueError("Use dice notation like `d20`, `2d6` or `4d8+3`.")
    count = int(m.group(1) or 1)
    sides = int(m.group(2))
    if not 1 <= count <= 100:
        raise ValueError("Roll between 1 and 100 dice.")
    if not 2 <= sides <= 1000:
        raise ValueError("Dice need between 2 and 1000 sides.")
    modifier = int(m.group(4) or 0) * (-1 if m.group(3) == "-" else 1)
    rolls = [_rng.randint(1, sides) for _ in range(count)]
    return rolls, modifier, sum(rolls) + modifier
