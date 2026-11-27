"""21.11 (XBOXGARANT): бот написал «Максим уже занимается Вашим вопросом». Леонид: никаких имён ни у одного магазина,
только от первого лица — «я»."""
from services.dialog import brain


def test_prompt_has_no_owner_name():
    assert "Максим" not in brain.RULES and "Макса" not in brain.RULES
    assert "Максим" not in brain.SELLER["owner"]
    assert "Макс" not in brain.system_prompt("")


def test_neutral_kb_and_rules():
    kb = ("- **Как зовут / кто пишет** — «Максим, менеджер магазина».\nСами не отвечаем — передать Максу.\n"
          "позвать Максима. ⚠ уточнить у Макса. Ultimate — максимальная подписка, максимум 20 мес.")
    out = brain.neutral(kb)
    assert "Максим" not in out and "Макс" not in out
    assert "передать владельцу" in out and "позвать владельца" in out and "у владельца" in out
    assert "максимальная" in out and "максимум" in out
    assert "Я продавец магазина" in out


def test_third_person_found():
    assert brain.third_person("Максим уже занимается Вашим вопросом — как придёт код, он сразу всё доделает.")
    assert brain.third_person("Сейчас владелец проверит поступление и напишет Вам.")
    assert brain.third_person("Выбор потом подтвердит менеджер.")
    assert brain.third_person("Да, Максим Валерьевич И. — всё верно.")


def test_third_person_not_found():
    assert not brain.third_person("Да, Ultimate — максимальная подписка, больше 650 игр.")
    assert not brain.third_person("Это максимальный срок в прайсе, максимум 20 мес.")
    assert not brain.third_person("Уже занимаюсь — как придёт код на почту, сразу всё доделаю.")
    assert not brain.third_person("Номер: 89990000000\nПолучатель: Максим И.\nСумма: 990 ₽")


def test_drop_third():
    assert brain.drop_third("Максим уже занимается. Как придёт код — напишите.") == "Как придёт код — напишите."
