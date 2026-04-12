"""CapturingReporter: trivially-correct, but used as a fixture all over."""

from server.wizard.reporter import CallbackReporter, CapturingReporter


def test_capturing_records_levels_and_phase():
    r = CapturingReporter()
    r.info("a")
    r.warning("b")
    r.error("c")
    r.phase("booting", "start")
    r.phase("done")

    assert r.messages() == ["a", "b", "c"]
    assert r.messages("warning") == ["b"]
    assert r.phase_names() == ["booting", "done"]


def test_callback_fans_out_to_emit_and_set_phase():
    emitted = []
    phased = []
    r = CallbackReporter(
        emit=lambda lvl, cat, msg: emitted.append((lvl, cat, msg)),
        set_phase=lambda name, msg=None: phased.append((name, msg)),
    )
    r.info("hello")
    r.warning("uh")
    r.error("bad")
    r.phase("p", "m")
    assert emitted == [
        ("info", "vm", "hello"),
        ("warning", "vm", "uh"),
        ("error", "vm", "bad"),
    ]
    assert phased == [("p", "m")]
