import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AudioQueue } from './audio';

class FakeAudio {
	paused = true;
	src = '';
	currentTime = 0;
	playbackRate = 1;
	listeners = new Map<string, Set<() => void>>();
	play = vi.fn(async () => {
		this.paused = false;
	});
	pause = vi.fn(() => {
		this.paused = true;
	});
	load = vi.fn();
	removeAttribute = vi.fn((name: string) => {
		if (name === 'src') this.src = '';
	});

	addEventListener(name: string, listener: () => void) {
		const listeners = this.listeners.get(name) ?? new Set();
		listeners.add(listener);
		this.listeners.set(name, listeners);
	}

	removeEventListener(name: string, listener: () => void) {
		this.listeners.get(name)?.delete(listener);
	}

	emit(name: string) {
		this.listeners.get(name)?.forEach((listener) => listener());
	}
}

describe('AudioQueue voice conversation lifecycle', () => {
	beforeEach(() => {
		vi.restoreAllMocks();
	});

	it('drains a TTS batch in order and signals relisten only after the last item', () => {
		const audio = new FakeAudio();
		const queue = new AudioQueue(audio as unknown as HTMLAudioElement);
		const events: string[] = [];
		queue.onStopped = ({ event }) => events.push(event);

		queue.beginBatch();
		queue.enqueue('blob:first');
		queue.enqueue('blob:second');
		audio.emit('ended');
		audio.emit('ended');
		expect(events).toEqual([]);

		queue.endBatch();
		expect(events).toEqual(['empty-queue']);
		expect(audio.play).toHaveBeenCalledTimes(2);
	});

	it('stops playback for barge-in and revokes current and queued object URLs', () => {
		const audio = new FakeAudio();
		const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
		const queue = new AudioQueue(audio as unknown as HTMLAudioElement);
		const events: string[] = [];
		queue.onStopped = ({ event }) => events.push(event);
		queue.enqueue('blob:current');
		queue.enqueue('blob:queued');

		queue.stop();

		expect(events).toEqual(['stop']);
		expect(queue.isIdle()).toBe(true);
		expect(revoke).toHaveBeenCalledWith('blob:current');
		expect(revoke).toHaveBeenCalledWith('blob:queued');
		expect(audio.pause).toHaveBeenCalled();
	});

	it('cleans listeners and resources when destroyed', () => {
		const audio = new FakeAudio();
		const queue = new AudioQueue(audio as unknown as HTMLAudioElement);
		queue.enqueue('blob:current');

		queue.destroy();
		audio.emit('ended');

		expect(queue.isIdle()).toBe(true);
		expect(audio.listeners.get('ended')?.size).toBe(0);
		expect(audio.listeners.get('error')?.size).toBe(0);
	});
});
