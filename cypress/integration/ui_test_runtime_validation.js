const plan = Cypress.env("runtimePlan");
const scenarioResults = new Map();

function redactError(value) {
	return String(value)
		.replace(/\b(authorization\s*:\s*)(?:bearer\s+)?[^\s,;}]+/gi, "$1[REDACTED]")
		.replace(/\b((?:set-)?cookie\s*:\s*)[^\r\n]+/gi, "$1[REDACTED]")
		.replace(/(["']?(?:password|pwd|token|csrf_token)["']?\s*:\s*)["'][^"']*["']/gi, '$1"[REDACTED]"')
		.replace(/\b(password|pwd|token|csrf_token)\b\s*[:=]\s*[^\s,;}]+/gi, "$1=[REDACTED]")
		.slice(0, 2048);
}

function emptyResult(scenario) {
	return {
		attempts: [{ kind: "initial", number: 1 }],
		blocked_reason: null,
		duration_ms: 0,
		evidence: [],
		error: null,
		fallbacks: [],
		id: scenario.id,
		layouts: [],
		ready: false,
		status: "pass",
	};
}

function finishScenario(scenario, result, started) {
	result.duration_ms = Date.now() - started;
	if (result.fallbacks.length === 0 && result.status === "pass") {
		result.status = "blocked";
		result.blocked_reason = "scenario produced no effective translation lookup evidence";
	}
	if (result.duration_ms > scenario.scenario_timeout_ms && result.status === "pass") {
		result.status = "blocked";
		result.blocked_reason = `scenario exceeded ${scenario.scenario_timeout_ms} ms`;
	}
	return cy.task("runtime:record", result);
}

function armLookupRecorder(window) {
	window.__frappeLtLookups = [];
	function instrument(frappe) {
		if (!frappe || frappe.__frappeLtLookupRecorder) return;
		let original = frappe._;
		let wrapped;
		Object.defineProperty(frappe, "_", {
			configurable: true,
			enumerable: true,
			get() {
				return wrapped || original;
			},
			set(value) {
				original = value;
				wrapped =
					typeof value === "function"
						? function (source, replace, context) {
								const lookupEffective = original.call(this, source, null, context);
								const rendered = original.apply(this, arguments);
								window.__frappeLtLookups.push({
									context: context || null,
									effective: lookupEffective,
									rendered,
									source: String(source).trim(),
								});
								return rendered;
							}
						: value;
			},
		});
		frappe._ = original;
		Object.defineProperty(frappe, "__frappeLtLookupRecorder", { value: true });
	}

	let frappe = window.frappe;
	instrument(frappe);
	Object.defineProperty(window, "frappe", {
		configurable: true,
		enumerable: true,
		get() {
			return frappe;
		},
		set(value) {
			frappe = value;
			instrument(value);
		},
	});
}

function recordLayout(result) {
	return cy.document().then((document) => {
		const root = document.documentElement;
		const targetFor = (element) => {
			if (element.dataset.fieldname) return `[data-fieldname="${CSS.escape(element.dataset.fieldname)}"]`;
			if (element.getAttribute("role")) return `[role="${CSS.escape(element.getAttribute("role"))}"]`;
			return element.tagName.toLowerCase();
		};
		const isVisible = (element) => {
			const style = document.defaultView.getComputedStyle(element);
			const bounds = element.getBoundingClientRect();
			return style.display !== "none" && style.visibility !== "hidden" && bounds.width > 0 && bounds.height > 0;
		};
		if (root.scrollWidth > root.clientWidth + 1) {
			result.layouts.push({
				detail: `page width ${root.scrollWidth}px exceeds viewport ${root.clientWidth}px`,
				kind: "horizontal_overflow",
				scenario_id: result.id,
				severity: "functional",
				target: "document",
			});
		}
		for (const element of document.querySelectorAll("button, input, select, [role='button']")) {
			if (!isVisible(element)) continue;
			const bounds = element.getBoundingClientRect();
			const target = targetFor(element);
			if (
				bounds.left < 0 ||
				bounds.right > root.clientWidth + 1 ||
				bounds.top < 0 ||
				bounds.bottom > root.clientHeight + 1
			) {
				result.layouts.push({
					detail: "interactive control is clipped outside the viewport",
					kind: "clipped",
					scenario_id: result.id,
					severity: "functional",
					target,
				});
			}
			const x = Math.min(Math.max(bounds.left + bounds.width / 2, 0), root.clientWidth - 1);
			const y = Math.min(Math.max(bounds.top + bounds.height / 2, 0), root.clientHeight - 1);
			const covering = document.elementFromPoint(x, y);
			if (covering && covering !== element && !element.contains(covering)) {
				result.layouts.push({
					detail: `interactive control is covered by ${targetFor(covering)}`,
					kind: "covered",
					scenario_id: result.id,
					severity: "functional",
					target,
				});
			}
			if (element.matches("button, [role='button']") && (bounds.width < 8 || bounds.height < 8)) {
				result.layouts.push({
					detail: "interactive control has no usable hit area",
					kind: "unusable",
					scenario_id: result.id,
					severity: "functional",
					target,
				});
			}
		}
		for (const element of document.querySelectorAll(".control-label, .page-title, button")) {
			if (!isVisible(element)) continue;
			const style = document.defaultView.getComputedStyle(element);
			const horizontallyClipped =
				element.scrollWidth > element.clientWidth + 1 && ["hidden", "clip"].includes(style.overflowX);
			const verticallyClipped =
				element.scrollHeight > element.clientHeight + 1 && ["hidden", "clip"].includes(style.overflowY);
			if (horizontallyClipped || verticallyClipped) {
				result.layouts.push({
					detail: "interface text is clipped by its container",
					kind: "clipped",
					scenario_id: result.id,
					severity: "functional",
					target: targetFor(element),
				});
			} else if (element.classList.contains("control-label")) {
				const lineHeight = Number.parseFloat(style.lineHeight);
				if (Number.isFinite(lineHeight) && element.scrollHeight > lineHeight * 1.5) {
					result.layouts.push({
						detail: "interface label wraps onto multiple lines",
						kind: "wrapping",
						scenario_id: result.id,
						severity: "cosmetic",
						target: targetFor(element),
					});
				}
			}
		}
	});
}

function installLookupRecorder() {
	return cy.window().then((window) => {
		if (!window.frappe || typeof window.frappe._ !== "function") {
			throw new Error("Frappe client translation lookup is unavailable");
		}
		if (!window.frappe.__frappeLtLookupRecorder) armLookupRecorder(window);
	});
}

function locateRenderedLookup(document, rendered, scenario) {
	if (typeof rendered !== "string" || !rendered) return null;
	const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
	while (walker.nextNode()) {
		const node = walker.currentNode;
		if (!node.nodeValue.includes(rendered) || !node.parentElement) continue;
		const element = node.parentElement;
		const style = document.defaultView.getComputedStyle(element);
		if (style.display === "none" || style.visibility === "hidden" || element.getClientRects().length === 0) {
			continue;
		}
		for (const exclusion of scenario.expected_exclusions) {
			if (element.closest(exclusion.target)) {
				return { excluded: true, exclusionId: exclusion.id, target: exclusion.target };
			}
		}
		if (element.closest("[data-fieldname]")) {
			const field = element.closest("[data-fieldname]").dataset.fieldname;
			return { excluded: false, exclusionId: null, target: `[data-fieldname="${CSS.escape(field)}"]` };
		}
		if (element.closest("[role]")) {
			const role = element.closest("[role]").getAttribute("role");
			return { excluded: false, exclusionId: null, target: `[role="${CSS.escape(role)}"]` };
		}
		return { excluded: false, exclusionId: null, target: element.tagName.toLowerCase() };
	}
	return null;
}

function collectLookups(scenario, result) {
	return cy.window().then((window) => {
		const unique = new Map();
		for (const lookup of window.__frappeLtLookups || []) {
			unique.set(`${lookup.source}\u0000${lookup.context || ""}\u0000${lookup.rendered}`, lookup);
		}
		return cy.wrap([...unique.values()], { log: false }).each((lookup) => {
			cy.runtimeCall("frappe_lt.runtime_control.resolve_translation", {
				context: lookup.context,
				run_id: plan.run_id,
				scenario_id: scenario.id,
				source: lookup.source,
				token: plan.token,
			}).then((response) => {
				const resolved = response.body.message;
				const location = locateRenderedLookup(window.document, lookup.rendered, scenario);
				if (resolved.effective !== lookup.effective) {
					throw new Error(`loaded dictionary disagrees with effective lookup for ${lookup.source}`);
				}
				result.fallbacks.push({
					effective: resolved.effective,
					excluded: location?.excluded || false,
					exclusion_id: location?.exclusionId || null,
					key: resolved.key,
					scenario_id: scenario.id,
					source: resolved.source,
					target: { type: "locator", value: location?.target || "body[data-route]" },
					visible: location !== null,
				});
			});
		});
	});
}

function collectServerLookups(scenario, result, lookups, targetType, output) {
	const unique = new Map();
	for (const lookup of lookups) {
		unique.set(`${lookup.key.source}\u0000${lookup.key.context || ""}`, lookup);
	}
	return cy.wrap([...unique.values()], { log: false }).each((lookup) => {
		cy.runtimeCall("frappe_lt.runtime_control.resolve_translation", {
			context: lookup.key.context,
			run_id: plan.run_id,
			scenario_id: scenario.id,
			source: lookup.key.source,
			token: plan.token,
		}).then((response) => {
			const resolved = response.body.message;
			const interval = renderedInterval(output, resolved.effective);
			if (resolved.effective !== lookup.effective) {
				throw new Error(`server output lookup disagrees with effective translation for ${lookup.key.source}`);
			}
			result.fallbacks.push({
				effective: resolved.effective,
				excluded: false,
				exclusion_id: null,
				key: resolved.key,
				scenario_id: scenario.id,
				source: resolved.source,
				target: {
					type: "output_interval",
					value:
						interval === null
							? `${targetType}:not-rendered`
							: `${targetType}:${interval.start}-${interval.end}`,
				},
				visible: interval !== null,
			});
		});
	});
}

function renderedInterval(output, effective) {
	const exact = output.indexOf(effective);
	if (exact >= 0) return { start: exact, end: exact + effective.length };
	const marker = "__FRAPPE_LT_RENDERED_VALUE__";
	const template = effective
		.replace(/\{[^{}]+\}/g, marker)
		.replace(/%\([^)]+\)[#0 +\-]?\d*(?:\.\d+)?[a-zA-Z]/g, marker)
		.replace(/%[sdif]/g, marker);
	if (!template.includes(marker)) return null;
	const pattern = template
		.split(marker)
		.map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
		.join("[\\s\\S]+?");
	const match = new RegExp(pattern).exec(output);
	return match ? { start: match.index, end: match.index + match[0].length } : null;
}

function loginFor(scenario) {
	const credential = plan.credentials[scenario.role_profile_id];
	if (!credential) throw new Error(`missing credentials for ${scenario.role_profile_id}`);
	const bootstrap = scenario.kind === "portal" || scenario.kind === "email" ? "/me" : "/app";
	cy.runtimeLogin(scenario);
	return cy.visit(bootstrap);
}

for (const scenario of plan.scenarios) {
	it(scenario.id, { defaultCommandTimeout: scenario.step_timeout_ms }, function () {
		const started = Date.now();
		const result = emptyResult(scenario);
		scenarioResults.set(scenario.id, { result, started });
		cy.viewport(scenario.viewport.width, scenario.viewport.height);
		loginFor(scenario);

		if (scenario.kind === "email") {
			const user = plan.fixtures[scenario.fixture_id].user;
			cy.runtimeCall("frappe_lt.runtime_control.capture_welcome_email", {
				run_id: plan.run_id,
				scenario_id: scenario.id,
				token: plan.token,
				user,
			}).then((response) => {
				expect(response.body.message.output).to.include("MIME-Version");
				result.ready = true;
				collectServerLookups(
					scenario,
					result,
					response.body.message.lookups,
					"email:subject-body",
					response.body.message.visible_output
				);
				cy.then(() => {
					finishScenario(scenario, result, started);
				});
			});
			return;
		}

		if (scenario.kind === "print") {
			const fixture = plan.fixtures[scenario.fixture_id];
			const route = `/printview?doctype=Sales%20Invoice&name=${encodeURIComponent(fixture.name)}`;
			let printCapture;
			cy.runtimeCall("frappe_lt.runtime_control.capture_print", {
				doctype: "Sales Invoice",
				name: fixture.name,
				run_id: plan.run_id,
				scenario_id: scenario.id,
				token: plan.token,
			}).then((capture) => {
				printCapture = capture.body.message;
			});
			cy.request({ failOnStatusCode: false, url: route }).then((response) => {
				if (response.status >= 400) {
					result.status = "blocked";
					result.blocked_reason = `print route returned HTTP ${response.status}`;
					finishScenario(scenario, result, started);
					return;
				}
				cy.visit(route);
				cy.get("body").should("be.visible");
				cy.then(() => {
					result.ready = true;
				});
				cy.document().then((document) => {
					collectServerLookups(
						scenario,
						result,
						printCapture.lookups,
						"print:visible-text",
						document.body.innerText
					);
				});
				recordLayout(result);
				cy.then(() => {
					finishScenario(scenario, result, started);
				});
			});
			return;
		}

		const route = scenario.target.route.replace(
			"{fixture.item_name}",
			plan.fixtures[scenario.fixture_id]?.item_name || ""
		);
		cy.request({ failOnStatusCode: false, url: route }).then((response) => {
			if (response.status >= 400) {
				result.status = "blocked";
				result.blocked_reason = `route returned HTTP ${response.status}`;
				finishScenario(scenario, result, started);
				return;
			}
			if (scenario.kind === "desk") {
				cy.visit("/app", { onBeforeLoad: armLookupRecorder });
			} else {
				cy.visit(route);
			}
			if (scenario.kind === "desk") {
				cy.get("body").should("have.attr", "data-ajax-state", "complete");
				installLookupRecorder();
				cy.window().then((window) => window.frappe.set_route(route.replace(/^\/app\/?/, "")));
				cy.get("body").should("have.attr", "data-ajax-state", "complete");
				cy.get("body").should(($body) => {
					expect($body.attr("data-route")).to.include(scenario.readiness.value);
				});
				cy.then(() => {
					result.ready = true;
				});
				collectLookups(scenario, result);
			} else {
				cy.get("body").should("be.visible");
				cy.location("pathname").should("eq", scenario.readiness.value);
				cy.then(() => {
					result.ready = true;
				});
				cy.runtimeCall("frappe_lt.runtime_control.capture_portal", {
					route,
					run_id: plan.run_id,
					scenario_id: scenario.id,
					token: plan.token,
				}).then((response) => {
					cy.document().then((document) => {
						collectServerLookups(
							scenario,
							result,
							response.body.message.lookups,
							"portal:visible-text",
							document.body.innerText
						);
					});
				});
			}
			recordLayout(result);
			cy.then(() => {
				finishScenario(scenario, result, started);
			});
		});
	});

}

afterEach(function () {
	if (this.currentTest.state === "failed") {
		const scenario = plan.scenarios.find((candidate) => candidate.id === this.currentTest.title);
		if (scenario) {
			const recorded = scenarioResults.get(scenario.id);
			const result = recorded?.result || emptyResult(scenario);
			result.duration_ms = recorded ? Date.now() - recorded.started : 0;
			result.blocked_reason = null;
			result.error = redactError(this.currentTest.err?.message || "Cypress assertion failed");
			result.status = "fail";
			cy.task("runtime:record", result);
		}
	}
});
