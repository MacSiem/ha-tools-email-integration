"""Constants for HA Tools Email."""

from __future__ import annotations

DOMAIN = "ha_tools_email"
NAME = "HA Tools Email"
VERSION = "2.0.3"

STORAGE_KEY = f"{DOMAIN}.storage"
STORAGE_VERSION = 1

DATA_LOAD_CONFIG = "load_config"
DATA_SCHEDULER_UNSUBS = "scheduler_unsubs"
DATA_SEND_EMAIL = "send_email"
DATA_SERVICES_REGISTERED = "services_registered"
DATA_STORAGE = "storage"
DATA_WS_REGISTERED = "ws_registered"

EVENT_SCHEDULES_CHANGED = f"{DOMAIN}_schedules_changed"

KIND_LOG_DIGEST = "log_digest"
KIND_ENERGY_REPORT = "energy_report"
VALID_KINDS = {KIND_LOG_DIGEST, KIND_ENERGY_REPORT}

CADENCE_DAILY = "daily"
CADENCE_WEEKLY = "weekly"
CADENCE_MONTHLY = "monthly"
VALID_CADENCES = {CADENCE_DAILY, CADENCE_WEEKLY, CADENCE_MONTHLY}

DEFAULT_SCHEDULE_TIME = "07:30"
DEFAULT_WEEKLY_WEEKDAY = 0
DEFAULT_MONTHLY_DAY = 1
DEFAULT_CURRENCY = "PLN"
DEFAULT_PRICE_PER_KWH = 0.0

SMTP_DEFAULTS = {
    "server": "",
    "port": 587,
    "username": "",
    "password": "",
    "sender": "",
    "encryption": "starttls",
    "default_recipient": "",
}
