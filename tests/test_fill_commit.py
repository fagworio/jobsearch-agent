"""Confirmacao de escrita no formulario.

O ``fill`` do Playwright nao garante que o valor sobreviva: widgets controlados
pelo React limpam o campo sozinhos, e o autopreenchimento do proprio board
reescreve campos quando a resposta do upload chega depois. Sem conferencia o
pipeline anunciava "formulario preenchido" com campos obrigatorios vazios, o
navegador recusava o submit por validacao nativa e nada era enviado — em
silencio.
"""

from types import SimpleNamespace

import pytest

from jobsearch_agent.browser import BrowserSessionError, PlaywrightFormFiller, _write_and_confirm


class _Results:
    """Sem sugestoes: o caminho de typeahead fica inativo nestes testes."""

    def count(self) -> int:
        return 0


class _Container:
    def locator(self, _selector) -> _Results:
        return _Results()


class _Locator:
    """Controle preguicoso.

    ``outcomes`` e o que o controle passa a devolver depois de cada escrita, o
    que permite simular um widget que o React limpa sozinho. Sem escrita o valor
    inicial e vazio, como num formulario recem-aberto.
    """

    def __init__(self, outcomes: list[str] | None = None, *, unreadable: bool = False, page=None):
        self.outcomes = list(outcomes or [])
        self.unreadable = unreadable
        self.value = ""
        self.writes: list[str] = []
        self.page = page

    def locator(self, _selector) -> _Container:
        return _Container()

    def count(self) -> int:
        return 1

    def fill(self, value: str) -> None:
        self.writes.append(value)
        self.value = self.outcomes.pop(0) if self.outcomes else value

    def input_value(self) -> str:
        if self.unreadable:
            raise RuntimeError("input_value is not supported for this widget")
        return self.value


class _Page:
    def __init__(self, locators: dict[str, _Locator] | None = None):
        self.locators = locators or {}

    def evaluate(self, *_args, **_kwargs):
        return True

    def wait_for_timeout(self, _milliseconds):
        return None

    def locator(self, selector):
        return self.locators[selector]


def _write(locator: _Locator, value: str, field_key: str = "email") -> None:
    _write_and_confirm(locator, value, field_key)


def test_write_and_confirm_accepts_a_value_that_survives():
    locator = _Locator()
    _write(locator, "Candidate", "name")
    assert locator.writes == ["Candidate"]


def test_write_and_confirm_retries_then_succeeds():
    """A primeira escrita e limpa (React re-renderizou); a segunda sobrevive."""
    locator = _Locator(["", "Candidate"], page=_Page())
    _write(locator, "Candidate")
    assert locator.writes == ["Candidate", "Candidate"]


def test_write_and_confirm_raises_when_the_value_never_sticks():
    locator = _Locator(["", "", ""], page=_Page())
    with pytest.raises(BrowserSessionError, match="VALUE_NOT_COMMITTED: location"):
        _write(locator, "Springfield", "location")


def test_unreadable_widget_is_not_reported_as_empty():
    """Nao saber ler e diferente de estar vazio: nao pode reprovar o formulario."""
    locator = _Locator(unreadable=True, page=_Page())
    _write(locator, "Springfield", "location")
    assert locator.writes == ["Springfield"]


def _plan(entries):
    return SimpleNamespace(
        actions=[
            SimpleNamespace(action_type="fill", field_key=key, value=value, attachment_path="")
            for key, value in entries
        ]
    )


class _Bindings:
    def __init__(self, selectors):
        self.selectors = selectors

    def for_field(self, key):
        return SimpleNamespace(locator=self.selectors[key])


def _field(field_type="text", key="field"):
    return SimpleNamespace(field_type=field_type, key=key)


def test_repair_rewrites_the_fields_the_board_cleared():
    """O board limpa o campo depois do preenchimento; o reparo reescreve."""
    locator = _Locator(["", "Candidate"])
    page = _Page({"#email": locator})
    filler = PlaywrightFormFiller()
    filler._repair_missing_values(
        page, _plan([("email", "Candidate")]), {"email": _field()}, _Bindings({"email": "#email"}), None
    )
    assert locator.writes == ["Candidate", "Candidate"]


def test_repair_gives_up_with_the_field_name():
    """Um campo que nunca aceita o valor precisa aparecer pelo nome."""
    locator = _Locator([""] * 60)
    page = _Page({"#location": locator})
    filler = PlaywrightFormFiller()
    with pytest.raises(BrowserSessionError, match="VALUE_NOT_COMMITTED: location"):
        filler._repair_missing_values(
            page,
            _plan([("location", "Springfield")]),
            {"location": _field(key="location")},
            _Bindings({"location": "#location"}),
            None,
        )


def test_repair_ignores_widgets_it_cannot_read():
    locator = _Locator(unreadable=True)
    page = _Page({"#phone": locator})
    PlaywrightFormFiller()._repair_missing_values(
        page, _plan([("phone", "+5531")]), {"phone": _field()}, _Bindings({"phone": "#phone"}), None
    )


def test_repair_skips_controls_confirmed_by_their_own_selection():
    """Combobox, radio e checkbox sao confirmados pela propria selecao."""
    locator = _Locator()
    page = _Page({})
    kinds = {"c": "combobox", "r": "radio", "b": "checkbox"}
    fields = {key: _field(kind) for key, kind in kinds.items()}
    bindings = _Bindings({key: f"#{key}" for key in kinds})
    PlaywrightFormFiller()._repair_missing_values(
        page,
        _plan([(key, "Yes") for key in kinds]),
        fields,
        bindings,
        None,
    )
    assert locator.writes == []
