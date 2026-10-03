"""Closed operational diagnostics; payloads and traceback text are never output.

Library logs have no application-owned safe message contract. Project their
records before ANY handler (including later-installed handlers) formats them.
This is a structural projection, not a best-effort content/secret sanitizer.
"""

import logging
import re


def exception_type(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "Exception"


def log_failure(logger: logging.Logger, *, component: str, code: str, exc: BaseException) -> None:
    # component/code are closed constants at the call sites, never request data.
    logger.error("component=%s code=%s error_type=%s", component, code, exception_type(exc))


def configure_private_logging() -> None:
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_meyar_private", False):
        return

    class PrivateFactory:
        _meyar_private = True

        def __call__(self, *args, **kwargs):
            record = previous(*args, **kwargs)
            component = None
            for prefix, name in (
                ("sqlalchemy", "database"), ("uvicorn", "server"),
                ("httpx", "local_http"), ("httpcore", "local_http"),
            ):
                if record.name == prefix or record.name.startswith(prefix + "."):
                    component = name
                    break
            if component is None:
                return record
            error_type = (
                exception_type(record.exc_info[1])
                if record.exc_info and record.exc_info[1] else "none"
            )
            message = f"component={component} code=LIBRARY_DIAGNOSTIC error_type={error_type}"
            if record.name == "uvicorn.access":
                # Uvicorn's access args: client address, method, raw target, version, status.
                values = record.args
                method, status = "OTHER", 0
                if isinstance(values, tuple) and len(values) == 5:
                    if values[1] in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                        method = values[1]
                    if isinstance(values[4], int) and 100 <= values[4] <= 599:
                        status = values[4]
                # Preserve the formatter's five-argument interface; omit target/address.
                message = '%s - "%s %s HTTP/%s" %s'
                record.args = ("private", method, "private", "1.1", status)
            else:
                record.args = ()
            record.msg = message
            record.exc_info = record.exc_text = record.stack_info = None
            return record

    logging.setLogRecordFactory(PrivateFactory())


configure_private_logging()
