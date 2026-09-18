from datetime import timedelta

DELTA_THRESHOLD = 0.33  # percentage point change to alert on metrics

ALERT_CACHE_KEY = "metrics:alert"
ALERT_CACHE_TIMEOUT = timedelta(days=1.5).total_seconds()

ALERT_LIMIT = timedelta(hours=1)  # window to limit alerts for a single team

DISABLED_REMINDER_THRESHOLD = timedelta(days=7)
