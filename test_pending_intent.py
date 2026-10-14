# Тесты модуля «память намерения» — одно незакрытое намерение «напиши …»,
# которое живёт в агенте между первым вопросом «куда написать?» и ответом.
import pending_intent as pi


def test_store_get_clear():
    pi.clear()
    assert pi.get() is None
    pi.store("напиши", "привет")
    got = pi.get()
    assert got is not None and got.prefix == "напиши" and got.text == "привет"
    pi.clear()
    assert pi.get() is None


def test_interpret_destination_answer():
    assert pi.interpret_response("в телеграм Диме") == "answer"


def test_interpret_fresh_command_is_replace():
    assert pi.interpret_response("напиши в вк Ане привет") == "replace"


def test_interpret_cancel():
    assert pi.interpret_response("отмена") == "cancel"
    assert pi.interpret_response("Не надо") == "cancel"


def test_merge_builds_goal():
    pi.clear()
    pi.store("напиши", "привет")
    assert pi.merge(pi.get(), "в телеграм Диме") == "напиши в телеграм Диме: привет"