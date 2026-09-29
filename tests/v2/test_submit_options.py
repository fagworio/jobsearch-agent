from job_agent_v2.submit import _hydrate_choice_options


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
