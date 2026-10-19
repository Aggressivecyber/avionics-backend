import unittest

from ground_station.relations import Expression, RelationEngine


def sample(source, value, time_ns, **changes):
    result = {"source": source, "parameter": "value", "value": value, "unit": "", "time_ns": time_ns,
        "time_group": "host", "usable": True, "quality": [], "record_index": time_ns,
        "generation": 1, "origin_generation": 1, "recording_session_id": "session-a",
        "origin_session_id": None, "configuration_sha256": "config-a", "uncertainty_ns": 0, "max_age_ns": 0}
    result.update(changes)
    return result


BINDINGS = [{"alias": alias, "source": source, "parameter": "value"}
            for alias, source in [("A", "left"), ("B", "right"), ("C", "third")]]


class RelationTests(unittest.TestCase):
    def test_arithmetic_boolean_and_alias_dependencies(self):
        engine = RelationEngine()
        difference = engine.add("温差", "A-B", BINDINGS, "degC", 5_000_000)
        condition = engine.add("条件", "A+B>C and abs(A-B)<=1", BINDINGS, "", 5_000_000)
        engine.feed([sample("left", 20, 10_000_000), sample("right", 20.5, 12_000_000)])
        self.assertEqual(difference.value, -.5)
        self.assertEqual(len(difference.bindings), 2)
        self.assertIsNone(condition.value)
        engine.feed([sample("third", 30, 13_000_000)])
        self.assertIs(condition.value, True)
        self.assertEqual(condition.time_ns, 13_000_000)
        self.assertEqual(len(condition.evidence), 3)

    def test_window_and_clock_group_must_match(self):
        engine = RelationEngine()
        relation = engine.add("差值", "A-B", BINDINGS, "", 5)
        engine.feed([sample("left", 1, 10), sample("right", 2, 16)])
        self.assertIsNone(relation.value)
        self.assertIn("窗口", relation.status)
        engine.feed([sample("right", 2, 11, time_group="device")])
        engine.feed([sample("left", 1, 12)])
        self.assertIsNone(relation.value)
        self.assertIn("时基", relation.status)

    def test_quality_and_display_gap_do_not_reuse_old_values(self):
        engine = RelationEngine()
        relation = engine.add("差值", "A-B", BINDINGS, "", 100)
        engine.feed([sample("left", 1, 10), sample("right", 2, 11)])
        self.assertEqual(relation.value, -1)
        engine.feed([sample("left", 3, 12, usable=False, quality=["late_for_group"])])
        self.assertIsNone(relation.value)
        engine.feed([sample("right", 5, 13)])
        self.assertIsNone(relation.value)
        engine.feed([sample("left", 3, 14)])
        self.assertEqual(relation.value, -2)
        engine.feed([sample("left", 7, 15)], omitted=1)
        self.assertIsNone(relation.value)
        self.assertTrue(any(value is None for _, value in relation.history))

    def test_generation_and_session_boundary_reset_inputs(self):
        engine = RelationEngine()
        relation = engine.add("差值", "A-B", BINDINGS, "", 100)
        engine.feed([sample("left", 1, 10), sample("right", 2, 11)])
        engine.feed([sample("left", 100, 20, generation=2)])
        self.assertIsNone(relation.value)
        engine.feed([sample("right", 90, 21)])
        self.assertEqual(relation.value, 10)
        self.assertEqual(relation.origin_ns, 21)
        engine.reset()
        self.assertEqual(len(engine.relations), 1)
        self.assertEqual(len(relation.history), 0)
        self.assertIsNone(relation.value)

    def test_zero_divisor_is_visible_and_recovers(self):
        engine = RelationEngine()
        relation = engine.add("比值", "A/B", BINDINGS, "", 100)
        engine.feed([sample("left", 1, 10), sample("right", 0, 11)])
        self.assertIn("除数", relation.status)
        self.assertIsNone(relation.value)
        engine.feed([sample("right", 2, 12)])
        self.assertEqual(relation.value, .5)

    def test_uncertainty_and_parameter_validity_limit_alignment(self):
        engine = RelationEngine()
        relation = engine.add("差值", "A-B", BINDINGS, "", 5)
        engine.feed([sample("left", 1, 10, uncertainty_ns=3), sample("right", 2, 11, uncertainty_ns=3)])
        self.assertIsNone(relation.value)
        self.assertIn("窗口", relation.status)
        engine.feed([sample("left", 1, 20, max_age_ns=1), sample("right", 2, 22)])
        self.assertIsNone(relation.value)
        self.assertIn("有效时间", relation.status)

    def test_expression_cannot_execute_code(self):
        for text in ["__import__('os').system('x')", "A.__class__", "[A][0]", "A**100", "lambda: A", "Z+1", "1e999"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                Expression(text, ["A", "B"])
        self.assertIs(Expression("B != 0 and A/B > 1", ["A", "B"]).evaluate({"A": 5, "B": 0}), False)

    def test_names_capacity_and_bounded_history(self):
        engine = RelationEngine()
        relation = engine.add("关系", "A*2", BINDINGS, "", 100)
        for i in range(700):
            engine.feed([sample("left", i, i)])
        self.assertEqual(len(relation.history), 512)
        with self.assertRaises(ValueError):
            engine.add(" 关系 ", "A", BINDINGS, "", 100)
        engine.remove(relation)
        engine.feed([sample("left", 1, 800)])
        self.assertEqual(len(engine.relations), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
