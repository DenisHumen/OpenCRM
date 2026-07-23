import bcrypt

BCRYPT_ROUNDS = 12
MIN_PASSWORD_LENGTH = 10
# PIN короткий и числовой, к нему требования длины пароля не применяются
PIN_MIN_LEN, PIN_MAX_LEN = 4, 8


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def is_valid_password(password: str) -> bool:
    return len(password) >= MIN_PASSWORD_LENGTH


def is_valid_pin(pin: str) -> bool:
    return pin.isdigit() and PIN_MIN_LEN <= len(pin) <= PIN_MAX_LEN
