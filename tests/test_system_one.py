import json
import math
import unittest
from dataclasses import replace
from pathlib import Path

from motor.models import Action, Candidate, Node, Selector, State
from motor.system_one import prepare_input, rank, validate_scores


def make_state(excluded=False):
    return State("app.test", "device", "1", "snapshot", (
        Node("title", text="Home", bounds=(0, 0, 100, 40)),
        Node("edit", resource_id="app:password", text="secret-value", role="edit", editable=True,
             clickable=True, bounds=(0, 50, 200, 100)),
        Node("tab", resource_id="app:tab", text="Products", role="tab", clickable=True,
             selected=True, checkable=True, checked=True, bounds=(100, 100, 200, 150)),
        Node("scroll", resource_id="app:list", role="list", scrollable=True,
             bounds=(0, 150, 200, 300)),
    ), width=200, height=400, excluded=excluded)


def candidates():
    return (Candidate(Action("CLICK", Selector(resource_id="app:tab", text="Products", role="tab"))),
            Candidate(Action("CLICK", Selector(resource_id="app:password", text="secret-value", role="edit"))))


class ReverseScorer:
    def score(self, state, items):
        self.state = state
        self.items = items
        return (replace(items[1], score=0.9, confidence=0.8, source="system_one"),
                replace(items[0], score=0.2, confidence=0.7, source="system_one"))


class SystemOneTests(unittest.TestCase):
    def test_prepare_input_normalizes_bounds_and_redacts_editable_values(self):
        prepared = prepare_input(make_state(), candidates())
        self.assertEqual(prepared["schema_version"], "daxqo.motor.policy/0.1")
        self.assertEqual(prepared["controls"][1]["bounds"], [0.5, 0.25, 1.0, 0.375])
        self.assertTrue(prepared["controls"][1]["selected"])
        self.assertTrue(prepared["controls"][1]["checkable"])
        self.assertEqual(prepared["controls"][2]["text"], "")
        self.assertNotIn("secret-value", json.dumps(prepared))
        self.assertEqual(len(prepared["candidates"]), 2)
        self.assertNotIn("id", prepared["candidates"][0]["action"])
        self.assertNotIn("secret-value", json.dumps(prepared["candidates"][1]))

    def test_excluded_state_rejected(self):
        with self.assertRaisesRegex(ValueError, "Excluded"):
            prepare_input(make_state(excluded=True), candidates())
        with self.assertRaisesRegex(ValueError, "Excluded"):
            rank(make_state(excluded=True), candidates())

    def test_validation_rejects_nan_unknown_duplicate_and_missing_actions(self):
        original = candidates()
        with self.assertRaisesRegex(ValueError, "finite"):
            validate_scores(original, (replace(original[0], score=math.nan), original[1]))
        with self.assertRaisesRegex(ValueError, "unknown"):
            validate_scores(original, (Candidate(Action("BACK")), original[1]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_scores(original, (original[0], original[0]))
        with self.assertRaisesRegex(ValueError, "every candidate"):
            validate_scores(original, original[:1])
        with self.assertRaisesRegex(ValueError, "confidence"):
            validate_scores(original, (replace(original[0], confidence=1.1), original[1]))
        duplicate_original = (original[0], replace(original[0], action=replace(original[0].action, id="new-id")))
        with self.assertRaisesRegex(ValueError, "Original candidates"):
            validate_scores(duplicate_original, duplicate_original)

    def test_rank_rejects_duplicate_inputs_before_calling_scorer(self):
        class NeverCall:
            def score(self, state, items):
                raise AssertionError("scorer must not receive duplicate candidates")
        values = candidates()
        duplicate = (values[0], replace(values[0], action=replace(values[0].action, id="another")))
        with self.assertRaisesRegex(ValueError, "Original candidates"):
            rank(make_state(), duplicate, NeverCall())

    def test_rank_uses_sanitized_state_and_returns_validated_reordering(self):
        scorer = ReverseScorer()
        decision = rank(make_state(), candidates(), scorer)
        self.assertEqual(scorer.state.nodes[1].text, "")
        self.assertEqual(scorer.items[1].action.selector.text, "")
        result = decision.public()
        self.assertEqual(result["source"], "system_one")
        self.assertEqual(result["chosen_action"]["selector"]["resource_id"], "app:password")
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertEqual(result["candidate_actions"][0]["score"], 0.9)

    def test_default_rank_preserves_reflex_order(self):
        result = rank(make_state(), candidates()).public()
        self.assertEqual(result["source"], "reflex")
        self.assertEqual(result["chosen_action"]["selector"]["resource_id"], "app:tab")

    def test_schema_matches_prepared_input(self):
        schema_path = Path(__file__).resolve().parents[1] / "schema" / "motor-decision-v0.1.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        prepared = prepare_input(make_state(), candidates())
        self.assertEqual(schema["properties"]["schema_version"]["const"], prepared["schema_version"])
        self.assertEqual(set(prepared), set(schema["required"]))


if __name__ == "__main__":
    unittest.main()
