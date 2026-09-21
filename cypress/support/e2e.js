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
		headers: { "X-Frappe-CSRF-Token": csrfToken },
		log: false,
		method: "POST",
		url: `/api/method/${method}`,
	});
});

Cypress.on("uncaught:exception", (error) => {
	throw error;
});
