import pytest

from app.core.config import Settings
from app.integrations.rpa.wing_price_lock_bot import WingPriceLockBot, WingPriceLockError


def _settings(**overrides) -> Settings:
    base = dict(use_mock_rpa=True, database_url_sync="sqlite:///:memory:")
    base.update(overrides)
    return Settings(**base)


@pytest.mark.asyncio
async def test_mock_mode_always_succeeds_without_session():
    bot = WingPriceLockBot(settings=_settings())

    result = await bot.disable_auto_price_adjustment("HQ2312")

    assert result is True


@pytest.mark.asyncio
async def test_explicit_use_mock_false_overrides_settings_default():
    bot = WingPriceLockBot(settings=_settings(use_mock_rpa=True), use_mock=False)

    with pytest.raises(WingPriceLockError, match="세션"):
        await bot.disable_auto_price_adjustment("HQ2312")


@pytest.mark.asyncio
async def test_real_mode_without_session_cookies_raises_clear_error():
    bot = WingPriceLockBot(settings=_settings(use_mock_rpa=False), session_cookies=None)

    with pytest.raises(WingPriceLockError, match="save_wing_session"):
        await bot.disable_auto_price_adjustment("HQ2312")


def test_defaults_to_settings_use_mock_rpa_flag():
    bot_mock = WingPriceLockBot(settings=_settings(use_mock_rpa=True))
    bot_real = WingPriceLockBot(settings=_settings(use_mock_rpa=False))

    assert bot_mock._use_mock is True  # noqa: SLF001
    assert bot_real._use_mock is False  # noqa: SLF001
