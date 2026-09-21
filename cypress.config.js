const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const { defineConfig } = require("cypress");

const planPath = process.env.FRAPPE_LT_RUNTIME_PLAN;
const resultPath = process.env.FRAPPE_LT_RUNTIME_RESULT;
const runRoot = process.env.FRAPPE_LT_RUNTIME_ROOT;
if (!planPath || !resultPath || !runRoot) {
	throw new Error("runtime Cypress paths were not supplied by validate-lithuanian-runtime");
}
const plan = JSON.parse(fs.readFileSync(planPath, "utf8"));
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
			on("task", {
				"runtime:record"(result) {
					results.set(result.id, result);
					return null;
				},
				"runtime:digest"(relativePath) {
					const absolute = path.resolve(runRoot, relativePath);
					if (!absolute.startsWith(path.resolve(runRoot) + path.sep)) {
						throw new Error("evidence path escaped the runtime root");
					}
					const content = fs.readFileSync(absolute);
					return {
						bytes: content.length,
						sha256: crypto.createHash("sha256").update(content).digest("hex"),
					};
				},
			});
			on("after:run", () => {
				const output = canonical({
					schema_version: 2,
					scenarios: [...results.values()].sort((left, right) => left.id.localeCompare(right.id)),
				});
				fs.writeFileSync(resultPath, JSON.stringify(output) + "\n", { mode: 0o600 });
			});
			return config;
		},
	},
});
