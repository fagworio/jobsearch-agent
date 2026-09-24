"""Escolha de opcao em combobox — os defeitos que a vaga real da Fueled revelou.

A execucao real contra o Greenhouse da Fueled mostrou a resposta DECLARADA
("Immediately") e a opcao REAL do board ("Available immediately") como coisas
diferentes, e o preenchimento antigo digitava a resposta antes de ler a lista —
digitar FILTRA, a lista zerava e o campo obrigatorio ficava vazio enquanto a
telemetria dizia "respondida". Estes testes fixam a ordem nova: abrir, casar
contra as opcoes reais e so entao digitar.
"""

from types import SimpleNamespace

import pytest

from jobsearch_agent.browser import BrowserSessionError, _combobox_queries, _fill_combobox
from jobsearch_agent.models import ApplicationAnswer, ApplicationField


class _Option:
    def __init__(self, label: str, clicks: list[str], box=None):
        self.label = label
        self._clicks = clicks
        self._box = box

    def inner_text(self) -> str:
        return self.label

    def is_visible(self) -> bool:
        return True

    def click(self) -> None:
        self._clicks.append(self.label)
        if self._box is not None:
            # Escolher grava o valor FORA do input, como no react-select.
            self._box.chosen = self.label


class _Collection:
    def __init__(self, items: list):
        self._items = list(items)

    def count(self) -> int:
        return len(self._items)

    def nth(self, index: int):
        return self._items[index]


class _ListBox:
    def __init__(self, box_id: str, labels: list[str], clicks: list[str], *, visible: bool = True, present: bool = True):
        self.box_id = box_id
        self.clicks = clicks
        self.visible = visible
        self.present = present
        self.chosen = ""
        self.set_labels(labels)

    def set_labels(self, labels: list[str]) -> None:
        if self.chosen and self.chosen not in labels:
            self.chosen = ""
        self.options = [_Option(label, self.clicks, self) for label in labels]

    def get_attribute(self, name: str):
        return self.box_id if name == "id" else None

    def is_visible(self) -> bool:
        return self.visible

    def get_by_role(self, _role: str) -> _Collection:
        return _Collection(self.options)


class _Page:
    def __init__(self, boxes: list[_ListBox], selected: dict[str, str] | None = None):
        self.boxes = boxes
        self.selected = selected or {}

    def get_by_role(self, _role: str) -> _Collection:
        return _Collection([box for box in self.boxes if box.present])

    def evaluate(self, _script, argument=None, **_kwargs):
        if isinstance(argument, str):
            if argument in self.selected:
                return self.selected[argument]
            for box in self.boxes:
                # O input `question_X` guarda o valor no listbox `react-select-question_X-listbox`.
                if argument and argument in box.box_id and box.chosen:
                    return box.chosen
            return ""
        return True

    def wait_for_timeout(self, _milliseconds) -> None:
        return None


class _ComboLocator:
    """Combobox que FILTRA: `fill` reduz a lista, exatamente como o react-select."""

    def __init__(
        self,
        listbox: _ListBox,
        *,
        field_id: str = "q-notice",
        filters: dict[str, list[str]] | None = None,
        opens_listbox: bool = True,
        enabled: bool = True,
    ):
        self.listbox = listbox
        self.field_id = field_id
        self.filters = filters or {}
        self.opens_listbox = opens_listbox
        self.enabled = enabled
        self.queries: list[str] = []
        self.typed: list[str] = []

    def is_enabled(self) -> bool:
        return self.enabled

    def click(self) -> None:
        return None

    def fill(self, value: str) -> None:
        self.queries.append(value)
        self._loaded(value)

    def press(self, _key: str) -> None:
        return None

    def press_sequentially(self, value: str, delay: int = 0) -> None:
        self.typed.append(value)
        self._loaded(value)

    def _loaded(self, value: str) -> None:
        if self.opens_listbox:
            self.listbox.present = True
            self.listbox.visible = True
        # Filtro vazio devolve a lista INTEIRA, como no react-select.
        self.listbox.set_labels(self.filters.get(value, self.filters.get("", [])))

    def get_attribute(self, name: str):
        return self.field_id if name == "id" else None


def _binding(**overrides) -> SimpleNamespace:
    values = {"multiple": False, "autocomplete": "list", "listbox_id": "", "option_locators": {}, "option_values": {}}
    values.update(overrides)
    return SimpleNamespace(**values)


def _field(**overrides) -> ApplicationField:
    values = {
        "key": "q-notice",
        "label": "If offered the role, how much notice do you need to provide before you can start? *",
        "field_type": "combobox",
        "semantic_type": "notice_period",
        "required": True,
    }
    values.update(overrides)
    return ApplicationField(**values)


def _answer(field: ApplicationField, value: str, *, supported_by: list[str], semantic_type: str) -> None:
    field.answer = ApplicationAnswer(
        question_key="k",
        question=field.label,
        answer=value,
        supported_by=list(supported_by),
        source="CandidatePreferences.notice_period",
        confidence=1.0,
        approved=True,
        semantic_type=semantic_type,
        field_key=field.key,
    )


def test_a_declared_answer_written_another_way_is_matched_by_an_equivalent_option():
    """Achado real: a opcao do board e "Available immediately"."""
    clicks: list[str] = []
    listbox = _ListBox("react-select-q-notice-listbox", ["Available immediately", "2 weeks", "3–4 weeks"], clicks)
    locator = _ComboLocator(listbox)
    field = _field()
    _answer(field, "Immediately", supported_by=["preferences.notice_period"], semantic_type="notice_period")

    _fill_combobox(_Page([listbox]), locator, _binding(), field, "Immediately")

    assert clicks == ["Available immediately"]
    # Nada foi digitado: o filtro nao teve chance de zerar a lista.
    assert locator.queries == []


def test_an_answer_already_identical_to_the_option_is_clicked_without_typing():
    clicks: list[str] = []
    listbox = _ListBox("react-select-q-source-listbox", ["Social media", "Job board", "Other"], clicks)
    locator = _ComboLocator(listbox, field_id="q-source")
    field = _field(key="q-source", label="How did you hear about this opportunity? *", field_type="combobox", semantic_type="unknown")
    _answer(field, "Social media", supported_by=["answer_policy:referral_source"], semantic_type="unknown")

    _fill_combobox(_Page([listbox]), locator, _binding(), field, "Social media")

    assert clicks == ["Social media"]
    assert locator.queries == []


def test_a_city_typeahead_is_typed_only_after_the_open_list_comes_back_empty():
    """O widget de cidade so responde depois da primeira letra."""
    clicks: list[str] = []
    listbox = _ListBox("react-select-candidate-location-listbox", [], clicks)
    locator = _ComboLocator(
        listbox,
        field_id="candidate-location",
        filters={"Betim": ["Betim, Minas Gerais, Brazil", "Betimongo, Chad"]},
    )
    field = _field(key="candidate-location", label="Location (City) *", field_type="combobox", semantic_type="unknown")
    _answer(field, "Betim, Minas Gerais, Brazil", supported_by=["profile.identity.current_location"], semantic_type="current_location")

    _fill_combobox(_Page([listbox]), locator, _binding(), field, "Betim, Minas Gerais, Brazil")

    assert locator.queries == []
    assert locator.typed == ["Betim, Minas Gerais, Brazil", "Betim"]
    assert clicks == ["Betim, Minas Gerais, Brazil"]


def test_the_listbox_is_discovered_from_the_react_select_convention():
    """`aria-controls` vazio: o id tem de sair da convencao `react-select-<id>-listbox`."""
    clicks: list[str] = []
    ours = _ListBox("react-select-question_18722973008-listbox", ["Available immediately"], clicks)
    other = _ListBox("react-select-country-listbox", ["Brazil +55"], clicks, visible=False)
    locator = _ComboLocator(ours, field_id="question_18722973008")
    field = _field(key="question_18722973008")
    _answer(field, "Immediately", supported_by=["preferences.notice_period"], semantic_type="notice_period")

    _fill_combobox(_Page([other, ours]), locator, _binding(listbox_id=""), field, "Immediately")

    assert clicks == ["Available immediately"]


def test_a_value_that_is_not_an_option_fails_loudly_instead_of_being_typed():
    """Melhor parar do que gravar algo que o board nao reconhece."""
    clicks: list[str] = []
    listbox = _ListBox("react-select-q-source-listbox", ["Employee referral", "Job board", "Other"], clicks)
    locator = _ComboLocator(listbox, field_id="q-source")
    field = _field(key="q-source", label="How did you hear about this opportunity? *", field_type="combobox")
    _answer(field, "Linkedin", supported_by=["answer_policy:referral_source"], semantic_type="unknown")

    with pytest.raises(BrowserSessionError, match="OPTION_NOT_FOUND_COMBOBOX_OPTION"):
        _fill_combobox(_Page([listbox]), locator, _binding(), field, "Linkedin")

    assert clicks == []


def test_queries_try_the_full_value_before_its_first_component():
    assert _combobox_queries("Betim, Minas Gerais, Brazil") == ("Betim, Minas Gerais, Brazil", "Betim")
    assert _combobox_queries("Brazil") == ("Brazil",)
    assert _combobox_queries("   ") == ()


def test_a_typeahead_whose_menu_only_exists_after_typing_is_typed_into():
    """Achado real: o menu do campo de cidade so nasce depois da primeira letra.

    Antes da correcao o widget caia em `UNSUPPORTED_COMBOBOX_LISTBOX_UNBOUND` e a
    candidatura parava num campo obrigatorio.
    """
    clicks: list[str] = []
    listbox = _ListBox(
        "react-select-candidate-location-listbox",
        [],
        clicks,
        present=False,
        visible=False,
    )
    locator = _ComboLocator(
        listbox,
        field_id="candidate-location",
        filters={"Betim": ["Betim, Minas Gerais, Brazil"]},
    )
    field = _field(key="candidate-location", label="Location (City) *", field_type="combobox", semantic_type="unknown")
    _answer(field, "Betim, Minas Gerais, Brazil", supported_by=["profile.identity.current_location"], semantic_type="current_location")

    _fill_combobox(_Page([listbox]), locator, _binding(listbox_id=""), field, "Betim, Minas Gerais, Brazil")

    assert clicks == ["Betim, Minas Gerais, Brazil"]
    # Digitado tecla por tecla: o typeahead reage a teclado, nao a `fill`.
    assert locator.typed == ["Betim, Minas Gerais, Brazil", "Betim"]


def test_a_widget_without_any_listbox_fails_with_the_structural_reason():
    clicks: list[str] = []
    listbox = _ListBox("react-select-q-notice-listbox", ["Available immediately"], clicks, present=False)
    locator = _ComboLocator(listbox, opens_listbox=False)
    field = _field()
    _answer(field, "Immediately", supported_by=["preferences.notice_period"], semantic_type="notice_period")

    with pytest.raises(BrowserSessionError, match="UNSUPPORTED_COMBOBOX_LISTBOX_UNBOUND"):
        _fill_combobox(_Page([listbox]), locator, _binding(listbox_id=""), field, "Immediately")


def test_a_value_already_chosen_by_the_board_is_not_retyped():
    """O autopreenchimento do board escolhe a opcao e DESABILITA o input.

    Nesse estado o input esta vazio e `disabled`; o valor vive fora dele. Sem ler
    a opcao escolhida o agente tentava digitar num campo desabilitado e gastava
    30s de timeout por tentativa.
    """
    clicks: list[str] = []
    listbox = _ListBox("react-select-candidate-location-listbox", [], clicks, present=False)
    locator = _ComboLocator(listbox, field_id="candidate-location", enabled=False)
    page = _Page([listbox], selected={"candidate-location": "Betim, Minas Gerais, Brazil"})
    field = _field(key="candidate-location", label="Location (City) *", field_type="combobox", semantic_type="unknown")
    _answer(field, "Betim, Minas Gerais, Brazil", supported_by=["profile.identity.current_location"], semantic_type="current_location")

    _fill_combobox(page, locator, _binding(listbox_id=""), field, "Betim, Minas Gerais, Brazil")

    assert clicks == []
    assert locator.queries == []


def test_a_disabled_widget_holding_another_value_fails_fast_with_the_reason():
    clicks: list[str] = []
    listbox = _ListBox("react-select-candidate-location-listbox", [], clicks, present=False)
    locator = _ComboLocator(listbox, field_id="candidate-location", enabled=False)
    page = _Page([listbox], selected={"candidate-location": "Sao Paulo, Brazil"})
    field = _field(key="candidate-location", label="Location (City) *", field_type="combobox", semantic_type="unknown")
    _answer(field, "Betim, Minas Gerais, Brazil", supported_by=["profile.identity.current_location"], semantic_type="current_location")

    with pytest.raises(BrowserSessionError, match="COMBOBOX_INPUT_DISABLED"):
        _fill_combobox(page, locator, _binding(listbox_id=""), field, "Betim, Minas Gerais, Brazil")


def test_a_choice_that_does_not_survive_in_the_widget_is_reported():
    """O valor escolhido tem de estar no widget: escolher e nao gravar e o mesmo
    que nao responder — o campo obrigatorio ficaria vazio e o board recusaria."""
    clicks: list[str] = []
    listbox = _ListBox("react-select-q-notice-listbox", ["Available immediately"], clicks)
    locator = _ComboLocator(listbox)
    page = _Page([listbox], selected={"q-notice": "2 weeks"})
    field = _field()
    _answer(field, "Immediately", supported_by=["preferences.notice_period"], semantic_type="notice_period")

    with pytest.raises(BrowserSessionError, match="VALUE_NOT_COMMITTED"):
        _fill_combobox(page, locator, _binding(), field, "Immediately")


def test_a_widget_that_already_holds_the_answer_is_left_alone():
    """Idempotencia: reabrir o menu de um campo ja respondido convidaria o board a
    trocar a resposta (ou a perde-la)."""
    clicks: list[str] = []
    listbox = _ListBox("react-select-q-notice-listbox", ["Available immediately"], clicks)
    locator = _ComboLocator(listbox)
    page = _Page([listbox], selected={"q-notice": "Available immediately"})
    field = _field()
    _answer(field, "Immediately", supported_by=["preferences.notice_period"], semantic_type="notice_period")

    _fill_combobox(page, locator, _binding(), field, "Immediately")

    assert clicks == []
    assert locator.typed == []


def test_a_choice_that_survives_in_the_widget_is_accepted():
    clicks: list[str] = []
    listbox = _ListBox("react-select-q-notice-listbox", ["Available immediately"], clicks)
    locator = _ComboLocator(listbox)
    field = _field()
    _answer(field, "Immediately", supported_by=["preferences.notice_period"], semantic_type="notice_period")

    _fill_combobox(_Page([listbox]), locator, _binding(), field, "Immediately")

    assert clicks == ["Available immediately"]


def test_a_widget_that_renders_only_part_of_the_label_still_counts_as_filled():
    """Achado real: o seletor de pais mostra a bandeira e o DDI ("+55") no lugar
    de "Brazil +55"."""
    clicks: list[str] = []
    listbox = _ListBox("react-select-country-listbox", ["Brazil +55"], clicks)
    locator = _ComboLocator(listbox, field_id="country")
    page = _Page([listbox], selected={"country": "+55"})
    field = _field(key="country", label="Country *", field_type="combobox", semantic_type="country")
    _answer(field, "Brazil", supported_by=["profile.identity.country"], semantic_type="country")

    _fill_combobox(page, locator, _binding(), field, "Brazil")

    # O widget mostrava "+55" (DDI) enquanto a resposta declarada era "Brazil":
    # sem comparar com a opcao CLICADA ("Brazil +55") isso reprovaria um campo
    # corretamente respondido — e o guard de confirmacao e para isso que existe.
    assert clicks == ["Brazil +55"]


def test_a_filter_that_hides_the_right_option_is_cleared_before_giving_up():
    """A resposta declarada ("Immediately") nao e prefixo do rotulo do board
    ("Available immediately"): digitar esvazia a lista. Limpar o filtro devolve a
    lista inteira e a escolha acontece contra as opcoes reais."""
    clicks: list[str] = []
    # Lista chega vazia (as opcoes do board carregam depois do clique).
    listbox = _ListBox("react-select-q-notice-listbox", [], clicks)
    locator = _ComboLocator(
        listbox,
        filters={
            "Immediately": [],
            "": ["Available immediately", "2 weeks"],
        },
    )
    field = _field()
    _answer(field, "Immediately", supported_by=["preferences.notice_period"], semantic_type="notice_period")

    _fill_combobox(_Page([listbox]), locator, _binding(), field, "Immediately")

    assert locator.typed == ["Immediately", ""]
    assert clicks == ["Available immediately"]


def test_the_option_table_is_found_even_when_the_field_semantics_are_unknown():
    """Achado real: o campo de aviso previo chegou com `semantic_type="unknown"`.

    A tabela tem de ser encontrada pela EVIDENCIA da resposta
    (`preferences.notice_period`), senao a resposta declarada vai sozinha contra a
    opcao do board e o campo obrigatorio fica vazio.
    """
    clicks: list[str] = []
    listbox = _ListBox("react-select-question_18722973008-listbox", ["Available immediately", "2 weeks"], clicks)
    locator = _ComboLocator(listbox, field_id="question_18722973008")
    field = _field(key="question_18722973008", semantic_type="unknown")
    field.answer = ApplicationAnswer(
        question_key="k",
        question=field.label,
        answer="Immediately",
        supported_by=["preferences.notice_period"],
        source="CandidatePreferences.notice_period",
        confidence=1.0,
        approved=True,
        semantic_type="unknown",
        field_key=field.key,
    )

    _fill_combobox(_Page([listbox]), locator, _binding(), field, "Immediately")

    assert clicks == ["Available immediately"]
    assert locator.typed == []
