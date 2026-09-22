import hashlib
import re
from collections import Counter

from frappe_lt.inventory import canonical_json

REVIEW_EVIDENCE_SCHEMA_VERSION = 1
REASONS = (
	"accepted_as_is",
	"approved_translation_exception",
	"foreign_language_correction",
	"grammar_correction",
	"meaning_correction",
	"new_translation",
	"punctuation_correction",
	"terminology_correction",
)
CORRECTION_REASONS = frozenset(
	{
		"foreign_language_correction",
		"grammar_correction",
		"meaning_correction",
		"punctuation_correction",
		"terminology_correction",
	}
)
ORIGIN_BY_REASON = {
	"accepted_as_is": "inherited_v15",
	"approved_translation_exception": "approved_exception",
	"foreign_language_correction": "corrected_inherited",
	"grammar_correction": "corrected_inherited",
	"meaning_correction": "corrected_inherited",
	"new_translation": "new_ai",
	"punctuation_correction": "corrected_inherited",
	"terminology_correction": "corrected_inherited",
}
EVIDENCE_FIELDS = {"agent", "explanation", "model", "reason", "run_id", "status"}
PROVENANCE_FIELDS = {"origin", "review"}
SHA256 = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


def _preimage(entry):
	provenance = entry["provenance"]
	review = provenance["review"]
	return {
		"agent": review["agent"],
		"explanation": review["explanation"],
		"key": entry["key"],
		"model": review["model"],
		"origin": provenance["origin"],
		"reason": review["reason"],
		"source_digest": entry["source_digest"],
		"status": review["status"],
		"translation": entry["translation"],
	}


def review_run_id(entry):
	"""Hash only the documented repository-stable review evidence preimage."""
	return hashlib.sha256(canonical_json(_preimage(entry))).hexdigest()


def validate_review_evidence(entry):
	provenance = entry.get("provenance")
	if not isinstance(provenance, dict) or set(provenance) != PROVENANCE_FIELDS:
		raise ValueError("candidate provenance fields must be exact")
	review = provenance["review"]
	if not isinstance(review, dict) or set(review) != EVIDENCE_FIELDS:
		raise ValueError("Translation Review Evidence fields must be exact")
	if review["status"] != "reviewed":
		raise ValueError("Translation Review Evidence status must be 'reviewed'")
	reason = review["reason"]
	if not isinstance(reason, str) or reason not in ORIGIN_BY_REASON:
		raise ValueError("Translation Review Evidence reason is invalid")
	if provenance["origin"] != ORIGIN_BY_REASON[reason]:
		raise ValueError("candidate origin does not match Translation Review Evidence reason")
	for field in ("agent", "model"):
		value = review[field]
		if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
			raise ValueError(f"Translation Review Evidence {field} identifier is invalid")
	explanation = review["explanation"]
	if explanation is not None and (
		not isinstance(explanation, str)
		or not explanation.strip()
		or explanation != explanation.strip()
		or len(explanation) > 500
	):
		raise ValueError("Translation Review Evidence explanation is invalid")
	if reason in CORRECTION_REASONS | {"approved_translation_exception"} and explanation is None:
		raise ValueError(f"Translation Review Evidence reason {reason!r} requires an explanation")
	run_id = review["run_id"]
	if not isinstance(run_id, str) or SHA256.fullmatch(run_id) is None:
		raise ValueError("Translation Review Evidence run_id must be lowercase SHA-256")
	if run_id != review_run_id(entry):
		raise ValueError("Translation Review Evidence run_id does not match its canonical preimage")
	return reason


def evidence_summary(entries, expected_keys):
	reasons = Counter(validate_review_evidence(entry) for entry in entries)
	expected_keys = set(expected_keys)
	covered = {
		(entry["key"]["source"], entry["key"]["context"])
		for entry in entries
		if (
			bool(entry["translation"])
			or entry["provenance"]["review"]["reason"] == "approved_translation_exception"
		)
		and (entry["key"]["source"], entry["key"]["context"]) in expected_keys
	}
	return {
		"corrected_inherited": sum(reasons[reason] for reason in CORRECTION_REASONS),
		"reason_counts": {reason: reasons[reason] for reason in REASONS},
		"translation_coverage": {"covered": len(covered), "total": len(expected_keys)},
		"translation_exceptions": reasons["approved_translation_exception"],
	}
