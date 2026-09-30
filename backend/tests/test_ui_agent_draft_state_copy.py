"""Issue #84 owner visual acceptance: the JD-draft assistant summary must be
truthful for the draft's validated server-owned state.

Three states, derived only from the validated draft view (never model prose):
READY_TO_CONFIRM, BLOCKED_REVIEW and NO_SCORABLE_CRITERIA. Empty criterion
tables are never rendered, and no state other than READY_TO_CONFIRM may say
"təsdiqləyin" or offer the confirm action.
"""

import re

import pytest
from fakes import FakeLLMProvider
from httpx import AsyncClient

from meyar.config import Settings, get_settings
from meyar.llm.dependency import get_llm_provider
from meyar.llm.provider import ModelSchemaInvalidError
from meyar.main import app

CONFIRM_ACTION = re.compile(r'action="/ui/agent/drafts/[0-9a-f-]+/confirm"')
CRITERIA_TABLE = 'class="criteria-table'
LEAKS = ("MODEL_VALIDATED", "DETERMINISTIC", "SEMANTIC_CONFLICT", "qwen", "fake-model")


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


async def _analyze(client: AsyncClient, tenant_and_user, message: str) -> str:
    _tenant, user, password, _membership = tenant_and_user
    # The model proposal is rejected, so every rendered state is owned by the
    # server's deterministic boundary alone (the copy must not depend on it).
    fake = FakeLLMProvider(jd_draft_error=ModelSchemaInvalidError("schema-invalid"))
    app.dependency_overrides[get_llm_provider] = lambda: fake
    login = await client.post(
        "/ui/login",
        data={"username": user.username, "password": password},
        follow_redirects=False,
    )
    assert login.status_code == 303
    page = await client.get("/ui/agent")
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page.text)
    assert match is not None
    response = await client.post("/ui/agent", data={"message": message, "csrf_token": match[1]})
    assert response.status_code == 200
    for leak in LEAKS:
        assert leak not in response.text
    # Span ids exist only as hidden form attributes, never as visible text.
    visible = re.sub(r"<[^>]+>", " ", response.text)
    assert "req-0" not in visible
    return response.text


async def test_ready_draft_says_confirm_and_renders_populated_sections(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    html = await _analyze(
        client,
        tenant_and_user,
        "Vakansiya: Backend Engineer\nPython tələb olunur.\nDocker üstünlükdür.",
    )
    assert (
        "MEYAR bu mətndən 2 meyar hazırladı: 1 mütləq, 1 üstünlük. "
        "Nəzərdən keçirin və təsdiqləyin." in html
    )
    assert "Mütləq tələblər" in html
    assert "Üstünlük tələbləri" in html
    assert html.count(CRITERIA_TABLE) == 2
    assert CONFIRM_ACTION.search(html)
    assert "Tələbləri təsdiqlə və namizədləri sırala" in html


async def test_unsupported_only_draft_never_offers_confirmation(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    html = await _analyze(
        client, tenant_and_user, "Vakansiya kimi analiz et: Namizəd Bakıda yaşamalıdır."
    )
    assert (
        "Bu mətndən avtomatik qiymətləndirmə üçün meyar çıxmadı. Aşağıdakı məlumat "
        "sıralamaya daxil edilmir." in html
    )
    assert "təsdiqləyin" not in html
    assert "hazırdır" not in html
    assert not CONFIRM_ACTION.search(html)
    assert "Tələbləri təsdiqlə və namizədləri sırala" not in html
    # No empty admin-form tables; the informational requirement stays visible.
    assert CRITERIA_TABLE not in html
    assert "Üstünlük tələbləri" not in html
    assert "Namizəd Bakıda yaşamalıdır" in html
    assert "qiymətləndirməyə daxil edilmir" in html


async def test_blocking_conflict_asks_for_resolution_not_confirmation(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    html = await _analyze(
        client,
        tenant_and_user,
        "Vakansiya: Backend Engineer\nPython tələb olunur.\nPython üstünlükdür.",
    )
    assert (
        "Qaralamada 1 tələb dəqiqləşdirmə tələb edir. Namizədləri sıralamadan əvvəl "
        "aşağıdakı seçimi tamamlayın." in html
    )
    assert "təsdiqləyin" not in html
    assert "hazırdır" not in html
    assert not CONFIRM_ACTION.search(html)
    assert "Tələbləri təsdiqlə və namizədləri sırala" not in html
    # Resolution controls remain; no empty tables; no contradictory
    # "re-analyse the vacancy" banner next to in-place resolution controls.
    assert 'name="option"' in html
    assert 'name="decision" value="exclude"' in html
    assert "Keçərli variantı seçin" in html
    assert CRITERIA_TABLE not in html
    assert "Mütləq tələblər" not in html
    assert "Üstünlük tələbləri" not in html
    assert "Avtomatik sıralama üçün hazır meyar yoxdur" not in html
