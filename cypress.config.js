const fs = require("fs");
const path = require("path");
const { defineConfig } = require("cypress");
const { EvidencePublisher, redactJson } = require("./cypress/support/runtime_evidence");
const { browserFromRunResults } = require("./cypress/support/runtime_toolchain");

const planPath = process.env.FRAPPE_LT_RUNTIME_PLAN;
const resultPath = process.env.FRAPPE_LT_RUNTIME_RESULT;
const runRoot = process.env.FRAPPE_LT_RUNTIME_ROOT;
if (!planPath || !resultPath || !runRoot) {
	throw new Error("runtime Cypress paths were not supplied by validate-lithuanian-runtime");
}
const plan = JSON.parse(fs.readFileSync(planPath, "utf8"));
const planFields = [
	"credentials",
	"diagnostic_sampling",
	"fixtures",
	"run_id",
	"schema_version",
	"scenarios",
	"token",
].sort();
if (
	!plan ||
	Array.isArray(plan) ||
	Object.keys(plan).sort().join("\0") !== planFields.join("\0") ||
	plan.schema_version !== 2 ||
	typeof plan.diagnostic_sampling !== "boolean"
) {
	throw new Error("unsupported or malformed runtime browser plan");
}
const results = new Map();

function canonical(value) {
	if (Array.isArray(value)) return value.map(canonical);
	if (value && typeof value === "object") {
		return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
	}
	return value;
}

module.exports = defineConfig({
	defaultCommandTimeout: 20000,
	pageLoadTimeout: 15000,
	retries: { runMode: 0, openMode: 0 },
	screenshotOnRunFailure: false,
	screenshotsFolder: path.join(runRoot, "evidence"),
	video: false,
	viewportHeight: 960,
	viewportWidth: 1400,
	e2e: {
		baseUrl: "http://development.localhost:8000",
		specPattern: "cypress/integration/ui_test_runtime_validation.js",
		supportFile: "cypress/support/e2e.js",
		testIsolation: false,
		setupNodeEvents(on, config) {
			config.env.runtimePlan = plan;
			const secrets = [plan.token];
			for (const credential of Object.values(plan.credentials)) {
				for (const value of Object.values(credential)) secrets.push(value);
			}
			const evidence = new EvidencePublisher(runRoot, {
				diagnosticSampling: plan.diagnostic_sampling,
				secrets,
			});
			on("task", {
				"runtime:record"(result) {
					const safeResult = redactJson(result, secrets);
					results.set(safeResult.id, safeResult);
					return null;
				},
				"runtime:publishEvidence"(request) {
					return evidence.publish(request);
				},
			});
			on("after:run", (runResults) => {
				const toolchain = canonical({
					browser: browserFromRunResults(runResults),
					cypress: { version: require("cypress/package.json").version },
					node: { version: process.version },
					plugins: {
						"@testing-library/cypress": require("@testing-library/cypress/package.json").version,
					},
					schema_version: 1,
				});
				const output = canonical({
					schema_version: 4,
					scenarios: [...results.values()].sort((left, right) =>
						left.id < right.id ? -1 : left.id > right.id ? 1 : 0
					),
					toolchain,
				});
				fs.writeFileSync(resultPath, JSON.stringify(output) + "\n", { mode: 0o600 });
			});
			return config;
		},
	},
});
