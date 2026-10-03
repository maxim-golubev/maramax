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
