import json
import re
from pathlib import Path, PurePosixPath

from frappe_lt.inventory import _json_object

SCHEMA_VERSION = 1
ROOT = Path(__file__).parent
SCENARIOS_PATH = ROOT / "runtime_scenarios.json"
ROLE_PROFILES_PATH = ROOT / "runtime_role_profiles.json"
CLASSIFICATIONS_PATH = ROOT / "runtime_candidate_classifications.json"
MAX_CONTRACT_BYTES = 2 * 1024 * 1024
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,159}")
SAFE_CANDIDATE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:%-]{0,239}")


def _exact(value: object, fields: set[str], label: str) -> dict:
	if not isinstance(value, dict) or set(value) != fields:
		raise ValueError(f"{label} fields must be exactly {sorted(fields)}")
	return value


def _text(value: object, label: str, *, pattern=SAFE_ID) -> str:
	if not isinstance(value, str) or not value or (pattern is not None and not pattern.fullmatch(value)):
		raise ValueError(f"{label} must be valid nonempty text")
	return value


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
	if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
		raise ValueError(f"{label} must be an integer from {minimum} through {maximum}")
	return value


def load_json(path: Path) -> dict:
	try:
		content = path.read_bytes()
	except OSError as error:
		raise ValueError(f"could not read runtime contract {path.name}: {error}") from error
	if len(content) > MAX_CONTRACT_BYTES:
		raise ValueError(f"runtime contract {path.name} exceeds {MAX_CONTRACT_BYTES} bytes")
	if content.startswith(b"\xef\xbb\xbf") or b"\r\n" in content or not content.endswith(b"\n"):
		raise ValueError(f"runtime contract {path.name} is not canonical UTF-8/LF")
	try:
		return json.loads(
			content,
			object_pairs_hook=_json_object,
			parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON number {value}")),
		)
	except (UnicodeDecodeError, json.JSONDecodeError) as error:
		raise ValueError(f"invalid runtime contract {path.name}: {error}") from error


def _validate_profile(profile: object) -> dict:
	profile = _exact(
		profile,
		{
			"administrator",
			"defaults",
			"id",
			"language",
			"module_profile",
			"portal_link",
			"roles",
			"user_type",
		},
		"Runtime Role Profile",
	)
	_text(profile["id"], "Runtime Role Profile id")
	if not isinstance(profile["administrator"], bool):
		raise ValueError("Runtime Role Profile administrator must be boolean")
	if profile["language"] != "lt":
		raise ValueError("Runtime Role Profile language must be lt")
	if profile["user_type"] not in {"System User", "Website User"}:
		raise ValueError("Runtime Role Profile user_type is invalid")
	if profile["module_profile"] is not None and not isinstance(profile["module_profile"], str):
		raise ValueError("Runtime Role Profile module_profile must be text or null")
	if not isinstance(profile["defaults"], dict) or any(
		not isinstance(key, str) or not isinstance(value, str) for key, value in profile["defaults"].items()
	):
		raise ValueError("Runtime Role Profile defaults must be a text map")
	if (
		not isinstance(profile["roles"], list)
		or profile["roles"] != sorted(set(profile["roles"]))
		or any(not isinstance(role, str) or not role for role in profile["roles"])
	):
		raise ValueError("Runtime Role Profile roles must be unique sorted text")
	if profile["administrator"] and (profile["id"] != "administrator" or profile["roles"]):
		raise ValueError("Administrator must be a separate role-free profile")
	if not profile["administrator"] and not profile["roles"]:
		raise ValueError("normal Runtime Role Profile must specify exact roles")
	if profile["portal_link"] is not None:
		link = _exact(profile["portal_link"], {"doctype", "dynamic_link_doctype"}, "portal_link")
		_text(link["doctype"], "portal_link doctype", pattern=None)
		_text(link["dynamic_link_doctype"], "portal_link dynamic_link_doctype", pattern=None)
	return profile


def validate_role_profiles(value: object) -> dict:
	value = _exact(value, {"profiles", "schema_version"}, "Runtime Role Profiles")
	if value["schema_version"] != SCHEMA_VERSION or not isinstance(value["profiles"], list):
		raise ValueError("unsupported Runtime Role Profiles schema")
	profiles = [_validate_profile(profile) for profile in value["profiles"]]
	ids = [profile["id"] for profile in profiles]
	if len(ids) != len(set(ids)):
		raise ValueError("Runtime Role Profiles must have unique ids")
	value["profiles"] = sorted(profiles, key=lambda profile: profile["id"])
	return value


def _validate_scenario(scenario: object, profile_ids: set[str]) -> dict:
	scenario = _exact(
		scenario,
		{
			"candidate_id",
			"expected_exclusions",
			"fixture_id",
			"id",
			"kind",
			"mutability",
			"readiness",
			"role_profile_id",
			"scenario_timeout_ms",
			"step_timeout_ms",
			"target",
			"viewport",
		},
		"Runtime Scenario",
	)
	_text(scenario["id"], "Runtime Scenario id")
	_text(scenario["candidate_id"], "Runtime candidate id", pattern=SAFE_CANDIDATE_ID)
	if scenario["kind"] not in {"desk", "email", "portal", "print"}:
		raise ValueError("Runtime Scenario kind is invalid")
	if scenario["mutability"] not in {"mutable", "read_only"}:
		raise ValueError("Runtime Scenario mutability is invalid")
	if scenario["role_profile_id"] not in profile_ids:
		raise ValueError(f"unknown Runtime Role Profile {scenario['role_profile_id']!r}")
	if scenario["fixture_id"] is not None:
		_text(scenario["fixture_id"], "fixture_id")
	target = scenario["target"]
	if not isinstance(target, dict) or set(target) not in ({"route"}, {"output"}):
		raise ValueError("Runtime Scenario target must contain exactly route or output")
	target_field = next(iter(target))
	_text(target[target_field], f"Runtime Scenario {target_field}", pattern=None)
	if scenario["kind"] in {"email", "print"} and target_field != "output":
		raise ValueError("Runtime Output Scenario must specify output")
	if scenario["kind"] in {"desk", "portal"} and target_field != "route":
		raise ValueError("browser Runtime Scenario must specify route")
	readiness = _exact(scenario["readiness"], {"type", "value"}, "readiness")
	if readiness["type"] not in {"api", "output", "route"}:
		raise ValueError("Runtime Scenario readiness type is invalid")
	_text(readiness["value"], "Runtime Scenario readiness value", pattern=None)
	viewport = _exact(scenario["viewport"], {"height", "width"}, "viewport")
	_integer(viewport["width"], "viewport width", 320, 3840)
	_integer(viewport["height"], "viewport height", 480, 2160)
	step = _integer(scenario["step_timeout_ms"], "step_timeout_ms", 1000, 120000)
	scenario_timeout = _integer(scenario["scenario_timeout_ms"], "scenario_timeout_ms", step, 600000)
	if scenario_timeout < step:
		raise ValueError("scenario_timeout_ms must not be less than step_timeout_ms")
	if not isinstance(scenario["expected_exclusions"], list):
		raise ValueError("expected_exclusions must be a list")
	exclusion_ids = []
	for exclusion in scenario["expected_exclusions"]:
		exclusion = _exact(exclusion, {"id", "reason", "target"}, "Expected Runtime Exclusion")
		exclusion_ids.append(_text(exclusion["id"], "Expected Runtime Exclusion id"))
		_text(exclusion["reason"], "Expected Runtime Exclusion reason", pattern=None)
		_text(exclusion["target"], "Expected Runtime Exclusion target", pattern=None)
	if exclusion_ids != sorted(exclusion_ids) or len(exclusion_ids) != len(set(exclusion_ids)):
		raise ValueError("Expected Runtime Exclusions must have unique sorted ids")
	return scenario


def validate_scenarios(value: object, profile_ids: set[str]) -> dict:
	value = _exact(value, {"scenarios", "schema_version"}, "Runtime Scenario Manifest")
	if value["schema_version"] != SCHEMA_VERSION or not isinstance(value["scenarios"], list):
		raise ValueError("unsupported Runtime Scenario Manifest schema")
	scenarios = [_validate_scenario(scenario, profile_ids) for scenario in value["scenarios"]]
	ids = [scenario["id"] for scenario in scenarios]
	if len(ids) != len(set(ids)):
		raise ValueError("Runtime Scenarios must have unique ids")
	value["scenarios"] = sorted(scenarios, key=lambda scenario: scenario["id"])
	return value


def validate_classifications(value: object) -> dict:
	value = _exact(value, {"classifications", "schema_version"}, "candidate classifier")
	if value["schema_version"] != SCHEMA_VERSION or not isinstance(value["classifications"], list):
		raise ValueError("unsupported candidate classifier schema")
	ids = []
	for record in value["classifications"]:
		record = _exact(record, {"candidate_id", "reason", "reviewed_by"}, "candidate classification")
		ids.append(_text(record["candidate_id"], "classified candidate id", pattern=SAFE_CANDIDATE_ID))
		_text(record["reason"], "candidate classification reason", pattern=None)
		_text(record["reviewed_by"], "candidate classification reviewer", pattern=None)
	if len(ids) != len(set(ids)):
		raise ValueError("candidate classifications must have unique ids")
	value["classifications"] = sorted(value["classifications"], key=lambda record: record["candidate_id"])
	return value


def load_contracts(
	scenarios_path: Path = SCENARIOS_PATH,
	profiles_path: Path = ROLE_PROFILES_PATH,
	classifications_path: Path = CLASSIFICATIONS_PATH,
) -> dict:
	profiles = validate_role_profiles(load_json(profiles_path))
	profile_ids = {profile["id"] for profile in profiles["profiles"]}
	scenarios = validate_scenarios(load_json(scenarios_path), profile_ids)
	classifications = validate_classifications(load_json(classifications_path))
	return {
		"classifications": classifications,
		"profiles": profiles,
		"scenarios": scenarios,
	}


def safe_relative_path(value: object) -> PurePosixPath:
	if not isinstance(value, str) or not value or "\\" in value:
		raise ValueError("evidence path must be nonempty relative POSIX text")
	path = PurePosixPath(value)
	if path.is_absolute() or str(path) != value or any(part in {"", ".", ".."} for part in path.parts):
		raise ValueError(f"unsafe evidence path {value!r}")
	return path
