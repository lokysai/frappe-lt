function escapeCss(value) {
	if (globalThis.CSS?.escape) return globalThis.CSS.escape(value);
	return String(value).replace(/[^a-zA-Z0-9_-]/g, (character) => `\\${character}`);
}

function meaningfulTarget(element) {
	if (element.dataset?.fieldname) return `[data-fieldname="${escapeCss(element.dataset.fieldname)}"]`;
	if (element.id) return `#${escapeCss(element.id)}`;
	if (element.getAttribute("name")) return `[name="${escapeCss(element.getAttribute("name"))}"]`;
	if (element.getAttribute("data-label")) {
		return structuralAttributeTarget(element, "data-label");
	}
	if (element.getAttribute("aria-label")) {
		return structuralAttributeTarget(element, "aria-label");
	}
	if (element.getAttribute("role")) return `[role="${escapeCss(element.getAttribute("role"))}"]`;
	return null;
}

function structuralAttributeTarget(element, attribute) {
	const parts = [];
	for (let candidate = element; candidate?.tagName; candidate = candidate.parentElement) {
		if (candidate.id) {
			parts.unshift(`#${escapeCss(candidate.id)}`);
			break;
		}
		const tag = candidate.tagName.toLowerCase();
		let part = candidate === element ? `${tag}[${attribute}]` : tag;
		const siblings = candidate.parentElement?.children
			? [...candidate.parentElement.children].filter((sibling) => sibling.tagName === candidate.tagName)
			: [];
		if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(candidate) + 1})`;
		parts.unshift(part);
		if (tag === "body") break;
	}
	return parts.join(" > ") || `[${attribute}]`;
}

function isVisuallyHidden(element, getComputedStyle) {
	for (let candidate = element; candidate; candidate = candidate.parentElement) {
		const style = getComputedStyle(candidate);
		const bounds = candidate.getBoundingClientRect();
		const tiny = bounds.width <= 2 && bounds.height <= 2;
		const clipped = style.clip && !["auto", "none"].includes(style.clip);
		const clipPath = style.clipPath && style.clipPath !== "none";
		const clipValues = String(style.clip || "").match(/-?\d+(?:\.\d+)?/g);
		const fullyClipped =
			String(style.clip || "").startsWith("rect(") &&
			clipValues?.length === 4 &&
			clipValues.every((value) => Number(value) === 0);
		const screenReaderOnly =
			candidate.classList?.contains("sr-only") &&
			style.position === "absolute" &&
			["clip", "hidden"].includes(style.overflow) &&
			fullyClipped;
		if (screenReaderOnly) return true;
		if (tiny && (clipped || clipPath)) return true;
	}
	return false;
}

function isClippedByAncestor(element, getComputedStyle) {
	const bounds = element.getBoundingClientRect();
	for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
		const style = getComputedStyle(ancestor);
		const ancestorBounds = ancestor.getBoundingClientRect();
		const clipsX = ["clip", "hidden"].includes(style.overflowX);
		const clipsY = ["clip", "hidden"].includes(style.overflowY);
		if (
			(clipsX && (bounds.left < ancestorBounds.left || bounds.right > ancestorBounds.right)) ||
			(clipsY && (bounds.top < ancestorBounds.top || bounds.bottom > ancestorBounds.bottom))
		) {
			return true;
		}
	}
	return false;
}

function isDisabledControl(element) {
	return Boolean(element.disabled) || element.getAttribute?.("aria-disabled") === "true";
}

function isUnusableControl(element, style, viewportWidth) {
	const bounds = element.getBoundingClientRect();
	if (isDisabledControl(element)) return false;
	const outsideHorizontally = bounds.left < 0 || bounds.right > viewportWidth;
	const minimumHitArea = viewportWidth <= 480 ? 24 : 8;
	const type = element.getAttribute?.("type")?.toLowerCase();
	const associatedLabels = ["checkbox", "radio"].includes(type) ? [...(element.labels || [])] : [];
	const hitBounds = associatedLabels.reduce(
		(largest, label) => {
			const candidate = label.getBoundingClientRect();
			return candidate.width * candidate.height > largest.width * largest.height ? candidate : largest;
		},
		bounds
	);
	return (
		outsideHorizontally ||
		style.pointerEvents === "none" ||
		hitBounds.width < minimumHitArea ||
		hitBounds.height < minimumHitArea
	);
}

function renderedIntervals(output, effective) {
	if (typeof effective !== "string" || !effective) return [];
	const exact = [];
	for (let start = output.indexOf(effective); start >= 0; start = output.indexOf(effective, start + 1)) {
		exact.push({ start, end: start + effective.length });
	}
	if (exact.length) return exact;
	const marker = "__FRAPPE_LT_RENDERED_VALUE__";
	const template = effective
		.replace(/\{[^{}]+\}/g, marker)
		.replace(/%\([^)]+\)[#0 +\-]?\d*(?:\.\d+)?[a-zA-Z]/g, marker)
		.replace(/%[sdif]/g, marker);
	if (!template.includes(marker)) return [];
	const parts = template.split(marker);
	let pattern = "";
	for (let index = 0; index < parts.length; index += 1) {
		pattern += parts[index].replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
		if (index < parts.length - 1) {
			pattern += index === parts.length - 2 && parts[index + 1] === "" ? "[^<\\r\\n]+" : "[\\s\\S]+?";
		}
	}
	return [...output.matchAll(new RegExp(pattern, "g"))].map((match) => ({
		start: match.index,
		end: match.index + match[0].length,
	}));
}

function correlateOutput(output, effective) {
	const intervals = renderedIntervals(output, effective);
	return {
		interval: intervals.length === 1 ? intervals[0] : null,
		renderStatus: intervals.length === 0 ? "unrendered" : intervals.length === 1 ? "unique" : "ambiguous",
	};
}

function exactOutputExclusion(output, correlation, exclusions, approvedValues) {
	if (correlation.renderStatus !== "unique" || !correlation.interval) return null;
	const rendered = output.slice(correlation.interval.start, correlation.interval.end);
	for (const exclusion of exclusions) {
		if ((approvedValues[exclusion.target] || []).includes(rendered)) return exclusion;
	}
	return null;
}

function isBlockingFallback(finding) {
	return (
		finding.active &&
		finding.render_status !== "unrendered" &&
		finding.visible &&
		!finding.excluded &&
		(finding.source === "missing" || finding.effective.trim() === finding.key.source)
	);
}

function finalizeScenarioResult(scenario, result, durationMs) {
	result.duration_ms = durationMs;
	if (!result.ready && result.status === "pass") {
		result.status = "blocked";
		result.blocked_reason = "scenario did not prove readiness";
	}
	if (durationMs > scenario.scenario_timeout_ms && result.status === "pass") {
		result.status = "blocked";
		result.blocked_reason = `scenario exceeded ${scenario.scenario_timeout_ms} ms`;
	}
	if (result.status === "pass") {
		const blockingFallback = result.fallbacks.some(isBlockingFallback);
		const blockingLayout = result.layouts.some((finding) => finding.severity === "functional");
		if (blockingFallback || blockingLayout) {
			result.status = "fail";
			result.error = "scenario produced blocking runtime findings";
		}
		if (!result.fallbacks.some((finding) => finding.active) && result.status === "pass") {
			result.status = "blocked";
			result.blocked_reason = "scenario produced no active effective translation lookup evidence";
		}
	}
	const attempt = result.attempts[result.attempts.length - 1];
	attempt.duration_ms = result.duration_ms;
	attempt.outcome = result.status === "fail" ? "assertion_failure" : result.status;
	attempt.error = result.status === "pass" ? null : result.blocked_reason || result.error;
	return result;
}

module.exports = {
	correlateOutput,
	exactOutputExclusion,
	finalizeScenarioResult,
	isBlockingFallback,
	isClippedByAncestor,
	isDisabledControl,
	isUnusableControl,
	isVisuallyHidden,
	meaningfulTarget,
	renderedIntervals,
};
