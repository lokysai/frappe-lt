from copy import deepcopy
from unittest import TestCase

from frappe_lt.review_evidence import evidence_summary, review_run_id, validate_review_evidence


def _entry(reason, origin, explanation=None):
	entry = {
		"flags": [],
		"key": {"context": None, "source": reason},
		"provenance": {
			"origin": origin,
			"review": {
				"agent": "opencode",
				"explanation": explanation,
				"model": "openai/gpt-5.6-sol",
				"reason": reason,
				"run_id": "",
				"status": "reviewed",
			},
		},
		"source_digest": "a" * 64,
		"translation": f"lt:{reason}",
	}
	entry["provenance"]["review"]["run_id"] = review_run_id(entry)
	return entry


class TranslationReviewEvidenceTest(TestCase):
	def test_every_reason_has_an_explicit_origin_relation_and_report_total(self):
		entries = [
			_entry("accepted_as_is", "inherited_v15"),
			_entry("new_translation", "new_ai"),
			_entry("terminology_correction", "corrected_inherited", "Corrected terminology."),
			_entry("grammar_correction", "corrected_inherited", "Corrected grammar."),
			_entry("meaning_correction", "corrected_inherited", "Corrected meaning."),
			_entry("punctuation_correction", "corrected_inherited", "Corrected punctuation."),
			_entry(
				"foreign_language_correction",
				"corrected_inherited",
				"Removed a foreign-language fragment.",
			),
			_entry(
				"approved_translation_exception",
				"approved_exception",
				"Reviewed technical identifier remains unchanged.",
			),
		]
		for entry in entries:
			validate_review_evidence(entry)

		self.assertEqual(
			evidence_summary(
				entries,
				{(entry["key"]["source"], entry["key"]["context"]) for entry in entries},
			),
			{
				"corrected_inherited": 5,
				"reason_counts": {
					"accepted_as_is": 1,
					"approved_translation_exception": 1,
					"foreign_language_correction": 1,
					"grammar_correction": 1,
					"meaning_correction": 1,
					"new_translation": 1,
					"punctuation_correction": 1,
					"terminology_correction": 1,
				},
				"translation_coverage": {"covered": 8, "total": 8},
				"translation_exceptions": 1,
			},
		)

	def test_evidence_exact_fields_types_origin_and_conditional_explanation_are_fail_closed(self):
		valid = _entry("accepted_as_is", "inherited_v15")
		cases = {}
		for field in ("agent", "explanation", "model", "reason", "run_id", "status"):
			missing = deepcopy(valid)
			missing["provenance"]["review"].pop(field)
			cases[f"missing {field}"] = (missing, "fields")
		wrong = deepcopy(valid)
		wrong["provenance"]["review"]["agent"] = 1
		cases["agent"] = (wrong, "agent")
		wrong = deepcopy(valid)
		wrong["provenance"]["review"]["model"] = None
		cases["model"] = (wrong, "model")
		wrong = deepcopy(valid)
		wrong["provenance"]["review"]["explanation"] = 1
		cases["explanation"] = (wrong, "explanation")
		wrong = deepcopy(valid)
		wrong["provenance"]["review"]["status"] = 1
		cases["status"] = (wrong, "status")
		wrong = deepcopy(valid)
		wrong["provenance"]["review"]["reason"] = 1
		cases["reason"] = (wrong, "reason")
		wrong = deepcopy(valid)
		wrong["provenance"]["review"]["run_id"] = 1
		cases["run_id"] = (wrong, "run_id")
		wrong = deepcopy(valid)
		wrong["provenance"]["review"]["run_id"] = "A" * 64
		cases["lowercase"] = (wrong, "lowercase SHA-256")
		wrong = deepcopy(valid)
		wrong["provenance"]["review"]["unknown"] = "value"
		cases["unknown"] = (wrong, "fields")
		wrong = deepcopy(valid)
		wrong["provenance"]["origin"] = "new_ai"
		wrong["provenance"]["review"]["run_id"] = review_run_id(wrong)
		cases["origin"] = (wrong, "origin")
		correction = _entry("grammar_correction", "corrected_inherited", "Required.")
		correction["provenance"]["review"]["explanation"] = None
		correction["provenance"]["review"]["run_id"] = review_run_id(correction)
		cases["requires an explanation"] = (correction, "requires an explanation")
		for label, (entry, expected) in cases.items():
			with self.subTest(label=label), self.assertRaisesRegex(ValueError, expected):
				validate_review_evidence(entry)

	def test_run_id_hashes_only_the_documented_stable_review_preimage(self):
		entry = _entry("grammar_correction", "corrected_inherited", "Corrected grammar.")
		original = review_run_id(entry)
		reordered = deepcopy(entry)
		reordered["key"] = {"source": "grammar_correction", "context": None}
		reordered["provenance"]["review"]["run_id"] = "f" * 64
		reordered["flags"] = ["run-local-flag"]
		self.assertEqual(review_run_id(reordered), original)

		changes = {
			"agent": lambda value: value["provenance"]["review"].update(agent="other-agent"),
			"explanation": lambda value: value["provenance"]["review"].update(explanation="Other."),
			"key": lambda value: value.update(key={"context": "ctx", "source": "grammar_correction"}),
			"model": lambda value: value["provenance"]["review"].update(model="other/model"),
			"origin": lambda value: value["provenance"].update(origin="new_ai"),
			"reason": lambda value: value["provenance"]["review"].update(reason="meaning_correction"),
			"source_digest": lambda value: value.update(source_digest="b" * 64),
			"status": lambda value: value["provenance"]["review"].update(status="other"),
			"translation": lambda value: value.update(translation="Kitas"),
		}
		for field, change in changes.items():
			with self.subTest(field=field):
				changed = deepcopy(entry)
				change(changed)
				self.assertNotEqual(review_run_id(changed), original)
