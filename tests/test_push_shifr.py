"""Шифрование Web Push сверено с примером RFC 8291 §5 байт в байт; подпись VAPID — ключом."""

import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from core.services import push_shifr as ps

# RFC 8291 §5 и приложение A.
TEKST = b"When I grow up, I want to be a watermelon"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
UA_OTKRYTYY = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AS_ZAKRYTYY = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
SOL = "DGv6ra1nlYgDCS1FRnbzlw"
ITOG = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
    "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
    "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)


def test_shifr_sovpadaet_s_primerom_rfc_8291():
    otpravitel = ec.derive_private_key(int.from_bytes(ps.iz_b64u(AS_ZAKRYTYY), "big"), ps.KRIVAYA)
    telo = ps.zashifrovat(TEKST, UA_OTKRYTYY, AUTH, otpravitel=otpravitel, sol=ps.iz_b64u(SOL))
    assert ps.b64u(telo) == ITOG


def test_klyuch_vapid_postoyanen_i_zavisit_ot_sekreta():
    a = ps.otkrytyy(ps.klyuch_iz_sekreta("один секрет"))
    assert a == ps.otkrytyy(ps.klyuch_iz_sekreta("один секрет"))
    assert a != ps.otkrytyy(ps.klyuch_iz_sekreta("другой секрет"))
    assert len(ps.iz_b64u(a)) == 65 and ps.iz_b64u(a)[0] == 4


def test_podpis_vapid_proveryaetsya_otkrytym_klyuchom():
    klyuch = ps.klyuch_iz_sekreta("секрет")
    zagolovok = ps.podpis_vapid(klyuch, "https://fcm.googleapis.com/fcm/send/abc", "mailto:a@b.c", teper=1_000)
    t_chast, k_chast = zagolovok.removeprefix("vapid ").split(", ")
    golova, telo, podpis = t_chast.removeprefix("t=").split(".")
    assert json.loads(ps.iz_b64u(telo)) == {"aud": "https://fcm.googleapis.com", "exp": 1_000 + 12 * 3600, "sub": "mailto:a@b.c"}
    syraya = ps.iz_b64u(podpis)
    der = encode_dss_signature(int.from_bytes(syraya[:32], "big"), int.from_bytes(syraya[32:], "big"))
    otkr = ec.EllipticCurvePublicKey.from_encoded_point(ps.KRIVAYA, ps.iz_b64u(k_chast.removeprefix("k=")))
    otkr.verify(der, f"{golova}.{telo}".encode(), ec.ECDSA(hashes.SHA256()))
