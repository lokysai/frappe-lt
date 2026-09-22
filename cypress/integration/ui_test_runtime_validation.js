const plan = Cypress.env("runtimePlan");
const scenarioResults = new Map();
const {
	correlateOutput,
	exactOutputExclusion,
	finalizeScenarioResult,
	isClippedByAncestor,
	isDisabledControl,
	isUnusableControl,
	isVisuallyHidden,
	meaningfulTarget,
} = require("../support/runtime_validation_helpers");

function redactError(value) {
	return String(value)
		.replace(/\b(authorization\s*:\s*)(?:bearer\s+)?[^\s,;}]+/gi, "$1[REDACTED]")
		.replace(/\b((?:set-)?cookie\s*:\s*)[^\r\n]+/gi, "$1[REDACTED]")
		.replace(/(["']?(?:password|pwd|token|csrf_token)["']?\s*:\s*)["'][^"']*["']/gi, '$1"[REDACTED]"')
		.replace(/\b(password|pwd|token|csrf_token)\b\s*[:=]\s*[^\s,;}]+/gi, "$1=[REDACTED]")
		.slice(0, 2048);
}

function canonical(value) {
	if (Array.isArray(value)) return value.map(canonical);
	if (value && typeof value === "object") {
		return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
	}
	return value;
}

function publishScenario(scenario, result, artifacts) {
	const browserEvidence = canonical({ ...result, evidence: [] });
	const publishable = result.fallbacks.some((finding) => !finding.active)
		? []
		: [
				{ content: JSON.stringify(browserEvidence) + "\n", kind: "browser", mime: "application/json" },
				...artifacts,
			];
	return cy
		.task(
			"runtime:publishEvidence",
			{
				artifacts: publishable,
				scenarioId: scenario.id,
				status: result.status,
			},
			{ log: false }
		)
		.then((evidence) => {
			result.evidence = evidence;
			return cy.task("runtime:record", result, { log: false });
		});
}

function emptyResult(scenario) {
	return {
		attempts: [{ duration_ms: 0, error: null, kind: "initial", number: 1, outcome: "pass" }],
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
	finalizeScenarioResult(scenario, result, Date.now() - started);
	const artifacts = result.fallbacks.some((finding) => !finding.active)
		? []
		: scenarioResults.get(scenario.id)?.artifacts || [];
	return publishScenario(scenario, result, artifacts);
}

function checkDeadline(scenario, started) {
	if (Date.now() - started > scenario.scenario_timeout_ms) {
		throw new Error(`scenario exceeded ${scenario.scenario_timeout_ms} ms during execution`);
	}
}

function uniqueTarget(document, element) {
	const target = meaningfulTarget(element);
	return target && document.querySelectorAll(target).length === 1 ? target : null;
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
								if (typeof source === "string" && source) {
									window.__frappeLtLookups.push({
										context: context || null,
										effective: lookupEffective,
										raw_source: source,
										rendered,
									});
								}
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
		const isVisible = (element) => {
			const style = document.defaultView.getComputedStyle(element);
			const bounds = element.getBoundingClientRect();
			return (
				style.display !== "none" &&
				style.visibility !== "hidden" &&
				bounds.width > 0 &&
				bounds.height > 0 &&
				!isVisuallyHidden(element, document.defaultView.getComputedStyle.bind(document.defaultView))
			);
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
		for (const element of document.querySelectorAll(
			"a[href], button, input, select, summary, textarea, [role='button'], [tabindex]:not([tabindex='-1'])"
		)) {
			if (!isVisible(element)) continue;
			if (isDisabledControl(element)) continue;
			const bounds = element.getBoundingClientRect();
			const style = document.defaultView.getComputedStyle(element);
			const target = uniqueTarget(document, element);
			if (!target) continue;
			if (isClippedByAncestor(element, document.defaultView.getComputedStyle.bind(document.defaultView))) {
				result.layouts.push({
					detail: "interactive control is clipped by a clipping ancestor",
					kind: "clipped",
					scenario_id: result.id,
					severity: "functional",
					target,
				});
			}
			const x = bounds.left + bounds.width / 2;
			const y = bounds.top + bounds.height / 2;
			const covering =
				x >= 0 && x < root.clientWidth && y >= 0 && y < root.clientHeight
					? document.elementFromPoint(x, y)
					: null;
			if (covering && covering !== element && !element.contains(covering)) {
				const coveringTarget = meaningfulTarget(covering) || covering.tagName.toLowerCase();
				result.layouts.push({
					detail: `interactive control is covered by ${coveringTarget}`,
					kind: "covered",
					scenario_id: result.id,
					severity: "functional",
					target,
				});
			}
			if (isUnusableControl(element, style, root.clientWidth)) {
				result.layouts.push({
					detail: "interactive control is outside the horizontal viewport, pointer-disabled, or has no usable hit area",
					kind: "unusable",
					scenario_id: result.id,
					severity: "functional",
					target,
				});
			}
		}
		for (const element of document.querySelectorAll(
			"a[href], button, label, legend, th, td, .control-label, .list-row, .page-title, [data-fieldname]"
		)) {
			if (!isVisible(element)) continue;
			const target = uniqueTarget(document, element);
			if (!target) continue;
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
					target,
				});
			} else if (element.classList.contains("control-label")) {
				const lineHeight = Number.parseFloat(style.lineHeight);
				if (Number.isFinite(lineHeight) && element.scrollHeight > lineHeight * 1.5) {
					result.layouts.push({
						detail: "interface label wraps onto multiple lines",
						kind: "wrapping",
						scenario_id: result.id,
						severity: "cosmetic",
						target,
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
	if (typeof rendered !== "string" || !rendered) {
		return { excluded: false, exclusionId: null, renderStatus: "unrendered", target: "body[data-route]" };
	}
	const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
	const matches = new Set();
	while (walker.nextNode()) {
		const node = walker.currentNode;
		if (!node.nodeValue.includes(rendered) || !node.parentElement) continue;
		const element = node.parentElement;
		const style = document.defaultView.getComputedStyle(element);
		if (style.display === "none" || style.visibility === "hidden" || element.getClientRects().length === 0) {
			continue;
		}
		matches.add(element);
	}
	if (matches.size !== 1) {
		return {
			excluded: false,
			exclusionId: null,
			renderStatus: matches.size ? "ambiguous" : "unrendered",
			target: matches.size ? `body[data-route]:ambiguous:${matches.size}` : "body[data-route]",
		};
	}
	const [element] = matches;
	for (const exclusion of scenario.expected_exclusions) {
		if (element.closest(exclusion.target)) {
			return {
				excluded: true,
				exclusionId: exclusion.id,
				renderStatus: "unique",
				target: exclusion.target,
			};
		}
	}
	const target = uniqueTarget(document, element);
	if (!target) {
		return {
			excluded: false,
			exclusionId: null,
			renderStatus: "ambiguous",
			target: "body[data-route]:ambiguous-locator",
		};
	}
	return {
		excluded: false,
		exclusionId: null,
		renderStatus: "unique",
		target,
	};
}

function collectLookups(scenario, result) {
	return cy.window().then((window) => {
		const unique = new Map();
		for (const lookup of window.__frappeLtLookups || []) {
			unique.set(`${lookup.raw_source}\u0000${lookup.context || ""}\u0000${lookup.rendered}`, lookup);
		}
		return cy.wrap([...unique.values()], { log: false }).each((lookup) => {
			scenarioResults.get(scenario.id).resolvingLookup = true;
			cy.runtimeCall("frappe_lt.runtime_control.resolve_translation", {
				context: lookup.context,
				lookup_path: "client",
				run_id: plan.run_id,
				scenario_id: scenario.id,
				source: lookup.raw_source,
				token: plan.token,
			}).then((response) => {
				scenarioResults.get(scenario.id).resolvingLookup = false;
				const resolved = response.body.message;
				const location = locateRenderedLookup(window.document, lookup.rendered, scenario);
				if (!resolved.active) scenarioResults.get(scenario.id).artifacts = [];
				if (resolved.effective !== lookup.effective) {
					throw new Error("loaded dictionary disagrees with effective translation lookup");
				}
				const evidence = lookupEvidence(resolved);
				result.fallbacks.push({
					active: resolved.active,
					effective: evidence.effective,
					excluded: location?.excluded || false,
					exclusion_id: location?.exclusionId || null,
					key: evidence.key,
					raw_source: evidence.raw_source,
					render_status: location.renderStatus,
					schema_version: 1,
					scenario_id: scenario.id,
					source: evidence.source,
					target: {
						type: "locator",
						value:
							!resolved.active && !location.excluded
								? `diagnostic:${resolved.diagnostic_id}`
								: location.target,
					},
					visible: location.renderStatus !== "unrendered",
				});
			});
		});
	});
}

function lookupEvidence(resolved) {
	if (resolved.active) return resolved;
	if (!/^hmac-sha256:[0-9a-f]{32}:[0-9a-f]{64}$/.test(resolved.diagnostic_id)) {
		throw new Error("inactive translation lookup is missing its trusted diagnostic identifier");
	}
	return {
		effective: resolved.diagnostic_id,
		key: { context: null, source: resolved.diagnostic_id },
		raw_source: resolved.diagnostic_id,
		source: "missing",
	};
}

function collectServerLookups(scenario, result, lookups, targetType, output) {
	const unique = new Map();
	const fixture = plan.fixtures[scenario.fixture_id] || {};
	const outputValues = Array.isArray(fixture.output_values) ? fixture.output_values : [];
	const approvedValues = {
		"output:fixture-values": [...outputValues, ...Object.values(fixture)].filter(
			(value) =>
				typeof value === "string" &&
				(outputValues.includes(value) || value.startsWith(`frappe-lt-runtime-${plan.run_id}`))
		),
		"output:recipient": typeof fixture.user === "string" ? [fixture.user] : [],
	};
	for (const lookup of lookups) {
		unique.set(`${lookup.raw_source}\u0000${lookup.key.context || ""}\u0000${lookup.effective}`, lookup);
	}
	return cy.wrap([...unique.values()], { log: false }).each((lookup) => {
		scenarioResults.get(scenario.id).resolvingLookup = true;
		cy.runtimeCall("frappe_lt.runtime_control.resolve_translation", {
			context: lookup.key.context,
			lookup_path: "server",
			run_id: plan.run_id,
			scenario_id: scenario.id,
			source: lookup.raw_source,
			token: plan.token,
		}).then((response) => {
			scenarioResults.get(scenario.id).resolvingLookup = false;
			const resolved = response.body.message;
			const correlation = correlateOutput(output, resolved.effective);
			const exclusion = exactOutputExclusion(
				output,
				correlation,
				scenario.expected_exclusions,
				approvedValues
			);
			if (!resolved.active) scenarioResults.get(scenario.id).artifacts = [];
			if (resolved.effective !== lookup.effective) {
				throw new Error("server output disagrees with effective translation lookup");
			}
			const evidence = lookupEvidence(resolved);
			result.fallbacks.push({
				active: resolved.active,
				effective: evidence.effective,
				excluded: Boolean(exclusion),
				exclusion_id: exclusion?.id || null,
				key: evidence.key,
				raw_source: evidence.raw_source,
				render_status: correlation.renderStatus,
				schema_version: 1,
				scenario_id: scenario.id,
				source: evidence.source,
				target: {
					type: "output_interval",
					value:
						correlation.renderStatus === "unique"
							? `${targetType}:${correlation.interval.start}-${correlation.interval.end}`
							: `${targetType}:${correlation.renderStatus}`,
				},
				visible: correlation.renderStatus !== "unrendered",
			});
		});
	});
}

function loginFor(scenario) {
	const credential = plan.credentials[scenario.role_profile_id];
	if (!credential) throw new Error(`missing credentials for ${scenario.role_profile_id}`);
	const bootstrap = scenario.kind === "portal" || scenario.kind === "email" ? "/me" : "/desk";
	cy.runtimeLogin(scenario);
	return cy.visit(bootstrap);
}

function verifyReadiness(scenario, result, proof = {}) {
	if (scenario.readiness.type === "route") {
		const command =
			scenario.kind === "desk"
				? cy.get("body").should(($body) => {
						expect($body.attr("data-route")).to.include(scenario.readiness.value);
					})
				: cy.location("pathname").should("eq", scenario.readiness.value);
		return command.then(() => {
			result.ready = true;
		});
	}
	if (scenario.readiness.type === "output") {
		return cy.then(() => {
			if (typeof proof.output !== "string" || !proof.output) {
				throw new Error(`output readiness ${scenario.readiness.value} was not proven`);
			}
			result.ready = true;
		});
	}
	if (scenario.readiness.type === "api") {
		return cy.runtimeCall(scenario.readiness.value, {
			run_id: plan.run_id,
			scenario_id: scenario.id,
			token: plan.token,
		}).then((response) => {
			expect(response.status).to.be.within(200, 299);
			result.ready = true;
		});
	}
	throw new Error(`unsupported readiness type ${scenario.readiness.type}`);
}

describe("runtime harness outcome contract", () => {
	it("records unavailable scenarios as blocked", () => {
		const scenario = { id: "blocked-readiness-contract", scenario_timeout_ms: 100 };
		const result = emptyResult(scenario);
		finalizeScenarioResult(scenario, result, 10);
		expect(result.status).to.equal("blocked");
		expect(result.blocked_reason).to.include("readiness");
		expect(result.evidence).to.deep.equal([]);
		return cy.task("runtime:recordHarness", canonical(result), { log: false });
	});

	it("records elapsed scenario deadlines as blocked", () => {
		const scenario = { id: "blocked-timeout-contract", scenario_timeout_ms: 100 };
		const result = emptyResult(scenario);
		result.ready = true;
		finalizeScenarioResult(scenario, result, scenario.scenario_timeout_ms + 1);
		expect(result.status).to.equal("blocked");
		expect(result.blocked_reason).to.include(`exceeded ${scenario.scenario_timeout_ms} ms`);
		expect(result.evidence).to.deep.equal([]);
		return cy.task("runtime:recordHarness", canonical(result), { log: false });
	});
});

for (const scenario of plan.scenarios) {
	it(scenario.id, {
		defaultCommandTimeout: scenario.step_timeout_ms,
		pageLoadTimeout: scenario.step_timeout_ms,
		requestTimeout: scenario.step_timeout_ms,
		responseTimeout: scenario.step_timeout_ms,
	}, function () {
		const started = Date.now();
		const result = emptyResult(scenario);
		scenarioResults.set(scenario.id, { artifacts: [], resolvingLookup: false, result, started });
		cy.viewport(scenario.viewport.width, scenario.viewport.height);
		loginFor(scenario);
		cy.then(() => checkDeadline(scenario, started));

		if (scenario.kind === "email") {
			const user = plan.fixtures[scenario.fixture_id].user;
			cy.runtimeCall("frappe_lt.runtime_control.capture_welcome_email", {
				run_id: plan.run_id,
				scenario_id: scenario.id,
				token: plan.token,
				user,
			}).then((response) => {
				if (!response.body.message.output.includes("MIME-Version")) {
					throw new Error("captured email is not a MIME message");
				}
				if (typeof response.body.message.subject !== "string" || !response.body.message.subject) {
					throw new Error("captured email subject is unavailable");
				}
				verifyReadiness(scenario, result, { output: response.body.message.visible_output });
				collectServerLookups(
					scenario,
					result,
					response.body.message.lookups,
					"email:subject-body",
					response.body.message.visible_output
				);
				cy.then(() => {
					checkDeadline(scenario, started);
					if (!result.fallbacks.some((finding) => !finding.active)) {
						scenarioResults.get(scenario.id).artifacts = [
							{ content: response.body.message.output, kind: "email", mime: "text/plain" },
						];
					}
					finishScenario(scenario, result, started);
				});
			});
			return;
		}

		if (scenario.kind === "print") {
			const fixture = plan.fixtures[scenario.fixture_id];
			let printCapture;
			cy.runtimeCall("frappe_lt.runtime_control.capture_print", {
				doctype: fixture.doctype,
				name: fixture.name,
				run_id: plan.run_id,
				scenario_id: scenario.id,
				token: plan.token,
			}).then((capture) => {
				printCapture = capture.body.message;
				expect(printCapture.status).to.eq(200);
				expect(printCapture.suppressed_access_logs).to.eq(1);
				cy.document().then((document) => {
					document.open();
					document.write(printCapture.html);
					document.close();
				});
				cy.get("body").should("be.visible");
				verifyReadiness(scenario, result, { output: printCapture.html });
				collectServerLookups(
					scenario,
					result,
					printCapture.lookups,
					"print:http-body",
					printCapture.html
				);
				recordLayout(result);
				cy.then(() => {
					checkDeadline(scenario, started);
					if (!result.fallbacks.some((finding) => !finding.active)) {
						scenarioResults.get(scenario.id).artifacts = [
							{ content: printCapture.html, kind: "print", mime: "text/html" },
						];
					}
					finishScenario(scenario, result, started);
				});
			});
			return;
		}

		let portalArtifact = null;
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
				cy.visit("/desk", { onBeforeLoad: armLookupRecorder });
			} else {
				cy.visit(route);
			}
			if (scenario.kind === "desk") {
				cy.get("body").should("have.attr", "data-ajax-state", "complete");
				installLookupRecorder();
				cy.window().then((window) => window.frappe.set_route(route.replace(/^\/desk\/?/, "")));
				cy.get("body").should("have.attr", "data-ajax-state", "complete");
				verifyReadiness(scenario, result);
				collectLookups(scenario, result);
			} else {
				cy.get("body").should("be.visible");
				verifyReadiness(scenario, result);
				cy.runtimeCall("frappe_lt.runtime_control.capture_portal", {
					route,
					run_id: plan.run_id,
					scenario_id: scenario.id,
					token: plan.token,
				}).then((response) => {
					expect(response.body.message.status).to.eq(200);
					portalArtifact = { content: response.body.message.html, kind: "portal", mime: "text/html" };
					collectServerLookups(
						scenario,
						result,
						response.body.message.lookups,
						"portal:http-body",
						response.body.message.html
					);
				});
			}
			recordLayout(result);
			cy.then(() => {
				checkDeadline(scenario, started);
				if (portalArtifact && !result.fallbacks.some((finding) => !finding.active)) {
					scenarioResults.get(scenario.id).artifacts = [portalArtifact];
				}
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
			const durationMs = recorded ? Date.now() - recorded.started : 0;
			const error = recorded?.resolvingLookup
				? "runtime translation lookup request failed"
				: redactError(this.currentTest.err?.message || "Cypress assertion failed");
			const blocked = !result.ready || error.includes("scenario exceeded");
			result.blocked_reason = blocked ? error : null;
			result.error = blocked ? null : error;
			result.status = blocked ? "blocked" : "fail";
			finalizeScenarioResult(scenario, result, durationMs);
			const artifacts = result.fallbacks.some((finding) => !finding.active)
				? []
				: recorded?.artifacts || [];
			return publishScenario(scenario, result, artifacts);
		}
	}
});
