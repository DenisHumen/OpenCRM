import bcrypt

BCRYPT_ROUNDS = 12
MIN_PASSWORD_LENGTH = 10

#: Предел bcrypt — 72 БАЙТА, и считать надо именно их.
#:
#: С четвёртой версии bcrypt лишнее не обрезает молча, а отказывает:
#: «password cannot be longer than 72 bytes». Проверка ниже считала знаки и
#: только снизу, поэтому длинная фраза проходила её и падала уже внутри
#: хэширования — человек, придумавший хороший длинный пароль, получал
#: пятисотую вместо подсказки. Воспроизведено запросом на регистрацию.
#:
#: Знаки и байты здесь расходятся, и расходятся не поровну: латиницей до
#: предела 72 знака, кириллицей — 36, потому что каждая буква весит два байта.
#: То есть считать знаки значило бы запрещать латинскую фразу, которая
#: помещается, и пропускать русскую, которая нет.
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
