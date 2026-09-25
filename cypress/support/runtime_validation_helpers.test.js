const test = require("node:test");
const assert = require("node:assert/strict");

const {
	correlateOutput,
	exactOutputExclusion,
	finalizeScenarioResult,
	isBlockingFallback,
	isClippedByAncestor,
	isDisabledControl,
	isUnusableControl,
	isVisuallyHidden,
	meaningfulTarget,
} = require("./runtime_validation_helpers");

function element(
	bounds,
	{ attributes = {}, classes = [], dataset = {}, disabled = false, focused = false, labels = [], parentElement = null } = {}
) {
	return {
		classList: { contains: (name) => classes.includes(name) },
		dataset,
		disabled,
		id: attributes.id || "",
		labels,
		parentElement,
		tagName: "BUTTON",
		getAttribute: (name) => attributes[name] || null,
		getBoundingClientRect: () => bounds,
		matches: (selector) => selector === ":focus" && focused,
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

test("mobile controls are unusable when outside the viewport, pointer-disabled, or too small", () => {
	const mobileWidth = 390;
	assert.equal(
		isUnusableControl(
			element({ bottom: 60, height: 40, left: 400, right: 440, top: 20, width: 40 }),
			{ pointerEvents: "auto" },
			mobileWidth
		),
		true
	);
	assert.equal(
		isUnusableControl(
			element({ bottom: 60, height: 40, left: 20, right: 60, top: 20, width: 40 }),
			{ pointerEvents: "none" },
			mobileWidth
		),
		true
	);
	assert.equal(
		isUnusableControl(
			element({ bottom: 30, height: 10, left: 20, right: 30, top: 20, width: 10 }),
			{ pointerEvents: "auto" },
			mobileWidth
		),
		true
	);
	assert.equal(
		isUnusableControl(
			element({ bottom: 60, height: 40, left: 20, right: 60, top: 20, width: 40 }),
			{ pointerEvents: "auto" },
			mobileWidth
		),
		false
	);
});

test("disabled mobile controls are ignored and checkbox or radio labels provide the hit target", () => {
	const mobileWidth = 390;
	const tiny = { bottom: 24, height: 10, left: 14, right: 24, top: 14, width: 10 };
	assert.equal(isDisabledControl(element(tiny, { disabled: true })), true);
	assert.equal(isDisabledControl(element(tiny, { attributes: { "aria-disabled": "true" } })), true);
	assert.equal(isDisabledControl(element(tiny)), false);
	assert.equal(
		isUnusableControl(element(tiny, { disabled: true }), { pointerEvents: "none" }, mobileWidth),
		false
	);
	assert.equal(
		isUnusableControl(
			element(tiny, { attributes: { type: "checkbox" }, labels: [element({ height: 32, width: 80 })] }),
			{ pointerEvents: "auto" },
			mobileWidth
		),
		false
	);
	assert.equal(
		isUnusableControl(
			element(tiny, { attributes: { type: "radio" }, labels: [element({ height: 12, width: 80 })] }),
			{ pointerEvents: "auto" },
			mobileWidth
		),
		true
	);
	assert.equal(
		isUnusableControl(
			element(
				{ bottom: 24, height: 10, left: 400, right: 410, top: 14, width: 10 },
				{ attributes: { type: "checkbox" }, labels: [element({ height: 32, width: 80 })] }
			),
			{ pointerEvents: "auto" },
			mobileWidth
		),
		true
	);
	assert.equal(
		isUnusableControl(
			element(tiny, { attributes: { type: "checkbox" }, labels: [element({ height: 32, width: 80 })] }),
			{ pointerEvents: "none" },
			mobileWidth
		),
		true
	);
});

test("layout findings require a stable meaningful target", () => {
	assert.equal(meaningfulTarget(element({}, { dataset: { fieldname: "item_code" } })), '[data-fieldname="item_code"]');
	assert.equal(meaningfulTarget(element({}, { attributes: { "aria-label": "Open menu" } })), "button[aria-label]");
	assert.equal(meaningfulTarget(element({}, { attributes: { "data-label": "Save" } })), "button[data-label]");
	assert.equal(
		meaningfulTarget(element({}, { attributes: { "aria-label": "Skip", role: "link" } })),
		"button[aria-label]"
	);
	const first = element({}, { attributes: { "aria-label": "Private A" } });
	const second = element({}, { attributes: { "aria-label": "Private B" } });
	const parent = { children: [first, second], id: "toolbar", parentElement: null, tagName: "DIV" };
	first.parentElement = parent;
	second.parentElement = parent;
	assert.equal(meaningfulTarget(second), "#toolbar > button[aria-label]:nth-of-type(2)");
	assert.equal(meaningfulTarget(element({})), null);
});

test("screen-reader-only controls are not treated as visible layout targets", () => {
	const skipLink = element({ height: 20, width: 100 }, { classes: ["sr-only", "sr-only-focusable"] });
	const hiddenStyle = {
		clip: "rect(0px, 0px, 0px, 0px)",
		clipPath: "none",
		overflow: "hidden",
		position: "absolute",
	};
	assert.equal(isVisuallyHidden(skipLink, () => hiddenStyle), true);
	assert.equal(isVisuallyHidden(skipLink, () => ({ overflow: "visible", position: "static" })), false);
	assert.equal(
		isVisuallyHidden(skipLink, () => ({
			clip: "auto",
			clipPath: "inset(0)",
			overflow: "hidden",
			position: "absolute",
		})),
		false
	);
	const focusedSkipLink = element(
		{ height: 20, width: 100 },
		{ classes: ["sr-only", "sr-only-focusable"], focused: true }
	);
	assert.equal(isVisuallyHidden(focusedSkipLink, () => hiddenStyle), true);
	assert.equal(
		isVisuallyHidden(focusedSkipLink, () => ({
			clip: "auto",
			clipPath: "none",
			overflow: "visible",
			position: "static",
		})),
		false
	);
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
	assert.deepEqual(correlateOutput("Sveiki, Jonai", "Sveiki, {0}"), {
		interval: { start: 0, end: 13 },
		renderStatus: "unique",
	});
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
		active: true,
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
	assert.equal(isBlockingFallback({ ...finding, effective: " Save ", source: "database" }), true);
	assert.equal(isBlockingFallback({ ...finding, active: false }), false);
});

test("scenario outcome contract blocks missing readiness and elapsed scenario timeout", () => {
	const scenario = { scenario_timeout_ms: 100 };
	const base = {
		attempts: [{ duration_ms: 0, error: null, kind: "initial", number: 1, outcome: "pass" }],
		blocked_reason: null,
		error: null,
		fallbacks: [{ active: true, excluded: false, render_status: "unrendered", visible: false }],
		layouts: [],
		ready: true,
		status: "pass",
	};
	const withInactiveDiagnostic = finalizeScenarioResult(
		scenario,
		{
			...base,
			attempts: base.attempts.map((item) => ({ ...item })),
			fallbacks: [
				...base.fallbacks,
				{ active: false, excluded: false, render_status: "ambiguous", visible: true },
			],
		},
		20
	);
	assert.equal(withInactiveDiagnostic.status, "pass");

	const unavailable = finalizeScenarioResult(scenario, { ...base, attempts: base.attempts.map((item) => ({ ...item })), ready: false }, 20);
	assert.equal(unavailable.status, "blocked");
	assert.match(unavailable.blocked_reason, /readiness/);
	assert.equal(unavailable.attempts[0].outcome, "blocked");

	const timedOut = finalizeScenarioResult(scenario, { ...base, attempts: base.attempts.map((item) => ({ ...item })) }, 101);
	assert.equal(timedOut.status, "blocked");
	assert.match(timedOut.blocked_reason, /exceeded 100 ms/);
	assert.equal(timedOut.attempts[0].error, timedOut.blocked_reason);

	const failed = finalizeScenarioResult(
		scenario,
		{
			...base,
			attempts: base.attempts.map((item) => ({ ...item })),
			error: "Cypress assertion failed",
			status: "fail",
		},
		30
	);
	assert.equal(failed.status, "fail");
	assert.equal(failed.attempts[0].outcome, "assertion_failure");
	assert.equal(failed.attempts[0].error, failed.error);
});
