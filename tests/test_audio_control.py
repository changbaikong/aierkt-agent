from audio_control import adjust_volume, available


def test_audio_control_reports_backend_state():
    result = available()
    assert result["available"] is False
    assert "backend" in result


def test_audio_adjustment_without_backend_is_explicitly_unavailable():
    result = adjust_volume(-10)
    assert result["status"] == "unavailable"
    assert "音量" in result["message"]
