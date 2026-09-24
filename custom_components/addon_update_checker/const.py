"""Konstanten fuer Addon Update Checker."""

DOMAIN = "addon_update_checker"
PLATFORMS = ["sensor"]

# Config Entry Keys
CONF_GITHUB_USERNAME = "github_username"
CONF_GITHUB_TOKEN = "github_token"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_AUTO_BUMP = "auto_bump"

# Default Werte
DEFAULT_SCAN_INTERVAL_MINUTES = 1440  # 24 Stunden
MIN_SCAN_INTERVAL_MINUTES = 1
MAX_SCAN_INTERVAL_MINUTES = 10080
DEFAULT_AUTO_BUMP = True

# GitHub API
GITHUB_API_BASE = "https://api.github.com"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com"

# PyPI API
PYPI_API_BASE = "https://pypi.org/pypi"

# Storage
STORAGE_KEY = "addon_update_checker_versions"
STORAGE_VERSION = 1
