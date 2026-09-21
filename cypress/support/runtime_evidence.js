const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const ARTIFACTS = {
	browser: { filename: "browser.json", mime: "application/json" },
	email: { filename: "email.txt", mime: "text/plain" },
	portal: { filename: "portal.html", mime: "text/html" },
	print: { filename: "print.html", mime: "text/html" },
};
const SAFE_SCENARIO_ID = /^[a-z0-9][a-z0-9._:-]{0,159}$/;
const DEFAULT_LIMITS = {
	artifact: 2 * 1024 * 1024,
	run: 32 * 1024 * 1024,
	scenario: 8 * 1024 * 1024,
};

function escaped(value) {
	return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function canonical(value) {
	if (Array.isArray(value)) return value.map(canonical);
	if (value && typeof value === "object") {
		return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
	}
	return value;
}

function redact(content, secrets) {
	let text = String(content);
	for (const secret of secrets) {
		if (typeof secret !== "string" || !secret) continue;
		for (const form of new Set([secret, encodeURIComponent(secret), encodeURI(secret)])) {
			text = text.replace(new RegExp(escaped(form), "g"), "[REDACTED]");
		}
	}
	return text
		.replace(/\b(authorization\s*:\s*)(?:bearer\s+)?[^\s,;}]+/gi, "$1[REDACTED]")
		.replace(/\b((?:set-)?cookie\s*:\s*)[^\r\n]+/gi, "$1[REDACTED]")
		.replace(
			/(["']?(?:password|pwd|token|csrf_token|api_key|api_secret|access_token|reset_token)["']?\s*[:=]\s*)["'][^"']*["']/gi,
			'$1"[REDACTED]"'
		)
		.replace(
			/\b(password|pwd|token|csrf_token|api_key|api_secret|access_token|reset_token|key|sid)\b\s*[:=]\s*[^\s,;}<>&"']+/gi,
			"$1=[REDACTED]"
		)
		.replace(
			/([?&](?:key|token|csrf_token|api_key|api_secret|access_token|reset_token)=)[^&\s<>"']+/gi,
			"$1[REDACTED]"
		)
		.replace(
			/(%3[fF]|%26)(?:key|token|csrf_token|api_key|api_secret|access_token|reset_token)%3[dD][^%\s<>"']+/gi,
			"$1key%3D[REDACTED]"
		)
		.replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, "[REDACTED]")
		.replace(/\b[A-Z0-9._+-]+%40[A-Z0-9.-]+(?:\.|%2e)[A-Z]{2,}\b/gi, "[REDACTED]")
		.replace(/frappe-lt-runtime-[0-9a-f]{32}[A-Za-z0-9@._:-]*/gi, "[REDACTED]")
		.replace(
			/((?:data-)?(?:csrf[-_]token|api[-_]key|api[-_]secret|access[-_]token|reset[-_]token)=["'])[^"']*/gi,
			"$1[REDACTED]"
		)
		.replace(/<input\b[^>]*\btype=["'](?:hidden|password)["'][^>]*>/gi, '<input value="[REDACTED]">');
}

function assertRedacted(content, secrets) {
	if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(content)) {
		throw new Error("evidence redaction safety check failed: control character");
	}
	for (const secret of secrets) {
		if (typeof secret !== "string" || !secret) continue;
		if ([secret, encodeURIComponent(secret), encodeURI(secret)].some((form) => content.includes(form))) {
			throw new Error("evidence redaction safety check failed: supplied secret remains");
		}
	}
	if (
		/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/i.test(content) ||
		/\b[A-Z0-9._+-]+%40[A-Z0-9.-]+(?:\.|%2e)[A-Z]{2,}\b/i.test(content)
	) {
		throw new Error("evidence redaction safety check failed: recipient address remains");
	}
	if (
		/\b(?:authorization|(?:set-)?cookie)\s*:\s*(?!\[REDACTED\])\S/i.test(content) ||
		/(?:["']?(?:password|pwd|token|csrf_token|api_key|api_secret|access_token|reset_token|sid)["']?\s*[:=]\s*)["']?(?!\[REDACTED\])[^\s,;}<>&"']+/i.test(content) ||
		/((?:data-)?(?:csrf[-_]token|api[-_]key|api[-_]secret|access[-_]token|reset[-_]token)=["'])(?!\[REDACTED\])[^"']+/i.test(content) ||
		/(?:[?&](?:key|token|csrf_token|api_key|api_secret|access_token|reset_token)=|(?:%3f|%26)(?:key|token|csrf_token|api_key|api_secret|access_token|reset_token)%3d)(?!\[REDACTED\])[^&%\s<>"']+/i.test(content) ||
		/frappe-lt-runtime-[0-9a-f]{32}[A-Za-z0-9@._:-]*/i.test(content)
	) {
		throw new Error("evidence redaction safety check failed: sensitive material remains");
	}
}

function redactJson(value, secrets) {
	const redacted = redact(JSON.stringify(value), secrets);
	assertRedacted(redacted, secrets);
	return JSON.parse(redacted);
}

function ensureDirectory(root, components) {
	let current = root;
	for (const component of components) {
		current = path.join(current, component);
		try {
			fs.mkdirSync(current, { mode: 0o700 });
		} catch (error) {
			if (error.code !== "EEXIST") throw error;
		}
		if (fs.lstatSync(current).isSymbolicLink()) throw new Error("evidence path uses a symlink");
		if (!fs.statSync(current).isDirectory()) throw new Error("evidence path component is not a directory");
	}
	return current;
}

function safeLimit(value, fallback, label) {
	const limit = value === undefined ? fallback : value;
	if (!Number.isSafeInteger(limit) || limit < 1) throw new Error(`${label} must be a positive integer`);
	return limit;
}

class EvidencePublisher {
	constructor(
		runRoot,
		{
			diagnosticSampling = false,
			maxArtifactBytes,
			maxRunBytes,
			maxScenarioBytes,
			secrets = [],
		} = {}
	) {
		if (typeof runRoot !== "string" || !runRoot || fs.lstatSync(runRoot).isSymbolicLink()) {
			throw new Error("runtime evidence root must be an existing non-symlink directory");
		}
		this.runRoot = path.resolve(runRoot);
		if (!fs.statSync(this.runRoot).isDirectory()) throw new Error("runtime evidence root must be a directory");
		if (typeof diagnosticSampling !== "boolean" || !Array.isArray(secrets)) {
			throw new Error("evidence policy options are malformed");
		}
		if (secrets.some((secret) => typeof secret !== "string")) {
			throw new Error("evidence secrets must be text");
		}
		this.diagnosticSampling = diagnosticSampling;
		this.secrets = secrets;
		this.maxArtifactBytes = safeLimit(maxArtifactBytes, DEFAULT_LIMITS.artifact, "artifact byte limit");
		this.maxScenarioBytes = safeLimit(maxScenarioBytes, DEFAULT_LIMITS.scenario, "scenario byte limit");
		this.maxRunBytes = safeLimit(maxRunBytes, DEFAULT_LIMITS.run, "run byte limit");
		this.runBytes = 0;
		this.scenarioBytes = new Map();
	}

	publish({ artifacts, scenarioId, status }) {
		if (typeof scenarioId !== "string" || !SAFE_SCENARIO_ID.test(scenarioId)) {
			throw new Error("invalid scenario id for evidence");
		}
		if (!["blocked", "fail", "pass"].includes(status) || !Array.isArray(artifacts)) {
			throw new Error("invalid evidence publication request");
		}
		if (status === "pass" && !this.diagnosticSampling) return [];
		const prepared = artifacts.map((artifact) => {
			const policy = ARTIFACTS[artifact.kind];
			if (!policy || artifact.mime !== policy.mime || typeof artifact.content !== "string") {
				throw new Error("evidence kind or MIME is not allowed");
			}
			if (Buffer.byteLength(artifact.content, "utf8") > this.maxArtifactBytes) {
				throw new Error("evidence artifact byte limit exceeded");
			}
			const relativePath = `evidence/${scenarioId}/${policy.filename}`;
			let redacted = redact(artifact.content, this.secrets);
			assertRedacted(redacted, this.secrets);
			if (artifact.mime === "application/json") {
				try {
					redacted = JSON.stringify(canonical(JSON.parse(redacted))) + "\n";
				} catch (error) {
					throw new Error(`evidence JSON is invalid: ${error.message}`);
				}
			}
			const content = Buffer.from(redacted, "utf8");
			if (content.length > this.maxArtifactBytes) throw new Error("evidence artifact byte limit exceeded");
			return { artifact, content, policy, relativePath };
		}).sort((left, right) =>
			left.relativePath < right.relativePath ? -1 : left.relativePath > right.relativePath ? 1 : 0
		);
		if (new Set(prepared.map(({ relativePath }) => relativePath)).size !== prepared.length) {
			throw new Error("duplicate evidence artifact kind");
		}
		const publicationBytes = prepared.reduce((total, { content }) => total + content.length, 0);
		const scenarioBytes = (this.scenarioBytes.get(scenarioId) || 0) + publicationBytes;
		if (scenarioBytes > this.maxScenarioBytes) throw new Error("evidence scenario byte limit exceeded");
		if (this.runBytes + publicationBytes > this.maxRunBytes) {
			throw new Error("evidence run byte limit exceeded");
		}
		const published = [];
		const created = [];
		try {
			for (const { artifact, content, policy, relativePath } of prepared) {
				const directory = ensureDirectory(this.runRoot, ["evidence", scenarioId]);
				const absolutePath = path.join(directory, policy.filename);
				const descriptor = fs.openSync(
					absolutePath,
					fs.constants.O_WRONLY |
						fs.constants.O_CREAT |
						fs.constants.O_EXCL |
						(fs.constants.O_NOFOLLOW || 0),
					0o600
				);
				created.push(absolutePath);
				try {
					fs.writeFileSync(descriptor, content);
					fs.fsyncSync(descriptor);
				} finally {
					fs.closeSync(descriptor);
				}
				published.push({
					bytes: content.length,
					kind: artifact.kind,
					mime: artifact.mime,
					path: relativePath,
					sha256: crypto.createHash("sha256").update(content).digest("hex"),
				});
			}
		} catch (error) {
			for (const absolutePath of created) fs.rmSync(absolutePath, { force: true });
			throw error;
		}
		this.scenarioBytes.set(scenarioId, scenarioBytes);
		this.runBytes += publicationBytes;
		return published;
	}
}

module.exports = { EvidencePublisher, redactJson };
