import io
import json

import pytest

from crowley.session import supervisor as sup


class Fake:
    def __init__(self, state="BLOCKED", blocker="", generation=7):
        self.state, self.blocker, self.generation = state, blocker, generation
        self.resumes = []
        self.resume_ok = True
        self.now = 1000.0

    def request(self, payload):
        if payload["command"] == "status":
            return {
                "ok": True,
                "status": {"state": self.state, "blocker": self.blocker, "generation": self.generation},
            }
        self.resumes.append(payload)
        return {"ok": self.resume_ok, "error": "" if self.resume_ok else "boom"}

    def make(self, **kw):
        out = io.StringIO()
        s = sup.Supervisor(self.request, monotonic=lambda: self.now, out=out, **kw)
        return s, out


@pytest.mark.parametrize("prefix", sup.TRANSIENT_BLOCKER_PREFIXES)
def test_each_transient_blocker_resumes_with_generation(prefix):
    fake = Fake(blocker=prefix + " (extra detail)")
    s, out = fake.make()
    s.step()
    assert fake.resumes == [{"command": "resume", "generation": 7}]
    assert json.loads(out.getvalue().splitlines()[-1])["event"] == "resume"


def test_paused_never_resumes():
    fake = Fake(state="PAUSED", blocker=sup.TRANSIENT_BLOCKER_PREFIXES[0])
    s, _ = fake.make()
    s.step()
    assert fake.resumes == []


def test_non_allowlisted_blocked_never_resumes():
    fake = Fake(blocker="guardian revoked: unattributed input")
    s, out = fake.make()
    s.step()
    assert fake.resumes == []
    assert "ignored_blocker" in out.getvalue()


def test_minimum_interval():
    fake = Fake(blocker=sup.TRANSIENT_BLOCKER_PREFIXES[1])
    s, _ = fake.make(min_resume_interval=45)
    s.step()
    fake.now += 44
    s.step()
    assert len(fake.resumes) == 1
    fake.now += 1
    s.step()
    assert len(fake.resumes) == 2


def test_failure_budget_stops_and_logs_once_then_recovers():
    fake = Fake(blocker=sup.TRANSIENT_BLOCKER_PREFIXES[1])
    s, out = fake.make(min_resume_interval=45, failure_budget=2)
    for _ in range(5):
        s.step()
        fake.now += 50
    assert len(fake.resumes) == 2
    assert out.getvalue().count("budget_exhausted") == 1
    fake.state = "READY"
    s.step()
    assert "recovered" in out.getvalue()
    fake.state = "BLOCKED"
    fake.now += 50
    s.step()
    assert len(fake.resumes) == 3


def test_status_failure_does_not_resume_and_bad_shape_raises():
    s = sup.Supervisor(lambda _p: {"ok": False, "error": "down"}, out=io.StringIO())
    s.step()
    s = sup.Supervisor(lambda _p: {"ok": True, "status": {"state": "BLOCKED"}}, out=io.StringIO())
    with pytest.raises(sup.SupervisorError):
        s.step()


def test_invalid_settings_rejected():
    with pytest.raises(ValueError):
        sup.Supervisor(lambda _p: {}, min_resume_interval=0, out=io.StringIO())
    with pytest.raises(ValueError):
        sup.Supervisor(lambda _p: {}, failure_budget=0, out=io.StringIO())


def test_paused_holds_and_their_stale_deadlines_are_never_allowlisted() -> None:
    from crowley.session.supervisor import TRANSIENT_BLOCKER_PREFIXES

    for blocker in (
        "deadline expired in PAUSED",
        "deadline expired in STOPPED",
        "active chat buffer is not an empty focused command line",
        "input guardian: unattributed XI2 event class=RawKeyPress source=6 target=None",
    ):
        assert not blocker.startswith(TRANSIENT_BLOCKER_PREFIXES)
