from job_agent_v2.models import Field
from job_agent_v2.submit import _accept_prefilled_profile_value, _hydrate_choice_options


class Browser:
    def __init__(self):
        self.calls = []

    def inspect_field_options(self, field_id, *, tab_id):
        self.calls.append((field_id, tab_id))
        return {"options": ["Latin America (Mexico, Central America, South America & Caribbean)", "Europe"]}


def test_hydrates_only_deterministic_comboboxes():
    browser = Browser()
    result = _hydrate_choice_options(
        browser,
        {
            "fields": [
                {
                    "id": "region",
                    "type": "combobox",
                    "label": "Please select the region where you currently live:",
                    "options": [],
                },
                {
                    "id": "unknown",
                    "type": "combobox",
                    "label": "Which best describes your experience?",
                    "options": [],
                },
            ]
        },
        tab_id=7,
    )

    assert result["fields"][0]["options"] == [
        "Latin America (Mexico, Central America, South America & Caribbean)",
        "Europe",
    ]
    assert result["fields"][1]["options"] == []
    assert browser.calls == [("region", 7)]


def test_accepts_non_placeholder_provider_formatted_profile_combobox():
    field = Field("location", "Location*", kind="combobox", required=True)
    assert _accept_prefilled_profile_value(field, "Av. Arthur Trindade, Betim MG", {"location": "profile"})
    assert not _accept_prefilled_profile_value(field, "Select...", {"location": "profile"})
    assert not _accept_prefilled_profile_value(field, "Av. Arthur Trindade, Betim MG", {"location": "rule"})
