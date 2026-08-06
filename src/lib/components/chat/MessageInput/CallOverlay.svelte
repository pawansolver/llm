<script lang="ts">
	import { createEventDispatcher, getContext, onDestroy, onMount, tick } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { audioQueue, config, models, settings, showCallOverlay, TTSWorker } from '$lib/stores';
	import { blobToFile } from '$lib/utils';
	import {
		getAudioFileExtension,
		getSupportedRecordingMimeType,
		normalizeVadSettings,
		shouldStopAfterSilence
	} from '$lib/utils/dictation';
	import {
		selectBrowserVoice,
		transitionConversation,
		type ConversationState
	} from '$lib/utils/voiceConversation';
	import { generateEmoji } from '$lib/apis';
	import { synthesizeOpenAISpeech, transcribeAudio } from '$lib/apis/audio';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import VideoInputMenu from './CallOverlay/VideoInputMenu.svelte';
	import { KokoroWorker } from '$lib/workers/KokoroWorker';
	import { WEBUI_API_BASE_URL } from '$lib/constants';

	const dispatch = createEventDispatcher();
	const i18n: any = getContext('i18n');

	export let eventTarget: EventTarget;
	export let submitPrompt: Function;
	export let stopResponse: Function;
	export let files;
	export let chatId;
	export let modelId;

	let model: any = null;
	let conversationState: ConversationState = 'idle';
	let errorMessage = '';
	let retryAction: (() => void | Promise<void>) | null = null;
	let muted = false;
	let emoji: string | null = null;
	let camera = false;
	let cameraStream: MediaStream | null = null;
	let rmsLevel = 0;
	let videoInputDevices: MediaDeviceInfo[] = [];
	let selectedVideoInputDeviceId: string | null = null;

	let audioStream: MediaStream | null = null;
	let mediaRecorder: MediaRecorder | null = null;
	let audioChunks: Blob[] = [];
	let recorderMimeType = '';
	let discardRecording = false;
	let audioContext: AudioContext | null = null;
	let analyser: AnalyserNode | null = null;
	let analysisFrame: number | null = null;
	let heardVoice = false;
	let lastVoiceAt = 0;
	let bargeInTriggered = false;

	let currentMessageId: string | null = null;
	let chatStreaming = false;
	let messageFinished = false;
	let ttsAbortController = new AbortController();
	let ttsChain: Promise<void> = Promise.resolve();
	let currentUtterance: SpeechSynthesisUtterance | null = null;
	let resolveUtterance: (() => void) | null = null;
	let wakeLock: any = null;
	let destroyed = false;

	$: loading = conversationState === 'transcribing' || conversationState === 'thinking';
	$: assistantSpeaking = conversationState === 'speaking';

	const setState = (next: ConversationState) => {
		conversationState = transitionConversation(conversationState, next);
	};

	const getTtsEngine = () =>
		$settings?.audio?.tts?.engine ?? ($config as any)?.audio?.tts?.engine ?? '';

	const getVoiceId = () => {
		const configuredVoice = ($config as any)?.audio?.tts?.voice;
		return (
			model?.info?.meta?.tts?.voice ??
			($settings?.audio?.tts?.defaultVoice === configuredVoice
				? ($settings?.audio?.tts?.voice ?? configuredVoice)
				: configuredVoice)
		);
	};

	const fail = (message: string, retry: (() => void | Promise<void>) | null = null) => {
		errorMessage = message;
		retryAction = retry;
		if (conversationState !== 'error') setState('error');
	};

	const getVideoInputDevices = async () => {
		const devices = await navigator.mediaDevices.enumerateDevices();
		videoInputDevices = devices.filter((device) => device.kind === 'videoinput');
		if ('getDisplayMedia' in navigator.mediaDevices) {
			videoInputDevices = [
				...videoInputDevices,
				{ deviceId: 'screen', label: 'Screen Share' } as MediaDeviceInfo
			];
		}
		if (selectedVideoInputDeviceId === null && videoInputDevices.length) {
			const savedDeviceId = localStorage.getItem('selectedVideoInputDeviceId');
			selectedVideoInputDeviceId =
				savedDeviceId && videoInputDevices.some(({ deviceId }) => deviceId === savedDeviceId)
					? savedDeviceId
					: videoInputDevices[0].deviceId;
		}
	};

	const startVideoStream = async () => {
		const video = document.getElementById('camera-feed') as HTMLVideoElement | null;
		if (!video) return;
		cameraStream =
			selectedVideoInputDeviceId === 'screen'
				? await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false })
				: await navigator.mediaDevices.getUserMedia({
						video: {
							deviceId: selectedVideoInputDeviceId
								? { exact: selectedVideoInputDeviceId }
								: undefined
						}
					});
		await getVideoInputDevices();
		video.srcObject = cameraStream;
		await video.play();
	};

	const startCamera = async () => {
		await getVideoInputDevices();
		if (cameraStream) return;
		camera = true;
		await tick();
		try {
			await startVideoStream();
		} catch (error) {
			camera = false;
			toast.error(`${error}`);
		}
	};

	const stopVideoStream = async () => {
		cameraStream?.getTracks().forEach((track) => track.stop());
		cameraStream = null;
	};

	const stopCamera = async () => {
		await stopVideoStream();
		camera = false;
	};

	const takeScreenshot = () => {
		const video = document.getElementById('camera-feed') as HTMLVideoElement | null;
		const canvas = document.getElementById('camera-canvas') as HTMLCanvasElement | null;
		if (!video || !canvas || !video.videoWidth) return;
		canvas.width = video.videoWidth;
		canvas.height = video.videoHeight;
		canvas.getContext('2d')?.drawImage(video, 0, 0, video.videoWidth, video.videoHeight);
		return canvas.toDataURL('image/png');
	};

	const stopAnalysis = async () => {
		if (analysisFrame !== null) cancelAnimationFrame(analysisFrame);
		analysisFrame = null;
		analyser?.disconnect();
		analyser = null;
		const context = audioContext;
		audioContext = null;
		if (context && context.state !== 'closed') await context.close().catch(() => undefined);
	};

	const ensureAudioStream = async () => {
		if (audioStream?.active) return;
		audioStream = await navigator.mediaDevices.getUserMedia({
			audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
		});
		startAnalysis();
	};

	const stopRecorder = (discard = false) => {
		if (!mediaRecorder || mediaRecorder.state === 'inactive') return;
		discardRecording = discard;
		try {
			mediaRecorder.stop();
		} catch {
			mediaRecorder = null;
		}
	};

	const startRecorder = () => {
		if (!audioStream || mediaRecorder?.state === 'recording') return;
		audioChunks = [];
		discardRecording = false;
		heardVoice = false;
		lastVoiceAt = Date.now();
		recorderMimeType =
			getSupportedRecordingMimeType(MediaRecorder.isTypeSupported?.bind(MediaRecorder)) ?? '';
		mediaRecorder = recorderMimeType
			? new MediaRecorder(audioStream, { mimeType: recorderMimeType })
			: new MediaRecorder(audioStream);
		recorderMimeType = mediaRecorder.mimeType || recorderMimeType || 'audio/webm';
		mediaRecorder.ondataavailable = ({ data }) => {
			if (data.size) audioChunks.push(data);
		};
		mediaRecorder.onstop = () => {
			const chunks = audioChunks;
			const mimeType = recorderMimeType;
			const discard = discardRecording;
			mediaRecorder = null;
			audioChunks = [];
			if (!discard) void transcribeRecording(chunks, mimeType);
		};
		mediaRecorder.start(250);
	};

	const startListening = async () => {
		if (destroyed || !$showCallOverlay || muted) return;
		try {
			if (conversationState !== 'listening') setState('listening');
			errorMessage = '';
			retryAction = null;
			bargeInTriggered = false;
			await ensureAudioStream();
			startRecorder();
		} catch (error: any) {
			const message =
				error?.name === 'NotAllowedError'
					? $i18n.t('Microphone permission was denied. Allow microphone access and try again.')
					: $i18n.t('Unable to access the microphone. Check your device and try again.');
			fail(message, startListening);
		}
	};

	const transcribeRecording = async (chunks: Blob[], mimeType: string) => {
		if (destroyed || muted) return;
		const blob = new Blob(chunks, { type: mimeType });
		if (blob.size < 100) {
			setState('idle');
			await startListening();
			return;
		}
		setState('transcribing');
		emoji = null;
		const imageUrl = cameraStream ? takeScreenshot() : undefined;
		if (imageUrl) files = [{ type: 'image', url: imageUrl }];
		const extension = getAudioFileExtension(mimeType);
		const file = blobToFile(blob, `recording.${extension}`);
		try {
			const result = await transcribeAudio(
				localStorage.token,
				file,
				$settings?.audio?.stt?.language
			);
			const text = result?.text?.trim();
			if (!text) {
				setState('idle');
				await startListening();
				return;
			}
			setState('thinking');
			await submitPrompt(text, { _raw: true });
		} catch (error) {
			fail(`${error}`, () => transcribeRecording(chunks, mimeType));
		}
	};

	const startAnalysis = () => {
		if (!audioStream || audioContext) return;
		const AudioContextConstructor =
			window.AudioContext ||
			(window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
		if (!AudioContextConstructor) return;
		audioContext = new AudioContextConstructor();
		const source = audioContext.createMediaStreamSource(audioStream);
		analyser = audioContext.createAnalyser();
		analyser.fftSize = 1024;
		source.connect(analyser);
		const samples = new Uint8Array(analyser.fftSize);
		const vad = normalizeVadSettings({
			...$settings?.audio?.stt?.vad,
			enabled: true
		});

		const analyse = () => {
			if (destroyed || !analyser || !$showCallOverlay) return;
			analyser.getByteTimeDomainData(samples);
			let squares = 0;
			for (const sample of samples) squares += ((sample - 128) / 128) ** 2;
			const rms = Math.sqrt(squares / samples.length);
			const micEnabled =
				!muted &&
				(conversationState === 'listening' ||
					(conversationState === 'speaking' && ($settings?.voiceInterruption ?? false)));
			rmsLevel = micEnabled ? rms : 0;

			if (micEnabled && rms >= vad.threshold) {
				if (
					conversationState === 'speaking' &&
					($settings?.voiceInterruption ?? false) &&
					!bargeInTriggered
				) {
					bargeInTriggered = true;
					void stopAllAudio(true, true);
				} else if (conversationState === 'listening') {
					heardVoice = true;
					lastVoiceAt = Date.now();
				}
			} else if (
				conversationState === 'listening' &&
				shouldStopAfterSilence({
					enabled: true,
					heardVoice,
					level: rms,
					threshold: vad.threshold,
					now: Date.now(),
					lastVoiceAt,
					silenceDurationMs: vad.silenceDurationMs
				})
			) {
				heardVoice = false;
				stopRecorder(false);
			}
			analysisFrame = requestAnimationFrame(analyse);
		};
		analysisFrame = requestAnimationFrame(analyse);
	};

	const waitForVoices = async () => {
		const initial = speechSynthesis.getVoices();
		if (initial.length) return initial;
		return await new Promise<SpeechSynthesisVoice[]>((resolve) => {
			const timeout = window.setTimeout(() => {
				speechSynthesis.removeEventListener('voiceschanged', changed);
				resolve(speechSynthesis.getVoices());
			}, 1000);
			const changed = () => {
				window.clearTimeout(timeout);
				speechSynthesis.removeEventListener('voiceschanged', changed);
				resolve(speechSynthesis.getVoices());
			};
			speechSynthesis.addEventListener('voiceschanged', changed, { once: true });
		});
	};

	const speakBrowserSentence = async (content: string, signal: AbortSignal) => {
		if (signal.aborted || !$showCallOverlay) return;
		const voices = await waitForVoices();
		if (signal.aborted) return;
		await new Promise<void>((resolve, reject) => {
			resolveUtterance = resolve;
			currentUtterance = new SpeechSynthesisUtterance(content);
			currentUtterance.rate = $settings?.audio?.tts?.playbackRate ?? 1;
			const voice = selectBrowserVoice(voices, getVoiceId(), $settings?.audio?.stt?.language);
			if (voice) currentUtterance.voice = voice;
			currentUtterance.onend = () => {
				currentUtterance = null;
				resolveUtterance = null;
				resolve();
			};
			currentUtterance.onerror = (event) => {
				currentUtterance = null;
				resolveUtterance = null;
				event.error === 'canceled' || event.error === 'interrupted'
					? resolve()
					: reject(new Error(event.error));
			};
			speechSynthesis.speak(currentUtterance);
		});
	};

	const synthesizeSentence = async (content: string, signal: AbortSignal) => {
		if (signal.aborted || !content.trim()) return;
		if ($settings?.showEmojiInCall ?? false) {
			void generateEmoji(localStorage.token, modelId, content, chatId).then((value) => {
				if (!signal.aborted && value) emoji = value;
			});
		}
		if (conversationState !== 'speaking') setState('speaking');
		const engine = getTtsEngine();
		if (engine === '') {
			await speakBrowserSentence(content, signal);
			return;
		}
		let url: string | undefined;
		if (engine === 'browser-kokoro') {
			let worker = $TTSWorker as KokoroWorker | null;
			if (!worker) {
				worker = new KokoroWorker($settings?.audio?.tts?.engineConfig?.dtype ?? 'fp32');
				(TTSWorker as any).set(worker);
				await worker.init();
			}
			url = await worker.generate({ text: content, voice: getVoiceId() });
		} else {
			const response = await synthesizeOpenAISpeech(localStorage.token, getVoiceId(), content);
			if (response) url = URL.createObjectURL(await response.blob());
		}
		if (!signal.aborted && url) $audioQueue?.enqueue(url);
		else if (url?.startsWith('blob:')) URL.revokeObjectURL(url);
	};

	const finishConversation = async () => {
		if (destroyed || !messageFinished || chatStreaming) return;
		currentMessageId = null;
		emoji = null;
		if (conversationState === 'speaking' || conversationState === 'thinking') setState('idle');
		await startListening();
	};

	const stopAllAudio = async (stopGeneration = true, relisten = true) => {
		ttsAbortController.abort();
		ttsAbortController = new AbortController();
		ttsChain = Promise.resolve();
		speechSynthesis.cancel();
		resolveUtterance?.();
		resolveUtterance = null;
		currentUtterance = null;
		$audioQueue?.stop();
		emoji = null;
		if (stopGeneration && chatStreaming) stopResponse();
		chatStreaming = false;
		messageFinished = true;
		if (conversationState === 'speaking' || conversationState === 'thinking') setState('idle');
		if (relisten) await startListening();
	};

	const chatStartHandler = (event: Event) => {
		const { id } = (event as CustomEvent).detail;
		stopRecorder(true);
		void stopAllAudio(false, false);
		currentMessageId = id;
		chatStreaming = true;
		messageFinished = false;
		ttsAbortController = new AbortController();
		ttsChain = Promise.resolve();
		if (conversationState === 'idle' || conversationState === 'listening') setState('thinking');
		if (getTtsEngine() !== '') {
			const queue = $audioQueue;
			queue?.setId(`call-${id}`);
			queue?.setPlaybackRate($settings?.audio?.tts?.playbackRate ?? 1);
			queue?.beginBatch();
			if (!queue) {
				fail($i18n.t('Audio playback is unavailable.'), startListening);
				return;
			}
			queue.onStopped = ({ event: queueEvent }) => {
				if (queueEvent === 'empty-queue') void finishConversation();
				if (queueEvent === 'playback-blocked') {
					fail($i18n.t('Tap retry to resume audio playback.'), async () => {
						setState('speaking');
						await $audioQueue?.play();
					});
				}
				if (queueEvent === 'error') {
					fail($i18n.t('Audio playback failed.'), startListening);
				}
			};
		}
	};

	const chatEventHandler = (event: Event) => {
		const { id, content } = (event as CustomEvent).detail;
		if (id !== currentMessageId || !content?.trim()) return;
		const signal = ttsAbortController.signal;
		ttsChain = ttsChain
			.then(() => synthesizeSentence(content, signal))
			.catch((error) => {
				if (!signal.aborted) fail(`${error}`, startListening);
			});
	};

	const chatFinishHandler = (event: Event) => {
		const { id } = (event as CustomEvent).detail;
		if (id !== currentMessageId) return;
		chatStreaming = false;
		messageFinished = true;
		void ttsChain.finally(() => {
			if (getTtsEngine() === '') void finishConversation();
			else $audioQueue?.endBatch();
		});
	};

	const toggleMute = async () => {
		muted = !muted;
		if (muted) {
			stopRecorder(true);
			if (conversationState === 'listening') setState('idle');
		} else {
			await startListening();
		}
	};

	const handleKeydown = (event: KeyboardEvent) => {
		const target = event.target as HTMLElement;
		if (['INPUT', 'TEXTAREA'].includes(target.tagName) || target.isContentEditable) return;
		if (event.key.toLocaleLowerCase() === 'm') {
			event.preventDefault();
			void toggleMute();
		} else if (event.key === 'Escape') {
			event.preventDefault();
			void endCall();
		}
	};

	const requestWakeLock = async () => {
		if (!('wakeLock' in navigator) || wakeLock || document.visibilityState !== 'visible') return;
		try {
			wakeLock = await (navigator as any).wakeLock.request('screen');
			wakeLock.addEventListener('release', () => (wakeLock = null), { once: true });
		} catch {
			wakeLock = null;
		}
	};

	const handleVisibilityChange = () => {
		if (document.visibilityState === 'visible') void requestWakeLock();
	};

	const handleOffline = () => {
		if (conversationState === 'transcribing' || getTtsEngine() !== '') {
			fail($i18n.t('You are offline. Reconnect and try again.'), startListening);
		}
	};

	const handleOnline = () => {
		if (conversationState === 'error' && errorMessage) {
			errorMessage = '';
			setState('idle');
			void startListening();
		}
	};

	const cleanup = async () => {
		if (destroyed) return;
		destroyed = true;
		await stopAllAudio(false, false);
		stopRecorder(true);
		mediaRecorder = null;
		audioStream?.getTracks().forEach((track) => track.stop());
		audioStream = null;
		await stopAnalysis();
		await stopCamera();
		await wakeLock?.release?.().catch(() => undefined);
		wakeLock = null;
		eventTarget.removeEventListener('chat:start', chatStartHandler);
		eventTarget.removeEventListener('chat', chatEventHandler);
		eventTarget.removeEventListener('chat:finish', chatFinishHandler);
		document.removeEventListener('keydown', handleKeydown);
		document.removeEventListener('visibilitychange', handleVisibilityChange);
		window.removeEventListener('offline', handleOffline);
		window.removeEventListener('online', handleOnline);
	};

	const endCall = async () => {
		await cleanup();
		showCallOverlay.set(false);
		dispatch('close');
	};

	onMount(async () => {
		model = $models.find((item) => item.id === modelId);
		eventTarget.addEventListener('chat:start', chatStartHandler);
		eventTarget.addEventListener('chat', chatEventHandler);
		eventTarget.addEventListener('chat:finish', chatFinishHandler);
		document.addEventListener('keydown', handleKeydown);
		document.addEventListener('visibilitychange', handleVisibilityChange);
		window.addEventListener('offline', handleOffline);
		window.addEventListener('online', handleOnline);
		await requestWakeLock();
		await startListening();
	});

	onDestroy(() => {
		void cleanup();
	});
</script>

{#if $showCallOverlay}
	<div
		class="max-w-lg w-full h-full max-h-[100dvh] flex flex-col justify-between p-3 md:p-6"
		role="dialog"
		aria-modal="true"
		aria-label={$i18n.t('Voice conversation')}
	>
		{#if camera}
			<button
				type="button"
				class="flex justify-center items-center w-full h-20 min-h-20"
				on:click={() => {
					if (assistantSpeaking) {
						stopAllAudio();
					}
				}}
			>
				{#if emoji}
					<div
						class="  transition-all rounded-full"
						style="font-size:{rmsLevel * 100 > 4
							? '4.5'
							: rmsLevel * 100 > 2
								? '4.25'
								: rmsLevel * 100 > 1
									? '3.75'
									: '3.5'}rem;width: 100%; text-align:center;"
					>
						{emoji}
					</div>
				{:else if loading || assistantSpeaking}
					<svg
						class="size-12 text-gray-900 dark:text-gray-400"
						viewBox="0 0 24 24"
						fill="currentColor"
						xmlns="http://www.w3.org/2000/svg"
						><style>
							.spinner_qM83 {
								animation: spinner_8HQG 1.05s infinite;
							}
							.spinner_oXPr {
								animation-delay: 0.1s;
							}
							.spinner_ZTLf {
								animation-delay: 0.2s;
							}
							@keyframes spinner_8HQG {
								0%,
								57.14% {
									animation-timing-function: cubic-bezier(0.33, 0.66, 0.66, 1);
									transform: translate(0);
								}
								28.57% {
									animation-timing-function: cubic-bezier(0.33, 0, 0.66, 0.33);
									transform: translateY(-6px);
								}
								100% {
									transform: translate(0);
								}
							}
						</style><circle class="spinner_qM83" cx="4" cy="12" r="3" /><circle
							class="spinner_qM83 spinner_oXPr"
							cx="12"
							cy="12"
							r="3"
						/><circle class="spinner_qM83 spinner_ZTLf" cx="20" cy="12" r="3" /></svg
					>
				{:else}
					<div
						class=" {rmsLevel * 100 > 4
							? ' size-[4.5rem]'
							: rmsLevel * 100 > 2
								? ' size-16'
								: rmsLevel * 100 > 1
									? 'size-14'
									: 'size-12'}  transition-all rounded-full bg-cover bg-center bg-no-repeat"
						style={`background-image: url('${WEBUI_API_BASE_URL}/models/model/profile/image?id=${model?.id}&lang=${$i18n.language}&voice=true');`}
					></div>
				{/if}
				<!-- navbar -->
			</button>
		{/if}

		<div class="flex justify-center items-center flex-1 h-full w-full max-h-full">
			{#if !camera}
				<button
					type="button"
					aria-label={assistantSpeaking
						? $i18n.t('Interrupt assistant')
						: $i18n.t('Voice conversation status')}
					on:click={() => {
						if (assistantSpeaking) {
							stopAllAudio();
						}
					}}
				>
					{#if emoji}
						<div
							class="  transition-all rounded-full"
							style="font-size:{rmsLevel * 100 > 4
								? '13'
								: rmsLevel * 100 > 2
									? '12'
									: rmsLevel * 100 > 1
										? '11.5'
										: '11'}rem;width:100%;text-align:center;"
						>
							{emoji}
						</div>
					{:else if loading || assistantSpeaking}
						<svg
							class="size-44 text-gray-900 dark:text-gray-400"
							viewBox="0 0 24 24"
							fill="currentColor"
							xmlns="http://www.w3.org/2000/svg"
							><style>
								.spinner_qM83 {
									animation: spinner_8HQG 1.05s infinite;
								}
								.spinner_oXPr {
									animation-delay: 0.1s;
								}
								.spinner_ZTLf {
									animation-delay: 0.2s;
								}
								@keyframes spinner_8HQG {
									0%,
									57.14% {
										animation-timing-function: cubic-bezier(0.33, 0.66, 0.66, 1);
										transform: translate(0);
									}
									28.57% {
										animation-timing-function: cubic-bezier(0.33, 0, 0.66, 0.33);
										transform: translateY(-6px);
									}
									100% {
										transform: translate(0);
									}
								}
							</style><circle class="spinner_qM83" cx="4" cy="12" r="3" /><circle
								class="spinner_qM83 spinner_oXPr"
								cx="12"
								cy="12"
								r="3"
							/><circle class="spinner_qM83 spinner_ZTLf" cx="20" cy="12" r="3" /></svg
						>
					{:else}
						<div
							class=" {rmsLevel * 100 > 4
								? ' size-52'
								: rmsLevel * 100 > 2
									? 'size-48'
									: rmsLevel * 100 > 1
										? 'size-44'
										: 'size-40'} transition-all rounded-full bg-cover bg-center bg-no-repeat"
							style={`background-image: url('${WEBUI_API_BASE_URL}/models/model/profile/image?id=${model?.id}&lang=${$i18n.language}&voice=true');`}
						></div>
					{/if}
				</button>
			{:else}
				<div class="relative flex video-container w-full max-h-full pt-2 pb-4 md:py-6 px-2 h-full">
					<!-- svelte-ignore a11y-media-has-caption -->
					<video
						id="camera-feed"
						autoplay
						class="rounded-2xl h-full min-w-full object-cover object-center"
						playsinline
					></video>

					<canvas id="camera-canvas" style="display:none;"></canvas>

					<div class=" absolute top-4 md:top-8 left-4">
						<button
							type="button"
							aria-label={$i18n.t('Stop camera')}
							class="p-1.5 text-white cursor-pointer backdrop-blur-xl bg-black/10 rounded-full"
							on:click={() => {
								stopCamera();
							}}
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								viewBox="0 0 16 16"
								fill="currentColor"
								class="size-6"
							>
								<path
									d="M5.28 4.22a.75.75 0 0 0-1.06 1.06L6.94 8l-2.72 2.72a.75.75 0 1 0 1.06 1.06L8 9.06l2.72 2.72a.75.75 0 1 0 1.06-1.06L9.06 8l2.72-2.72a.75.75 0 0 0-1.06-1.06L8 6.94 5.28 4.22Z"
								/>
							</svg>
						</button>
					</div>
				</div>
			{/if}
		</div>

		<div class="flex flex-col items-center gap-4 pb-4 w-full">
			<button
				type="button"
				class="z-10"
				aria-label={assistantSpeaking
					? $i18n.t('Interrupt assistant')
					: $i18n.t('Voice conversation status')}
				on:click={() => {
					if (assistantSpeaking) {
						stopAllAudio();
					}
				}}
			>
				<div class="line-clamp-1 text-sm font-normal" aria-live="polite" aria-atomic="true">
					{#if conversationState === 'error'}
						{errorMessage || $i18n.t('Voice conversation failed.')}
					{:else if conversationState === 'transcribing'}
						{$i18n.t('Transcribing...')}
					{:else if loading}
						{$i18n.t('Thinking...')}
					{:else if muted}
						{$i18n.t('Muted')}
					{:else if assistantSpeaking}
						{$i18n.t('Tap to interrupt')}
					{:else}
						{$i18n.t('Listening...')}
					{/if}
				</div>
			</button>

			<div class="flex items-center justify-center gap-4 z-10">
				{#if conversationState === 'error' && retryAction}
					<Tooltip content={$i18n.t('Retry')}>
						<button
							type="button"
							class="p-3 rounded-full bg-gray-50 dark:bg-gray-900"
							aria-label={$i18n.t('Retry voice conversation')}
							on:click={async () => {
								const retry = retryAction;
								errorMessage = '';
								setState('idle');
								await retry?.();
							}}
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								fill="none"
								viewBox="0 0 24 24"
								stroke-width="1.5"
								stroke="currentColor"
								class="size-5"
								aria-hidden="true"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d="M16.023 9.348h4.992V4.356m-.001 4.992-3.181-3.183a8.25 8.25 0 1 0 2.16 8.04"
								/>
							</svg>
						</button>
					</Tooltip>
				{/if}
				{#if camera}
					<VideoInputMenu
						devices={videoInputDevices}
						on:change={async (e) => {
							console.log(e.detail);
							selectedVideoInputDeviceId = e.detail;
							localStorage.setItem('selectedVideoInputDeviceId', e.detail);
							await stopVideoStream();
							await startVideoStream();
						}}
					>
						<button
							aria-label={$i18n.t('Switch camera')}
							class="p-3 rounded-full bg-gray-50 dark:bg-gray-900"
							type="button"
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								viewBox="0 0 20 20"
								fill="currentColor"
								class="size-5"
							>
								<path
									fill-rule="evenodd"
									d="M15.312 11.424a5.5 5.5 0 0 1-9.201 2.466l-.312-.311h2.433a.75.75 0 0 0 0-1.5H3.989a.75.75 0 0 0-.75.75v4.242a.75.75 0 0 0 1.5 0v-2.43l.31.31a7 7 0 0 0 11.712-3.138.75.75 0 0 0-1.449-.39Zm1.23-3.723a.75.75 0 0 0 .219-.53V2.929a.75.75 0 0 0-1.5 0V5.36l-.31-.31A7 7 0 0 0 3.239 8.188a.75.75 0 1 0 1.448.389A5.5 5.5 0 0 1 13.89 6.11l.311.31h-2.432a.75.75 0 0 0 0 1.5h4.243a.75.75 0 0 0 .53-.219Z"
									clip-rule="evenodd"
								/>
							</svg>
						</button>
					</VideoInputMenu>
				{:else}
					<Tooltip content={$i18n.t('Camera')}>
						<button
							aria-label={$i18n.t('Camera')}
							class="p-3 rounded-full bg-gray-50 dark:bg-gray-900"
							type="button"
							on:click={async () => {
								await navigator.mediaDevices.getUserMedia({ video: true });
								startCamera();
							}}
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								fill="none"
								viewBox="0 0 24 24"
								stroke-width="1.5"
								stroke="currentColor"
								class="size-5"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d="M6.827 6.175A2.31 2.31 0 0 1 5.186 7.23c-.38.054-.757.112-1.134.175C2.999 7.58 2.25 8.507 2.25 9.574V18a2.25 2.25 0 0 0 2.25 2.25h15A2.25 2.25 0 0 0 21.75 18V9.574c0-1.067-.75-1.994-1.802-2.169a47.865 47.865 0 0 0-1.134-.175 2.31 2.31 0 0 1-1.64-1.055l-.822-1.316a2.192 2.192 0 0 0-1.736-1.039 48.774 48.774 0 0 0-5.232 0 2.192 2.192 0 0 0-1.736 1.039l-.821 1.316Z"
								/>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d="M16.5 12.75a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0ZM18.75 10.5h.008v.008h-.008V10.5Z"
								/>
							</svg>
						</button>
					</Tooltip>
				{/if}

				<Tooltip content={muted ? $i18n.t('Unmute') + ' (M)' : $i18n.t('Mute') + ' (M)'}>
					<button
						class="p-3 rounded-full transition-colors duration-200 {muted
							? 'bg-red-500 text-white'
							: 'bg-gray-50 dark:bg-gray-900'}"
						type="button"
						aria-label={muted ? $i18n.t('Unmute') : $i18n.t('Mute')}
						on:click={toggleMute}
					>
						{#if muted}
							<!-- Mic Off icon -->
							<svg
								xmlns="http://www.w3.org/2000/svg"
								fill="none"
								viewBox="0 0 24 24"
								stroke-width="1.5"
								stroke="currentColor"
								class="size-5"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d="M12 18.75a6 6 0 0 0 6-6v-1.5m-6 7.5a6 6 0 0 1-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 0 1-3-3V4.5a3 3 0 1 1 6 0v8.25a3 3 0 0 1-3 3Z"
								/>
								<line
									x1="3"
									y1="3"
									x2="21"
									y2="21"
									stroke="currentColor"
									stroke-width="1.5"
									stroke-linecap="round"
								/>
							</svg>
						{:else}
							<!-- Mic On icon -->
							<svg
								xmlns="http://www.w3.org/2000/svg"
								fill="none"
								viewBox="0 0 24 24"
								stroke-width="1.5"
								stroke="currentColor"
								class="size-5"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d="M12 18.75a6 6 0 0 0 6-6v-1.5m-6 7.5a6 6 0 0 1-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 0 1-3-3V4.5a3 3 0 1 1 6 0v8.25a3 3 0 0 1-3 3Z"
								/>
							</svg>
						{/if}
					</button>
				</Tooltip>

				<button
					aria-label={$i18n.t('End call')}
					class="p-3 rounded-full bg-gray-50 dark:bg-gray-900"
					on:click={endCall}
					type="button"
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 20 20"
						fill="currentColor"
						class="size-5"
					>
						<path
							d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z"
						/>
					</svg>
				</button>
			</div>
		</div>
	</div>
{/if}
