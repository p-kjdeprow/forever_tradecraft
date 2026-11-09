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


def test_reload_sent_deadlines_transient_but_paused_is_not():
    assert sup.is_transient_blocker("deadline expired in SAVE_RELOAD_SENT")
    assert sup.is_transient_blocker("deadline expired in ACK_RELOAD_SENT")
    assert not sup.is_transient_blocker("deadline expired in PAUSED")
    assert not sup.is_transient_blocker("deadline expired in STOPPED")


class FakeRunner:
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    def _do(self, name):
        self.calls.append(name)
        if name == self.fail_at:
            raise RuntimeError(f"{name} broke")

    def stop_session(self):
        self._do("stop")

    def terminate_game(self):
        self._do("terminate")
        return 1

    def run_launcher(self):
        self._do("launcher")

    def start_session(self):
        self._do("start")


STUCK = "something nobody allowlisted"
FULL = ["stop", "terminate", "launcher", "start"]


def test_reopen_deadline_is_allowlisted():
    fake = Fake(blocker="reopen deadline expired: AH did not reopen")
    s, _ = fake.make()
    s.step()
    assert fake.resumes == [{"command": "resume", "generation": 7}]


def test_relaunch_after_threshold_then_one_shot_resume():
    fake, runner = Fake(blocker=STUCK), FakeRunner()
    s, out = fake.make(relaunch=runner, relaunch_threshold=600)
    s.step()
    fake.now += 599
    s.step()
    assert runner.calls == []
    fake.now += 1
    s.step()
    assert runner.calls == FULL
    assert fake.resumes == []
    fake.now += 5
    fake.generation = 9
    s.step()  # first BLOCKED after the relaunch resumes despite the allowlist
    s.step()
    assert fake.resumes == [{"command": "resume", "generation": 9}]
    assert "relaunch_done" in out.getvalue()


def test_status_outage_never_relaunches():
    # An unanswered status may hide a PAUSED takeover behind a slow startup.
    runner = FakeRunner()
    now = [0.0]
    s = sup.Supervisor(
        lambda _p: {"ok": False, "error": "down"},
        monotonic=lambda: now[0],
        relaunch=runner,
        relaunch_threshold=600,
        out=io.StringIO(),
    )
    for step in range(10):
        now[0] = step * 600.0
        s.step()
    assert runner.calls == []


@pytest.mark.parametrize(
    "blocker",
    [
        "executor click/action survived restart; explicit reconciliation required",
        "unrecoverable scan cycle",
    ],
)
def test_operator_reconciliation_blockers_never_relaunch(blocker):
    fake, runner = Fake(blocker=blocker), FakeRunner()
    s, _ = fake.make(relaunch=runner, relaunch_threshold=10)
    for _ in range(5):
        s.step()
        fake.now += 100
    assert runner.calls == [] and fake.resumes == []


def test_post_relaunch_resume_is_consumed_by_paused():
    fake, runner = Fake(blocker=STUCK), FakeRunner()
    s, _ = fake.make(relaunch=runner, relaunch_threshold=600)
    s.step()
    fake.now += 600
    s.step()
    assert runner.calls == FULL
    fake.state = "PAUSED"
    s.step()
    fake.state = "BLOCKED"
    fake.generation += 1
    fake.now += 5
    s.step()
    assert fake.resumes == []


def test_stop_failure_still_restarts_daemon():
    fake, runner = Fake(blocker=STUCK), FakeRunner(fail_at="stop")
    s, out = fake.make(relaunch=runner, relaunch_threshold=600)
    s.step()
    fake.now += 600
    s.step()
    assert runner.calls == ["stop", "start"]
    assert "relaunch_failed" in out.getvalue()


@pytest.mark.parametrize("state", ["PAUSED", "STOPPED"])
def test_operator_states_never_relaunch(state):
    fake, runner = Fake(state=state, blocker=STUCK), FakeRunner()
    s, _ = fake.make(relaunch=runner, relaunch_threshold=10)
    for _ in range(5):
        s.step()
        fake.now += 100
    assert runner.calls == [] and fake.resumes == []


def test_rate_limit_and_no_progress_budget():
    fake, runner = Fake(blocker=STUCK), FakeRunner()
    s, out = fake.make(relaunch=runner, relaunch_threshold=600)
    s.step()  # starts the blocked clock
    fake.now += 600
    s.step()
    assert len(runner.calls) == 4
    fake.generation += 1
    s.step()  # post-relaunch resume
    fake.now += 600
    s.step()  # blocked again, threshold met but inside the 30 minute limit
    fake.now += 600
    s.step()
    assert len(runner.calls) == 4
    fake.now += 1000
    s.step()
    assert len(runner.calls) == 8
    for _ in range(3):
        fake.generation += 1
        fake.now += 1900
        s.step()
        s.step()
        fake.now += 600
        s.step()
    assert len(runner.calls) == 12  # the third relaunch was the last
    assert out.getvalue().count("relaunch_budget_exhausted") == 1


def test_progress_resets_relaunch_budget():
    fake, runner = Fake(blocker=STUCK), FakeRunner()
    s, _ = fake.make(relaunch=runner, relaunch_threshold=600, relaunch_budget=1)
    s.step()
    fake.now += 600
    s.step()
    assert len(runner.calls) == 4
    fake.state = "SCANNING"
    s.step()
    fake.state = "BLOCKED"
    fake.now += 2000
    s.step()
    fake.now += 600
    s.step()
    assert len(runner.calls) == 8


def test_failed_step_ends_attempt_without_resume_and_restores_daemon():
    fake, runner = Fake(blocker=STUCK), FakeRunner(fail_at="launcher")
    s, out = fake.make(relaunch=runner, relaunch_threshold=600)
    s.step()
    fake.now += 600
    s.step()
    assert runner.calls == FULL
    assert "relaunch_failed" in out.getvalue()
    fake.now += 5
    s.step()
    assert fake.resumes == []


def test_restart_startup_observer_timeouts_are_transient():
    assert sup.is_transient_blocker("automatic restart startup failed: identity observer timed out")
    assert sup.is_transient_blocker("automatic restart startup failed: identity observer deadline expired")
    assert not sup.is_transient_blocker("automatic restart startup failed: something else")
