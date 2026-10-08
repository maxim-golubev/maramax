import logging

from parakeet_dictation import logger_config


def test_a_log_file_that_cannot_be_opened_is_named_with_the_reason(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(logger_config, "_LOGGER_CONFIGURED", True)
    monkeypatch.setattr(logger_config.logger, "handlers", [])
    blocker = tmp_path / "logs"
    blocker.write_text("a file where the log folder should be")
    log_path = blocker / "maramax.log"

    with caplog.at_level(logging.WARNING, logger=logger_config.logger.name):
        logger_config.setup_logging(log_path)

    assert logger_config.logger.handlers == []
    assert f"Could not open diagnostic log {log_path}: " in caplog.text
    assert "File exists" in caplog.text


def test_console_lines_take_the_tint_of_their_severity_unless_told_not_to():
    record = logging.LogRecord("maramax", logging.WARNING, __file__, 1, "disk %s", ("full",), None)
    plain = logger_config.ConsoleFormatter(tinted=False).format(record)
    tinted = logger_config.ConsoleFormatter(tinted=True).format(record)
    stamp = plain.split(" - ")[0]
    assert plain == f"{stamp} - WARNING - disk full"
    assert tinted == f"{stamp} - \033[33mWARNING - disk full\033[0m"
    custom = logging.LogRecord("maramax", logging.ERROR + 5, __file__, 1, "between levels", (), None)
    assert "\033[31m" in logger_config.ConsoleFormatter(tinted=True).format(custom)   # The tint of ERROR, the level below.
    trace = logging.LogRecord("maramax", 5, __file__, 1, "below debug", (), None)
    assert "\033[" not in logger_config.ConsoleFormatter(tinted=True).format(trace)
