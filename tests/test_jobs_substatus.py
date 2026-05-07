"""SSE sub-status plumbing — _forward_progress carries ytdlp_runner's
sub_status through to the live channel without persisting it to the
jobs.progress_json column.

The frontend renders the sub-status as a translated label below the
progress bar so the user can see *which* file (video / audio / merge /
thumbnail / …) yt-dlp is on. Per CLAUDE.md §1, the affordance must not
leak into forensic artifacts: the persisted payload keeps the legacy
schema, only the in-memory SSE event carries sub_status.
"""

from __future__ import annotations

import asyncio

import pytest

from app import jobs as jobs_mod
from app import ytdlp_runner


@pytest.mark.asyncio
async def test_forward_progress_emits_substatus_but_does_not_persist(
    monkeypatch, capsule_dirs
):
    persisted_payloads: list[dict] = []

    def fake_flush(_conn, _job_id, payload):
        persisted_payloads.append(dict(payload))

    monkeypatch.setattr(jobs_mod, "_flush_progress", fake_flush)

    orch = jobs_mod.JobOrchestrator()
    job = jobs_mod.Job(id="t1", case_id=1, url="https://example.com/v")

    channel: asyncio.Queue = asyncio.Queue()
    orch._channels[job.id] = channel
    in_queue: asyncio.Queue = asyncio.Queue()

    update = ytdlp_runner.ProgressUpdate(
        status="downloading",
        downloaded_bytes=512,
        total_bytes=1024,
        speed=200.0,
        eta=3,
        filename="abc.f140.webm",
        raw={"info_dict": {"vcodec": "none", "acodec": "opus"}},
        sub_status="audio",
    )
    await in_queue.put(update)
    await in_queue.put(None)

    await orch._forward_progress(job, in_queue)

    events: list[dict] = []
    while not channel.empty():
        events.append(channel.get_nowait())

    progress_events = [e for e in events if e and e.get("event") == "progress"]
    assert progress_events, "expected at least one progress SSE event"
    payload = progress_events[0]["data"]
    assert payload["sub_status"] == "audio"
    assert payload["downloaded_bytes"] == 512
    assert payload["total_bytes"] == 1024

    # Persistence path must NOT include sub_status — keeps forensic artifacts
    # and the jobs.progress_json column on the legacy schema.
    assert persisted_payloads, "final flush should have run with last update"
    for p in persisted_payloads:
        assert "sub_status" not in p


@pytest.mark.asyncio
async def test_forward_progress_postprocess_event_carries_merging(
    monkeypatch, capsule_dirs
):
    monkeypatch.setattr(jobs_mod, "_flush_progress", lambda *_a, **_k: None)

    orch = jobs_mod.JobOrchestrator()
    job = jobs_mod.Job(id="t2", case_id=1, url="https://example.com/v")
    channel: asyncio.Queue = asyncio.Queue()
    orch._channels[job.id] = channel

    in_queue: asyncio.Queue = asyncio.Queue()
    await in_queue.put(
        ytdlp_runner.ProgressUpdate(
            status="postprocess",
            downloaded_bytes=None,
            total_bytes=None,
            speed=None,
            eta=None,
            filename=None,
            raw={"postprocess_marker": "[Merger] Merging formats into x.mp4"},
            sub_status="merging",
        )
    )
    await in_queue.put(None)

    await orch._forward_progress(job, in_queue)

    progress_events = []
    while not channel.empty():
        e = channel.get_nowait()
        if e and e.get("event") == "progress":
            progress_events.append(e["data"])

    assert progress_events
    assert progress_events[0]["sub_status"] == "merging"
    # Postprocess events have no bytes; the frontend renders the label alone.
    assert progress_events[0]["downloaded_bytes"] is None
    assert progress_events[0]["total_bytes"] is None
