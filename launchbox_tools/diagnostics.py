from __future__ import annotations


def describe_exception(exc: BaseException) -> str:
    message = str(exc).strip()
    return message or type(exc).__name__
