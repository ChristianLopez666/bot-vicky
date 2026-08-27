from sheet_log_policy import should_persist_log


def test_successful_bot_reply_is_not_persisted():
    assert should_persist_log("saliente", "bot", "ok", "") is False


def test_inbound_client_message_is_persisted():
    assert should_persist_log("entrante", "cliente", "", "") is True


def test_failed_bot_reply_is_persisted():
    assert should_persist_log("saliente", "bot", "error", "Meta failure") is True


def test_advisor_event_is_persisted():
    assert should_persist_log("saliente", "asesor", "ok", "") is True


def test_lead_backup_is_persisted():
    assert should_persist_log("respaldo_lead", "sistema", "advisor_notify_failed", "") is True
