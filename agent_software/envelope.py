import json

_RETRYABLE = {
    "bad_parameter": True,
    "sql_error": True,
    "timeout": True,
    "internal": False,
    "forbidden": False,
}

def ok(data):
    return json.dumps({
        "ok": True,
        "error_type": None,
        "error_message": None,
        "retryable": False,
        "data": data,
    },ensure_ascii=False)


def fail(error_type,error_message,retryable=None):
    if retryable is None:
        retryable = _RETRYABLE.get(error_type, False)
    return json.dumps({
        "ok": False,
        "error_type": error_type,
        "error_message": error_message,
        "retryable": retryable,
        "data": None,
    },ensure_ascii=False)