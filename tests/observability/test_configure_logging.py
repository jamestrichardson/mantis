"""Tests for configure_logging(): the process-startup entry point that
wires the ``mantis`` logger tree to JSONFormatter. Kept separate from
test_logging.py since it mutates real logger state and must clean up
after itself.
"""

from __future__ import annotations

import logging

from mantis.observability.logging import JSONFormatter, configure_logging


def _reset_mantis_logger():
    logger = logging.getLogger("mantis")
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


def test_configure_logging_attaches_a_json_formatter():
    _reset_mantis_logger()
    try:
        configure_logging("INFO")
        logger = logging.getLogger("mantis")

        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0].formatter, JSONFormatter)
        assert logger.level == logging.INFO
        assert logger.propagate is False
    finally:
        _reset_mantis_logger()


def test_configure_logging_defaults_to_env_var(monkeypatch):
    _reset_mantis_logger()
    monkeypatch.setenv("MANTIS_LOG_LEVEL", "WARNING")
    try:
        configure_logging()
        logger = logging.getLogger("mantis")
        assert logger.level == logging.WARNING
    finally:
        _reset_mantis_logger()


def test_configure_logging_is_idempotent_about_handler_count():
    # Calling it twice (e.g. a test importing a CLI entry point twice)
    # must not accumulate duplicate handlers and double-emit every line.
    _reset_mantis_logger()
    try:
        configure_logging("INFO")
        configure_logging("INFO")
        logger = logging.getLogger("mantis")
        assert len(logger.handlers) == 1
    finally:
        _reset_mantis_logger()
