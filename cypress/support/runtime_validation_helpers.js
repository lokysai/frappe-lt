function escapeCss(value) {
	if (globalThis.CSS?.escape) return globalThis.CSS.escape(value);
	return String(value).replace(/[^a-zA-Z0-9_-]/g, (character) => `\\${character}`);
}

function meaningfulTarget(element) {
	if (element.dataset?.fieldname) return `[data-fieldname="${escapeCss(element.dataset.fieldname)}"]`;
	if (element.getAttribute("role")) return `[role="${escapeCss(element.getAttribute("role"))}"]`;
	if (element.id) return `#${escapeCss(element.id)}`;
	if (element.getAttribute("name")) return `[name="${escapeCss(element.getAttribute("name"))}"]`;
	if (element.getAttribute("data-label")) {
		return `[data-label="${escapeCss(element.getAttribute("data-label"))}"]`;
	}
	if (element.getAttribute("aria-label")) {
		return `[aria-label="${escapeCss(element.getAttribute("aria-label"))}"]`;
	}
	return null;
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
	const pattern = template
		.split(marker)
		.map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
		.join("[\\s\\S]+?");
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

module.exports = {
	correlateOutput,
	exactOutputExclusion,
	isClippedByAncestor,
	meaningfulTarget,
	renderedIntervals,
};
