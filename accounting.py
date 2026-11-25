"""Exact accounting mathematics for overlapping equal-weight building reports.

The module is intentionally independent of Flask and SQLite so the linear
algebra and conflict detection can be tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from fractions import Fraction
import re
from typing import Iterable, Sequence


class AccountingInputError(ValueError):
    """Raised when a caller-supplied accounting value cannot be used."""


@dataclass(frozen=True)
class Building:
    building_no: int
    variance: Fraction
    code: str = ""
    label: str = ""

    @property
    def display_code(self) -> str:
        return self.code or str(self.building_no)


@dataclass(frozen=True)
class Report:
    report_id: str
    building_nos: tuple[int, ...]
    mean: Fraction

    @property
    def coverage_count(self) -> int:
        return len(self.building_nos)

    @property
    def building_set(self) -> set[int]:
        return set(self.building_nos)


@dataclass
class AccountingResult:
    status: str
    report_ids: list[str] = field(default_factory=list)
    estimate: Fraction | None = None
    variance: Fraction | None = None
    weights: list[Fraction] = field(default_factory=list)
    covariance: list[list[Fraction]] = field(default_factory=list)
    conflict_ids: list[str] = field(default_factory=list)
    minimal_conflicts: list[list[str]] = field(default_factory=list)
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


_FRACTION_RE = re.compile(r"^\s*([+-]?\s*\d+)\s*/\s*(\d+)\s*$")
_DECIMAL_RE = re.compile(
    r"^\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*$"
)


def parse_rational(
    value: object,
    *,
    positive: bool = False,
    nonnegative: bool = False,
    field_name: str = "数值",
) -> Fraction:
    """Parse an exact decimal, integer, Decimal, or ``a/b`` fraction.

    Floats are converted through their shortest decimal representation to avoid
    treating ``0.1`` as the binary approximation of 1/10.
    """

    percent = False
    if isinstance(value, Fraction):
        result = value
    elif isinstance(value, Decimal):
        result = Fraction(value)
    elif isinstance(value, bool):
        raise AccountingInputError(f"{field_name}不能是布尔值")
    elif isinstance(value, int):
        result = Fraction(value)
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise AccountingInputError(f"{field_name}必须是有限数")
        result = Fraction(Decimal(str(value)))
    elif isinstance(value, str):
        text = value.strip().replace("／", "/")
        if not text:
            raise AccountingInputError(f"{field_name}不能为空")
        percent = text.endswith("%")
        if percent:
            text = text[:-1].strip()

        match = _FRACTION_RE.match(text)
        if match:
            numerator = match.group(1).replace(" ", "")
            denominator = match.group(2).replace(" ", "")
            try:
                den = int(denominator)
            except ValueError as exc:
                raise AccountingInputError(f"{field_name}格式不正确：{value}") from exc
            if den == 0:
                raise AccountingInputError(f"{field_name}分母不能为零")
            result = Fraction(int(numerator), den)
        elif _DECIMAL_RE.match(text):
            try:
                result = Fraction(Decimal(text))
            except (InvalidOperation, ValueError) as exc:
                raise AccountingInputError(f"{field_name}格式不正确：{value}") from exc
        else:
            # Fraction also accepts a few exact literal forms; keep the error
            # message domain-specific if that fails.
            try:
                result = Fraction(text)
            except (ValueError, ZeroDivisionError) as exc:
                raise AccountingInputError(
                    f"{field_name}必须是精确小数或分数，例如 0.25 或 1/4：{value}"
                ) from exc
    else:
        raise AccountingInputError(f"{field_name}必须是小数、整数或分数")

    if percent:
        result /= 100

    if positive and result <= 0:
        raise AccountingInputError(f"{field_name}必须为正数")
    if nonnegative and result < 0:
        raise AccountingInputError(f"{field_name}不能为负数")
    return result


def natural_key(value: str) -> list[object]:
    """Sort report numbers naturally: R2 before R10."""

    parts = re.split(r"(\d+)", str(value))
    return [int(part) if part.isdigit() else part.casefold() for part in parts]


def sorted_reports(reports: Sequence[Report]) -> list[Report]:
    return sorted(reports, key=lambda report: natural_key(report.report_id))


def format_fraction(value: Fraction | None) -> str:
    if value is None:
        return ""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def format_decimal(value: Fraction | None, places: int = 18) -> str:
    if value is None:
        return ""
    sign = "-" if value < 0 else ""
    value = abs(value)
    with __import__("decimal").localcontext() as ctx:
        ctx.prec = max(40, places * 2 + 10)
        decimal_value = Decimal(value.numerator) / Decimal(value.denominator)
        quantum = Decimal(1).scaleb(-places)
        rounded = decimal_value.quantize(quantum, rounding=ROUND_HALF_UP)
        text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text == "0":
        return "0"
    return sign + text


def fraction_to_float(value: Fraction | None) -> float | None:
    if value is None:
        return None
    return value.numerator / value.denominator


def build_incidence_matrix(
    building_count: int, reports: Sequence[Report]
) -> list[list[Fraction]]:
    matrix: list[list[Fraction]] = []
    for report in reports:
        if report.coverage_count == 0:
            raise AccountingInputError("报告至少须覆盖一栋楼")
        row = [Fraction(0) for _ in range(building_count)]
        weight = Fraction(1, report.coverage_count)
        for building_no in report.building_nos:
            index = building_no - 1
            if not 0 <= index < building_count:
                raise AccountingInputError(
                    f"报告 {report.report_id} 引用了不存在的楼号 {building_no}"
                )
            row[index] = weight
        matrix.append(row)
    return matrix


def system_is_feasible(
    rows: Sequence[Sequence[Fraction]], rhs: Sequence[Fraction]
) -> bool:
    """Return whether the exact rational linear system has a solution."""

    if not rows:
        return True
    variable_count = len(rows[0])
    matrix = [list(map(Fraction, row)) + [Fraction(value)] for row, value in zip(rows, rhs)]
    row_count = len(matrix)
    rank = 0

    for col in range(variable_count):
        pivot = next(
            (candidate for candidate in range(rank, row_count) if matrix[candidate][col] != 0),
            None,
        )
        if pivot is None:
            continue
        matrix[rank], matrix[pivot] = matrix[pivot], matrix[rank]
        pivot_value = matrix[rank][col]
        for j in range(col, variable_count + 1):
            matrix[rank][j] /= pivot_value

        for r in range(row_count):
            if r == rank or matrix[r][col] == 0:
                continue
            factor = matrix[r][col]
            for j in range(col, variable_count + 1):
                matrix[r][j] -= factor * matrix[rank][j]
        rank += 1

    return not any(
        all(matrix[r][c] == 0 for c in range(variable_count))
        and matrix[r][variable_count] != 0
        for r in range(row_count)
    )


def find_minimal_conflicts(
    matrix: Sequence[Sequence[Fraction]], means: Sequence[Fraction], ids: Sequence[str]
) -> list[list[str]]:
    """Find all inclusion-minimal infeasible report subsets.

    There are at most 12 reports, so enumerating 2**12 subsets is simple and
    gives an exact, understandable witness.
    """

    from itertools import combinations

    count = len(ids)
    minimal: list[tuple[int, ...]] = []

    def contains_known_conflict(indices: tuple[int, ...]) -> bool:
        selected = set(indices)
        return any(set(conflict).issubset(selected) for conflict in minimal)

    for size in range(1, count + 1):
        for indices in combinations(range(count), size):
            if contains_known_conflict(indices):
                continue
            subset_rows = [matrix[i] for i in indices]
            subset_rhs = [means[i] for i in indices]
            if not system_is_feasible(subset_rows, subset_rhs):
                minimal.append(indices)

    return [[ids[i] for i in indices] for indices in minimal]


def solve_consistent(
    coefficient_matrix: Sequence[Sequence[Fraction]], rhs: Sequence[Fraction]
) -> list[Fraction]:
    """Gaussian-Jordan solve for a known-consistent rational system.

    Free variables are set to zero. Callers dealing with singular covariance
    only use invariants that do not depend on the chosen solution.
    """

    n = len(coefficient_matrix)
    if n != len(rhs):
        raise AccountingInputError("线性方程组尺寸不一致")
    matrix = [
        [Fraction(coefficient_matrix[i][j]) for j in range(n)] + [Fraction(rhs[i])]
        for i in range(n)
    ]
    rank = 0
    pivot_columns: list[int] = []

    for col in range(n):
        pivot = next((r for r in range(rank, n) if matrix[r][col] != 0), None)
        if pivot is None:
            continue
        matrix[rank], matrix[pivot] = matrix[pivot], matrix[rank]
        pivot_value = matrix[rank][col]
        for j in range(n + 1):
            matrix[rank][j] /= pivot_value
        for r in range(n):
            if r == rank or matrix[r][col] == 0:
                continue
            factor = matrix[r][col]
            for j in range(n + 1):
                matrix[r][j] -= factor * matrix[rank][j]
        pivot_columns.append(col)
        rank += 1

    for row in matrix:
        if all(row[j] == 0 for j in range(n)) and row[n] != 0:
            raise ArithmeticError("协方差方程意外不一致")

    solution = [Fraction(0) for _ in range(n)]
    for row_index, col in enumerate(pivot_columns):
        solution[col] = matrix[row_index][n]
    return solution


def covariance_matrix(
    buildings: Sequence[Building], reports: Sequence[Report]
) -> list[list[Fraction]]:
    by_no = {building.building_no: building for building in buildings}
    count = len(reports)
    covariance = [[Fraction(0) for _ in range(count)] for _ in range(count)]
    sets = [report.building_set for report in reports]
    sizes = [report.coverage_count for report in reports]

    for i, report in enumerate(reports):
        own_variance = sum(
            (by_no[no].variance for no in report.building_nos), Fraction(0)
        )
        covariance[i][i] = own_variance / Fraction(sizes[i] * sizes[i])

    for i in range(count):
        for j in range(i + 1, count):
            shared_variance = sum(
                (by_no[no].variance for no in (sets[i] & sets[j])), Fraction(0)
            )
            value = shared_variance / Fraction(sizes[i] * sizes[j])
            covariance[i][j] = value
            covariance[j][i] = value

    return covariance


def combine_reports(
    buildings: Sequence[Building], unsorted_reports: Sequence[Report]
) -> AccountingResult:
    """Compute the exact BLUE or identify an inconsistent report subset."""

    reports = sorted_reports(unsorted_reports)
    ids = [report.report_id for report in reports]
    building_count = len(buildings)

    if building_count == 0:
        return AccountingResult(
            status="no_buildings",
            message="请先录入本轮各楼的正方差。",
        )
    if not reports:
        return AccountingResult(
            status="no_reports",
            message="尚未录入报告；录入后将自动合算。",
        )

    matrix = build_incidence_matrix(building_count, reports)
    means = [report.mean for report in reports]

    if system_is_feasible(matrix, means):
        conflicts: list[list[str]] = []
    else:
        conflicts = find_minimal_conflicts(matrix, means, ids)
        primary = conflicts[0] if conflicts else ids
        return AccountingResult(
            status="conflict",
            report_ids=ids,
            covariance=covariance_matrix(buildings, reports),
            conflict_ids=primary,
            minimal_conflicts=conflicts,
            message=(
                "这些均值不能由同一组楼宇估计值同时产生，"
                "已接收合规重报并撤下合算值。"
            ),
        )

    covariance = covariance_matrix(buildings, reports)
    ones = [Fraction(1) for _ in reports]

    # C a = 1. Since 1 = A * 1_buildings and C = A diag(var) A^T, 1 is in
    # range(C), including when redundant reports make C singular.
    a = solve_consistent(covariance, ones)
    precision = sum(a, Fraction(0))
    if precision <= 0:
        raise ArithmeticError("合算精度非正；请检查楼宇方差是否均为正数")

    weights = [value / precision for value in a]
    estimate = sum((weight * mean for weight, mean in zip(weights, means)), Fraction(0))
    variance = Fraction(1) / precision

    return AccountingResult(
        status="ok",
        report_ids=ids,
        estimate=estimate,
        variance=variance,
        weights=weights,
        covariance=covariance,
        message="已按共用楼宇协方差求得最小方差线性无偏估计。",
    )


def result_to_dict(result: AccountingResult) -> dict[str, object]:
    """Convert an AccountingResult to JSON/SQLite-friendly data."""

    def frac(value: Fraction | None) -> dict[str, object] | None:
        if value is None:
            return None
        return {
            "fraction": format_fraction(value),
            "numerator": value.numerator,
            "denominator": value.denominator,
            "decimal": format_decimal(value),
            "float": fraction_to_float(value),
        }

    return {
        "status": result.status,
        "ok": result.ok,
        "message": result.message,
        "report_ids": list(result.report_ids),
        "estimate": format_fraction(result.estimate),
        "estimate_info": frac(result.estimate),
        "estimate_float": fraction_to_float(result.estimate),
        "estimate_decimal": format_decimal(result.estimate),
        "variance": format_fraction(result.variance),
        "variance_info": frac(result.variance),
        "variance_float": fraction_to_float(result.variance),
        "variance_decimal": format_decimal(result.variance),
        "weights": [format_fraction(w) for w in result.weights],
        "weights_float": [fraction_to_float(w) for w in result.weights],
        "covariance": [[format_fraction(v) for v in row] for row in result.covariance],
        "covariance_float": [
            [fraction_to_float(v) for v in row] for row in result.covariance
        ],
        "conflict_ids": list(result.conflict_ids),
        "minimal_conflicts": [list(group) for group in result.minimal_conflicts],
    }
