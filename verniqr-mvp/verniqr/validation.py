import re


class DomainError(Exception):
    def __init__(self, message: str, code: str = "INVALID_INPUT", status: int = 400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def clean_text(value: str, minimum: int, maximum: int, label: str) -> str:
    value = value.strip()
    if not minimum <= len(value) <= maximum:
        raise DomainError(f"{label}: от {minimum} до {maximum} символов.")
    if any(ord(char) < 32 and char not in "\n\t\r" for char in value):
        raise DomainError(f"{label}: недопустимые управляющие символы.")
    return value


def validate_contact(kind: str, value: str) -> str:
    value = value.strip()
    if kind == "phone":
        value = re.sub(r"[\s()\-]", "", value)
        if not re.fullmatch(r"\+[1-9]\d{7,14}", value):
            raise DomainError("Введите телефон в международном формате, например +79001234567.")
    elif kind == "tg":
        value = value.removeprefix("@").removeprefix("https://t.me/")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value):
            raise DomainError(
                "Введите публичный Telegram username: 5–32 символа, например @username."
            )
    elif kind == "email":
        if len(value) > 254 or not re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+",
            value,
        ):
            raise DomainError("Введите корректный email, например name@example.ru.")
    else:
        raise DomainError("Неизвестный тип контакта.")
    return value
