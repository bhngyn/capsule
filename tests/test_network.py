"""NetworkMonitor — plan §U7."""

from __future__ import annotations

import asyncio
import itertools

import pytest

from app import network


@pytest.mark.asyncio
async def test_threshold_flips_offline_and_calls_callback():
    triggered = {"offline": False, "resume_count": 0}

    async def on_offline():
        triggered["offline"] = True

    async def on_resume():
        triggered["resume_count"] += 1

    async def stub_probe(url):
        return False

    nm = network.NetworkMonitor(
        threshold=3, window_s=60.0, probe_interval_s=0.05,
        probe=stub_probe, on_offline=on_offline, on_resume=on_resume,
    )
    assert nm.is_offline() is False
    for _ in range(3):
        await nm.record_failure()
    # Give the probe loop a tick.
    await asyncio.sleep(0.01)
    assert nm.is_offline() is True
    assert triggered["offline"] is True
    # Stop the probe loop cleanly.
    if nm._probe_task and not nm._probe_task.done():
        nm._probe_task.cancel()
        try:
            await nm._probe_task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_window_eviction_prevents_stale_failures():
    """Failures outside the window must not count toward the threshold."""
    times = iter([0.0, 1.0, 200.0, 201.0])  # last two are >60s after first
    nm = network.NetworkMonitor(
        threshold=3, window_s=60.0, probe=lambda url: _async_false(),
        clock=lambda: next(times),
    )
    await nm.record_failure()  # t=0
    await nm.record_failure()  # t=1
    # Two stale failures evicted; we still need 3 in-window.
    await nm.record_failure()  # t=200, window is empty after eviction
    assert nm.is_offline() is False


async def _async_false():
    return False


@pytest.mark.asyncio
async def test_probe_success_flips_back_online_and_resumes():
    triggered = {"offline_count": 0, "resume_count": 0}

    async def on_offline():
        triggered["offline_count"] += 1

    async def on_resume():
        triggered["resume_count"] += 1

    # First two probes fail, third succeeds.
    probe_results = iter([False, False, True])

    async def stub_probe(url):
        try:
            return next(probe_results)
        except StopIteration:
            return True

    nm = network.NetworkMonitor(
        threshold=2, window_s=60.0, probe_interval_s=0.01,
        probe=stub_probe, on_offline=on_offline, on_resume=on_resume,
    )
    await nm.record_failure()
    await nm.record_failure()
    assert nm.is_offline() is True

    # Probe loop runs in the background; wait for resume.
    for _ in range(50):
        if not nm.is_offline():
            break
        await asyncio.sleep(0.05)
    assert nm.is_offline() is False
    assert triggered["offline_count"] == 1
    assert triggered["resume_count"] == 1


@pytest.mark.asyncio
async def test_force_probe_now_short_circuits_when_online():
    async def stub_probe(url):
        return True
    nm = network.NetworkMonitor(probe=stub_probe)
    ok = await nm.force_probe_now()
    assert ok is True
    assert nm.is_offline() is False


@pytest.mark.asyncio
async def test_record_success_clears_recent_failures():
    nm = network.NetworkMonitor(
        threshold=3, window_s=60.0,
        probe=lambda url: _async_false(),
    )
    await nm.record_failure()
    await nm.record_failure()
    assert nm.state().failure_count_in_window == 2
    await nm.record_success()
    assert nm.state().failure_count_in_window == 0


@pytest.mark.asyncio
async def test_set_probe_url_persists():
    nm = network.NetworkMonitor()
    nm.set_probe_url("https://my.tunnel.example/health")
    assert nm.state().probe_url == "https://my.tunnel.example/health"
