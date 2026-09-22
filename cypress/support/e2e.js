Cypress.Commands.add("runtimeLogin", (scenario) => {
	const plan = Cypress.env("runtimePlan");
	const csrfToken = Cypress.env("runtimeCsrfToken");
	return cy.request({
		body: { run_id: plan.run_id, scenario_id: scenario.id, token: plan.token },
		headers: csrfToken ? { "X-Frappe-CSRF-Token": csrfToken } : {},
		log: false,
		method: "POST",
		url: "/api/method/frappe_lt.runtime_control.runtime_login",
	}).then((response) => {
		const csrfToken = response.body.message.csrf_token;
		if (!csrfToken) throw new Error("runtime login did not return a CSRF token");
		Cypress.env("runtimeCsrfToken", csrfToken);
	});
});

Cypress.Commands.add("runtimeCall", (method, body) => {
	const csrfToken = Cypress.env("runtimeCsrfToken");
	if (!csrfToken) throw new Error("authenticated Frappe CSRF token is unavailable");
	return cy.request({
		body,
		failOnStatusCode: false,
		headers: { "X-Frappe-CSRF-Token": csrfToken },
		log: false,
		method: "POST",
		url: `/api/method/${method}`,
	}).then((response) => {
		if (response.status < 200 || response.status >= 300) {
			throw new Error(`runtime control request failed with HTTP ${response.status}`);
		}
		return response;
	});
});

Cypress.on("uncaught:exception", (error) => {
	throw error;
});
