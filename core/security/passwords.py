# Кроме bcrypt, модуль не знает ничего — ни `core`, ни `config`. На этом и
# держится импорт снизу вверх из `config.settings.config_errors`: цикла нет.
import bcrypt

BCRYPT_ROUNDS = 12
MIN_PASSWORD_LENGTH = 10

#: Предел bcrypt — 72 БАЙТА, и считать надо именно их.
#:
#: С четвёртой версии bcrypt лишнее не обрезает, а отказывает: проверка по
#: знакам пропускала длинную фразу, и человек с хорошим паролем получал
#: пятисотую вместо подсказки. Латиницей влезает 72 знака, кириллицей — 36.
MAX_PASSWORD_BYTES = 72
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
    return (
        len(password) >= MIN_PASSWORD_LENGTH
        and len(password.encode()) <= MAX_PASSWORD_BYTES
    )


def is_valid_pin(pin: str) -> bool:
    return pin.isdigit() and PIN_MIN_LEN <= len(pin) <= PIN_MAX_LEN
