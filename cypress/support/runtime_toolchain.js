function browserFromRunResults(results) {
	const name = results?.browserName;
	const version = results?.browserVersion;
	if (typeof name !== "string" || !name || typeof version !== "string" || !version) {
		throw new Error("Cypress did not report an exact browser version");
	}
	return { name, version };
}

module.exports = { browserFromRunResults };
