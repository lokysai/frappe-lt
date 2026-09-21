const test = require("node:test");
const assert = require("node:assert/strict");

const {
	correlateOutput,
	exactOutputExclusion,
	isBlockingFallback,
	isClippedByAncestor,
	isUntrustedRenderedLookup,
	isVisuallyHidden,
	meaningfulTarget,
} = require("./runtime_validation_helpers");

function element(bounds, { attributes = {}, dataset = {}, parentElement = null } = {}) {
	return {
		dataset,
		id: attributes.id || "",
		parentElement,
		getAttribute: (name) => attributes[name] || null,
		getBoundingClientRect: () => bounds,
	};
}

test("below-fold controls are not clipped unless a clipping ancestor cuts them", () => {
	const page = element({ bottom: 2000, left: 0, right: 390, top: 0 });
	const belowFold = element({ bottom: 940, left: 10, right: 100, top: 900 }, { parentElement: page });
	const visibleOverflow = () => ({ overflowX: "visible", overflowY: "visible" });
	assert.equal(isClippedByAncestor(belowFold, visibleOverflow), false);

	const clipper = element(
		{ bottom: 850, left: 0, right: 390, top: 0 },
		{ parentElement: page }
	);
	belowFold.parentElement = clipper;
	const hiddenOverflow = (candidate) => ({
		overflowX: "visible",
		overflowY: candidate === clipper ? "hidden" : "visible",
	});
	assert.equal(isClippedByAncestor(belowFold, hiddenOverflow), true);
});

test("layout findings require a stable meaningful target", () => {
	assert.equal(meaningfulTarget(element({}, { dataset: { fieldname: "item_code" } })), '[data-fieldname="item_code"]');
	assert.equal(meaningfulTarget(element({}, { attributes: { "aria-label": "Open menu" } })), '[aria-label="Open\\ menu"]');
	assert.equal(meaningfulTarget(element({}, { attributes: { "data-label": "Save" } })), '[data-label="Save"]');
	assert.equal(
		meaningfulTarget(element({}, { attributes: { "aria-label": "Skip", role: "link" } })),
		'[aria-label="Skip"]'
	);
	assert.equal(meaningfulTarget(element({})), null);
});

test("screen-reader-only controls are not treated as visible layout targets", () => {
	const control = element({ height: 1, width: 1 });
	assert.equal(
		isVisuallyHidden(control, () => ({
			clip: "rect(0px, 0px, 0px, 0px)",
			clipPath: "none",
		})),
		true
	);
	const hiddenParent = element({ height: 1, width: 1 });
	const child = element({ height: 20, width: 100 }, { parentElement: hiddenParent });
	assert.equal(
		isVisuallyHidden(child, (candidate) => ({
			clip: "auto",
			clipPath: candidate === hiddenParent ? "inset(50%)" : "none",
		})),
		true
	);
	assert.equal(
		isVisuallyHidden(element({ height: 20, width: 100 }), () => ({
			clip: "auto",
			clipPath: "none",
		})),
		false
	);
});

test("output correlation distinguishes unique, ambiguous, and unrendered text", () => {
	assert.deepEqual(correlateOutput("One Save", "Save"), {
		interval: { start: 4, end: 8 },
		renderStatus: "unique",
	});
	assert.equal(correlateOutput("Save then Save", "Save").renderStatus, "ambiguous");
	assert.equal(correlateOutput("Nothing", "Save").renderStatus, "unrendered");
});

test("output exclusions apply only to exact approved values at the captured interval", () => {
	const recipient = "frappe-lt-runtime-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@invalid.example";
	const output = `Recipient: ${recipient}`;
	const correlation = correlateOutput(output, recipient);
	const exclusions = [{ id: "identity-values", target: "output:recipient" }];
	assert.deepEqual(
		exactOutputExclusion(output, correlation, exclusions, { "output:recipient": [recipient] }),
		{ id: "identity-values", target: "output:recipient" }
	);
	assert.equal(
		exactOutputExclusion(output, correlateOutput(output, "frappe-lt-runtime"), exclusions, {
			"output:recipient": [recipient],
		}),
		null
	);
	assert.equal(exactOutputExclusion(output, correlation, exclusions, { "output:recipient": [] }), null);
	const marker = "frappe-lt-runtime-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-todo";
	const wrapped = `<div>${marker}</div>`;
	assert.equal(
		exactOutputExclusion(
			wrapped,
			correlateOutput(wrapped, wrapped),
			[{ id: "fixture", target: "output:fixture-values" }],
			{ "output:fixture-values": [marker] }
		),
		null
	);
});

test("rendered missing translations block even when their target is ambiguous", () => {
	const finding = {
		effective: "Save",
		excluded: false,
		key: { source: "Save" },
		render_status: "ambiguous",
		source: "missing",
		visible: true,
	};
	assert.equal(isBlockingFallback(finding), true);
	assert.equal(isBlockingFallback({ ...finding, render_status: "unrendered", visible: false }), false);
	assert.equal(isBlockingFallback({ ...finding, excluded: true }), false);
	assert.equal(isBlockingFallback({ ...finding, effective: "Išsaugoti", source: "frappe_lt" }), false);
});

test("rendered lookups absent from the inventory fail closed unless exactly excluded", () => {
	assert.equal(isUntrustedRenderedLookup(false, "unique", false), true);
	assert.equal(isUntrustedRenderedLookup(false, "ambiguous", false), true);
	assert.equal(isUntrustedRenderedLookup(false, "unrendered", false), false);
	assert.equal(isUntrustedRenderedLookup(false, "unique", true), false);
	assert.equal(isUntrustedRenderedLookup(true, "unique", false), false);
});
