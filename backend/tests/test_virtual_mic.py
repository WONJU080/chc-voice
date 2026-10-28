from __future__ import annotations

import numpy as np

from chc_voice.audio.output import PcmOutput


class FakeStream:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.started = False
        self.stopped = False
        self.closed = False
        FakeStream.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def make_output(**kwargs) -> PcmOutput:
    FakeStream.instances.clear()
    output = PcmOutput(0, stream_factory=FakeStream, **kwargs)
    output.start()
    return output


def pull(output: PcmOutput, frames: int = 480) -> np.ndarray:
    stream = FakeStream.instances[-1]
    data = np.zeros((frames, 2), dtype=np.int16)
    stream.callback(data, frames, None, None)
    return data


def test_tone_is_written_to_both_channels() -> None:
    output = make_output(gain=0.8)
    output.write_tone(440.0, 0.2, 0.5)
    data = pull(output)
    assert np.any(data != 0)
    assert np.array_equal(data[:, 0], data[:, 1])
    output.stop()


def test_silence_after_clear() -> None:
    output = make_output()
    output.write_tone(440.0, 0.2, 0.5)
    pull(output)
    output.clear()
    data = pull(output)
    assert np.all(data == 0)
    output.stop()


def test_gain_zero_is_silent() -> None:
    output = make_output(gain=0.0)
    output.write_tone(440.0, 0.2, 0.5)
    data = pull(output)
    assert np.all(data == 0)
    output.stop()


def test_limiter_prevents_clipping() -> None:
    output = make_output(gain=1.0)
    output.write_samples(np.ones(480, dtype=np.float32) * 2.0)
    data = pull(output)
    assert int(np.abs(data).max()) <= 32767
    output.stop()


def test_pending_seconds_and_max_buffer() -> None:
    output = make_output(max_seconds=0.1)
    output.write_tone(440.0, 1.0, 0.2)
    assert output.pending_seconds <= 0.1001
    output.stop()


def test_stop_releases_stream() -> None:
    output = make_output()
    output.stop()
    stream = FakeStream.instances[-1]
    assert stream.stopped and stream.closed
    assert not output.running
