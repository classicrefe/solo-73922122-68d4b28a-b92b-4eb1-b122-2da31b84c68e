import unittest
from fractions import Fraction

from accounting import (
    Building,
    Report,
    combine_reports,
    format_decimal,
    parse_rational,
)


def buildings(*variances):
    return [Building(i + 1, Fraction(str(v))) for i, v in enumerate(variances)]


def report(report_id, nos, mean):
    return Report(report_id, tuple(nos), Fraction(str(mean)))


class ParseTests(unittest.TestCase):
    def test_exact_decimal_and_fraction(self):
        self.assertEqual(parse_rational("0.1"), Fraction(1, 10))
        self.assertEqual(parse_rational(".25"), Fraction(1, 4))
        self.assertEqual(parse_rational("1/3"), Fraction(1, 3))
        self.assertEqual(parse_rational("  -2/8 "), Fraction(-1, 4))
        self.assertEqual(parse_rational("2.5e1"), Fraction(25))

    def test_positive_variance(self):
        with self.assertRaises(ValueError):
            parse_rational("0", positive=True)
        with self.assertRaises(ValueError):
            parse_rational("-1", positive=True)
        with self.assertRaises(ValueError):
            parse_rational("not-a-number")

    def test_decimal_display(self):
        self.assertEqual(format_decimal(Fraction(1, 4)), "0.25")
        self.assertTrue(format_decimal(Fraction(1, 3)).startswith("0.333333"))


class CombineTests(unittest.TestCase):
    def test_single_equal_weight_report(self):
        result = combine_reports(buildings(1, 1), [report("R1", [1, 2], "1/2")])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.estimate, Fraction(1, 2))
        self.assertEqual(result.variance, Fraction(1, 2))

    def test_disjoint_reports_use_inverse_variance_weights(self):
        result = combine_reports(
            buildings(1, 4),
            [report("R1", [1], 10), report("R2", [2], 20)],
        )
        self.assertEqual(result.estimate, Fraction(12))
        self.assertEqual(result.variance, Fraction(4, 5))
        self.assertEqual(result.weights, [Fraction(4, 5), Fraction(1, 5)])

    def test_overlap_covariance_changes_result(self):
        result = combine_reports(
            buildings(1, 1, 1),
            [report("R1", [1, 2], 1), report("R2", [2, 3], 3)],
        )
        self.assertEqual(result.covariance[0][0], Fraction(1, 2))
        self.assertEqual(result.covariance[0][1], Fraction(1, 4))
        self.assertEqual(result.estimate, Fraction(2))
        self.assertEqual(result.variance, Fraction(3, 8))
        self.assertEqual(result.weights, [Fraction(1, 2), Fraction(1, 2)])

    def test_duplicate_report_does_not_improve_precision(self):
        bs = buildings(1, 4)
        one = combine_reports(bs, [report("R1", [1, 2], 3)])
        duplicated = combine_reports(
            bs,
            [report("R1", [1, 2], 3), report("R2", [1, 2], 3)],
        )
        self.assertEqual(one.variance, Fraction(5, 4))
        self.assertEqual(duplicated.variance, one.variance)
        self.assertEqual(duplicated.estimate, Fraction(3))

    def test_linear_combination_report_does_not_improve_precision(self):
        bs = buildings(1, 1, 1)
        base = combine_reports(
            bs,
            [report("R1", [1], 0), report("R2", [2], 2)],
        )
        with_redundant = combine_reports(
            bs,
            [
                report("R1", [1], 0),
                report("R2", [2], 2),
                report("R12", [1, 2], 1),
            ],
        )
        self.assertEqual(base.variance, Fraction(1, 2))
        self.assertEqual(with_redundant.variance, Fraction(1, 2))
        self.assertEqual(with_redundant.estimate, Fraction(1))

    def test_pair_conflict_lists_pair(self):
        result = combine_reports(
            buildings(1, 1),
            [report("R1", [1, 2], "1/2"), report("R2", [1, 2], "0.6")],
        )
        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.conflict_ids, ["R1", "R2"])
        self.assertEqual(result.minimal_conflicts, [["R1", "R2"]])
        self.assertIsNone(result.estimate)
        self.assertIsNone(result.variance)

    def test_three_report_dependency_conflict(self):
        result = combine_reports(
            buildings(1, 1, 1),
            [
                report("R1", [1], 0),
                report("R2", [2], 2),
                report("R12", [1, 2], 2),  # should be 1
            ],
        )
        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.conflict_ids, ["R1", "R2", "R12"])

    def test_four_report_average_relation_conflict(self):
        result = combine_reports(
            buildings(1, 1, 1),
            [
                report("R12", [1, 2], 1),
                report("R23", [2, 3], 2),
                report("R13", [1, 3], 3),
                report("R123", [1, 2, 3], 10),
            ],
        )
        self.assertEqual(result.status, "conflict")
        self.assertEqual(
            set(result.conflict_ids), {"R12", "R23", "R13", "R123"}
        )
        self.assertTrue(
            any(set(group) == {"R12", "R23", "R13", "R123"}
                for group in result.minimal_conflicts)
        )

    def test_decimal_and_fraction_are_exactly_compatible(self):
        result = combine_reports(
            buildings(1),
            [report("R1", [1], "0.1"), report("R2", [1], "1/10")],
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.estimate, Fraction(1, 10))


if __name__ == "__main__":
    unittest.main()
