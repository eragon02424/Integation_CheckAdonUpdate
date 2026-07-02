"""Konstanten für Addon Update Checker."""

DOMAIN = "addon_update_checker"
PLATFORMS = ["sensor"]

# Config Entry Keys
CONF_GITHUB_USERNAME = "github_username"
CONF_SCAN_INTERVAL = "scan_interval"

# Default Werte
DEFAULT_SCAN_INTERVAL_MINUTES = 1440  # 24 Stunden
MIN_SCAN_INTERVAL_MINUTES = 1          # 1 Minute (zum Testen)
MAX_SCAN_INTERVAL_MINUTES = 10080      # 1 Woche

# GitHub API
GITHUB_API_BASE = "https://api.github.com"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com"

# Patterns um externe GitHub Release URLs in Dockerfiles zu erkennen
# Unterstützte Formate:
#   https://github.com/owner/repo/releases/download/v1.2.3/...
#   https://api.github.com/repos/owner/repo/releases/latest
#   FROM owner/image:1.2.3  (Docker Hub mit version tag)
DOCKERFILE_GITHUB_PATTERNS = [
    # GitHub Releases Download Link mit fester Version
    r'https://github\.com/([\w-]+)/([\w-]+)/releases/download/v?([\d\.]+)/',
    # GitHub API releases/latest (dynamisch - keine feste Version)
    r'https://api\.github\.com/repos/([\w-]+)/([\w-]+)/releases/latest',
    # GitHub Releases Download Link mit Variable ${LATEST} oder ähnlich
    r'https://github\.com/([\w-]+)/([\w-]+)/releases/download/\$\{?\w+\}?/',
]

# Storage Key für persistente Versionsdaten (überlebt HA Neustart)
STORAGE_KEY = "addon_update_checker_versions"
STORAGE_VERSION = 1
