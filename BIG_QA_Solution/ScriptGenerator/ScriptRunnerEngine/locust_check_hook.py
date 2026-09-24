"""
locust_check_hook.py
--------------------
Turns a Locust run into a single pass of the journey, for the Script Check.

This file is not part of the application. It is handed to Locust as a second
locustfile (`locust -f <script under check>,<this file>`) and runs inside the
performance project's own virtualenv, so it may import nothing beyond Locust
and gevent.

Why it exists: core Locust has no "run once" switch. `--run-time` is a clock,
so a one-user check still repeats the task until the clock expires - which is
not a script check, it is a short load test. (`--iterations` comes from the
separate locust-plugins package, which the generated framework does not
install.) The task is therefore wrapped so the run ends as soon as the first
pass returns, and `--run-time` is left in place only as an upper bound for a
journey that hangs.

A script whose tasks are TaskSet classes is left alone: dispatching those
correctly means reimplementing Locust's own task machinery. Such runs stay
bounded by `--run-time`, and a line is printed so the console says as much.
"""

import inspect

import gevent
from locust import events

# Set from the init event, which is the only place the web UI is handed out.
_web_ui = None


@events.init.add_listener
def capture_web_ui(environment, web_ui=None, **_kwargs):
    global _web_ui
    _web_ui = web_ui


@events.test_start.add_listener
def stop_after_one_iteration(environment, **_kwargs):
    """Wrap every user class's tasks so the run ends after the first pass."""
    runner = getattr(environment, "runner", None)
    if runner is None:
        return

    for user_class in getattr(environment, "user_classes", []) or []:
        tasks = list(getattr(user_class, "tasks", []) or [])
        if not tasks:
            continue
        if any(inspect.isclass(task) for task in tasks):
            print(f"[script check] {user_class.__name__} uses TaskSet classes; "
                  f"falling back to the run-time limit instead of a single pass.")
            continue
        user_class.tasks = [_run_once(task, environment) for task in tasks]


def _run_once(task, environment):
    done = []

    def wrapper(user):
        try:
            task(user)
        finally:
            # One pass is all a check needs, and a user class may be spawned
            # more than once if someone raises the user count by hand.
            if not done:
                done.append(True)
                # Shutting down from inside the task's own greenlet would kill
                # it mid-call and lose the result, so hand it to a fresh one.
                gevent.spawn_later(0, _shutdown, environment)

    wrapper.__name__ = getattr(task, "__name__", "task")
    wrapper.__doc__ = getattr(task, "__doc__", None)
    # Locust reads this when building its weighted task list; the list handed
    # to us is already expanded, so every wrapper weighs the same.
    wrapper.locust_task_weight = 1
    return wrapper


def _shutdown(environment):
    """
    End the run, then the process - the sequence Locust's own `--run-time`
    timer uses.

    Stopping the runner is not enough on its own under `--autostart`: the
    process stays alive on the web UI greenlet until both the runner and the
    web UI are shut down, which is why a check used to sit idle until
    `--run-time` expired. The `--autoquit` grace period is honoured so the
    web UI stays readable for a few seconds after the pass, which is the
    reason the check runs headed at all.
    """
    runner = environment.runner
    runner.stop()

    if _web_ui is None:
        runner.quit()
        return

    autoquit = getattr(getattr(environment, "parsed_options", None), "autoquit", -1)
    if autoquit is None or autoquit < 0:
        # No grace period asked for; leave the UI up as Locust would.
        return
    gevent.sleep(autoquit)
    runner.quit()
    _web_ui.stop()
