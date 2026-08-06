type AudioQueueEvent = 'stop' | 'empty-queue' | 'id-change' | 'playback-blocked' | 'error';

interface AudioQueueStopDetail {
	event: AudioQueueEvent;
	id: string | null;
}

export type OnStoppedCallback = (detail: AudioQueueStopDetail) => void;

export class AudioQueue {
	private audio: HTMLAudioElement;
	private queue: string[] = [];
	private current: string | null = null;
	private batchOpen = false;
	private readonly _onEnded = () => {
		this.#releaseCurrent();
		this.next();
	};
	private readonly _onError = () => {
		this.#releaseCurrent();
		this.onStopped?.({ event: 'error', id: this.id });
		this.next();
	};

	id: string | null = null;
	onStopped: OnStoppedCallback | null = null;

	constructor(audioElement: HTMLAudioElement) {
		this.audio = audioElement;
		this.audio.addEventListener('ended', this._onEnded);
		this.audio.addEventListener('error', this._onError);
	}

	setId(newId: string) {
		if (this.id === newId) return;

		this.#halt();
		this.id = newId;
		this.onStopped?.({ event: 'id-change', id: newId });
	}

	setPlaybackRate(rate: number) {
		this.audio.playbackRate = rate;
	}

	beginBatch() {
		this.batchOpen = true;
	}

	endBatch() {
		this.batchOpen = false;
		if (!this.current && this.queue.length === 0) {
			this.#notifyEmpty();
		}
	}

	enqueue(url: string) {
		this.queue.push(url);

		// Auto-play if nothing is currently playing or loaded
		if (this.audio.paused && !this.current) {
			this.next();
		}
	}

	async play() {
		if (!this.current && this.queue.length > 0) {
			this.next();
		} else if (this.current) {
			try {
				await this.audio.play();
			} catch {
				this.onStopped?.({ event: 'playback-blocked', id: this.id });
			}
		}
	}

	next() {
		this.current = this.queue.shift() ?? null;

		if (this.current) {
			this.audio.src = this.current;
			void this.audio
				.play()
				.catch(() => this.onStopped?.({ event: 'playback-blocked', id: this.id }));
		} else if (!this.batchOpen) {
			this.#notifyEmpty();
		}
	}

	stop() {
		this.#halt();
		this.onStopped?.({ event: 'stop', id: this.id });
	}

	destroy() {
		this.audio.removeEventListener('ended', this._onEnded);
		this.audio.removeEventListener('error', this._onError);
		this.#halt();
		this.onStopped = null;
	}

	isIdle() {
		return !this.current && this.queue.length === 0;
	}

	/**
	 * Pause audio and clear queue without firing onStopped.
	 * Callers that need the callback should invoke it themselves.
	 */
	#halt() {
		this.audio.pause();
		this.audio.currentTime = 0;
		this.audio.removeAttribute('src');
		this.audio.load();
		this.#releaseCurrent();
		this.queue.forEach((url) => this.#revoke(url));
		this.queue = [];
		this.current = null;
		this.batchOpen = false;
	}

	#notifyEmpty() {
		this.audio.removeAttribute('src');
		this.audio.load();
		this.onStopped?.({ event: 'empty-queue', id: this.id });
	}

	#releaseCurrent() {
		if (this.current) this.#revoke(this.current);
		this.current = null;
	}

	#revoke(url: string) {
		if (url.startsWith('blob:')) URL.revokeObjectURL(url);
	}
}
