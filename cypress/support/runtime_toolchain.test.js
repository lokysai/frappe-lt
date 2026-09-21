const test = require("node:test");
const assert = require("node:assert/strict");

const { browserFromRunResults } = require("./runtime_toolchain");

test("reads exact browser identity from Cypress run results", () => {
	assert.deepEqual(
		browserFromRunResults({ browserName: "chrome", browserVersion: "140.0.7339.80" }),
		{ name: "chrome", version: "140.0.7339.80" }
	);
});

test("rejects missing setup-time-style browser metadata", () => {
	assert.throws(() => browserFromRunResults({ browser: {} }), /exact browser version/);
});
