type AudioQueueEvent = 'stop' | 'empty-queue' | 'id-change' | 'playback-blocked' | 'error';

interface AudioQueueStopDetail {
	event: AudioQueueEvent;
	id: string | null;
}

export type OnStoppedCallback = (detail: AudioQueueStopDetail) => void;

export class AudioQueue {
	private audio: HTMLAudioElement;
	private preloader: HTMLAudioElement | null = null;
	private queue: string[] = [];
	private current: string | null = null;
	private batchOpen = false;
	private readonly _onEnded = () => {
		this.#releaseCurrent(false);
		this.next();
	};
	private readonly _onError = () => {
		this.#releaseCurrent(true);
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
		} else {
			this.#preloadNext();
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
			this.#preloadNext();
			void this.audio
				.play()
				.catch(() => this.onStopped?.({ event: 'playback-blocked', id: this.id }));
		} else if (!this.batchOpen) {
			this.#notifyEmpty();
		}
	}

	#preloadNext() {
		const nextUrl = this.queue[0];
		if (!nextUrl) return;
		try {
			if (!this.preloader && typeof Audio !== 'undefined') {
				this.preloader = new Audio();
				this.preloader.preload = 'auto';
			}
			if (this.preloader && this.preloader.src !== nextUrl) {
				this.preloader.src = nextUrl;
				this.preloader.load();
			}
		} catch {
			// Preload may fail in testing/headless environments; safely ignore
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
		this.preloader = null;
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
		if (this.preloader) {
			this.preloader.removeAttribute('src');
			this.preloader.load();
		}
		this.#releaseCurrent(true);
		this.queue.forEach((url) => this.#revoke(url, true));
		this.queue = [];
		this.current = null;
		this.batchOpen = false;
	}

	#notifyEmpty() {
		// Do not synchronously removeAttribute('src') and load() here, because doing so
		// abruptly truncates whatever trailing audio buffer is still being flushed to the speaker.
		// Instead, notify completion gracefully and let #halt() or the next playback clean up.
		this.onStopped?.({ event: 'empty-queue', id: this.id });
	}

	#releaseCurrent(immediate = false) {
		if (this.current) this.#revoke(this.current, immediate);
		this.current = null;
	}

	#revoke(url: string, immediate = false) {
		if (url.startsWith('blob:')) {
			if (immediate) {
				try {
					URL.revokeObjectURL(url);
				} catch {}
			} else {
				// Delay revocation by 2.5s so hardware/Bluetooth speaker buffer finishes completely
				setTimeout(() => {
					try {
						URL.revokeObjectURL(url);
					} catch {}
				}, 2500);
			}
		}
	}
}
