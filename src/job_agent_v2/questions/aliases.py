from __future__ import annotations


ALIASES: dict[str, tuple[str, ...]] = {
    "employment.notice_period": (
        "what is your notice period",
        "when can you start",
        "how soon can you start",
        "earliest start date",
        "when would you be able to begin",
    ),
    "identity.pronouns": (
        "what are your pronouns",
        "preferred pronouns",
        "please select your pronouns",
    ),
    "experience.agency": (
        "experience working in a digital agency or consulting firm",
        "have you worked at an agency",
        "agency experience",
    ),
    "employment.sponsorship": (
        "will you require employer sponsorship",
        "require sponsorship or immigration support",
        "need visa sponsorship",
    ),
    "employment.current_company": (
        "current company",
        "current employer",
        "most recent company",
        "most recent employer",
    ),
}
