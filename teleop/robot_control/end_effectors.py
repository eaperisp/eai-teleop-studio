"""Shared end-effector names and validation rules."""

PASSIVE_END_EFFECTORS = frozenset({"none", "rubber"})

END_EFFECTOR_ALIASES = {
    "inspire": "inspire_ftp",
    "inspire_hand": "inspire_ftp",
    "yinshi": "inspire_ftp",
}

ACTIVE_END_EFFECTORS = frozenset({
    "dex1",
    "dex3",
    "inspire_ftp",
    "inspire_dfx",
    "brainco",
})

DISPLAY_END_EFFECTORS = ("dex1", "dex3", "inspire", "inspire_ftp", "inspire_dfx", "brainco")
SIDE_END_EFFECTORS = ("none", "rubber", *DISPLAY_END_EFFECTORS)

SINGLE_SIDE_ACTIVE_END_EFFECTORS = frozenset({"inspire_dfx", "inspire_ftp"})


def canonical_end_effector(value):
    if value is None:
        return None
    text = str(value).strip()
    return END_EFFECTOR_ALIASES.get(text, text)


def is_passive_end_effector(value):
    return canonical_end_effector(value) in PASSIVE_END_EFFECTORS
