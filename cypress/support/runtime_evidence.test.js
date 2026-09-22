const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { EvidencePublisher, redactJson } = require("./runtime_evidence");

function temporaryRun(t) {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), "frappe-lt-evidence-"));
	t.after(() => fs.rmSync(root, { recursive: true, force: true }));
	return root;
}

test("publishes deterministic evidence for failures but not passes by default", (t) => {
	const root = temporaryRun(t);
	const publisher = new EvidencePublisher(root, { diagnosticSampling: false });
	assert.deepEqual(
		publisher.publish({
			artifacts: [{ content: '{"safe":true}\n', kind: "browser", mime: "application/json" }],
			scenarioId: "desk-item-list",
			status: "pass",
		}),
		[]
	);
	const metadata = publisher.publish({
		artifacts: [{ content: '{"z":1,"a":2}', kind: "browser", mime: "application/json" }],
		scenarioId: "desk-item-list",
		status: "fail",
	});
	assert.equal(metadata.length, 1);
	assert.equal(metadata[0].path, "evidence/desk-item-list/browser.json");
	assert.equal(fs.readFileSync(path.join(root, metadata[0].path), "utf8"), '{"a":2,"z":1}\n');
});

test("diagnostic sampling explicitly publishes redacted pass evidence", (t) => {
	const root = temporaryRun(t);
	const secret = "reset-secret-value";
	const recipient = "runtime-user@invalid.example";
	const publisher = new EvidencePublisher(root, {
		diagnosticSampling: true,
		secrets: ["capability-secret"],
	});
	const metadata = publisher.publish({
		artifacts: [
			{
				content:
					`Authorization: Bearer capability-secret\nTo: ${recipient}\n` +
					`https://example.invalid/update-password%3Fkey%3D${encodeURIComponent(secret)}\n` +
					'<input type="hidden" name="csrf_token" value="csrf-secret">\n' +
					`frappe-lt-runtime-${"a".repeat(32)}-customer`,
				kind: "email",
				mime: "text/plain",
			},
		],
		scenarioId: "new-user-email",
		status: "pass",
	});
	const content = fs.readFileSync(path.join(root, metadata[0].path), "utf8");
	assert.equal(metadata[0].path, "evidence/new-user-email/email.txt");
	for (const sensitive of ["capability-secret", recipient, secret, "csrf-secret", "-customer"]) {
		assert.doesNotMatch(content, new RegExp(sensitive));
	}
	assert.match(content, /\[REDACTED\]/);
	assert.deepEqual(
		redactJson({ error: "token=capability-secret", recipient }, ["capability-secret"]),
		{ error: "token=[REDACTED]", recipient: "[REDACTED]" }
	);
});

test("rejects path traversal and symlinked evidence directories", (t) => {
	const root = temporaryRun(t);
	const outside = temporaryRun(t);
	const publisher = new EvidencePublisher(root);
	const artifact = [{ content: "{}", kind: "browser", mime: "application/json" }];
	assert.throws(
		() => publisher.publish({ artifacts: artifact, scenarioId: "../escape", status: "fail" }),
		/invalid scenario id/
	);
	fs.mkdirSync(path.join(root, "evidence"));
	fs.symlinkSync(outside, path.join(root, "evidence", "linked"));
	assert.throws(
		() => publisher.publish({ artifacts: artifact, scenarioId: "linked", status: "fail" }),
		/symlink/
	);
	assert.deepEqual(fs.readdirSync(outside), []);
});

test("enforces artifact, scenario, and run byte limits before publication", (t) => {
	const artifactRoot = temporaryRun(t);
	const artifactPublisher = new EvidencePublisher(artifactRoot, { maxArtifactBytes: 4 });
	assert.throws(
		() =>
			artifactPublisher.publish({
				artifacts: [{ content: "12345", kind: "browser", mime: "application/json" }],
				scenarioId: "artifact-limit",
				status: "fail",
			}),
		/artifact byte limit/
	);
	assert.equal(fs.existsSync(path.join(artifactRoot, "evidence")), false);

	const scenarioRoot = temporaryRun(t);
	const scenarioPublisher = new EvidencePublisher(scenarioRoot, {
		maxArtifactBytes: 10,
		maxScenarioBytes: 7,
	});
	assert.throws(
		() =>
			scenarioPublisher.publish({
				artifacts: [
					{ content: "1234", kind: "browser", mime: "application/json" },
					{ content: "5678", kind: "email", mime: "text/plain" },
				],
				scenarioId: "scenario-limit",
				status: "blocked",
			}),
		/scenario byte limit/
	);
	assert.equal(fs.existsSync(path.join(scenarioRoot, "evidence")), false);

	const runRoot = temporaryRun(t);
	const runPublisher = new EvidencePublisher(runRoot, {
		maxArtifactBytes: 10,
		maxRunBytes: 7,
		maxScenarioBytes: 10,
	});
	const first = runPublisher.publish({
		artifacts: [{ content: "1234", kind: "browser", mime: "application/json" }],
		scenarioId: "first",
		status: "fail",
	});
	assert.equal(first[0].sha256, require("node:crypto").createHash("sha256").update("1234\n").digest("hex"));
	assert.throws(
		() =>
			runPublisher.publish({
				artifacts: [{ content: "5678", kind: "browser", mime: "application/json" }],
				scenarioId: "second",
				status: "fail",
			}),
		/run byte limit/
	);
	assert.equal(fs.existsSync(path.join(runRoot, "evidence", "second")), false);
});

test("redaction and write failures abort publication", (t) => {
	const redactionRoot = temporaryRun(t);
	const publisher = new EvidencePublisher(redactionRoot);
	assert.throws(
		() =>
			publisher.publish({
				artifacts: [{ content: "invalid\0text", kind: "email", mime: "text/plain" }],
				scenarioId: "redaction-failure",
				status: "fail",
			}),
		/redaction safety check failed/
	);
	assert.equal(fs.existsSync(path.join(redactionRoot, "evidence")), false);

	const writeRoot = temporaryRun(t);
	fs.mkdirSync(path.join(writeRoot, "evidence", "write-failure"), { recursive: true });
	fs.writeFileSync(path.join(writeRoot, "evidence", "write-failure", "browser.json"), "existing");
	assert.throws(
		() =>
			new EvidencePublisher(writeRoot).publish({
				artifacts: [{ content: "{}", kind: "browser", mime: "application/json" }],
				scenarioId: "write-failure",
				status: "blocked",
			}),
		/EEXIST/
	);
	assert.equal(fs.readFileSync(path.join(writeRoot, "evidence", "write-failure", "browser.json"), "utf8"), "existing");
});
