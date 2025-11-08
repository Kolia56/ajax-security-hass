"""Constants for the Ajax integration."""

# Integration domain
DOMAIN = "ajax"

# Configuration and defaults
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_DEVICE_ID = "device_id"

# Default values
DEFAULT_DEVICE_MODEL = "Home Assistant"
DEFAULT_VERSION = "3.34"
DEFAULT_OS = "Linux"
DEFAULT_APP_LABEL = "Ajax"
DEFAULT_DEVICE_TYPE = "MOBILE"

# API endpoints
API_BASE_URL = "mobile-gw.prod.ajax.systems"
API_PORT = 443

# Update intervals (seconds)
UPDATE_INTERVAL = 30  # Poll every 30 seconds (real-time updates via streaming, polling is backup only)

# Device classes
DEVICE_CLASS_MOTION = "motion"
DEVICE_CLASS_DOOR = "door"
DEVICE_CLASS_WINDOW = "window"
DEVICE_CLASS_SMOKE = "smoke"
DEVICE_CLASS_TEMPERATURE = "temperature"
DEVICE_CLASS_BATTERY = "battery"

# Security states mapping
SECURITY_STATE_ARMED = "armed"
SECURITY_STATE_DISARMED = "disarmed"
SECURITY_STATE_NIGHT_MODE = "night_mode"
SECURITY_STATE_PARTIALLY_ARMED = "partially_armed"

# gRPC service paths
GRPC_LOGIN_SERVICE = "systems.ajax.api.ecosystem.v3.mobilegwsvc.service.login_by_password.LoginByPasswordService"
GRPC_GET_ACCOUNT_SERVICE = "systems.ajax.api.ecosystem.v3.mobilegwsvc.service.get_account.GetAccountService"
GRPC_SPACE_SERVICE = "systems.ajax.api.mobile.v2.space.SpaceService"
GRPC_SECURITY_SERVICE = "systems.ajax.api.mobile.v2.space.security.SpaceSecurityService"

# Platforms - will be implemented in future versions
# PLATFORMS = ["alarm_control_panel", "binary_sensor", "sensor"]
