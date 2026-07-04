import os


def get_env(name, default=None):
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def get_bool_env(name, default=False):
    value = get_env(name)
    if value is None:
        return default
    return value not in {"0", "false", "False", "no", "NO"}


def get_int_env(name, default):
    value = get_env(name)
    return default if value is None else int(value)


def get_float_env(name, default):
    value = get_env(name)
    return default if value is None else float(value)
