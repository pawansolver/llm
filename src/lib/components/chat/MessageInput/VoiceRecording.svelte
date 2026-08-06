<script lang="ts">
	import { getContext, onDestroy, onMount } from 'svelte';
	import dayjs from 'dayjs';
	import LocalizedFormat from 'dayjs/plugin/localizedFormat';

	import { transcribeAudio } from '$lib/apis/audio';
	import XMark from '$lib/components/icons/XMark.svelte';
	import { config, settings } from '$lib/stores';
	import { blobToFile } from '$lib/utils';
	import {
		appendTranscript,
		getAudioFileExtension,
		getSupportedRecordingMimeType,
		normalizeVadSettings,
		shouldStopAfterSilence
	} from '$lib/utils/dictation';

	dayjs.extend(LocalizedFormat);

	const i18n: any = getContext('i18n');

	export let recording = false;
	export let transcribe = true;
	export let displayMedia = false;
	export let echoCancellation = true;
	export let noiseSuppression = true;
	export let autoGainControl = true;
	export let className = ' p-2.5 w-full max-w-full';
	export let onCancel: () => void = () => {};
	export let onConfirm: (data: { text?: string; file?: File; blob?: Blob }) => void = () => {};
	export let onInterim: (data: { text: string; final: boolean }) => void = () => {};

	type RecordingState = 'idle' | 'requesting' | 'recording' | 'processing' | 'error';

	let state: RecordingState = 'idle';
	let errorMessage = '';
	let fallbackMessage = '';
	let durationSeconds = 0;
	let durationTimer: ReturnType<typeof setInterval> | null = null;
	let recognitionStopTimer: ReturnType<typeof setTimeout> | null = null;
	let recognitionRestartTimer: ReturnType<typeof setTimeout> | null = null;
	let stream: MediaStream | null = null;
	let mediaRecorder: MediaRecorder | null = null;
	let speechRecognition: any = null;
	let audioContext: AudioContext | null = null;
	let analyser: AnalyserNode | null = null;
	let animationFrame: number | null = null;
	let wakeLock: any = null;
	let audioChunks: Blob[] = [];
	let finalTranscript = '';
	let interimTranscript = '';
	let confirmed = false;
	let active = false;
	let starting = false;
	let destroyed = false;
	let sessionId = 0;
	let useWebSpeech = false;
	let resizeObserver: ResizeObserver | null = null;
	let containerWidth = 0;
	let visualizerData: number[] = Array(120).fill(0);

	$: visibleTranscript = appendTranscript(finalTranscript, interimTranscript);
	$: visualizerLength = Math.max(24, Math.floor(containerWidth / 5));
	$: if (recording && state === 'idle' && !active && !starting) {
		void startRecording();
	}
	$: if (!recording && (active || starting)) {
		void disposeSession(true);
	}

	const getEngine = () =>
		$settings?.audio?.stt?.engine || ($config as any)?.audio?.stt?.engine || '';
	const getLanguage = () => $settings?.audio?.stt?.language?.trim() || '';
	const getVadSettings = () => normalizeVadSettings($settings?.audio?.stt?.vad);

	const formatSeconds = (seconds: number) =>
		`${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;

	const requestWakeLock = async () => {
		if (!('wakeLock' in navigator) || wakeLock) return;
		try {
			wakeLock = await (navigator as any).wakeLock.request('screen');
		} catch {
			// Recording must remain usable when wake locks are unavailable.
		}
	};

	const releaseWakeLock = async () => {
		const currentWakeLock = wakeLock;
		wakeLock = null;
		if (currentWakeLock) {
			await currentWakeLock.release().catch(() => undefined);
		}
	};

	const stopTimers = () => {
		if (durationTimer) clearInterval(durationTimer);
		if (recognitionStopTimer) clearTimeout(recognitionStopTimer);
		if (recognitionRestartTimer) clearTimeout(recognitionRestartTimer);
		durationTimer = null;
		recognitionStopTimer = null;
		recognitionRestartTimer = null;
	};

	const stopAnalysis = async () => {
		if (animationFrame !== null) cancelAnimationFrame(animationFrame);
		animationFrame = null;
		analyser?.disconnect();
		analyser = null;
		const currentContext = audioContext;
		audioContext = null;
		if (currentContext && currentContext.state !== 'closed') {
			await currentContext.close().catch(() => undefined);
		}
	};

	const stopTracks = () => {
		stream?.getTracks().forEach((track) => track.stop());
		stream = null;
	};

	const disposeSession = async (discard: boolean) => {
		sessionId += 1;
		active = false;
		starting = false;
		stopTimers();

		if (speechRecognition) {
			speechRecognition.onresult = null;
			speechRecognition.onerror = null;
			speechRecognition.onend = null;
			try {
				speechRecognition.abort();
			} catch {
				// Recognition may already be stopped.
			}
			speechRecognition = null;
		}

		if (mediaRecorder && mediaRecorder.state !== 'inactive') {
			if (discard) mediaRecorder.onstop = null;
			try {
				mediaRecorder.stop();
			} catch {
				// Recorder may have stopped between the state check and this call.
			}
		}
		mediaRecorder = null;
		await stopAnalysis();
		stopTracks();
		await releaseWakeLock();

		if (discard) audioChunks = [];
		durationSeconds = 0;
		visualizerData = Array(visualizerLength).fill(0);
	};

	const fail = async (message: string) => {
		errorMessage = message;
		state = 'error';
		await disposeSession(true);
	};

	const mediaErrorMessage = (error: any) => {
		if (error?.name === 'NotAllowedError' || error?.name === 'SecurityError') {
			return $i18n.t('Microphone permission was denied. Allow microphone access and try again.');
		}
		if (error?.name === 'NotFoundError') {
			return $i18n.t('No microphone was found.');
		}
		if (error?.name === 'NotReadableError') {
			return $i18n.t('The microphone is already in use or unavailable.');
		}
		return $i18n.t('Unable to access the microphone. Check your device and try again.');
	};

	const startAnalysis = () => {
		if (!stream) return;
		const AudioContextConstructor =
			window.AudioContext ||
			(window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
		if (!AudioContextConstructor) return;

		audioContext = new AudioContextConstructor();
		const source = audioContext.createMediaStreamSource(stream);
		analyser = audioContext.createAnalyser();
		analyser.fftSize = 1024;
		source.connect(analyser);
		const samples = new Uint8Array(analyser.fftSize);
		const vad = getVadSettings();
		let heardVoice = false;
		let lastVoiceAt = Date.now();

		const analyse = () => {
			if (!active || state !== 'recording' || !analyser) return;
			analyser.getByteTimeDomainData(samples);
			let sumSquares = 0;
			for (const sample of samples) {
				const normalized = (sample - 128) / 128;
				sumSquares += normalized * normalized;
			}
			const rms = Math.sqrt(sumSquares / samples.length);
			visualizerData = [
				...visualizerData.slice(-(visualizerLength - 1)),
				Math.min(1, Math.max(0.02, rms * 8))
			];

			if (rms >= vad.threshold) {
				heardVoice = true;
				lastVoiceAt = Date.now();
			} else if (
				shouldStopAfterSilence({
					enabled: vad.enabled,
					heardVoice,
					level: rms,
					threshold: vad.threshold,
					now: Date.now(),
					lastVoiceAt,
					silenceDurationMs: vad.silenceDurationMs
				})
			) {
				void confirmRecording();
				return;
			}
			animationFrame = requestAnimationFrame(analyse);
		};
		animationFrame = requestAnimationFrame(analyse);
	};

	const getTranscript = () => appendTranscript(finalTranscript, interimTranscript);

	const emitTranscript = (isFinal: boolean) => {
		onInterim({ text: getTranscript(), final: isFinal });
	};

	const startWebSpeech = (currentSession: number) => {
		const Recognition =
			(window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
		if (!Recognition) {
			useWebSpeech = false;
			fallbackMessage = $i18n.t(
				'Browser speech recognition is unavailable. Audio will be transcribed locally after recording.'
			);
			return;
		}

		useWebSpeech = true;
		speechRecognition = new Recognition();
		speechRecognition.continuous = true;
		speechRecognition.interimResults = true;
		const language = getLanguage();
		if (language) speechRecognition.lang = language;

		speechRecognition.onresult = (event: any) => {
			if (currentSession !== sessionId) return;
			let nextInterim = '';
			for (let index = event.resultIndex; index < event.results.length; index += 1) {
				const result = event.results[index];
				const text = result?.[0]?.transcript ?? '';
				if (result.isFinal) finalTranscript = appendTranscript(finalTranscript, text);
				else nextInterim = appendTranscript(nextInterim, text);
			}
			interimTranscript = nextInterim;
			emitTranscript(false);
		};

		speechRecognition.onerror = (event: any) => {
			if (currentSession !== sessionId || ['aborted', 'no-speech'].includes(event.error)) return;
			if (['network', 'service-not-allowed'].includes(event.error)) {
				useWebSpeech = false;
				fallbackMessage = $i18n.t(
					'Browser speech recognition failed. Audio will be transcribed locally after recording.'
				);
				speechRecognition.onend = null;
				try {
					speechRecognition.abort();
				} catch {
					// Recognition has already stopped.
				}
				speechRecognition = null;
				return;
			}
			void fail(
				$i18n.t('Speech recognition error: {{error}}', {
					error: event.error
				})
			);
		};

		speechRecognition.onend = () => {
			if (currentSession !== sessionId || confirmed || !active || !useWebSpeech) return;
			try {
				speechRecognition.start();
			} catch {
				// Some browsers need a short delay before recognition can restart.
				recognitionRestartTimer = setTimeout(() => {
					if (currentSession === sessionId && active && !confirmed) {
						try {
							speechRecognition?.start();
						} catch {
							// The recorder remains available for manual confirmation.
						}
					}
				}, 150);
			}
		};

		try {
			speechRecognition.start();
		} catch {
			useWebSpeech = false;
			speechRecognition = null;
			fallbackMessage = $i18n.t(
				'Browser speech recognition is unavailable. Audio will be transcribed locally after recording.'
			);
		}
	};

	const completeRecording = (
		data: { text?: string; file?: File; blob?: Blob },
		currentSession: number
	) => {
		if (currentSession !== sessionId || !confirmed || destroyed) return;
		if (speechRecognition) {
			speechRecognition.onresult = null;
			speechRecognition.onerror = null;
			speechRecognition.onend = null;
			speechRecognition = null;
		}
		mediaRecorder = null;
		confirmed = false;
		recording = false;
		state = 'idle';
		durationSeconds = 0;
		visualizerData = Array(visualizerLength).fill(0);
		onConfirm(data);
	};

	const finishRecording = async (recorder: MediaRecorder, currentSession: number) => {
		const chunks = audioChunks;
		audioChunks = [];
		await stopAnalysis();
		stopTracks();
		await releaseWakeLock();
		stopTimers();
		active = false;

		if (!confirmed || destroyed) return;

		try {
			if (transcribe && useWebSpeech) {
				const transcript = getTranscript().trim();
				if (!transcript) throw new Error($i18n.t('No speech was recognized. Please try again.'));
				interimTranscript = '';
				onInterim({ text: transcript, final: true });
				completeRecording({ text: transcript }, currentSession);
				return;
			}

			const mimeType =
				chunks.find((chunk) => chunk.size > 0)?.type || recorder.mimeType || 'audio/webm';
			const audioBlob = new Blob(chunks, { type: mimeType });
			const extension = getAudioFileExtension(mimeType);
			const file = blobToFile(
				audioBlob,
				`Recording-${dayjs().format('YYYY-MM-DD-HH-mm-ss')}.${extension}`
			);

			if (!transcribe) {
				completeRecording({ file, blob: audioBlob }, currentSession);
				return;
			}

			const response = await transcribeAudio(localStorage.token, file, getLanguage() || undefined);
			if (currentSession !== sessionId || !confirmed || destroyed) return;
			if (!response) throw new Error($i18n.t('No transcription was returned.'));
			if (typeof response.text !== 'string' || !response.text.trim()) {
				throw new Error($i18n.t('No speech was recognized. Please try again.'));
			}
			completeRecording(response, currentSession);
		} catch (error) {
			if (currentSession !== sessionId || destroyed) return;
			await fail(`${error}`);
			return;
		}
	};

	const startRecording = async () => {
		starting = true;
		state = 'requesting';
		errorMessage = '';
		fallbackMessage = '';
		finalTranscript = '';
		interimTranscript = '';
		confirmed = false;
		audioChunks = [];
		const currentSession = ++sessionId;

		try {
			if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
				throw new Error($i18n.t('Audio recording is not supported in this browser.'));
			}

			if (displayMedia) {
				const displayStream = await navigator.mediaDevices.getDisplayMedia({
					audio: true,
					video: true
				});
				stream = new MediaStream(displayStream.getAudioTracks());
				displayStream.getVideoTracks().forEach((track) => track.stop());
			} else {
				stream = await navigator.mediaDevices.getUserMedia({
					audio: { echoCancellation, noiseSuppression, autoGainControl }
				});
			}
			if (currentSession !== sessionId || destroyed || !recording) {
				stopTracks();
				return;
			}

			const mimeType = getSupportedRecordingMimeType(
				MediaRecorder.isTypeSupported?.bind(MediaRecorder)
			);
			mediaRecorder = mimeType
				? new MediaRecorder(stream, { mimeType })
				: new MediaRecorder(stream);
			const recorder = mediaRecorder;
			recorder.ondataavailable = (event) => {
				if (event.data.size > 0) audioChunks.push(event.data);
			};
			recorder.onerror = () => {
				void fail($i18n.t('Recording failed. Check your microphone and try again.'));
			};
			recorder.onstop = () => void finishRecording(recorder, currentSession);

			if (transcribe && getEngine() === 'web') startWebSpeech(currentSession);

			recorder.start(250);
			active = true;
			starting = false;
			state = 'recording';
			durationTimer = setInterval(() => (durationSeconds += 1), 1000);
			await requestWakeLock();
			if (currentSession !== sessionId || !active) {
				await releaseWakeLock();
				return;
			}
			startAnalysis();
		} catch (error) {
			starting = false;
			const message =
				error instanceof Error && error.message.startsWith($i18n.t('Audio recording'))
					? error.message
					: mediaErrorMessage(error);
			await fail(message);
		}
	};

	const confirmRecording = async () => {
		if (!active || confirmed || !mediaRecorder) return;
		confirmed = true;
		state = 'processing';
		stopTimers();
		if (speechRecognition) {
			speechRecognition.onend = null;
			try {
				speechRecognition.stop();
			} catch {
				// Recognition may already be stopped.
			}
		}

		// Let Web Speech deliver its final result before finalizing the audio.
		recognitionStopTimer = setTimeout(
			() => {
				if (mediaRecorder?.state !== 'inactive') mediaRecorder?.stop();
			},
			useWebSpeech ? 350 : 0
		);
	};

	const cancelRecording = async () => {
		confirmed = false;
		recording = false;
		await disposeSession(true);
		state = 'idle';
		onCancel();
	};

	const retryRecording = async () => {
		await disposeSession(true);
		state = 'idle';
		if (recording) await startRecording();
	};

	const handleKeyDown = (event: KeyboardEvent) => {
		if (event.key === 'Escape' && recording) {
			event.preventDefault();
			void cancelRecording();
		}
	};

	const handleVisibilityChange = () => {
		if (active && document.visibilityState === 'visible') void requestWakeLock();
	};

	onMount(() => {
		window.addEventListener('keydown', handleKeyDown);
		document.addEventListener('visibilitychange', handleVisibilityChange);
		resizeObserver = new ResizeObserver(() => {
			visualizerData = visualizerData.slice(-visualizerLength);
		});
		resizeObserver.observe(document.body);
	});

	onDestroy(() => {
		destroyed = true;
		window.removeEventListener('keydown', handleKeyDown);
		document.removeEventListener('visibilitychange', handleVisibilityChange);
		resizeObserver?.disconnect();
		void disposeSession(true);
	});
</script>

<div
	bind:clientWidth={containerWidth}
	class="{state === 'processing' || state === 'requesting'
		? 'bg-gray-100/50 dark:bg-gray-850/50'
		: state === 'error'
			? 'bg-red-50 dark:bg-red-950/20'
			: 'bg-indigo-300/10 dark:bg-indigo-500/10'} rounded-2xl flex items-center gap-2 {className}"
	role="group"
	aria-label={$i18n.t('Voice dictation')}
>
	<button
		type="button"
		class="shrink-0 rounded-full p-1.5 hover:bg-gray-200 dark:hover:bg-gray-700"
		aria-label={$i18n.t('Cancel voice dictation')}
		on:click={() => void cancelRecording()}
	>
		<XMark className="size-4" />
	</button>

	<div class="min-w-0 flex-1">
		<div class="flex h-6 items-center gap-0.5 overflow-hidden" aria-hidden="true">
			{#each visualizerData as rms}
				<div
					class="w-[2px] shrink-0 rounded-full {state === 'error'
						? 'bg-red-400'
						: 'bg-indigo-500 dark:bg-indigo-400'}"
					style:height={`${Math.min(100, Math.max(14, rms * 100))}%`}
				></div>
			{/each}
		</div>
		<div class="truncate text-xs text-gray-600 dark:text-gray-300" role="status" aria-live="polite">
			{#if state === 'requesting'}
				{$i18n.t('Requesting microphone access…')}
			{:else if state === 'processing'}
				{$i18n.t('Transcribing audio…')}
			{:else if state === 'error'}
				{errorMessage}
			{:else if visibleTranscript}
				{visibleTranscript}
			{:else if fallbackMessage}
				{fallbackMessage}
			{:else}
				{$i18n.t('Listening…')}
			{/if}
		</div>
	</div>

	{#if state === 'error'}
		<button
			type="button"
			class="shrink-0 rounded-full bg-indigo-500 px-3 py-1.5 text-xs text-white"
			on:click={() => void retryRecording()}
		>
			{$i18n.t('Retry')}
		</button>
	{:else}
		<span class="shrink-0 text-sm tabular-nums text-indigo-500"
			>{formatSeconds(durationSeconds)}</span
		>
		{#if state === 'recording'}
			<button
				id="confirm-recording-button"
				type="button"
				aria-label={$i18n.t('Finish voice dictation')}
				class="shrink-0 rounded-full bg-indigo-500 p-1.5 text-white"
				on:click={() => void confirmRecording()}
			>
				<svg
					xmlns="http://www.w3.org/2000/svg"
					fill="none"
					viewBox="0 0 24 24"
					stroke-width="2.5"
					stroke="currentColor"
					class="size-4"
					aria-hidden="true"
				>
					<path stroke-linecap="round" stroke-linejoin="round" d="m4.5 12.75 6 6 9-13.5" />
				</svg>
			</button>
		{:else}
			<div
				class="size-7 animate-spin rounded-full border-2 border-gray-300 border-t-indigo-500"
				aria-hidden="true"
			></div>
		{/if}
	{/if}
</div>
