"""Ответ о неверном запросе не должен содержать сам запрос.

FastAPI по умолчанию возвращает в ошибке 422 поле `input` — присланное тело
целиком. На форме «укажите телефон» это безобидно. На входе в систему это
значит, что запрос без обязательного поля возвращает пароль обратно клиенту, а
оттуда он расходится по журналам прокси и отчётам об ошибках фронтенда: живёт
дольше самого запроса и дольше сессии.

Найдено живой проверкой, а не тестом модуля: тесты почты проверяли, что пароль
не приходит в списке и в карточке, но не то, что он приходит в ответе на
кривое тело. Поэтому проверка стоит отдельным файлом и общая для всего API —
дыра была не в блоке, а в обработчике по умолчанию.
"""

import pytest

from tests.conftest import API

SECRET = "ЭтоНеДолжноВернуться-42"

# Точки, где в теле бывает секрет. Ключ — адрес, значение — заведомо неполное
# тело: не хватает обязательного поля, поэтому ответ будет 422.
LEAKY = {
    f"{API}/auth/login": {"password": SECRET},
    f"{API}/auth/me/password": {"new_password": SECRET},
    f"{API}/mail/accounts": {"password": SECRET},
}


@pytest.fixture
def mail_on(root_client):
    """Почта выключена у нового пользователя, и `require_module` отвечает 403
    раньше, чем дело доходит до разбора тела. Проверять утечку надо на
    включённом блоке — на выключенном её и так нет."""
    root_client.post(f"{API}/modules/mail", json={"enabled": True})
    yield
    root_client.post(f"{API}/modules/mail", json={"enabled": False})


@pytest.mark.parametrize("url, body", LEAKY.items())
def test_validation_error_does_not_echo_the_body(root_client, mail_on, url, body):
    response = root_client.post(url, json=body)

    assert response.status_code == 422, (
        f"{url}: тело должно быть отвергнуто, иначе проверять нечего"
    )
    assert SECRET not in response.text, (
        f"{url}: присланный секрет вернулся в ответе об ошибке"
    )


def test_validation_error_still_says_what_is_wrong(root_client):
    """Убрать `input` — не значит превратить ошибку в «что-то пошло не так».

    Клиенту нужно знать, какого поля не хватает: без этого форма не сможет
    подсветить строку, и пользователь останется перед экраном без объяснения.
    """
    response = root_client.post(f"{API}/auth/login", json={"password": SECRET})
    error = response.json()["error"]

    assert error["code"] == "validation_error"
    details = error["details"]
    assert details, "ошибка без подробностей бесполезна форме"
    assert any("email" in detail["loc"] for detail in details), (
        "не сказано, какого поля не хватает"
    )
    assert all("input" not in detail for detail in details)
    assert all("ctx" not in detail for detail in details)


def test_nested_values_do_not_leak_either(root_client, mail_on):
    """Секрет может лежать не в корне тела, а во вложенном объекте — проверка
    обязана держаться и там, иначе она защищает только простые формы."""
    response = root_client.post(
        f"{API}/mail/accounts",
        json={"account": {"password": SECRET}, "imap_port": "не число"},
    )
    assert response.status_code == 422
    assert SECRET not in response.text
