"""Application-authored messages for correctable model input and setup problems."""


class ModelInputError(ValueError):
    """An application-authored paper, conversation, or configuration validation message."""


class InputTooLargeError(ModelInputError):
    """The paper or conversation exceeds the selected integration's limit."""


class ModelSetupError(RuntimeError):
    """An application-authored installation or login message safe to display."""
