app_name = "frappe_lt"
app_title = "Frappe LT"
app_publisher = "lokysai"
app_description = "Lithuanian translations for Frappe and ERPNext"
app_email = ""
app_license = "GPL-3.0-only"

required_apps = ["erpnext"]

before_install = "frappe_lt.profile.before_install"
after_install = "frappe_lt.profile.after_install"
before_uninstall = "frappe_lt.profile.before_uninstall"
after_uninstall = "frappe_lt.profile.after_uninstall"
