from __future__ import annotations


ALIASES: dict[str, tuple[str, ...]] = {
    "employment.notice_period": (
        "what is your notice period",
        "when can you start",
        "how soon can you start",
        "earliest start date",
        "when would you be able to begin",
        "how much notice do you need to provide before you can start",
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
        "agency or consulting firm",
    ),
    "employment.sponsorship": (
        "will you require employer sponsorship",
        "require sponsorship or immigration support",
        "need visa sponsorship",
        "require sponsorship",
    ),
    "employment.current_company": (
        "current company",
        "current employer",
        "most recent company",
        "most recent employer",
        "name of your current or most recent company",
    ),
    "identity.country": (
        "in which country do you currently work",
        "country where you currently work",
        "country of residence",
        "current country",
    ),
    "identity.nationality": (
        "nationality",
        "citizenship",
        "please indicate your nationality",
    ),
}
