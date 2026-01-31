import os


def get_bool_env(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    val = val.lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


def get_notifier_config():
    return {
        "enabled": os.getenv("FILMON_NOTIFY", "0") == "1",
        "pushover_token": os.getenv("PUSHOVER_TOKEN"),
        "pushover_user": os.getenv("PUSHOVER_USER"),
    }
